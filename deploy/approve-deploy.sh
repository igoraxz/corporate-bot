#!/bin/bash
# Deploy approval script — reviews sensitive file changes and approves/rejects.
# Installed at /opt/corporate-bot-watcher/approve-deploy.sh (root-owned, NOT in git).
# Same script works for staging at /opt/corporate-bot-staging-watcher/approve-staging-deploy.sh
#
# Usage:
#   sudo /opt/corporate-bot-watcher/approve-deploy.sh           # review + approve/reject
#   sudo /opt/corporate-bot-watcher/approve-deploy.sh --reject   # reject immediately
#   sudo /opt/corporate-bot-watcher/approve-deploy.sh --status   # show current state

set -euo pipefail

# Detect context from install path
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Auto-detect INSTANCE_NAME from install path if not set via env.
# /opt/aion-bot-watcher/ → aion-bot, /opt/family-bot-staging-watcher/ → family-bot
if [ -z "${INSTANCE_NAME:-}" ]; then
    _dir_name="$(basename "$SCRIPT_DIR")"
    INSTANCE_NAME="${_dir_name%-watcher}"
    INSTANCE_NAME="${INSTANCE_NAME%-staging}"
    INSTANCE_NAME="${INSTANCE_NAME:-corporate-bot}"
fi

if [[ "$SCRIPT_DIR" == *"staging"* ]]; then
    TRIGGER_DIR="${TRIGGER_DIR:-/home/botuser/${INSTANCE_NAME}/deploy/staging-triggers}"
    APPROVAL_DIR="$SCRIPT_DIR/approvals"
    WATCHER_NAME="STAGING"
else
    TRIGGER_DIR="${TRIGGER_DIR:-/home/botuser/${INSTANCE_NAME}/deploy/triggers}"
    APPROVAL_DIR="$SCRIPT_DIR/approvals"
    WATCHER_NAME="PROD"
fi

# Always use prod repo for git diff (has full history, both clones track same remote)
GIT_REPO="${GIT_REPO:-/home/botuser/${INSTANCE_NAME}}"

RESULT_FILE="$TRIGGER_DIR/deploy_result.json"
REQUEST_FILE="$TRIGGER_DIR/approval_request.json"

mkdir -p "$APPROVAL_DIR"

# --- Status mode ---
if [ "${1:-}" = "--status" ]; then
    echo "═══ $WATCHER_NAME DEPLOY STATUS ═══"
    if [ -f "$RESULT_FILE" ]; then
        python3 -c "import json; d=json.load(open('$RESULT_FILE')); print(json.dumps(d, indent=2))"
    else
        echo "No deploy result found."
    fi
    exit 0
fi

# --- Read approval request ---
if [ ! -f "$REQUEST_FILE" ]; then
    echo "No pending approval request found."
    exit 0
fi

APPROVAL_ID=$(python3 -c "import json; print(json.load(open('$REQUEST_FILE')).get('approval_id',''))" 2>/dev/null || echo "")
BASELINE=$(python3 -c "import json; print(json.load(open('$REQUEST_FILE')).get('baseline',''))" 2>/dev/null || echo "")
TARGET=$(python3 -c "import json; print(json.load(open('$REQUEST_FILE')).get('target',''))" 2>/dev/null || echo "")

if [ -z "$APPROVAL_ID" ]; then
    echo "Invalid approval request — missing approval_id"
    exit 1
fi

# Verify watcher is waiting
if [ -f "$RESULT_FILE" ]; then
    STATUS=$(python3 -c "import json; print(json.load(open('$RESULT_FILE')).get('status',''))" 2>/dev/null || echo "")
    if [ "$STATUS" != "pending_approval" ]; then
        echo "Watcher is not waiting for approval (status: $STATUS)"
        exit 0
    fi
fi

echo "═══════════════════════════════════════════════"
echo "  $WATCHER_NAME DEPLOY APPROVAL"
echo "═══════════════════════════════════════════════"
echo ""
echo "Approval ID: $APPROVAL_ID"
echo "Baseline:    ${BASELINE:0:12}"
echo "Target:      ${TARGET:0:12}"
echo ""

# Show sensitive files from request
echo "Sensitive files:"
python3 -c "
import json
req = json.load(open('$REQUEST_FILE'))
for f in req.get('sensitive_files', []):
    print(f'  → {f}')
" 2>/dev/null || true
echo ""

# Show diff using PROD repo (full history available)
if [ -n "$BASELINE" ] && [ -n "$TARGET" ]; then
    echo "─── Diff summary ───"
    git --no-pager -C "$GIT_REPO" diff --stat "$BASELINE".."$TARGET" -- \
        Dockerfile* *.Dockerfile docker-compose* compose*.yml compose*.yaml \
        entrypoint* seccomp* apparmor* *.service \
        deploy/*.sh scripts/*.sh .github/workflows/* 2>/dev/null | cat
    echo ""
    echo "─── Full diff of sensitive files ───"
    git --no-pager -C "$GIT_REPO" diff "$BASELINE".."$TARGET" -- \
        Dockerfile* *.Dockerfile docker-compose* compose*.yml compose*.yaml \
        entrypoint* seccomp* apparmor* *.service \
        deploy/*.sh scripts/*.sh .github/workflows/* 2>/dev/null | cat
fi
echo ""
echo "─────────────────────────────"

# --- Reject mode ---
if [ "${1:-}" = "--reject" ]; then
    python3 -c "
import json, sys, os
from datetime import datetime
req = json.load(open(sys.argv[1]))
d = {'approval_id': req.get('approval_id',''), 'rejected_at': datetime.now().isoformat(), 'rejected_by': os.environ.get('SUDO_USER', 'unknown')}
with open(sys.argv[2] + '/rejected.json', 'w') as f:
    json.dump(d, f, indent=2)
" "$REQUEST_FILE" "$APPROVAL_DIR"
    echo "❌ Deploy rejected"
    exit 0
fi

# --- Approve mode ---
read -p "Approve this deploy? [y/N] " -n 1 -r
echo ""

if [[ $REPLY =~ ^[Yy]$ ]]; then
    python3 -c "
import json, sys, os
from datetime import datetime
req = json.load(open(sys.argv[1]))
d = {'approval_id': req.get('approval_id',''), 'approved_at': datetime.now().isoformat(), 'approved_by': os.environ.get('SUDO_USER', 'unknown')}
with open(sys.argv[2] + '/approved.json', 'w') as f:
    json.dump(d, f, indent=2)
" "$REQUEST_FILE" "$APPROVAL_DIR"
    echo "✅ Deploy approved — watcher will proceed"
else
    python3 -c "
import json, sys, os
from datetime import datetime
req = json.load(open(sys.argv[1]))
d = {'approval_id': req.get('approval_id',''), 'rejected_at': datetime.now().isoformat(), 'rejected_by': os.environ.get('SUDO_USER', 'unknown')}
with open(sys.argv[2] + '/rejected.json', 'w') as f:
    json.dump(d, f, indent=2)
" "$REQUEST_FILE" "$APPROVAL_DIR"
    echo "❌ Deploy rejected"
fi
