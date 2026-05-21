#!/usr/bin/env bash
# Patch host-watcher to use /health/deep for deploy health checks.
# Run on HOST (not in container):
#   sudo bash /home/botuser/corporate-bot/scripts/patch_watcher_health.sh
#
# What it does:
# 1. Updates deploy/host-watcher.sh in repo to use /health/deep with /health fallback
# 2. Copies updated watcher to /opt/corporate-bot-watcher/ (installed location)
# 3. Restarts the watcher service
#
# The /health/deep endpoint exercises the critical query() code path (harness,
# prompt, browser profiles) — unlike /health which only checks HTTP liveness.
# Added after the _harness incident (Apr 29 2026).

set -euo pipefail

REPO_WATCHER="/home/botuser/corporate-bot/deploy/host-watcher.sh"
OPT_WATCHER="/opt/corporate-bot-watcher/host-watcher.sh"

if [[ ! -f "$REPO_WATCHER" ]]; then
    echo "ERROR: $REPO_WATCHER not found"
    exit 1
fi

echo "=== Patching health check in host-watcher ==="

# Find and replace the health check curl command.
# Pattern: curl to localhost:PORT/health (but NOT /health/deep already)
# Replace with: try /health/deep first, fallback to /health
#
# Common patterns in the watcher:
#   curl -sf http://localhost:$PORT/health
#   curl -sf --max-time N http://localhost:$PORT/health
#   curl -s http://localhost:${PORT}/health

# Use sed to replace the health check — match any curl line hitting /health
# but NOT /health/deep (avoid double-patching)
if grep -q '/health/deep' "$REPO_WATCHER"; then
    echo "Already patched — /health/deep found in $REPO_WATCHER"
else
    # Replace simple /health curl with deep+fallback pattern
    # This sed matches lines containing 'curl' and '/health' but not '/health/deep'
    # and not '/health/' followed by other paths
    sed -i.bak -E \
        's|(curl\s+-[a-z]+\s+)(--max-time\s+[0-9]+\s+)?(http://localhost:[^/]+)/health(\s*)\)|\1--max-time 10 \3/health/deep 2>/dev/null || \1--max-time 5 \3/health\4)|g' \
        "$REPO_WATCHER"

    # If the sed pattern didn't match (watcher format differs), try a simpler approach:
    if ! grep -q '/health/deep' "$REPO_WATCHER"; then
        echo "WARNING: Auto-sed didn't match. Trying line-by-line replacement..."
        # Restore backup and do manual replacement
        cp "$REPO_WATCHER.bak" "$REPO_WATCHER"

        # Find the line number with the health check
        HEALTH_LINE=$(grep -n '/health"' "$REPO_WATCHER" | grep -i curl | head -1 | cut -d: -f1)
        if [[ -n "$HEALTH_LINE" ]]; then
            echo "Found health check at line $HEALTH_LINE"
            # Show the line for manual review
            sed -n "${HEALTH_LINE}p" "$REPO_WATCHER"
            echo ""
            echo "Please manually edit $REPO_WATCHER:"
            echo "  Change the /health URL to /health/deep"
            echo "  Or add fallback: curl ... /health/deep || curl ... /health"
        else
            echo "ERROR: Could not find health check curl line in $REPO_WATCHER"
            echo "Please manually add /health/deep to the health check."
            exit 1
        fi
    else
        echo "Patched successfully!"
        echo "Diff:"
        diff "$REPO_WATCHER.bak" "$REPO_WATCHER" || true
    fi
fi

# Copy to /opt/ install location
echo ""
echo "=== Syncing to $OPT_WATCHER ==="
if [[ -f "$OPT_WATCHER" ]]; then
    cp "$REPO_WATCHER" "$OPT_WATCHER"
    chown botuser:botuser "$OPT_WATCHER"
    chmod 755 "$OPT_WATCHER"
    echo "Copied to $OPT_WATCHER"
else
    echo "WARNING: $OPT_WATCHER not found — skip copy"
fi

# Restart watcher service
echo ""
echo "=== Restarting watcher service ==="
if systemctl is-active --quiet host-watcher 2>/dev/null; then
    systemctl restart host-watcher
    echo "host-watcher service restarted"
elif systemctl is-active --quiet corporate-bot-watcher 2>/dev/null; then
    systemctl restart corporate-bot-watcher
    echo "corporate-bot-watcher service restarted"
else
    echo "WARNING: No watcher service found running. Restart manually."
fi

echo ""
echo "Done. Verify: journalctl -u host-watcher -n 5 --no-pager"
