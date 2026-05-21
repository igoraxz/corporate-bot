#!/usr/bin/env bash
# Staging Deploy Watcher — monitors trigger file for staging rebuilds.
#
# Simplified staging watcher:
# - Uses a SEPARATE git clone (~/corporate-bot-staging) — fully independent from prod
# - No auto-rollback (staging IS the test target — bot diagnoses failures)
# - No flock needed (independent repo, no git races with prod watcher)
# - Failed container logs captured for bot self-diagnosis
#
# Lives in /opt/corporate-bot-staging-watcher/ (root-owned, bot can't modify).
#
# Setup (as botuser):
#   git clone git@github-private:user/repo.git ~/corporate-bot-staging
#   ln -s ~/corporate-bot/.env ~/corporate-bot-staging/.env
#   ln -s ~/corporate-bot/data/google-workspace-creds ~/corporate-bot-staging/data/google-workspace-creds
#
# Install:
#   sudo mkdir -p /opt/corporate-bot-staging-watcher
#   sudo cp deploy/staging-watcher.sh /opt/corporate-bot-staging-watcher/
#   sudo chmod +x /opt/corporate-bot-staging-watcher/staging-watcher.sh
#   sudo chown root:root /opt/corporate-bot-staging-watcher/staging-watcher.sh
#   sudo cp deploy/staging-watcher.service /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now staging-watcher

set -euo pipefail

# --- Config (override via env or systemd) ---
# COMPOSE_DIR: the staging clone (independent from prod repo)
COMPOSE_DIR="${COMPOSE_DIR:-$HOME/corporate-bot-staging}"
# TRIGGER_DIR: where the bot writes triggers (in the prod repo, shared via /host-repo mount)
TRIGGER_DIR="${TRIGGER_DIR:-$HOME/corporate-bot/deploy/staging-triggers}"
STAGING_PROJECT="${STAGING_PROJECT:-corporate-bot-staging}"
STAGING_CONTAINER="${STAGING_CONTAINER:-corporate-bot-staging}"
BOT_PORT="${BOT_PORT:-8100}"
HEALTH_URL="http://localhost:${BOT_PORT}/health"
POLL_INTERVAL="${POLL_INTERVAL:-2}"

# Derived paths
TRIGGER="$TRIGGER_DIR/deploy_trigger.json"
RESULT="$TRIGGER_DIR/deploy_result.json"
LOG="$COMPOSE_DIR/deploy/staging_watcher.log"

# Failed deploy diagnostics (in trigger dir so prod container can read via /host-repo)
FAILED_LOG="$TRIGGER_DIR/staging_last_failed_startup.log"

# --- Functions ---

log_msg() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') — $1" | tee -a "$LOG"
}

write_result() {
    # Safe JSON construction via python3 (no shell interpolation)
    python3 -c "
import json, sys
d = json.loads(sys.argv[1])
with open(sys.argv[2], 'w') as f:
    json.dump(d, f)
" "$1" "$RESULT"
}

capture_failed_logs() {
    log_msg "Capturing failed staging container logs..."
    {
        echo "=== STAGING CONTAINER STATE ==="
        docker inspect "$STAGING_CONTAINER" 2>/dev/null | head -50 || echo "(container not found)"
        echo ""
        echo "=== STAGING CONTAINER LOGS (last 300 lines) ==="
        docker logs "$STAGING_CONTAINER" --tail 300 2>&1 || echo "(no logs)"
    } > "$FAILED_LOG" 2>&1
}

health_check() {
    local max_attempts=30  # 30 * 2s = 60s
    local attempt=0
    while [ $attempt -lt $max_attempts ]; do
        if curl -sf "$HEALTH_URL" > /dev/null 2>&1; then
            return 0
        fi
        sleep 2
        attempt=$((attempt + 1))
    done
    return 1
}

build_and_start() {
    local action="$1"
    cd "$COMPOSE_DIR"

    # Ensure shared network exists (for prod↔staging connectivity)
    docker network create ${BOT_NETWORK:-bot-shared} 2>/dev/null || true

    if [ "$action" = "restart" ]; then
        log_msg "Restarting staging container..."
        BOT_PORT="$BOT_PORT" docker compose \
            -p "$STAGING_PROJECT" \
            -f docker-compose.yml -f docker-compose.staging.yml \
            restart bot-core >> "$LOG" 2>&1
    else
        log_msg "Rebuilding staging ($action)..."
        BOT_PORT="$BOT_PORT" docker compose \
            -p "$STAGING_PROJECT" \
            -f docker-compose.yml -f docker-compose.staging.yml \
            build --no-cache --build-arg GIT_COMMIT="$(git -C "$COMPOSE_DIR" rev-parse HEAD 2>/dev/null || echo unknown)" bot-core >> "$LOG" 2>&1

        BOT_PORT="$BOT_PORT" docker compose \
            -p "$STAGING_PROJECT" \
            -f docker-compose.yml -f docker-compose.staging.yml \
            up -d --no-deps bot-core >> "$LOG" 2>&1
    fi
}


# Deploy approval config — /opt/ is OUTSIDE Docker mounts
# Derive instance name from STAGING_PROJECT (e.g. aion-bot-staging -> aion-bot)
_INSTANCE_NAME="${STAGING_PROJECT%-staging}"
APPROVAL_DIR="/opt/${STAGING_PROJECT}-watcher/approvals"
APPROVE_SCRIPT="/opt/${STAGING_PROJECT}-watcher/approve-deploy.sh"
APPROVAL_REQUEST_FILE="$TRIGGER_DIR/approval_request.json"
PROD_HEALTHY_FILE="$HOME/${_INSTANCE_NAME}/deploy/last_healthy_commit"

# ═══════════════════════════════════════════════════════════════════
# SENSITIVE FILE GATE — commit-bound approval at /opt/
#
# When the commit range since last healthy deploy touches infra files,
# the watcher pauses and waits for operator approval at /opt/.
# Approval is bound to a specific (baseline..target) commit pair.
# The approval dir is OUTSIDE all Docker mounts — bot cannot reach it.
# ═══════════════════════════════════════════════════════════════════

# Only files that auto-execute on deploy (not manually-copied scripts)
SENSITIVE_PATTERN='Dockerfile|docker-compose|entrypoint\.sh|seccomp|\.github/workflows/'

detect_sensitive_files() {
    local baseline="$1" target="$2"
    git -C "$COMPOSE_DIR" diff --name-only "$baseline".."$target" 2>/dev/null \
        | grep -E "$SENSITIVE_PATTERN" || true
}

write_approval_request() {
    local baseline="$1" target="$2" sensitive="$3"
    python3 -c "
import json, sys
from datetime import datetime
d = {
    'approval_id': sys.argv[1] + '..' + sys.argv[2],
    'baseline': sys.argv[1], 'target': sys.argv[2],
    'sensitive_files': [f for f in sys.argv[3].strip().split(chr(10)) if f],
    'requested_at': datetime.now().isoformat()
}
with open(sys.argv[4], 'w') as f:
    json.dump(d, f, indent=2)
" "$baseline" "$target" "$sensitive" "$APPROVAL_REQUEST_FILE"
}

check_approval() {
    local expected_id="$1"
    # Supersession check
    if [ -f "$TRIGGER" ]; then echo "superseded"; return; fi
    # Approved?
    if [ -f "$APPROVAL_DIR/approved.json" ]; then
        local fid
        fid=$(python3 -c "import json; print(json.load(open('$APPROVAL_DIR/approved.json')).get('approval_id',''))" 2>/dev/null || echo "")
        if [ "$fid" = "$expected_id" ]; then
            rm -f "$APPROVAL_DIR/approved.json"; echo "approved"; return
        fi
    fi
    # Rejected?
    if [ -f "$APPROVAL_DIR/rejected.json" ]; then
        local fid
        fid=$(python3 -c "import json; print(json.load(open('$APPROVAL_DIR/rejected.json')).get('approval_id',''))" 2>/dev/null || echo "")
        if [ "$fid" = "$expected_id" ]; then
            rm -f "$APPROVAL_DIR/rejected.json"; echo "rejected"; return
        fi
    fi
    echo "waiting"
}

wait_for_approval() {
    local approval_id="$1" sensitive_files="$2"
    rm -f "$APPROVAL_DIR/approved.json" "$APPROVAL_DIR/rejected.json"
    log_msg "⚠️  SENSITIVE FILES CHANGED — manual approval required:"
    echo "$sensitive_files" | while IFS= read -r f; do [ -n "$f" ] && log_msg "  → $f"; done
    log_msg "Approval ID: $approval_id"
    log_msg "Approve:  sudo $APPROVE_SCRIPT"
    log_msg "Reject:   sudo $APPROVE_SCRIPT --reject"
    while true; do
        sleep 5
        local d; d=$(check_approval "$approval_id")
        case "$d" in
            approved) log_msg "✅ Approval received"; return 0 ;;
            rejected) log_msg "❌ Rejected by operator"; return 1 ;;
            superseded) log_msg "⏭ Superseded by new trigger"; return 1 ;;
        esac
    done
}

# --- Main loop ---

mkdir -p "$TRIGGER_DIR"
mkdir -p "$(dirname "$LOG")"
log_msg "Staging watcher started"
log_msg "  Compose dir: $COMPOSE_DIR"
log_msg "  Trigger dir: $TRIGGER_DIR"
log_msg "  Project: $STAGING_PROJECT"
log_msg "  Container: $STAGING_CONTAINER"
log_msg "  Port: $BOT_PORT"
log_msg "  Health URL: $HEALTH_URL"

while true; do
    if [ -f "$TRIGGER" ]; then
        # Read trigger
        ACTION=$(python3 -c "import json,sys; t=json.load(open(sys.argv[1])); print(t.get('action','rebuild'))" "$TRIGGER" 2>/dev/null || echo "rebuild")
        REASON=$(python3 -c "import json,sys; t=json.load(open(sys.argv[1])); print(t.get('reason','')[:500])" "$TRIGGER" 2>/dev/null || echo "")

        log_msg "=== STAGING DEPLOY: action=$ACTION reason=$REASON ==="

        # Consume trigger immediately
        rm -f "$TRIGGER"

        # Write in-progress result (safe JSON via python3)
        write_result "$(python3 -c "
import json, sys
from datetime import datetime
print(json.dumps({
    'status': 'in_progress', 'action': sys.argv[1],
    'started': datetime.now().isoformat()
}))
" "$ACTION")"

        # Git fetch + sensitive file gate
        if [ "$ACTION" != "restart" ]; then
            log_msg "Fetching latest code..."
            git -C "$COMPOSE_DIR" fetch origin main >> "$LOG" 2>&1

            # Baseline = prod's last healthy commit
            STG_BASELINE=""
            if [ -f "$PROD_HEALTHY_FILE" ]; then
                STG_BASELINE=$(cat "$PROD_HEALTHY_FILE")
            fi
            PREV_STG=$(git -C "$COMPOSE_DIR" rev-parse HEAD 2>/dev/null || echo "")
            STG_BASELINE="${STG_BASELINE:-$PREV_STG}"
            FETCH_STG=$(git -C "$COMPOSE_DIR" rev-parse origin/main 2>/dev/null || echo "")

            # Detect sensitive files
            STG_SENSITIVE=$(detect_sensitive_files "$STG_BASELINE" "$FETCH_STG")
            if [ -n "$STG_SENSITIVE" ]; then
                STG_AID="${STG_BASELINE}..${FETCH_STG}"
                write_approval_request "$STG_BASELINE" "$FETCH_STG" "$STG_SENSITIVE"
                write_result "$(python3 -c "
import json, sys
from datetime import datetime
print(json.dumps({'status':'pending_approval','action':sys.argv[1],'approval_id':sys.argv[2],'started':datetime.now().isoformat()}))
" "$ACTION" "$STG_AID")"
                if ! wait_for_approval "$STG_AID" "$STG_SENSITIVE"; then
                    if [ -f "$TRIGGER" ]; then
                        write_result "$(python3 -c "import json;from datetime import datetime;print(json.dumps({'status':'superseded','finished':datetime.now().isoformat()}))")"
                    else
                        write_result "$(python3 -c "import json;from datetime import datetime;print(json.dumps({'status':'rejected','reason':'Operator rejected','finished':datetime.now().isoformat()}))")"
                    fi
                    rm -f "$APPROVAL_REQUEST_FILE"
                    sleep 5
                    continue
                fi
                rm -f "$APPROVAL_REQUEST_FILE"
            fi

            log_msg "Merging..."
            git -C "$COMPOSE_DIR" merge origin/main >> "$LOG" 2>&1 || {
                git -C "$COMPOSE_DIR" reset --hard origin/main >> "$LOG" 2>&1 || true
            }
        fi

        NEW_COMMIT=$(git -C "$COMPOSE_DIR" rev-parse HEAD 2>/dev/null || echo "")

        # Build and start
        if build_and_start "$ACTION"; then
            log_msg "Build succeeded, checking health..."

            if health_check; then
                log_msg "✅ Staging deploy SUCCESS (commit=$NEW_COMMIT)"
                write_result "$(python3 -c "
import json, sys
from datetime import datetime
print(json.dumps({
    'status': 'success', 'action': sys.argv[1], 'reason': sys.argv[2],
    'commit': sys.argv[3], 'finished': datetime.now().isoformat()
}))
" "$ACTION" "$REASON" "$NEW_COMMIT")"
            else
                log_msg "❌ Staging health check FAILED — capturing logs for bot diagnosis"
                capture_failed_logs
                # No auto-rollback: staging IS the test target.
                # Bot reads failed logs via deploy_staging_status and can fix + redeploy.
                write_result "$(python3 -c "
import json, sys
from datetime import datetime
print(json.dumps({
    'status': 'unhealthy', 'action': sys.argv[1], 'reason': sys.argv[2],
    'failed_commit': sys.argv[3], 'finished': datetime.now().isoformat()
}))
" "$ACTION" "$REASON" "$NEW_COMMIT")"
            fi
        else
            log_msg "❌ Staging build FAILED — capturing logs for bot diagnosis"
            capture_failed_logs
            write_result "$(python3 -c "
import json, sys
from datetime import datetime
print(json.dumps({
    'status': 'build_failed', 'action': sys.argv[1], 'reason': sys.argv[2],
    'finished': datetime.now().isoformat()
}))
" "$ACTION" "$REASON")"
        fi

        log_msg "=== STAGING DEPLOY DONE ==="
    fi

    sleep "$POLL_INTERVAL"
done
