#!/bin/bash
# Host-side deploy watcher — runs as regular user, watches for bot-triggered deploys.
# Requires: user in 'docker' group (sudo usermod -aG docker $USER)
#
# The bot writes trigger files to /host-repo/deploy/ (mounted from host).
# This watcher runs on the HOST and polls for trigger files.
#
# SETUP (run once on host — or use scripts/setup.sh):
#   chmod +x ~/your-corporate-bot/deploy/host-watcher.sh
#   sudo cp ~/your-corporate-bot/deploy/corporate-bot-watcher.service /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now corporate-bot-watcher
#
# Or run manually: ./deploy/host-watcher.sh

COMPOSE_DIR="${COMPOSE_DIR:-$HOME/corporate-bot}"
DEPLOY_DIR="$COMPOSE_DIR/deploy"
PROJECT_NAME="${COMPOSE_PROJECT_NAME:-$(basename "$COMPOSE_DIR")}"

# Use GIT_SSH_COMMAND from environment (systemd sets this for botuser's deploy key).
# Fallback to user's SSH config for manual runs.
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -F $HOME/.ssh/config}"

# Deploy triggers: bind-mounted dir shared between bot container and this watcher
# (bind mount, not Docker volume — so non-root watcher can read/write directly)
TRIGGERS_DIR="$DEPLOY_DIR/triggers"
TRIGGER="$TRIGGERS_DIR/deploy_trigger.json"
RESULT="$TRIGGERS_DIR/deploy_result.json"
LOG="$DEPLOY_DIR/deploy_watcher.log"
HEALTHY_COMMIT_FILE="$DEPLOY_DIR/last_healthy_commit"
HEALTH_URL="http://localhost:${BOT_PORT:-8000}/health"
HEALTH_TIMEOUT=120
HEALTH_INTERVAL=5
FAILED_LOG="$DEPLOY_DIR/last_failed_startup.log"

get_rollback_target() {
    # Prefer last known healthy commit over pre-pull commit
    if [ -f "$HEALTHY_COMMIT_FILE" ]; then
        cat "$HEALTHY_COMMIT_FILE"
    else
        echo "$1"  # fallback to PREV_COMMIT passed as arg
    fi
}

rollback_to() {
    # Safe rollback: revert to target commit WITHOUT destroying history.
    # Uses git revert to create a new commit that undoes the bad changes,
    # preserving all bot self-upgrade commits in history.
    local target="$1"
    local failed="$2"
    local current
    current=$(git -C "$COMPOSE_DIR" rev-parse HEAD 2>/dev/null)

    if [ "$current" = "$target" ]; then
        echo "$(date) — Already at rollback target $target" | tee -a "$LOG"
        return 0
    fi

    # Tag the failed state for forensics (auto-cleaned after 30 days by git gc)
    git -C "$COMPOSE_DIR" tag -f "failed-deploy-$(date +%Y%m%d-%H%M%S)" HEAD >> "$LOG" 2>&1 || true

    # Revert the range of bad commits (target..HEAD) in one shot
    echo "$(date) — Reverting commits $target..$current" | tee -a "$LOG"
    if git -C "$COMPOSE_DIR" revert --no-edit "$target..HEAD" >> "$LOG" 2>&1; then
        echo "$(date) — Revert commits created successfully" | tee -a "$LOG"
    else
        # If revert has conflicts, abort and fall back to checkout of files only
        git -C "$COMPOSE_DIR" revert --abort >> "$LOG" 2>&1 || true
        echo "$(date) — Revert had conflicts, using checkout-based rollback" | tee -a "$LOG"
        git -C "$COMPOSE_DIR" checkout "$target" -- . >> "$LOG" 2>&1
        git -C "$COMPOSE_DIR" commit -m "rollback: restore $target after failed deploy ($failed)" >> "$LOG" 2>&1
    fi
}

save_healthy_commit() {
    local commit="$1"
    echo "$commit" > "$HEALTHY_COMMIT_FILE"
    echo "$(date) — Saved healthy commit: $commit" | tee -a "$LOG"
}

wait_for_health() {
    local elapsed=0
    echo "$(date) — Waiting for health check ($HEALTH_URL, timeout ${HEALTH_TIMEOUT}s)..." | tee -a "$LOG"
    while [ $elapsed -lt $HEALTH_TIMEOUT ]; do
        if curl -sf "$HEALTH_URL" > /dev/null 2>&1; then
            echo "$(date) — Health check passed after ${elapsed}s" | tee -a "$LOG"
            return 0
        fi
        sleep $HEALTH_INTERVAL
        elapsed=$((elapsed + HEALTH_INTERVAL))
    done
    echo "$(date) — Health check FAILED after ${HEALTH_TIMEOUT}s!" | tee -a "$LOG"
    return 1
}

capture_failed_logs() {
    # Capture dying container's logs before rollback destroys them.
    # Called after health check failure, before container recreation.
    local failed_commit="$1"
    echo "$(date) — Capturing failed container logs..." | tee -a "$LOG"
    {
        echo "=== FAILED DEPLOY LOGS ==="
        echo "Commit: $failed_commit"
        echo "Captured: $(date -Iseconds)"
        echo ""
        echo "--- docker compose logs bot-core (last 300 lines) ---"
        docker compose logs --tail=300 --no-color bot-core 2>&1
        echo ""
        echo "--- docker inspect (State) ---"
        docker inspect --format='{{json .State}}' "${PROJECT_NAME}-bot-core" 2>/dev/null \
            | python3 -m json.tool 2>/dev/null || echo "(inspect failed)"
    } > "$FAILED_LOG" 2>&1
    echo "$(date) — Failed logs saved to $FAILED_LOG ($(wc -l < "$FAILED_LOG") lines)" | tee -a "$LOG"
}


# Deploy approval config — /opt/ is OUTSIDE Docker mounts
APPROVAL_DIR="/opt/${PROJECT_NAME}-watcher/approvals"
APPROVE_SCRIPT="/opt/${PROJECT_NAME}-watcher/approve-deploy.sh"
APPROVAL_REQUEST_FILE="$TRIGGERS_DIR/approval_request.json"

log_msg() {
    echo "$(date) — $1" | tee -a "$LOG"
}

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
# Initialize healthy commit file on first run (before any deploys)
if [ ! -f "$HEALTHY_COMMIT_FILE" ]; then
    INIT_COMMIT=$(git -C "$COMPOSE_DIR" rev-parse HEAD 2>/dev/null || echo "")
    if [ -n "$INIT_COMMIT" ]; then
        echo "$INIT_COMMIT" > "$HEALTHY_COMMIT_FILE"
        echo "$(date) — Initialized healthy commit: $INIT_COMMIT" | tee -a "$LOG"
    fi
fi

echo "$(date) — Deploy watcher started (user: $(whoami))" | tee -a "$LOG"
echo "  Compose dir: $COMPOSE_DIR" | tee -a "$LOG"
echo "  Watching: $TRIGGER" | tee -a "$LOG"

while true; do
    if [ -f "$TRIGGER" ]; then
        echo "$(date) — Deploy trigger detected!" | tee -a "$LOG"
        cat "$TRIGGER" | tee -a "$LOG"

        ACTION=$(python3 -c "import json,sys; print(json.load(sys.stdin).get('action','rebuild'))" < "$TRIGGER" 2>/dev/null || echo "rebuild")
        # Whitelist valid actions to prevent injection
        case "$ACTION" in
            rebuild|restart|rebuild-all) ;;
            *) echo "$(date) — Invalid action '$ACTION', defaulting to rebuild" | tee -a "$LOG"; ACTION="rebuild" ;;
        esac
        # Escape reason for safe JSON embedding (replace backslashes, quotes, newlines)
        REASON=$(python3 -c "
import json,sys
r = json.load(sys.stdin).get('reason','unknown')
# Output JSON-safe string (no surrounding quotes)
print(json.dumps(r)[1:-1])
" < "$TRIGGER" 2>/dev/null || echo "unknown")

        rm -f "$TRIGGER"
        echo "{\"status\":\"in_progress\",\"started\":\"$(date -Iseconds)\"}" > "$RESULT"

        cd "$COMPOSE_DIR"

        # Save current commit for rollback
        PREV_COMMIT=$(git -C "$COMPOSE_DIR" rev-parse HEAD 2>/dev/null || echo "")

        # Skip git for restart
        if [ "$ACTION" != "restart" ]; then

        # Fetch (don't merge — check sensitive files first)
        git -C "$COMPOSE_DIR" fetch origin main >> "$LOG" 2>&1

        # Baseline = last healthy commit (what's running in prod)
        GATE_BASELINE=""
        if [ -f "$HEALTHY_COMMIT_FILE" ]; then
            GATE_BASELINE=$(cat "$HEALTHY_COMMIT_FILE")
        fi
        GATE_BASELINE="${GATE_BASELINE:-$PREV_COMMIT}"
        FETCH_COMMIT=$(git -C "$COMPOSE_DIR" rev-parse origin/main 2>/dev/null || echo "")

        # Detect sensitive files since last healthy deploy
        SENSITIVE_FILES=$(detect_sensitive_files "$GATE_BASELINE" "$FETCH_COMMIT")
        if [ -n "$SENSITIVE_FILES" ]; then
            APPROVAL_ID="${GATE_BASELINE}..${FETCH_COMMIT}"
            write_approval_request "$GATE_BASELINE" "$FETCH_COMMIT" "$SENSITIVE_FILES"
            echo "{\"status\":\"pending_approval\",\"action\":\"$ACTION\",\"approval_id\":\"$APPROVAL_ID\",\"started\":\"$(date -Iseconds)\"}" > "$RESULT"
            if ! wait_for_approval "$APPROVAL_ID" "$SENSITIVE_FILES"; then
                if [ -f "$TRIGGER" ]; then
                    echo "{\"status\":\"superseded\",\"finished\":\"$(date -Iseconds)\"}" > "$RESULT"
                else
                    echo "{\"status\":\"rejected\",\"reason\":\"Operator rejected\",\"finished\":\"$(date -Iseconds)\"}" > "$RESULT"
                fi
                rm -f "$APPROVAL_REQUEST_FILE"
                sleep 5
                continue
            fi
            rm -f "$APPROVAL_REQUEST_FILE"
        fi

        # Merge
        git -C "$COMPOSE_DIR" merge origin/main >> "$LOG" 2>&1 || {
            echo "$(date) — Merge failed, trying reset" | tee -a "$LOG"
            git -C "$COMPOSE_DIR" reset --hard origin/main >> "$LOG" 2>&1 || true
        }

        fi  # end ACTION != restart

        NEW_COMMIT=$(git -C "$COMPOSE_DIR" rev-parse HEAD 2>/dev/null || echo "")

        # Ensure shared network exists (for prod↔staging connectivity)
        docker network create corporate-bot-shared 2>/dev/null || true

        case "$ACTION" in
            rebuild)
                echo "$(date) — Rebuilding bot-core ($PREV_COMMIT → $NEW_COMMIT)..." | tee -a "$LOG"
                if docker compose build --no-cache --build-arg GIT_COMMIT="$(git -C "$COMPOSE_DIR" rev-parse HEAD 2>/dev/null || echo unknown)" bot-core >> "$LOG" 2>&1; then
                    echo "$(date) — Build OK. Restarting..." | tee -a "$LOG"
                    docker compose up -d bot-core >> "$LOG" 2>&1

                    if wait_for_health; then
                        save_healthy_commit "$NEW_COMMIT"
                        # Prune old images and ALL build cache to prevent disk bloat
                        # (--no-cache builds don't reuse cache, so keeping it wastes disk)
                        echo "$(date) — Pruning dangling images and build cache..." | tee -a "$LOG"
                        docker image prune -f >> "$LOG" 2>&1
                        docker buildx prune -f --all >> "$LOG" 2>&1
                        echo "{\"status\":\"success\",\"action\":\"$ACTION\",\"reason\":\"$REASON\",\"commit\":\"$NEW_COMMIT\",\"finished\":\"$(date -Iseconds)\"}" > "$RESULT"
                        echo "$(date) — Deploy complete!" | tee -a "$LOG"
                    else
                        capture_failed_logs "$NEW_COMMIT"
                        ROLLBACK_TARGET=$(get_rollback_target "$PREV_COMMIT")
                        if [ -n "$ROLLBACK_TARGET" ] && [ "$ROLLBACK_TARGET" != "$NEW_COMMIT" ]; then
                            echo "$(date) — AUTO-ROLLBACK: reverting to $ROLLBACK_TARGET (last healthy)" | tee -a "$LOG"
                            rollback_to "$ROLLBACK_TARGET" "$NEW_COMMIT"
                            if docker compose build --no-cache --build-arg GIT_COMMIT="$(git -C "$COMPOSE_DIR" rev-parse HEAD 2>/dev/null || echo unknown)" bot-core >> "$LOG" 2>&1; then
                                docker compose up -d bot-core >> "$LOG" 2>&1
                                echo "$(date) — Rollback build complete, waiting for health..." | tee -a "$LOG"
                                if wait_for_health; then
                                    save_healthy_commit "$ROLLBACK_TARGET"
                                    echo "{\"status\":\"rolled_back\",\"action\":\"$ACTION\",\"reason\":\"$REASON\",\"failed_commit\":\"$NEW_COMMIT\",\"restored_commit\":\"$ROLLBACK_TARGET\",\"finished\":\"$(date -Iseconds)\"}" > "$RESULT"
                                    echo "$(date) — Rollback successful! Restored $ROLLBACK_TARGET" | tee -a "$LOG"
                                else
                                    echo "{\"status\":\"rollback_unhealthy\",\"action\":\"$ACTION\",\"reason\":\"$REASON\",\"failed_commit\":\"$NEW_COMMIT\",\"restored_commit\":\"$ROLLBACK_TARGET\",\"finished\":\"$(date -Iseconds)\"}" > "$RESULT"
                                    echo "$(date) — WARNING: Rollback deployed but health check still failing!" | tee -a "$LOG"
                                fi
                            else
                                echo "{\"status\":\"rollback_build_failed\",\"action\":\"$ACTION\",\"failed_commit\":\"$NEW_COMMIT\",\"finished\":\"$(date -Iseconds)\"}" > "$RESULT"
                                echo "$(date) — CRITICAL: Rollback build also failed!" | tee -a "$LOG"
                            fi
                        else
                            echo "{\"status\":\"unhealthy\",\"action\":\"$ACTION\",\"reason\":\"$REASON\",\"commit\":\"$NEW_COMMIT\",\"finished\":\"$(date -Iseconds)\"}" > "$RESULT"
                            echo "$(date) — Deploy unhealthy, no healthy commit to rollback to" | tee -a "$LOG"
                        fi
                    fi
                else
                    ROLLBACK_TARGET=$(get_rollback_target "$PREV_COMMIT")
                    echo "$(date) — BUILD FAILED! Reverting to $ROLLBACK_TARGET" | tee -a "$LOG"
                    if [ -n "$ROLLBACK_TARGET" ] && [ "$ROLLBACK_TARGET" != "$NEW_COMMIT" ]; then
                        rollback_to "$ROLLBACK_TARGET" "$NEW_COMMIT"
                    fi
                    echo "{\"status\":\"build_failed\",\"action\":\"$ACTION\",\"reason\":\"$REASON\",\"failed_commit\":\"$NEW_COMMIT\",\"restored_commit\":\"$ROLLBACK_TARGET\",\"finished\":\"$(date -Iseconds)\"}" > "$RESULT"
                fi
                ;;
            restart)
                echo "$(date) — Restarting bot-core (no rebuild)..." | tee -a "$LOG"
                docker compose restart bot-core >> "$LOG" 2>&1
                echo "{\"status\":\"success\",\"action\":\"$ACTION\",\"reason\":\"$REASON\",\"finished\":\"$(date -Iseconds)\"}" > "$RESULT"
                ;;
            rebuild-all)
                echo "$(date) — Rebuilding ALL services..." | tee -a "$LOG"
                # Pre-check Go compilation for wa-bridge (catches errors before slow Docker build)
                GO_IMAGE="golang:1.25-bookworm"
                echo "$(date) — Pre-checking Go compilation (wa-bridge)..." | tee -a "$LOG"
                GO_CHECK_OUTPUT=$(docker run --rm -v "$COMPOSE_DIR/whatsapp-bridge:/src:ro" -w /src "$GO_IMAGE" sh -c "CGO_ENABLED=1 go build -o /dev/null . 2>&1" 2>&1) || true
                GO_CHECK_EXIT=$?
                if [ -n "$GO_CHECK_OUTPUT" ]; then
                    echo "$GO_CHECK_OUTPUT" | tee -a "$LOG"
                fi
                if echo "$GO_CHECK_OUTPUT" | grep -q "^./.*\.go:[0-9]*:[0-9]*:"; then
                    echo "$(date) — Go compilation FAILED! Reverting to last healthy commit." | tee -a "$LOG"
                    ROLLBACK_TARGET=$(get_rollback_target "$PREV_COMMIT")
                    if [ -n "$ROLLBACK_TARGET" ] && [ "$ROLLBACK_TARGET" != "$NEW_COMMIT" ]; then
                        rollback_to "$ROLLBACK_TARGET" "$NEW_COMMIT"
                    fi
                    echo "{\"status\":\"build_failed\",\"action\":\"$ACTION\",\"reason\":\"Go compilation error: $REASON\",\"failed_commit\":\"$NEW_COMMIT\",\"restored_commit\":\"$ROLLBACK_TARGET\",\"finished\":\"$(date -Iseconds)\"}" > "$RESULT"
                    sleep 5
                    continue
                fi
                echo "$(date) — Go pre-check passed." | tee -a "$LOG"
                if docker compose build --no-cache --build-arg GIT_COMMIT="$(git -C "$COMPOSE_DIR" rev-parse HEAD 2>/dev/null || echo unknown)" >> "$LOG" 2>&1; then
                    docker compose up -d >> "$LOG" 2>&1
                    # Prune old images and ALL build cache to prevent disk bloat
                    docker image prune -f >> "$LOG" 2>&1
                    docker buildx prune -f --all >> "$LOG" 2>&1
                    echo "{\"status\":\"success\",\"action\":\"$ACTION\",\"reason\":\"$REASON\",\"finished\":\"$(date -Iseconds)\"}" > "$RESULT"
                else
                    echo "{\"status\":\"build_failed\",\"action\":\"$ACTION\",\"reason\":\"$REASON\",\"finished\":\"$(date -Iseconds)\"}" > "$RESULT"
                fi
                ;;
            *)
                echo "{\"status\":\"error\",\"message\":\"Unknown action: $ACTION\"}" > "$RESULT"
                ;;
        esac
    fi
    sleep 5
done
