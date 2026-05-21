#!/bin/bash
# Ensure Homebrew + nvm tools (tmux, claude) are in PATH for SSH command= context.
# Claude CLI is installed via nvm/npm, not Homebrew — must include
# ~/.nvm/versions/node/*/bin so the persistent daemon finds it via shutil.which.
# Glob expansion is done manually below since PATH cannot contain globs.
_NVM_BIN=""
if [ -d "$HOME/.nvm/versions/node" ]; then
    for _v in "$HOME/.nvm/versions/node"/*/bin; do
        [ -d "$_v" ] && _NVM_BIN="$_v:$_NVM_BIN"
    done
fi
export PATH="${_NVM_BIN}/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"
unset _NVM_BIN _v

# Desktop Relay Guard — Mac-side SSH command gatekeeper
#
# Install: place at ~/.relay/guard.sh on Mac
# Reference in ~/.ssh/authorized_keys:
#   command="$HOME/.relay/guard.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding ssh-ed25519 AAAA...
#
# This script validates TOTP auth sessions before executing any command.
# Only whitelisted commands (tmux, claude, relay-*) are allowed.
#
# Session lifecycle:
#   - Session file: {token}:{last_activity_timestamp}
#   - Sliding window TTL: 2h from LAST activity (not from auth time)
#   - Every successful command updates last_activity (heartbeat)
#   - On expiry/lock: all relay_* tmux sessions are killed

SESSION_FILE=~/.relay/session
AUDIT_LOG=~/.relay/audit.log
CMD="$SSH_ORIGINAL_COMMAND"
SESSION_TTL=7200  # 2 hours in seconds

# Log every attempt
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) CMD: $CMD" >> "$AUDIT_LOG"

# === HELPER: Kill all bot-created tmux sessions ===
kill_relay_sessions() {
    local killed=0
    for sess in $(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^relay_'); do
        tmux kill-session -t "$sess" 2>/dev/null && killed=$((killed + 1))
    done
    if [ "$killed" -gt 0 ]; then
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) KILLED_RELAY_SESSIONS: $killed" >> "$AUDIT_LOG"
    fi
    echo "$killed"
}

# === HELPER: Update last_activity heartbeat ===
update_heartbeat() {
    if [ -f "$SESSION_FILE" ]; then
        local token
        token=$(cut -d: -f1 "$SESSION_FILE")
        echo "${token}:$(date +%s)" > "$SESSION_FILE"
    fi
}

# Allow auth commands without session — validate code format first
if [[ "$CMD" == relay-auth\ * ]]; then
    CODE="${CMD#relay-auth }"
    # H2 fix: strict 6-digit validation prevents injection into auth.sh's python
    if ! echo "$CODE" | grep -qE '^[0-9]{6}$'; then
        echo "AUTH_FAILED:invalid_code_format"
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) AUTH_INVALID_FORMAT: $CODE" >> "$AUDIT_LOG"
        exit 1
    fi
    exec ~/.relay/auth.sh "$CODE"
fi

# Allow ping (health check, no session needed)
if [[ "$CMD" == "relay-ping" ]]; then
    echo "PONG:$(date +%s)"
    exit 0
fi

# === TOTP-EXEMPT: Backup operations ===
# Backup runs hourly via cron — cannot do interactive TOTP.
# Security: filename strictly validated, destination restricted, content AES-256 encrypted.
# Same SSH key as relay — just no session requirement for these commands.
BACKUP_DIR="$HOME/.backups/corporate-bot"

if [[ "$CMD" == relay-backup-put\ * ]]; then
    BFNAME="${CMD#relay-backup-put }"
    # Strict filename: bot-backup-YYYY-MM-DD-HHMM.7z only
    if ! echo "$BFNAME" | grep -qE '^bot-backup-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{4}\.7z$'; then
        echo "ERROR:invalid_filename"
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) BACKUP_REJECTED: invalid filename: $BFNAME" >> "$AUDIT_LOG"
        exit 1
    fi
    mkdir -p "$BACKUP_DIR" && chmod 700 "$BACKUP_DIR"
    # Size cap: 2 GB (encrypted 7z, typically ~200-500 MB)
    MAX_BACKUP_BYTES=$((2 * 1024 * 1024 * 1024))
    head -c "$MAX_BACKUP_BYTES" > "$BACKUP_DIR/$BFNAME"
    BSIZE=$(stat -f%z "$BACKUP_DIR/$BFNAME" 2>/dev/null || stat -c%s "$BACKUP_DIR/$BFNAME" 2>/dev/null || echo 0)
    # Reject suspiciously small files (partial write / empty stdin)
    if [ "$BSIZE" -lt 1024 ] 2>/dev/null; then
        rm -f "$BACKUP_DIR/$BFNAME"
        echo "ERROR:file_too_small:${BSIZE}"
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) BACKUP_REJECTED: too small: $BFNAME (${BSIZE} bytes)" >> "$AUDIT_LOG"
        exit 1
    fi
    echo "OK:${BSIZE}"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) BACKUP_PUT: $BFNAME (${BSIZE} bytes)" >> "$AUDIT_LOG"
    exit 0
fi

if [[ "$CMD" == "relay-backup-list" ]]; then
    if [ -d "$BACKUP_DIR" ]; then
        ls -lhtr "$BACKUP_DIR"/bot-backup-*.7z 2>/dev/null || echo "(none)"
    else
        echo "(no backup dir)"
    fi
    exit 0
fi

if [[ "$CMD" == "relay-backup-latest" ]]; then
    # Returns just the filename of the most recent backup (for --from-mac)
    if [ -d "$BACKUP_DIR" ]; then
        ls -1t "$BACKUP_DIR"/bot-backup-*.7z 2>/dev/null | head -1 | xargs basename 2>/dev/null || echo ""
    fi
    exit 0
fi

if [[ "$CMD" == relay-backup-get\ * ]]; then
    BFNAME="${CMD#relay-backup-get }"
    # Same strict filename validation as put
    if ! echo "$BFNAME" | grep -qE '^bot-backup-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{4}\.7z$'; then
        echo "ERROR:invalid_filename" >&2
        exit 1
    fi
    if [ ! -f "$BACKUP_DIR/$BFNAME" ]; then
        echo "ERROR:not_found" >&2
        exit 1
    fi
    cat "$BACKUP_DIR/$BFNAME"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) BACKUP_GET: $BFNAME" >> "$AUDIT_LOG"
    exit 0
fi

if [[ "$CMD" == "relay-backup-prune" ]]; then
    if [ ! -d "$BACKUP_DIR" ]; then echo "OK:no_backups"; exit 0; fi
    cd "$BACKUP_DIR" 2>/dev/null || exit 0
    ALL=$(ls -1t bot-backup-*.7z 2>/dev/null) || { echo "OK:no_backups"; exit 0; }
    [ -z "$ALL" ] && { echo "OK:no_backups"; exit 0; }
    # 4-tier retention (macOS date -j -f syntax):
    # Tier 1: 12 most recent (2-hourly, last 24h), Tier 2: newest/day last 7d,
    # Tier 3: newest/ISO-week last 28d, Tier 4: newest/month forever
    KEEP_FILE=$(mktemp)
    NOW_S=$(date +%s)
    # Tier 1: 12 most recent (last 24h at 2h cadence)
    echo "$ALL" | head -12 >> "$KEEP_FILE"
    # Tier 2: newest per calendar day, last 7 days
    SEEN=$(mktemp)
    while IFS= read -r f; do
        D=$(echo "$f" | sed 's/bot-backup-\([0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]\).*/\1/')
        TS=$(date -j -f "%Y-%m-%d" "$D" "+%s" 2>/dev/null || echo 0)
        AGE_D=$(( (NOW_S - TS) / 86400 ))
        [ "$AGE_D" -gt 7 ] && continue
        grep -qxF "$D" "$SEEN" && continue
        echo "$D" >> "$SEEN"; echo "$f" >> "$KEEP_FILE"
    done <<< "$ALL"
    rm -f "$SEEN"
    # Tier 3: newest per ISO week, last 28 days
    SEEN=$(mktemp)
    while IFS= read -r f; do
        D=$(echo "$f" | sed 's/bot-backup-\([0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]\).*/\1/')
        TS=$(date -j -f "%Y-%m-%d" "$D" "+%s" 2>/dev/null || echo 0)
        AGE_D=$(( (NOW_S - TS) / 86400 ))
        [ "$AGE_D" -gt 28 ] && continue
        WK=$(date -j -f "%Y-%m-%d" "$D" "+%G-W%V" 2>/dev/null || echo "unk")
        grep -qxF "$WK" "$SEEN" && continue
        echo "$WK" >> "$SEEN"; echo "$f" >> "$KEEP_FILE"
    done <<< "$ALL"
    rm -f "$SEEN"
    # Tier 4: newest per month, forever
    SEEN=$(mktemp)
    while IFS= read -r f; do
        M=$(echo "$f" | sed 's/bot-backup-\([0-9][0-9][0-9][0-9]-[0-9][0-9]\).*/\1/')
        grep -qxF "$M" "$SEEN" && continue
        echo "$M" >> "$SEEN"; echo "$f" >> "$KEEP_FILE"
    done <<< "$ALL"
    rm -f "$SEEN"
    KEEP=$(sort -u "$KEEP_FILE"); rm -f "$KEEP_FILE"
    DELETED=0
    while IFS= read -r f; do
        echo "$KEEP" | grep -qxF "$f" || { rm -f "$f" && DELETED=$((DELETED + 1)); }
    done <<< "$ALL"
    REMAINING=$(ls -1 bot-backup-*.7z 2>/dev/null | wc -l | tr -d ' ')
    echo "OK:${REMAINING}_remaining,${DELETED}_deleted"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) BACKUP_PRUNE: ${REMAINING} remaining, ${DELETED} deleted" >> "$AUDIT_LOG"
    exit 0
fi

if [[ "$CMD" == "relay-backup-collect" ]]; then
    # Collect Mac-side relay + Claude data as tar.gz to stdout
    # Used by backup.sh --with-mac
    cd "$HOME" || exit 1
    DIRS=""
    [ -d .relay ] && DIRS="$DIRS .relay"
    [ -d .claude ] && DIRS="$DIRS .claude"
    if [ -z "$DIRS" ]; then
        echo "WARN: No Mac data dirs found" >&2
        exit 0
    fi
    tar czf - \
        --exclude='.claude/projects' \
        --exclude='.relay/inbox/*' \
        --exclude='.relay/daemons/*.pid' \
        --exclude='.relay/totp_secret*' \
        $DIRS 2>/dev/null
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) BACKUP_COLLECT: Mac data sent" >> "$AUDIT_LOG"
    exit 0
fi

# All other commands require valid session
if [ ! -f "$SESSION_FILE" ]; then
    echo "AUTH_REQUIRED:no_session"
    exit 1
fi

# Check sliding window TTL (last_activity based)
LAST_ACTIVITY=$(cut -d: -f2 "$SESSION_FILE")
NOW=$(date +%s)
IDLE=$((NOW - LAST_ACTIVITY))

if [ "$IDLE" -gt "$SESSION_TTL" ]; then
    # Session expired — kill relay sessions and clean up
    KILLED=$(kill_relay_sessions)
    rm -f "$SESSION_FILE"
    echo "AUTH_REQUIRED:session_expired:idle_${IDLE}s:killed_${KILLED}_sessions"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) SESSION_EXPIRED idle=${IDLE}s killed=${KILLED}" >> "$AUDIT_LOG"
    exit 1
fi

# Reject shell metacharacters + newlines BEFORE whitelist check (prevent injection via eval)
LINES=$(echo "$CMD" | wc -l)
if [ "$LINES" -gt 1 ] || echo "$CMD" | grep -qE '[;|&$`><]|\$\('; then
    echo "BLOCKED:shell metacharacters detected"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) BLOCKED_INJECTION: $CMD" >> "$AUDIT_LOG"
    exit 1
fi

# Whitelist: only relay-* prefix commands with strict per-command validation.
# Generic tmux/claude/cat/ls/find/echo catch-all REMOVED (security hardening,
# FB-32 Apr 2026) — tmux new-session allows arbitrary binary execution.
# All remaining tmux usage is INTERNAL to guard.sh (kill_relay_sessions helper,
# relay-list-tmux command) — never via eval of user-supplied commands.
case "$CMD" in
    relay-status)
        # Show session status with idle time and TTL remaining
        # NOTE: NO heartbeat — this is a monitoring command (watchdog calls every ~2min).
        # Only user-initiated actions should refresh the sliding TTL.
        REMAINING=$((SESSION_TTL - IDLE))
        echo "SESSION_VALID:${REMAINING}s remaining:idle_${IDLE}s"
        # List relay sessions separately from other sessions
        RELAY_SESSIONS=$(tmux list-sessions -F '#{session_name}: #{session_windows} windows (created #{session_created_string})' 2>/dev/null | grep '^relay_' || true)
        OTHER_SESSIONS=$(tmux list-sessions -F '#{session_name}: #{session_windows} windows (created #{session_created_string})' 2>/dev/null | grep -v '^relay_' || true)
        if [ -n "$RELAY_SESSIONS" ]; then
            echo "RELAY_SESSIONS:"
            echo "$RELAY_SESSIONS"
        fi
        if [ -n "$OTHER_SESSIONS" ]; then
            echo "OTHER_SESSIONS:"
            echo "$OTHER_SESSIONS"
        fi
        if [ -z "$RELAY_SESSIONS" ] && [ -z "$OTHER_SESSIONS" ]; then
            echo "NO_TMUX_SESSIONS"
        fi
        ;;
    # NOTE: relay-sdk-query (ephemeral Claude invocation) removed —
    # all Claude relay traffic now uses the persistent daemon via
    # relay-sdk-persistent.
    relay-sdk-persistent\ *)
        # Spawn persistent daemon (one per chat) that stays alive across queries.
        # Args: <chat_id> <uuid> <cwd_b64> [label] [model_override] [effort_override]
        #   label: optional account-token label matching ^[a-z0-9_-]{1,32}$.
        #          When provided, exports oauth_token.<label> instead of
        #          the unlabeled default. Enables multi-account routing per
        #          relay chat without exposing token values to the bot.
        #   model_override: optional model name (e.g. "opus[1m]", "sonnet").
        #          "-" or empty = no override (auto-detect from JSONL).
        #   effort_override: optional effort level ("low"|"medium"|"high"|"xhigh").
        #          "-" or empty = no override (read from settings.json).
        # Stdin/stdout are forwarded directly (line-delimited JSON bot⇄daemon).
        PARGS="${CMD#relay-sdk-persistent }"
        PCHAT=$(echo "$PARGS" | cut -d' ' -f1)
        PUUID=$(echo "$PARGS" | cut -d' ' -f2)
        PCWD_B64=$(echo "$PARGS" | cut -d' ' -f3)
        PLABEL=$(echo "$PARGS" | cut -d' ' -f4)
        PMODEL=$(echo "$PARGS" | cut -d' ' -f5)
        PEFFORT=$(echo "$PARGS" | cut -d' ' -f6)
        if ! echo "$PCHAT" | grep -qE '^-?[0-9]+$'; then
            echo '{"type":"error","error":"invalid_chat_id"}'
            exit 1
        fi
        if [ "$PUUID" != "NEW" ] && ! echo "$PUUID" | grep -qE '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'; then
            echo '{"type":"error","error":"invalid_uuid_format"}'
            exit 1
        fi
        if ! echo "$PCWD_B64" | grep -qE '^[A-Za-z0-9+/=]+$'; then
            echo '{"type":"error","error":"invalid_cwd_b64"}'
            exit 1
        fi
        # Label is optional. Empty → default (oauth_token). Non-empty must
        # match strict regex to prevent path traversal / shell metachar use.
        if [ -n "$PLABEL" ] && ! echo "$PLABEL" | grep -qE '^[a-z0-9_-]{1,32}$'; then
            echo '{"type":"error","error":"invalid_account_label"}'
            exit 1
        fi
        # Model override: alphanumeric + dash/dot/brackets/underscore only
        # Use printf to avoid echo interpreting dash-prefixed values as flags
        if [ -n "$PMODEL" ] && [ "$PMODEL" != "-" ]; then
            if ! printf '%s\n' "$PMODEL" | grep -qE '^[][a-zA-Z0-9._-]{1,64}$'; then
                echo '{"type":"error","error":"invalid_model_override"}'
                exit 1
            fi
        fi
        # Effort override: strict enum
        if [ -n "$PEFFORT" ] && [ "$PEFFORT" != "-" ]; then
            if ! printf '%s\n' "$PEFFORT" | grep -qE '^(low|medium|high|xhigh)$'; then
                echo '{"type":"error","error":"invalid_effort_override"}'
                exit 1
            fi
        fi
        if [ ! -x "$HOME/.relay/sdk_persistent_daemon.py" ]; then
            echo '{"type":"error","error":"persistent_daemon_not_installed"}'
            exit 1
        fi
        update_heartbeat
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) SDK_PERSISTENT: chat=$PCHAT uuid=$PUUID label=${PLABEL:-default} model=${PMODEL:--} effort=${PEFFORT:--}" >> "$AUDIT_LOG"
        # v7 setup-token: export OAuth for Claude CLI inheritance (daemon spawns claude)
        # Multi-account: use oauth_token.<label> if label provided, else default file.
        # "-" is a sentinel for "no label" (bot passes it as positional arg placeholder).
        if [ -n "$PLABEL" ] && [ "$PLABEL" != "-" ]; then
            TOKEN_FILE="$HOME/.relay/oauth_token.$PLABEL"
        else
            # No label: try unlabeled default, then oauth_token.default (push_oauth_to_mac)
            TOKEN_FILE="$HOME/.relay/oauth_token"
            if [ ! -r "$TOKEN_FILE" ]; then
                TOKEN_FILE="$HOME/.relay/oauth_token.default"
            fi
        fi
        if [ -r "$TOKEN_FILE" ]; then
            TOKEN_VAL="$(cat "$TOKEN_FILE" 2>/dev/null | tr -d '[:space:]')"
            if [ -n "$TOKEN_VAL" ]; then
                export CLAUDE_CODE_OAUTH_TOKEN="$TOKEN_VAL"
            fi
            unset TOKEN_VAL
        fi
        unset TOKEN_FILE
        # Export model/effort overrides as env vars for daemon to read
        if [ -n "$PMODEL" ] && [ "$PMODEL" != "-" ]; then
            export RELAY_MODEL_OVERRIDE="$PMODEL"
        fi
        if [ -n "$PEFFORT" ] && [ "$PEFFORT" != "-" ]; then
            export RELAY_EFFORT_OVERRIDE="$PEFFORT"
        fi
        exec "$HOME/.relay/sdk_persistent_daemon.py" "$PCHAT" "$PUUID" "$PCWD_B64"
        ;;
    relay-ping-daemon\ *)
        # Liveness probe for a specific chat's daemon. Output:
        #   ALIVE:<pid>:<heartbeat_age_s>    (pid alive AND heartbeat fresh)
        #   STALE:<pid>:<heartbeat_age_s>    (pid alive but heartbeat > 60s)
        #   DEAD:<pid>                       (pidfile exists, pid gone)
        #   MISSING                          (no pidfile)
        PPCHAT="${CMD#relay-ping-daemon }"
        if ! echo "$PPCHAT" | grep -qE '^-?[0-9]+$'; then
            echo "ERROR:invalid_chat_id"
            exit 1
        fi
        # NOTE: NO heartbeat — this is a monitoring command (watchdog health sweep).
        PPIDF="$HOME/.relay/daemons/${PPCHAT}.pid"
        PSTATEF="$HOME/.relay/daemons/${PPCHAT}.state"
        if [ ! -f "$PPIDF" ]; then
            echo "MISSING"
            exit 0
        fi
        PPID_VAL="$(cat "$PPIDF" 2>/dev/null | tr -d '[:space:]')"
        if [ -z "$PPID_VAL" ] || ! kill -0 "$PPID_VAL" 2>/dev/null; then
            echo "DEAD:$PPID_VAL"
            exit 0
        fi
        # heartbeat freshness from state file
        HB_AGE="unknown"
        if [ -f "$PSTATEF" ]; then
            HB_EPOCH=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(int(d.get('last_heartbeat',0)))" "$PSTATEF" 2>/dev/null || echo 0)
            if [ "$HB_EPOCH" -gt 0 ]; then
                NOW_EPOCH=$(date +%s)
                HB_AGE=$((NOW_EPOCH - HB_EPOCH))
            fi
        fi
        if [ "$HB_AGE" != "unknown" ] && [ "$HB_AGE" -gt 60 ]; then
            echo "STALE:$PPID_VAL:${HB_AGE}s"
        else
            echo "ALIVE:$PPID_VAL:${HB_AGE}s"
        fi
        ;;
    relay-heartbeat)
        # Lightweight TOTP session refresh. Called by the bot on each relay
        # query to prevent session expiry while daemons are actively handling
        # queries (daemon runs via exec, so no new guard.sh invocation = no
        # automatic heartbeat). User activity in relay chats keeps the session alive.
        update_heartbeat
        REMAINING=$((SESSION_TTL - IDLE))
        echo "HEARTBEAT_OK:${REMAINING}s"
        ;;
    relay-list-daemons)
        # Dump all daemon pidfiles with status. Format per line:
        #   <chat_id>  <pid>  <status>  <heartbeat_age>  <uuid>
        # NOTE: NO heartbeat — this is a monitoring command (watchdog health sweep).
        DDIR="$HOME/.relay/daemons"
        if [ ! -d "$DDIR" ]; then
            echo "NO_DAEMONS_DIR"
            exit 0
        fi
        NOW_EPOCH=$(date +%s)
        FOUND=0
        for pidf in "$DDIR"/*.pid; do
            [ -f "$pidf" ] || continue
            FOUND=1
            base=$(basename "$pidf" .pid)
            pv=$(cat "$pidf" 2>/dev/null | tr -d '[:space:]')
            if [ -n "$pv" ] && kill -0 "$pv" 2>/dev/null; then
                st="ALIVE"
            else
                st="DEAD"
            fi
            sf="$DDIR/${base}.state"
            age="?"
            uuid="?"
            model="-"
            effort="-"
            if [ -f "$sf" ]; then
                # Single python3 call extracts all fields from state JSON
                _state=$(python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
print(int(d.get('last_heartbeat',0)))
print(d.get('uuid','?'))
print(d.get('model') or '-')
print(d.get('effort') or '-')
" "$sf" 2>/dev/null)
                if [ -n "$_state" ]; then
                    hb=$(echo "$_state" | sed -n '1p')
                    uu=$(echo "$_state" | sed -n '2p')
                    ml=$(echo "$_state" | sed -n '3p')
                    ef=$(echo "$_state" | sed -n '4p')
                    [ "$hb" -gt 0 ] 2>/dev/null && age=$((NOW_EPOCH - hb))s
                    [ -n "$uu" ] && uuid="$uu"
                    [ -n "$ml" ] && model="$ml"
                    [ -n "$ef" ] && effort="$ef"
                fi
            fi
            echo "$base $pv $st $age $uuid $model $effort"
        done
        [ "$FOUND" -eq 0 ] && echo "NO_DAEMONS"
        exit 0
        ;;
    relay-kill-daemon\ *)
        # Explicit TERM→KILL of a specific chat's daemon.
        PKCHAT="${CMD#relay-kill-daemon }"
        if ! echo "$PKCHAT" | grep -qE '^-?[0-9]+$'; then
            echo "ERROR:invalid_chat_id"
            exit 1
        fi
        PKF="$HOME/.relay/daemons/${PKCHAT}.pid"
        if [ ! -f "$PKF" ]; then
            echo "NO_PIDFILE"
            update_heartbeat
            exit 0
        fi
        KPID=$(cat "$PKF" 2>/dev/null | tr -d '[:space:]')
        if [ -z "$KPID" ] || ! kill -0 "$KPID" 2>/dev/null; then
            rm -f "$PKF" "$HOME/.relay/daemons/${PKCHAT}.state"
            echo "ALREADY_DEAD"
            update_heartbeat
            exit 0
        fi
        kill -TERM "$KPID" 2>/dev/null || true
        sleep 1
        if kill -0 "$KPID" 2>/dev/null; then
            kill -KILL "$KPID" 2>/dev/null || true
            sleep 1
        fi
        rm -f "$PKF" "$HOME/.relay/daemons/${PKCHAT}.state"
        echo "KILLED:$KPID"
        update_heartbeat
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) RELAY_KILL_DAEMON: chat=$PKCHAT pid=$KPID" >> "$AUDIT_LOG"
        ;;
    relay-list-jsonls*)
        # List JSONL session files under ~/.claude/projects/.
        # With arg (base64-encoded project path): list for that project only.
        # Without arg: list across ALL projects.
        # Returns `ls -lt` output (sorted by mtime, newest first).
        # FB-32: replaces generic `ls */.claude/*` catch-all.
        update_heartbeat
        JARG="${CMD#relay-list-jsonls}"
        JARG="${JARG# }"  # strip leading space if present
        if [ -n "$JARG" ]; then
            # Per-project listing: decode base64 project dir name
            if ! echo "$JARG" | grep -qE '^[A-Za-z0-9+/=_-]+$'; then
                echo "ERROR:invalid_project_arg"
                exit 1
            fi
            # The arg is the encoded project dir (e.g. "-Users-username-dev-project")
            # Validate: no shell metacharacters, only alnum/dash/underscore/dot
            if ! echo "$JARG" | grep -qE '^[-a-zA-Z0-9_.]+$'; then
                echo "ERROR:invalid_encoded_dir"
                exit 1
            fi
            ls -lt "$HOME/.claude/projects/$JARG/" 2>/dev/null || echo "(empty)"
        else
            # All projects: glob expand
            ls -lt "$HOME"/.claude/projects/*/*.jsonl 2>/dev/null || echo "(empty)"
        fi
        ;;
    relay-read-jsonl-header\ *)
        # Read the first N bytes of a JSONL to extract cwd (session working dir).
        # Returns raw text (NOT base64) — first 16KB of the file.
        # FB-32: replaces generic `cat */.claude/*` catch-all.
        JPATH="${CMD#relay-read-jsonl-header }"
        # Validate: must be under ~/.claude/projects/, no metacharacters
        if ! echo "$JPATH" | grep -qE '^/[a-zA-Z0-9_./ -]+$'; then
            echo "ERROR:invalid_path"
            exit 1
        fi
        if [[ "$JPATH" == *..* ]]; then
            echo "ERROR:path_traversal"
            exit 1
        fi
        # Must be under ~/.claude/projects/ and end in .jsonl
        case "$JPATH" in
            "$HOME"/.claude/projects/*.jsonl) ;;
            *) echo "ERROR:path_not_allowed"; exit 1 ;;
        esac
        if [ ! -f "$JPATH" ]; then
            echo "ERROR:file_not_found"
            exit 1
        fi
        # Return first 16KB only (cwd is in the first few lines)
        head -c 16384 "$JPATH"
        # No heartbeat — lightweight read, doesn't imply user activity
        ;;
    relay-list-tmux)
        # List tmux sessions — advisory monitoring, NO heartbeat.
        # Runs tmux internally (hardcoded command, no user input).
        # Used by watchdog for stray session detection (FB-32).
        tmux list-sessions -F '#{session_name}' 2>/dev/null || true
        ;;
    relay-reap-orphans)
        # Sweep: for every pidfile where PID is dead, remove pidfile+state.
        # NOTE: NO heartbeat — this is a monitoring/cleanup command.
        DDIR="$HOME/.relay/daemons"
        [ -d "$DDIR" ] || { echo "NO_DAEMONS_DIR"; exit 0; }
        REAPED=0
        for pidf in "$DDIR"/*.pid; do
            [ -f "$pidf" ] || continue
            base=$(basename "$pidf" .pid)
            pv=$(cat "$pidf" 2>/dev/null | tr -d '[:space:]')
            if [ -z "$pv" ] || ! kill -0 "$pv" 2>/dev/null; then
                rm -f "$pidf" "$DDIR/${base}.state"
                REAPED=$((REAPED + 1))
            fi
        done
        echo "REAPED:$REAPED"
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) RELAY_REAP: reaped=$REAPED" >> "$AUDIT_LOG"
        ;;
    relay-install-skills)
        # Stage skills/agents/CLAUDE.md for Mac-side approval.
        # Input: base64-encoded tar.gz on stdin.
        # Validates payload (path traversal, structure) then writes to
        # pending dir — requires Mac-side approve-install.sh to go live.
        # Re-entrant: new push atomically replaces previous pending.
        PENDING_DIR="$HOME/.relay/pending-installs/skills"
        # Re-entrant: clear previous pending for this type
        rm -rf "$PENDING_DIR"
        mkdir -p "$PENDING_DIR"
        # Decode to temp archive first for path validation (FB-32 hardening)
        TMPTAR="$PENDING_DIR/.archive.tar.gz"
        if ! base64 -d > "$TMPTAR" 2>/dev/null; then
            rm -rf "$PENDING_DIR"
            echo "ERROR:decode_failed"
            exit 1
        fi
        # Reject tar entries with path traversal or absolute paths
        if tar -tzf "$TMPTAR" 2>/dev/null | grep -qE '(^/|\.\.)'; then
            rm -rf "$PENDING_DIR"
            echo "ERROR:tar_path_traversal_rejected"
            echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) INSTALL_SKILLS_BLOCKED: path traversal in tar" >> "$AUDIT_LOG"
            exit 1
        fi
        if ! tar -xzf "$TMPTAR" -C "$PENDING_DIR" 2>/dev/null; then
            rm -rf "$PENDING_DIR"
            echo "ERROR:extract_failed"
            exit 1
        fi
        # Validate expected structure
        if [ ! -d "$PENDING_DIR/skills/coding-principles" ] || [ ! -d "$PENDING_DIR/agents" ]; then
            rm -rf "$PENDING_DIR"
            echo "ERROR:missing_expected_dirs"
            exit 1
        fi
        # Compute SHA256 of tar for TOCTOU verification by approve script
        SKILLS_SHA=$(shasum -a 256 "$TMPTAR" 2>/dev/null | cut -d' ' -f1 || echo "unknown")
        rm -f "$TMPTAR"
        # Write metadata for approve-install.sh
        echo "$SKILLS_SHA" > "$PENDING_DIR/.sha256"
        date +%s > "$PENDING_DIR/.timestamp"
        echo "PENDING_APPROVAL:skills:sha256=${SKILLS_SHA}"
        update_heartbeat
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) RELAY_INSTALL_SKILLS_PENDING: sha256=$SKILLS_SHA" >> "$AUDIT_LOG"
        ;;
    relay-install-daemon)
        # Stage daemon for Mac-side approval. Validates payload (size, shebang)
        # then writes to pending dir — requires approve-install.sh to go live.
        # Re-entrant: new push atomically replaces previous pending.
        PENDING_DIR="$HOME/.relay/pending-installs/daemon"
        # Re-entrant: clear previous pending for this type
        rm -rf "$PENDING_DIR"
        mkdir -p "$PENDING_DIR"
        DEST="$PENDING_DIR/sdk_persistent_daemon.py"
        TMP="$PENDING_DIR/.daemon.tmp"
        # Decode stdin → tmp file
        if ! base64 -d > "$TMP" 2>/dev/null; then
            rm -rf "$PENDING_DIR"
            echo "ERROR:base64_decode_failed"
            exit 1
        fi
        DSIZE=$(stat -f%z "$TMP" 2>/dev/null || stat -c%s "$TMP" 2>/dev/null || echo 0)
        if [ "$DSIZE" -eq 0 ]; then
            rm -rf "$PENDING_DIR"
            echo "ERROR:empty_payload"
            exit 1
        fi
        # 2 MB cap — daemon is ~60 KB today, huge headroom
        if [ "$DSIZE" -gt 2097152 ]; then
            rm -rf "$PENDING_DIR"
            echo "ERROR:daemon_too_large:${DSIZE}_bytes"
            exit 1
        fi
        # Shebang sanity: must start with `#!` and contain `python`
        FIRST=$(head -n 1 "$TMP")
        case "$FIRST" in
            \#!*python*) : ;;
            *)
                rm -rf "$PENDING_DIR"
                echo "ERROR:shebang_missing_or_not_python"
                exit 1
                ;;
        esac
        # chmod BEFORE rename so pending file has correct perms
        chmod 0755 "$TMP"
        DAEMON_SHA=$(shasum -a 256 "$TMP" 2>/dev/null | cut -d' ' -f1 || echo "unknown")
        mv "$TMP" "$DEST"
        # Write metadata for approve-install.sh
        echo "$DAEMON_SHA" > "$PENDING_DIR/.sha256"
        date +%s > "$PENDING_DIR/.timestamp"
        echo "PENDING_APPROVAL:daemon:${DSIZE}_bytes:sha256=${DAEMON_SHA}"
        update_heartbeat
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) RELAY_INSTALL_DAEMON_PENDING: size=$DSIZE sha256=$DAEMON_SHA" >> "$AUDIT_LOG"
        ;;
    relay-install-status)
        # Check pending install status. Session-required (TOTP).
        # Output: PENDING:<type>:<sha256>:<age_s> per pending item, or NONE.
        # Read-only — does not modify anything.
        PIDIR="$HOME/.relay/pending-installs"
        FOUND=0
        for ptype in daemon skills; do
            PDIR="$PIDIR/$ptype"
            if [ -d "$PDIR" ] && [ -f "$PDIR/.sha256" ]; then
                PSHA=$(cat "$PDIR/.sha256" 2>/dev/null || echo "unknown")
                PTS=$(cat "$PDIR/.timestamp" 2>/dev/null || echo 0)
                if [ "$PTS" -gt 0 ] 2>/dev/null; then
                    PNOW=$(date +%s)
                    PAGE=$((PNOW - PTS))
                else
                    PAGE="unknown"
                fi
                echo "PENDING:$ptype:$PSHA:${PAGE}s"
                FOUND=1
            fi
        done
        if [ "$FOUND" -eq 0 ]; then
            echo "NONE"
        fi
        # No heartbeat — monitoring command
        ;;
    relay-push-file\ *)
        # Push a file from bot → Mac inbox. Content arrives as base64 on stdin.
        # Args: <sha256_hex> <ext>
        # Output: absolute Mac path on success, ERROR:<reason> on failure.
        # Inbox: ~/.relay/inbox/<sha>.<ext> — content-addressed, idempotent.
        ARGS="${CMD#relay-push-file }"
        PSHA=$(echo "$ARGS" | cut -d' ' -f1)
        PEXT=$(echo "$ARGS" | cut -d' ' -f2)
        # Strict hex-64 sha, no shell metacharacters possible
        if ! echo "$PSHA" | grep -qE '^[0-9a-f]{64}$'; then
            echo "ERROR:invalid_sha256"
            exit 1
        fi
        # Extension allowlist — kept in sync with push_file() in desktop_relay.py
        case "$PEXT" in
            jpg|jpeg|png|gif|webp|heic|mp4|mov|webm|pdf|ogg|m4a|mp3|txt|md|json|html|css|js|docx|xlsx|csv|pptx|zip|7z|tar|gz|rar) ;;
            *) echo "ERROR:ext_not_allowed:${PEXT}"; exit 1 ;;
        esac
        PINBOX="$HOME/.relay/inbox"
        mkdir -p "$PINBOX" && chmod 700 "$PINBOX"
        # Opportunistic cleanup — delete inbox files older than 7 days on each
        # push. No separate cron/launchd needed. `find -maxdepth 1` limits
        # blast radius to the inbox itself; errors suppressed so cleanup never
        # blocks the push path.
        find "$PINBOX" -maxdepth 1 -type f -mtime +7 -delete 2>/dev/null || true
        PTARGET="$PINBOX/${PSHA}.${PEXT}"
        # Decode stdin → tmp file (base64 -d works on both macOS BigSur+ and Linux)
        if ! base64 -d > "${PTARGET}.tmp" 2>/dev/null; then
            rm -f "${PTARGET}.tmp"
            echo "ERROR:base64_decode_failed"
            exit 1
        fi
        PFSIZE=$(stat -f%z "${PTARGET}.tmp" 2>/dev/null || stat -c%s "${PTARGET}.tmp" 2>/dev/null || echo 0)
        if [ "$PFSIZE" -gt 52428800 ]; then
            rm -f "${PTARGET}.tmp"
            echo "ERROR:file_too_large:${PFSIZE}_bytes"
            exit 1
        fi
        if [ "$PFSIZE" -eq 0 ]; then
            rm -f "${PTARGET}.tmp"
            echo "ERROR:empty_payload"
            exit 1
        fi
        # chmod BEFORE rename so the final file never exists with default
        # perms (closes the microsecond window between mv and post-chmod).
        chmod 600 "${PTARGET}.tmp"
        mv "${PTARGET}.tmp" "$PTARGET"
        echo "$PTARGET"
        update_heartbeat
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) RELAY_PUSH: sha=${PSHA:0:12} ext=$PEXT size=$PFSIZE" >> "$AUDIT_LOG"
        ;;
    relay-fetch\ *)
        # Fetch a file as base64 for binary-safe transfer to bot server
        FILEPATH="${CMD#relay-fetch }"
        # Validate path: no shell metacharacters, must be absolute
        if ! echo "$FILEPATH" | grep -qE '^/[a-zA-Z0-9_./ ~-]+$'; then
            echo "ERROR:invalid_path"
            exit 1
        fi
        # Reject path traversal — the allow-regex above permits `.` so
        # `/Users/username/../../etc/passwd` would otherwise slip through.
        # Any `..` sequence is rejected outright. The Mac-side daemon
        # holds the user's full auth, so exfiltration via a bot-emitted
        # `[RELAY_FILE: ...]` tag must be blocked structurally here.
        if [[ "$FILEPATH" == *..* ]]; then
            echo "ERROR:path_traversal_rejected"
            exit 1
        fi
        # Resolve symlinks and recheck — prevents TOCTOU via symlink swap.
        # macOS `realpath` resolves all symlinks (like `readlink -f` on Linux).
        RESOLVED=$(realpath "$FILEPATH" 2>/dev/null || echo "")
        if [ -z "$RESOLVED" ]; then
            echo "ERROR:file_not_found"
            exit 1
        fi
        # Path allowlist — restrict to known directories (FB-32 hardening).
        # Only project dirs, Claude data, relay inbox/outbox, and tmp are allowed.
        case "$RESOLVED" in
            "$HOME"/dev/*|"$HOME"/.claude/*|"$HOME"/.relay/inbox/*|"$HOME"/.relay/outbox/*|/tmp/*|/private/tmp/*)
                ;;
            *)
                echo "ERROR:path_not_allowed"
                echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) FETCH_BLOCKED: resolved=$RESOLVED original=$FILEPATH" >> "$AUDIT_LOG"
                exit 1
                ;;
        esac
        if [ ! -f "$RESOLVED" ]; then
            echo "ERROR:file_not_found"
            exit 1
        fi
        # Size cap: 50MB max
        FSIZE=$(stat -f%z "$RESOLVED" 2>/dev/null || stat -c%s "$RESOLVED" 2>/dev/null || echo 0)
        if [ "$FSIZE" -gt 52428800 ]; then
            echo "ERROR:file_too_large:${FSIZE}_bytes"
            exit 1
        fi
        # macOS base64 requires -i for input file; Linux would also accept
        # `base64 FILE` but `base64 -i FILE` is the cross-platform form.
        base64 -i "$RESOLVED"
        update_heartbeat
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) RELAY_FETCH: path=$RESOLVED size=$FSIZE" >> "$AUDIT_LOG"
        ;;
    relay-lock)
        # Immediate session revocation + kill relay sessions
        KILLED=$(kill_relay_sessions)
        rm -f "$SESSION_FILE"
        echo "LOCKED:session revoked:killed_${KILLED}_relay_sessions"
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) MANUAL_LOCK killed=${KILLED}" >> "$AUDIT_LOG"
        ;;
    relay-claude-login)
        # Launch `claude /login` remotely, capture OAuth URL+port to stdout.
        # Helper daemon stays alive 5min to receive callback via SSH tunnel.
        # Output: https://...|PORT|HELPER_PID   on success
        #         ERROR:<reason>                on failure
        if [ ! -x "$HOME/.relay/claude_login.sh" ]; then
            echo "ERROR:launcher_not_installed"
            exit 1
        fi
        update_heartbeat
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) CLAUDE_LOGIN_START" >> "$AUDIT_LOG"
        exec "$HOME/.relay/claude_login.sh"
        ;;
    relay-claude-login-paste)
        # v7 setup-token: deliver the OAuth code from bot → helper.
        # Code is read from STDIN (never argv — avoids shell quoting / argv
        # length / history leakage). Helper polls claude-login.code and
        # types the code into setup-token's stdin.
        read -r CODE || true
        # Strip whitespace/CR the bot may have appended
        CODE="$(echo "$CODE" | tr -d '[:space:]')"
        if [ -z "$CODE" ]; then
            echo "ERROR:empty_code"
            exit 1
        fi
        # Strict alphabet: letters, digits, underscore, hyphen, hash.
        # Anthropic PKCE codes observed as `sessionKey#uuid` — hash allowed.
        if ! echo "$CODE" | grep -qE '^[A-Za-z0-9_#-]+$'; then
            echo "ERROR:invalid_code_format"
            exit 1
        fi
        # Length sanity (defensive)
        if [ "${#CODE}" -gt 512 ]; then
            echo "ERROR:code_too_long"
            exit 1
        fi
        # Atomic write, 0600 — helper picks up the file and deletes it.
        CODE_FILE_PATH="$HOME/.relay/claude-login.code"
        umask 077
        echo "$CODE" > "${CODE_FILE_PATH}.tmp" && mv "${CODE_FILE_PATH}.tmp" "$CODE_FILE_PATH"
        echo "CODE_ACCEPTED"
        update_heartbeat
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) CLAUDE_LOGIN_PASTE len=${#CODE}" >> "$AUDIT_LOG"
        ;;
    relay-claude-login-status)
        # Read current state of an in-flight claude /login flow.
        # Useful to check whether the background helper succeeded after the
        # bot-side port-forward replay.
        STATE_FILE="$HOME/.relay/claude-login.state"
        if [ ! -f "$STATE_FILE" ]; then
            echo "NO_STATE"
            exit 0
        fi
        # Safe to cat — file is written by our helper, no user-controlled content
        cat "$STATE_FILE"
        update_heartbeat
        ;;
    relay-promote-token\ *)
        # Multi-account: after a successful setup-token run writes
        # ~/.relay/oauth_token, rename it to oauth_token.<label> so the
        # default slot is free for future re-auths. Atomic mv — never leaves
        # the file partially renamed.
        # Arg: <label> matching ^[a-z0-9_-]{1,32}$
        PL="${CMD#relay-promote-token }"
        if ! echo "$PL" | grep -qE '^[a-z0-9_-]{1,32}$'; then
            echo "ERROR:invalid_label"
            exit 1
        fi
        SRC="$HOME/.relay/oauth_token"
        DST="$HOME/.relay/oauth_token.$PL"
        if [ ! -f "$SRC" ]; then
            echo "ERROR:no_source_token"
            exit 1
        fi
        # Atomic on same filesystem (~/.relay). 0600 preserved.
        if mv "$SRC" "$DST" 2>/dev/null; then
            echo "PROMOTED:$PL"
            update_heartbeat
            echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) PROMOTE_TOKEN: label=$PL" >> "$AUDIT_LOG"
        else
            echo "ERROR:mv_failed"
            exit 1
        fi
        ;;
    relay-list-accounts)
        # List known account labels (by filename only, not values).
        DIR="$HOME/.relay"
        LABELS=""
        [ -f "$DIR/oauth_token" ] && LABELS="default"
        for f in "$DIR"/oauth_token.*; do
            [ -f "$f" ] || continue
            bn=$(basename "$f")
            lbl="${bn#oauth_token.}"
            # Safety: only emit labels matching regex (defensive)
            if echo "$lbl" | grep -qE '^[a-z0-9_-]{1,32}$'; then
                if [ -n "$LABELS" ]; then
                    LABELS="$LABELS $lbl"
                else
                    LABELS="$lbl"
                fi
            fi
        done
        if [ -n "$LABELS" ]; then
            echo "LABELS:$LABELS"
        else
            echo "NO_LABELS"
        fi
        update_heartbeat
        ;;
    relay-push-token\ *)
        # Push an OAuth token directly for a named account label.
        # Arg: <label> matching ^[a-z0-9_-]{1,32}$
        # Token value arrives as base64 on stdin (same pattern as relay-push-file).
        # Writes to ~/.relay/oauth_token.<label> with 0600 perms.
        PTL="${CMD#relay-push-token }"
        if ! echo "$PTL" | grep -qE '^[a-z0-9_-]{1,32}$'; then
            echo "ERROR:invalid_label"
            exit 1
        fi
        PTDST="$HOME/.relay/oauth_token.$PTL"
        # Decode token from base64 stdin
        PTVAL="$(base64 -d 2>/dev/null || true)"
        if [ -z "$PTVAL" ]; then
            echo "ERROR:empty_payload"
            exit 1
        fi
        # Guard-side format validation (FB-32 hardening — defense-in-depth,
        # bot-side also validates). Reject anything that doesn't look like
        # an Anthropic OAuth token.
        case "$PTVAL" in
            sk-ant-*) ;;
            *) echo "ERROR:invalid_token_format"
               echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) PUSH_TOKEN_BLOCKED: invalid format for label=$PTL" >> "$AUDIT_LOG"
               exit 1 ;;
        esac
        # Length sanity: OAuth tokens are typically 100-300 chars
        if [ "${#PTVAL}" -gt 1024 ]; then
            echo "ERROR:token_too_long"
            exit 1
        fi
        # Write atomically via tmp file
        echo -n "$PTVAL" > "${PTDST}.tmp"
        chmod 600 "${PTDST}.tmp"
        mv "${PTDST}.tmp" "$PTDST"
        echo "PUSHED:$PTL"
        update_heartbeat
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) PUSH_TOKEN: label=$PTL" >> "$AUDIT_LOG"
        ;;
    relay-claude-login-kill)
        # Abort an in-flight claude /login helper (cleanup path).
        PID_FILE="$HOME/.relay/claude-login.pid"
        if [ -f "$PID_FILE" ]; then
            PID="$(cat "$PID_FILE" 2>/dev/null || true)"
            if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
                kill -TERM "$PID" 2>/dev/null || true
                sleep 0.3
                kill -KILL "$PID" 2>/dev/null || true
                echo "KILLED:$PID"
            else
                echo "NOT_RUNNING"
            fi
            rm -f "$PID_FILE"
        else
            echo "NO_PID_FILE"
        fi
        update_heartbeat
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) CLAUDE_LOGIN_KILL" >> "$AUDIT_LOG"
        ;;
    *)
        echo "BLOCKED:command not whitelisted"
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) BLOCKED: $CMD" >> "$AUDIT_LOG"
        exit 1
        ;;
esac
