#!/bin/bash
# Mac-side approval script for relay install requests (FB-40).
#
# SECURITY INVARIANT: This script is NOT in guard.sh's whitelist —
# the bot CANNOT invoke it remotely. Only interactive Mac terminal
# access can approve installs. DO NOT add a relay-approve-* command
# to guard.sh — that would break the security model.
#
# Install (root-owned so bot can't overwrite via ~/.relay/ access):
#   sudo cp approve-install.sh ~/.relay/approve-install.sh
#   sudo chown root:staff ~/.relay/approve-install.sh
#   sudo chmod 755 ~/.relay/approve-install.sh
#
# Usage:
#   ~/.relay/approve-install.sh              # review + approve/reject
#   ~/.relay/approve-install.sh --list       # list pending installs
#   ~/.relay/approve-install.sh --reject-all # reject all pending
#   ~/.relay/approve-install.sh --help       # show help

set -euo pipefail

PENDING_DIR="$HOME/.relay/pending-installs"
AUDIT_LOG="$HOME/.relay/audit.log"
AUTO_PURGE_AGE=86400  # 24 hours in seconds

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_audit() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) APPROVE: $1" >> "$AUDIT_LOG"
}

# Defense-in-depth: refuse to run non-interactively
if [ ! -t 0 ]; then
    echo "ERROR: This script must be run from an interactive terminal."
    echo "It cannot be invoked remotely — that's the security model."
    exit 1
fi

# Auto-purge pending dirs older than 24h
purge_old() {
    [ -d "$PENDING_DIR" ] || return 0
    local NOW
    NOW=$(date +%s)
    for ptype in daemon skills; do
        local PDIR="$PENDING_DIR/$ptype"
        [ -d "$PDIR" ] || continue
        local PTS
        PTS=$(cat "$PDIR/.timestamp" 2>/dev/null || echo 0)
        if [ "$PTS" -gt 0 ] 2>/dev/null; then
            local AGE=$((NOW - PTS))
            if [ "$AGE" -gt "$AUTO_PURGE_AGE" ]; then
                rm -rf "$PDIR"
                echo -e "${YELLOW}Auto-purged stale $ptype pending (${AGE}s old)${NC}"
                log_audit "AUTO_PURGE: type=$ptype age=${AGE}s"
            fi
        fi
    done
}

list_pending() {
    [ -d "$PENDING_DIR" ] || { echo "No pending installs."; return 0; }
    purge_old
    local FOUND=0
    for ptype in daemon skills; do
        local PDIR="$PENDING_DIR/$ptype"
        [ -d "$PDIR" ] && [ -f "$PDIR/.sha256" ] || continue
        FOUND=1
        local PSHA PTS AGE NOW
        PSHA=$(cat "$PDIR/.sha256" 2>/dev/null || echo "unknown")
        PTS=$(cat "$PDIR/.timestamp" 2>/dev/null || echo 0)
        AGE="unknown"
        if [ "$PTS" -gt 0 ] 2>/dev/null; then
            NOW=$(date +%s)
            AGE="$((NOW - PTS))s"
        fi
        echo -e "${BLUE}[$ptype]${NC} SHA256: $PSHA | Age: $AGE"
        if [ "$ptype" = "daemon" ] && [ -f "$PDIR/sdk_persistent_daemon.py" ]; then
            local DSIZE
            DSIZE=$(stat -f%z "$PDIR/sdk_persistent_daemon.py" 2>/dev/null || stat -c%s "$PDIR/sdk_persistent_daemon.py" 2>/dev/null || echo "?")
            echo "  File: sdk_persistent_daemon.py (${DSIZE} bytes)"
        elif [ "$ptype" = "skills" ]; then
            echo "  Contents:"
            find "$PDIR" -type f ! -name '.*' | sed "s|$PDIR/|    |" | sort
        fi
    done
    [ "$FOUND" -eq 0 ] && echo "No pending installs."
}

reject_all() {
    [ -d "$PENDING_DIR" ] || { echo "No pending installs."; return 0; }
    for ptype in daemon skills; do
        local PDIR="$PENDING_DIR/$ptype"
        [ -d "$PDIR" ] || continue
        local PSHA
        PSHA=$(cat "$PDIR/.sha256" 2>/dev/null || echo "unknown")
        rm -rf "$PDIR"
        echo -e "${RED}Rejected: $ptype (sha256=$PSHA)${NC}"
        log_audit "REJECTED: type=$ptype sha256=$PSHA"
    done
}

approve_daemon() {
    local PDIR="$PENDING_DIR/daemon"
    [ -d "$PDIR" ] && [ -f "$PDIR/sdk_persistent_daemon.py" ] || { echo "No pending daemon install."; return 0; }

    local PSHA LIVE
    PSHA=$(cat "$PDIR/.sha256" 2>/dev/null || echo "unknown")
    LIVE="$HOME/.relay/sdk_persistent_daemon.py"

    echo -e "\n${BLUE}═══ DAEMON INSTALL REVIEW ═══${NC}"
    echo "SHA256: $PSHA"

    if [ -f "$LIVE" ]; then
        echo -e "\n${YELLOW}Diff (live → pending):${NC}"
        diff --color=auto "$LIVE" "$PDIR/sdk_persistent_daemon.py" 2>/dev/null || true
    else
        echo -e "\n${YELLOW}NEW FILE (no live version exists):${NC}"
        head -30 "$PDIR/sdk_persistent_daemon.py"
        local TOTAL_LINES
        TOTAL_LINES=$(wc -l < "$PDIR/sdk_persistent_daemon.py" | tr -d ' ')
        echo "... ($TOTAL_LINES total lines)"
    fi

    # TOCTOU protection: re-verify SHA256 before approval prompt
    local CURRENT_SHA
    CURRENT_SHA=$(shasum -a 256 "$PDIR/sdk_persistent_daemon.py" 2>/dev/null | cut -d' ' -f1 || echo "changed")
    if [ "$CURRENT_SHA" != "$PSHA" ]; then
        echo -e "\n${RED}WARNING: Payload changed during review (replaced by a new push).${NC}"
        echo "Expected: $PSHA"
        echo "Current:  $CURRENT_SHA"
        echo "Aborting — re-run to review the new version."
        return 1
    fi

    echo ""
    read -p "Approve daemon install? [y/N] " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        # Backup live version
        if [ -f "$LIVE" ]; then
            cp "$LIVE" "${LIVE}.prev"
            echo "  Backed up live daemon → ${LIVE}.prev"
        fi
        # Move pending to live
        mv "$PDIR/sdk_persistent_daemon.py" "$LIVE"
        chmod 0755 "$LIVE"
        rm -rf "$PDIR"
        echo -e "${GREEN}✅ Daemon installed.${NC}"
        log_audit "APPROVED: type=daemon sha256=$PSHA"

        # Auto-kill all daemons so they respawn with new code.
        # Bot's pool detects dead daemons; install_status action triggers
        # immediate respawn (not lazy — daemons are always-live).
        local KILLED=0
        local KILL_PIDS=""
        if [ -d "$HOME/.relay/daemons" ]; then
            for pidf in "$HOME/.relay/daemons/"*.pid; do
                [ -f "$pidf" ] || continue
                local PID
                PID=$(cat "$pidf" 2>/dev/null | tr -d '[:space:]')
                if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
                    kill -TERM "$PID" 2>/dev/null || true
                    KILL_PIDS="$KILL_PIDS $PID"
                    KILLED=$((KILLED + 1))
                fi
                rm -f "$pidf"
                local base
                base=$(basename "$pidf" .pid)
                rm -f "$HOME/.relay/daemons/${base}.state"
            done
            # Brief wait for TERM to take effect, then force-kill survivors
            if [ "$KILLED" -gt 0 ]; then
                sleep 1
                for kpid in $KILL_PIDS; do
                    kill -0 "$kpid" 2>/dev/null && kill -KILL "$kpid" 2>/dev/null || true
                done
            fi
        fi
        if [ "$KILLED" -gt 0 ]; then
            echo "  Killed $KILLED daemon(s) — bot will auto-respawn them."
            log_audit "DAEMON_KILL_POST_APPROVE: killed=$KILLED"
        fi
    else
        rm -rf "$PDIR"
        echo -e "${RED}❌ Daemon install rejected.${NC}"
        log_audit "REJECTED: type=daemon sha256=$PSHA"
    fi
}

approve_skills() {
    local PDIR="$PENDING_DIR/skills"
    [ -d "$PDIR" ] && [ -f "$PDIR/.sha256" ] || { echo "No pending skills install."; return 0; }

    local PSHA SDEST ADEST CMD_FILE
    PSHA=$(cat "$PDIR/.sha256" 2>/dev/null || echo "unknown")
    SDEST="$HOME/.claude/skills"
    ADEST="$HOME/.claude/agents"
    CMD_FILE="$HOME/.claude/CLAUDE.md"

    echo -e "\n${BLUE}═══ SKILLS INSTALL REVIEW ═══${NC}"
    echo "SHA256: $PSHA"
    echo ""

    # Show skills diff
    if [ -d "$SDEST/coding-principles" ]; then
        echo -e "${YELLOW}Skills diff (live → pending):${NC}"
        diff -r --color=auto "$SDEST/coding-principles/" "$PDIR/skills/coding-principles/" 2>/dev/null || true
    else
        echo -e "${YELLOW}NEW: skills/coding-principles/ (no live version)${NC}"
        find "$PDIR/skills/coding-principles" -type f 2>/dev/null | while read -r f; do
            echo "  $(basename "$f")"
        done
    fi

    # Show agents diff
    echo ""
    for f in "$PDIR/agents/"*.md; do
        [ -f "$f" ] || continue
        local bn
        bn=$(basename "$f")
        if [ -f "$ADEST/$bn" ]; then
            local d
            d=$(diff --color=auto "$ADEST/$bn" "$f" 2>/dev/null || true)
            if [ -n "$d" ]; then
                echo -e "${YELLOW}Agent $bn changed:${NC}"
                echo "$d"
            fi
        else
            echo -e "${YELLOW}NEW agent: $bn${NC}"
        fi
    done

    # Show CLAUDE.md snippet — FULL content (prompt injection target!)
    local SNIPPET="$PDIR/CLAUDE.md.snippet"
    if [ -f "$SNIPPET" ]; then
        echo ""
        echo -e "${RED}⚠ CLAUDE.md SNIPPET — REVIEW CAREFULLY (prompt injection risk):${NC}"
        echo "─────────────────────────────────────────"
        cat "$SNIPPET"
        echo ""
        echo "─────────────────────────────────────────"
        local SNIP_LINES
        SNIP_LINES=$(wc -l < "$SNIPPET" | tr -d ' ')
        echo "($SNIP_LINES lines)"
    fi

    # TOCTOU protection: verify .sha256 hasn't changed
    local CURRENT_META_SHA
    CURRENT_META_SHA=$(cat "$PDIR/.sha256" 2>/dev/null || echo "changed")
    if [ "$CURRENT_META_SHA" != "$PSHA" ]; then
        echo -e "\n${RED}WARNING: Payload was replaced during review! Aborting.${NC}"
        return 1
    fi

    echo ""
    read -p "Approve skills install? [y/N] " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        mkdir -p "$SDEST" "$ADEST"

        # Backup live skills
        if [ -d "$SDEST/coding-principles" ]; then
            rm -rf "$SDEST/coding-principles.prev"
            cp -a "$SDEST/coding-principles" "$SDEST/coding-principles.prev"
            echo "  Backed up live skills → coding-principles.prev"
        fi

        # Install skills
        rsync -a --delete "$PDIR/skills/coding-principles/" "$SDEST/coding-principles/"

        # Install agents (only .md files)
        for f in "$PDIR/agents/"*.md; do
            [ -f "$f" ] || continue
            cp "$f" "$ADEST/"
        done

        # Install CLAUDE.md snippet (same idempotent logic as old guard.sh)
        local SNIP_STATUS="no_snippet"
        if [ -f "$SNIPPET" ]; then
            if grep -q "CORPORATE MAC CODING PRINCIPLES" "$CMD_FILE" 2>/dev/null; then
                # Backup + strip existing block + append fresh
                cp "$CMD_FILE" "${CMD_FILE}.prev"
                awk '/<!-- CORPORATE MAC CODING PRINCIPLES/,/<!-- END CORPORATE MAC CODING PRINCIPLES/{next} /<!-- ALFEROV RELAY AWARENESS/,/<!-- END ALFEROV RELAY AWARENESS/{next} {print}' "${CMD_FILE}.prev" > "$CMD_FILE"
                [ -s "$CMD_FILE" ] && echo "" >> "$CMD_FILE"
                cat "$SNIPPET" >> "$CMD_FILE"
                SNIP_STATUS="replaced"
            else
                [ -f "$CMD_FILE" ] && cp "$CMD_FILE" "${CMD_FILE}.prev"
                [ -f "$CMD_FILE" ] && echo "" >> "$CMD_FILE"
                cat "$SNIPPET" >> "$CMD_FILE"
                SNIP_STATUS="appended"
            fi
            echo "  CLAUDE.md snippet: $SNIP_STATUS"
        fi

        rm -rf "$PDIR"
        echo -e "${GREEN}✅ Skills installed (snippet: $SNIP_STATUS).${NC}"
        log_audit "APPROVED: type=skills sha256=$PSHA snippet=$SNIP_STATUS"
    else
        rm -rf "$PDIR"
        echo -e "${RED}❌ Skills install rejected.${NC}"
        log_audit "REJECTED: type=skills sha256=$PSHA"
    fi
}

# === Main ===
case "${1:-}" in
    --help|-h)
        echo "Usage: $0 [--list|--reject-all|--help]"
        echo ""
        echo "  (no args)     Review and approve/reject pending installs"
        echo "  --list        List pending installs"
        echo "  --reject-all  Reject all pending installs"
        echo "  --help        Show this help"
        echo ""
        echo "SECURITY: This script cannot be invoked by the bot — only"
        echo "from an interactive Mac terminal. This is by design."
        exit 0
        ;;
    --list)
        list_pending
        exit 0
        ;;
    --reject-all)
        reject_all
        exit 0
        ;;
    "")
        purge_old
        if [ ! -d "$PENDING_DIR" ] || { [ ! -d "$PENDING_DIR/daemon" ] && [ ! -d "$PENDING_DIR/skills" ]; }; then
            echo "No pending installs."
            exit 0
        fi
        # Process each type
        [ -d "$PENDING_DIR/daemon" ] && approve_daemon
        [ -d "$PENDING_DIR/skills" ] && approve_skills
        echo ""
        echo "Done."
        ;;
    *)
        echo "Unknown option: $1"
        echo "Run with --help for usage."
        exit 1
        ;;
esac
