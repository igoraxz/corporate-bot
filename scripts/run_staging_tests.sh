#!/usr/bin/env bash
# Run staging tests against a staging instance.
#
# Usage:
#   ./scripts/run_staging_tests.sh              # unit tests only
#   ./scripts/run_staging_tests.sh --live       # unit + live tests (starts staging container)
#   ./scripts/run_staging_tests.sh --live-only  # live tests only (assumes staging already running)
#
# Prerequisites:
#   - Docker + Docker Compose
#   - Python 3.12+ with pytest, httpx
#   - Production .env file in repo root

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
STAGING_PORT="${BOT_PORT_STAGING:-8100}"
STAGING_URL="http://localhost:${STAGING_PORT}"

# Cleanup trap: stop staging container on exit if we started it
_STAGING_STARTED=false
cleanup() {
    if [[ "$_STAGING_STARTED" == "true" ]]; then
        echo -e "${YELLOW}[CLEANUP]${NC} Stopping staging container..."
        cd "$REPO_DIR"
        docker compose -f docker-compose.yml -f docker-compose.staging.yml down 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

MODE="${1:-unit}"

# --- Unit tests (always run) ---
if [[ "$MODE" != "--live-only" ]]; then
    info "Running unit tests..."
    cd "$REPO_DIR"
    python -m pytest tests/test_staging.py -v -k "not Live and not Isolation" --tb=short
    info "Unit tests passed!"
fi

# --- Live tests (optional) ---
if [[ "$MODE" == "--live" || "$MODE" == "--live-only" ]]; then
    if [[ "$MODE" == "--live" ]]; then
        info "Starting staging container..."

        # Snapshot prod DB for staging (optional — skip if no prod DB)
        if [[ -f "$REPO_DIR/data/bot.db" ]]; then
            info "Snapshotting production DB..."
            python3 "$REPO_DIR/scripts/staging_snapshot_db.py" \
                --src "$REPO_DIR/data/bot.db" \
                --dst "$REPO_DIR/data/staging-snapshot.db"
        else
            warn "No production DB found — staging will start with empty DB"
        fi

        # Start staging via docker compose override
        cd "$REPO_DIR"
        docker compose -f docker-compose.yml -f docker-compose.staging.yml up -d --build bot-core
        _STAGING_STARTED=true

        # Wait for health
        info "Waiting for staging to be healthy..."
        for i in $(seq 1 30); do
            if curl -sf "${STAGING_URL}/health" > /dev/null 2>&1; then
                info "Staging is healthy!"
                break
            fi
            if [[ $i -eq 30 ]]; then
                error "Staging failed to start within 30s"
                docker compose -f docker-compose.yml -f docker-compose.staging.yml logs bot-core --tail=50
                docker compose -f docker-compose.yml -f docker-compose.staging.yml down
                exit 1
            fi
            sleep 1
        done
    fi

    info "Running live staging tests..."
    cd "$REPO_DIR"
    STAGING_URL="$STAGING_URL" python -m pytest tests/test_staging.py -v -k "Live or Isolation" --tb=short
    LIVE_EXIT=$?

    # Container cleanup handled by trap — no manual down needed here

    if [[ $LIVE_EXIT -ne 0 ]]; then
        error "Live tests failed!"
        exit $LIVE_EXIT
    fi
    info "Live tests passed!"
fi

info "All staging tests complete!"
