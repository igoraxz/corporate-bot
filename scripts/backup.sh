#!/usr/bin/env bash
# ============================================================================
# Corporate Bot — Encrypted Backup (hourly cron, 2h effective cadence)
# ============================================================================
# Creates a single AES-256 encrypted 7z archive of ALL bot data.
# Runs hourly via cron on the HOST machine; self-throttles to 2h (BACKUP_INTERVAL_S).
#
# What's backed up (everything for full restore):
#   - SQLite databases (WAL-safe VACUUM INTO snapshots)
#   - Telegram MTProto session, WhatsApp device keys
#   - Google OAuth tokens (all accounts), MS365 auth, Claude OAuth profiles
#   - Claude SDK sessions + JSONL history
#   - Media cache, Camofox browser profiles + browser-native data
#   - All JSON configs (harnesses, schedules, external chats, etc.)
#   - .env + .env.staging (all API keys and secrets)
#   - docker-compose.override.yml, deploy dir, prompts, harness projects
#   - Staging data (optional, --with-staging)
#   - Mac-side relay/Claude data (optional, --with-mac)
#   - Recent logs (last 2 rotations)
#
# Naming:  bot-backup-YYYY-MM-DD-HH00.7z
# Encryption: 7z native AES-256 (-mhe=on encrypts filenames too)
# Password: .backup-password file (mode 600). NEVER use env var.
#
# Destinations (automatic, symmetric retention):
#   1. Mac via SSH guard.sh over Tailscale
#      Retention: 12 x 2-hourly (last 24h) + 7 daily + 4 weekly + monthly forever
#      Uses relay-backup-put/prune/collect commands through guard.sh
#      (TOTP-exempt — runs unattended from cron)
#   2. Google Drive via rclone OAuth (drive.file scope)
#      Retention: 12 x 2-hourly (last 24h) + 7 daily + 4 weekly + monthly forever
#
# Usage:
#   ./scripts/backup.sh                    # full backup + upload
#   ./scripts/backup.sh --with-staging     # include staging volumes
#   ./scripts/backup.sh --with-mac         # include Mac-side data (~/.relay, ~/.claude)
#   ./scripts/backup.sh --local-only       # skip Mac/GDrive upload
#   ./scripts/backup.sh --dry-run          # show what would be backed up
#
# SECURITY: Install this script outside the repo for isolation (same as deploy watcher):
#   sudo mkdir -p /opt/corporate-bot-backup
#   sudo cp scripts/backup.sh /opt/corporate-bot-backup/backup.sh
#   sudo chown -R botuser:botuser /opt/corporate-bot-backup && chmod 755 /opt/corporate-bot-backup/backup.sh
#   The bot's path blocklist prevents writes to /opt/corporate-bot-backup/ — see docs/HOST_SECURITY.md
#
# Cron (hourly, as botuser — self-throttles to BACKUP_INTERVAL_S, default 2h):
#   0 * * * * /opt/corporate-bot-backup/backup.sh >> /home/botuser/corporate-bot/deploy/backup.log 2>&1
#
# Restore:
#   ./scripts/restore.sh <backup-file.7z>
#   ./scripts/restore.sh --from-mac
# ============================================================================
set -uo pipefail
# NOTE: -e is NOT set — we handle errors explicitly to provide good diagnostics

# --- Configuration (override via env vars) ---
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"

# Auto-load BACKUP_* vars from .env when running outside Docker (e.g. cron on host).
# Only exports BACKUP_-prefixed lines; safe to run alongside Docker which already has them.
if [ -f "${REPO_DIR}/.env" ]; then
    while IFS= read -r _eline; do
        case "$_eline" in BACKUP_*) export "$_eline" 2>/dev/null || true ;; esac
    done < <(grep -E '^BACKUP_[A-Z_]+=.' "${REPO_DIR}/.env" 2>/dev/null)
    unset _eline
fi
COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-corporate-bot}"
BOT_CONTAINER="${COMPOSE_PROJECT}"
WA_CONTAINER="${COMPOSE_PROJECT}-wa-bridge"
CAMOFOX_CONTAINER="${COMPOSE_PROJECT}-camofox"

# Backup destinations
MAC_USER="${BACKUP_MAC_USER:-your-mac-user}"
MAC_HOST="${BACKUP_MAC_HOST:-100.x.y.z}"
MAC_SSH_KEY="${BACKUP_MAC_SSH_KEY:-$HOME/.ssh/id_ed25519_bot}"
RCLONE_CONF="${BACKUP_RCLONE_CONF:-$HOME/.config/rclone/rclone.conf}"
# GDrive uses OAuth (drive.file scope) via user's rclone.conf [gdrive] remote.
# No service account needed — personal Drive has no SA quota.

# Encryption password file (NEVER use env var — visible in /proc)
PASSWORD_FILE="${REPO_DIR}/.backup-password"

# Naming
TIMESTAMP=$(date -u +%Y-%m-%d-%H00)
BACKUP_NAME="bot-backup-${TIMESTAMP}.7z"

# Ensure dirs created with restrictive perms (eliminates TOCTOU race on mktemp)
umask 077

# Use /dev/shm (RAM-backed) if available, else /tmp
if [ -d /dev/shm ] && [ -w /dev/shm ]; then
    WORK_DIR=$(mktemp -d "/dev/shm/bot-backup-XXXXXX")
else
    WORK_DIR=$(mktemp -d "/tmp/bot-backup-XXXXXX")
fi
STAGING="$WORK_DIR/data"

# Flags
LOCAL_ONLY=false
DRY_RUN=false
WITH_STAGING=false
WITH_MAC=false
for arg in "$@"; do
    case "$arg" in
        --local-only)     LOCAL_ONLY=true ;;
        --dry-run)        DRY_RUN=true ;;
        --with-staging)   WITH_STAGING=true ;;
        --with-mac)       WITH_MAC=true ;;
    esac
done

cleanup() {
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT INT TERM HUP

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

get_password() {
    if [ ! -f "$PASSWORD_FILE" ]; then
        echo "ERROR: No backup password file at ${PASSWORD_FILE}" >&2
        echo "  Create one: openssl rand -base64 24 > ${PASSWORD_FILE} && chmod 600 ${PASSWORD_FILE}" >&2
        exit 1
    fi
    cat "$PASSWORD_FILE"
}

ssh_opts() {
    local opts="-o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=yes"
    [ -n "$MAC_SSH_KEY" ] && opts="$opts -i $MAC_SSH_KEY"
    echo "$opts"
}

# prune_tiered <dir>: 4-tier retention on bot-backup-*.7z in <dir> (Linux date)
# Tier 1: keep 12 most recent (2-hourly window, last 24h)
# Tier 2: keep newest per calendar day, within last 7 days
# Tier 3: keep newest per ISO week, within last 28 days
# Tier 4: keep newest per month, forever (no expiry)
prune_tiered() {
    local DIR="$1"
    [ -d "$DIR" ] || return 0
    local ALL KEEP_FILE SEEN NOW_S DELETED f D M WK TS AGE_D KEEP
    ALL=$(ls -1t "$DIR"/bot-backup-*.7z 2>/dev/null) || return 0
    [ -z "$ALL" ] && return 0
    KEEP_FILE=$(mktemp)
    NOW_S=$(date +%s)
    # Tier 1: 12 most recent (last 24h at 2h cadence)
    echo "$ALL" | head -12 >> "$KEEP_FILE"
    # Tier 2: newest per day, last 7 days
    SEEN=$(mktemp)
    while IFS= read -r f; do
        D=$(basename "$f" | sed 's/bot-backup-\([0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}\).*/\1/')
        TS=$(date -d "$D" +%s 2>/dev/null || echo 0)
        AGE_D=$(( (NOW_S - TS) / 86400 ))
        [ "$AGE_D" -gt 7 ] && continue
        grep -qxF "$D" "$SEEN" && continue
        echo "$D" >> "$SEEN"; echo "$f" >> "$KEEP_FILE"
    done <<< "$ALL"
    rm -f "$SEEN"
    # Tier 3: newest per ISO week, last 28 days
    SEEN=$(mktemp)
    while IFS= read -r f; do
        D=$(basename "$f" | sed 's/bot-backup-\([0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}\).*/\1/')
        TS=$(date -d "$D" +%s 2>/dev/null || echo 0)
        AGE_D=$(( (NOW_S - TS) / 86400 ))
        [ "$AGE_D" -gt 28 ] && continue
        WK=$(date -d "$D" +%G-W%V 2>/dev/null || echo "unk")
        grep -qxF "$WK" "$SEEN" && continue
        echo "$WK" >> "$SEEN"; echo "$f" >> "$KEEP_FILE"
    done <<< "$ALL"
    rm -f "$SEEN"
    # Tier 4: newest per month, forever
    SEEN=$(mktemp)
    while IFS= read -r f; do
        M=$(basename "$f" | sed 's/bot-backup-\([0-9]\{4\}-[0-9]\{2\}\).*/\1/')
        grep -qxF "$M" "$SEEN" && continue
        echo "$M" >> "$SEEN"; echo "$f" >> "$KEEP_FILE"
    done <<< "$ALL"
    rm -f "$SEEN"
    KEEP=$(sort -u "$KEEP_FILE"); rm -f "$KEEP_FILE"
    DELETED=0
    while IFS= read -r f; do
        echo "$KEEP" | grep -qxF "$f" || { rm -f "$f" && DELETED=$((DELETED + 1)); }
    done <<< "$ALL"
    local REMAINING
    REMAINING=$(ls -1 "$DIR"/bot-backup-*.7z 2>/dev/null | wc -l | tr -d ' ')
    log "  Tiered prune: ${REMAINING} remaining, ${DELETED} deleted (2h/daily/weekly/monthly)"
}

# gdrive_prune_tiered_oauth: 4-tier retention on GDrive using user's rclone.conf
# Symmetric with Mac: 2h/24h, daily/7d, weekly/28d, monthly/forever
gdrive_prune_tiered_oauth() {
    local ALL KEEP_FILE SEEN NOW_S DELETED f D M WK TS AGE_D KEEP
    ALL=$(RCLONE_CONFIG="$RCLONE_CONF" rclone lsf gdrive: --include 'bot-backup-*.7z' -q 2>/dev/null \
          | sort -r) || return 0
    [ -z "$ALL" ] && return 0
    KEEP_FILE=$(mktemp)
    NOW_S=$(date +%s)
    # Tier 1: 12 most recent (last 24h at 2h cadence)
    echo "$ALL" | head -12 >> "$KEEP_FILE"
    # Tier 2: newest per day, last 7 days
    SEEN=$(mktemp)
    while IFS= read -r f; do
        D=$(echo "$f" | sed 's/bot-backup-\([0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}\).*/\1/')
        TS=$(date -d "$D" +%s 2>/dev/null || echo 0)
        AGE_D=$(( (NOW_S - TS) / 86400 ))
        [ "$AGE_D" -gt 7 ] && continue
        grep -qxF "$D" "$SEEN" && continue
        echo "$D" >> "$SEEN"; echo "$f" >> "$KEEP_FILE"
    done <<< "$ALL"
    rm -f "$SEEN"
    # Tier 3: newest per ISO week, last 28 days
    SEEN=$(mktemp)
    while IFS= read -r f; do
        D=$(echo "$f" | sed 's/bot-backup-\([0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}\).*/\1/')
        TS=$(date -d "$D" +%s 2>/dev/null || echo 0)
        AGE_D=$(( (NOW_S - TS) / 86400 ))
        [ "$AGE_D" -gt 28 ] && continue
        WK=$(date -d "$D" +%G-W%V 2>/dev/null || echo "unk")
        grep -qxF "$WK" "$SEEN" && continue
        echo "$WK" >> "$SEEN"; echo "$f" >> "$KEEP_FILE"
    done <<< "$ALL"
    rm -f "$SEEN"
    # Tier 4: newest per month, forever
    SEEN=$(mktemp)
    while IFS= read -r f; do
        M=$(echo "$f" | sed 's/bot-backup-\([0-9]\{4\}-[0-9]\{2\}\).*/\1/')
        grep -qxF "$M" "$SEEN" && continue
        echo "$M" >> "$SEEN"; echo "$f" >> "$KEEP_FILE"
    done <<< "$ALL"
    rm -f "$SEEN"
    KEEP=$(sort -u "$KEEP_FILE"); rm -f "$KEEP_FILE"
    DELETED=0
    while IFS= read -r f; do
        if ! echo "$KEEP" | grep -qxF "$f"; then
            RCLONE_CONFIG="$RCLONE_CONF" rclone deletefile "gdrive:${f}" \
                --drive-use-trash=false -q 2>/dev/null && DELETED=$((DELETED + 1))
        fi
    done <<< "$ALL"
    log "  GDrive tiered prune: ${DELETED} deleted (2h/daily/weekly/monthly)"
}

# --- Concurrent-run protection (with stale-lock auto-recovery) ---
LOCKFILE="/tmp/bot-backup.lock"
LOCK_MAX_AGE_S="${BACKUP_LOCK_MAX_AGE_S:-1800}"  # 30 min — backup never takes this long

# Check for stale lock: if lock file exists and is older than LOCK_MAX_AGE_S,
# the previous backup likely hung (7z stall, OOM, etc.). Remove the stale lock
# so this run can proceed. The flock is process-scoped — if the holding PID is
# dead, the kernel already released it, but the FILE persists (flock doesn't
# auto-delete). If the PID is alive but stuck, we kill it.
if [ -f "$LOCKFILE" ]; then
    _lock_age=$(( $(date +%s) - $(stat -c %Y "$LOCKFILE" 2>/dev/null || echo 0) ))
    if [ "$_lock_age" -gt "$LOCK_MAX_AGE_S" ]; then
        # Lock file is stale — check if the holding process is still alive
        _lock_pid=$(cat "$LOCKFILE" 2>/dev/null | head -1)
        if [ -n "$_lock_pid" ] && kill -0 "$_lock_pid" 2>/dev/null; then
            # Process still alive but stuck — kill it
            echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] WARNING: Killing stuck backup process (PID $_lock_pid, ${_lock_age}s old)"
            kill -TERM "$_lock_pid" 2>/dev/null
            sleep 2
            kill -9 "$_lock_pid" 2>/dev/null || true
        fi
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Auto-recovered stale lock (${_lock_age}s old, max ${LOCK_MAX_AGE_S}s)"
        rm -f "$LOCKFILE"
    fi
fi

exec 200>"$LOCKFILE"
if ! flock -n 200; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Another backup is already running — skipping"
    exit 0
fi
# Write our PID for stale-lock detection by future runs
echo $$ >&200

# --- Cadence throttle (2h minimum between backups) ---
# Cron runs hourly but we only want to backup every 2h.
# Skip silently if a backup exists that's less than 2 hours old.
BACKUP_INTERVAL_S="${BACKUP_INTERVAL_S:-7200}"
if ! $DRY_RUN; then
    _LAST=$(ls -1t "${REPO_DIR}/deploy/backups"/bot-backup-*.7z 2>/dev/null | head -1)
    if [ -n "$_LAST" ] && [ -f "$_LAST" ]; then
        _AGE=$(( $(date +%s) - $(stat -c %Y "$_LAST") ))
        if [ "$_AGE" -lt "$BACKUP_INTERVAL_S" ]; then
            echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Backup skipped — last backup ${_AGE}s ago (<${BACKUP_INTERVAL_S}s)"
            exit 0
        fi
    fi
fi

# --- Pre-flight ---
log "=== Backup started (${BACKUP_NAME}) ==="
BACKUP_PASS=$(get_password)

if ! command -v 7z &>/dev/null; then
    log "ERROR: 7z not found. Install: sudo apt install p7zip-full"
    exit 1
fi

if ! docker ps --format '{{.Names}}' | grep -q "^${BOT_CONTAINER}$"; then
    log "ERROR: Bot container '${BOT_CONTAINER}' not running"
    exit 1
fi

$DRY_RUN && log "DRY RUN — no files will be written"

# ======================================================================
# Step 1: SQLite snapshots (VACUUM INTO — consistent, no write lock)
# ======================================================================
log "Step 1/5: SQLite snapshots..."
mkdir -p "$STAGING"

if ! $DRY_RUN; then
    # Cleanup orphan temp files from previous interrupted runs
    docker exec "$BOT_CONTAINER" rm -f /app/data/tmp/backup-conversations.db 2>/dev/null || true

    # conversations.db — main DB (messages, facts, RAG, media index, task queue)
    if docker exec "$BOT_CONTAINER" python3 -c "
import sqlite3, sys
try:
    c = sqlite3.connect('/app/data/conversations.db', timeout=30)
    c.execute(\"VACUUM INTO '/app/data/tmp/backup-conversations.db'\")
    c.close(); print('OK: conversations.db')
except Exception as e:
    print(f'FAIL: {e}', file=sys.stderr); sys.exit(1)
"; then
        docker cp "${BOT_CONTAINER}:/app/data/tmp/backup-conversations.db" "$STAGING/conversations.db"
        docker exec "$BOT_CONTAINER" rm -f /app/data/tmp/backup-conversations.db
    else
        log "  ERROR: conversations.db snapshot failed — aborting"
        exit 1
    fi

    # NOTE: task_queue is a table inside conversations.db (not a separate file)
    # NOTE: shared-media volume intentionally skipped (ephemeral transit files)

    # pm.db — PM Agent Bus database (projects, sprints, tickets, agent_runs)
    docker exec "$BOT_CONTAINER" rm -f /app/data/tmp/backup-pm.db 2>/dev/null || true
    if docker exec "$BOT_CONTAINER" python3 -c "
import sqlite3, sys, os
src = '/app/data/pm.db'
if not os.path.exists(src):
    print('SKIP: pm.db not found (PM not initialized)', file=sys.stderr); sys.exit(0)
try:
    c = sqlite3.connect(src, timeout=30)
    c.execute(\"VACUUM INTO '/app/data/tmp/backup-pm.db'\")
    c.close(); print('OK: pm.db')
except Exception as e:
    print(f'WARN: pm.db: {e}', file=sys.stderr); sys.exit(1)
" 2>&1; then
        docker cp "${BOT_CONTAINER}:/app/data/tmp/backup-pm.db" "$STAGING/pm.db" 2>/dev/null || true
        docker exec "$BOT_CONTAINER" rm -f /app/data/tmp/backup-pm.db 2>/dev/null || true
    fi

    # WA bridge databases (messages + device keys)
    # Snapshot via bot-core Python (wa-data mounted read-only at /app/wa-data/)
    # VACUUM INTO is WAL-safe — consistent snapshot even if WA bridge is writing
    for db in messages.db whatsapp.db; do
        TMP_PATH="/app/data/tmp/backup-wa-${db}"
        SRC_PATH="/app/wa-data/${db}"
        docker exec "$BOT_CONTAINER" rm -f "$TMP_PATH" 2>/dev/null || true
        if docker exec "$BOT_CONTAINER" python3 -c "
import sqlite3, sys, os
src = '${SRC_PATH}'
if not os.path.exists(src):
    print(f'SKIP: {src} not found (WA bridge may not be running)', file=sys.stderr); sys.exit(0)
try:
    c = sqlite3.connect(src, timeout=30)
    c.execute(\"VACUUM INTO '${TMP_PATH}'\")
    c.close(); print('OK: wa-${db}')
except Exception as e:
    print(f'WARN: wa-${db}: {e}', file=sys.stderr); sys.exit(1)
" 2>&1; then
            docker cp "${BOT_CONTAINER}:${TMP_PATH}" "$STAGING/wa-${db}" 2>/dev/null || true
            docker exec "$BOT_CONTAINER" rm -f "$TMP_PATH" 2>/dev/null || true
        fi
    done
fi

# ======================================================================
# Step 2: Copy everything else from containers + host
# ======================================================================
log "Step 2/5: Copying configs, credentials, sessions..."

if ! $DRY_RUN; then
    # --- Credentials & auth (P0 — unrecoverable) ---
    for dir in tg_session google-workspace-creds ms365-mcp oauth_profiles; do
        mkdir -p "$STAGING/$dir"
        docker cp "${BOT_CONTAINER}:/app/data/${dir}/." "$STAGING/${dir}/" 2>/dev/null || true
    done

    # Claude SDK sessions + credentials (P1)
    mkdir -p "$STAGING/claude"
    docker cp "${BOT_CONTAINER}:/home/botuser/.claude/." "$STAGING/claude/" 2>/dev/null || true

    # Camofox browser profiles (bot-side, in bot-data volume)
    mkdir -p "$STAGING/camofox-profiles"
    docker cp "${BOT_CONTAINER}:/app/data/camofox-profiles/." "$STAGING/camofox-profiles/" 2>/dev/null || true

    # Camofox browser-native data (separate camofox-data volume)
    if docker ps --format '{{.Names}}' | grep -q "^${CAMOFOX_CONTAINER}$"; then
        mkdir -p "$STAGING/camofox-data"
        docker cp "${CAMOFOX_CONTAINER}:/home/node/.camofox/." "$STAGING/camofox-data/" 2>/dev/null || true
    fi

    # Media cache (separate volume mounted inside bot-data)
    mkdir -p "$STAGING/media_cache"
    docker cp "${BOT_CONTAINER}:/app/data/media_cache/." "$STAGING/media_cache/" 2>/dev/null || true

    # Harness project dirs (settings.json, skills per harness)
    mkdir -p "$STAGING/harness-projects"
    docker cp "${BOT_CONTAINER}:/app/data/harness-projects/." "$STAGING/harness-projects/" 2>/dev/null || true

    # Prompts (safety net — may have local edits not in git)
    mkdir -p "$STAGING/prompts"
    docker cp "${BOT_CONTAINER}:/app/data/prompts/." "$STAGING/prompts/" 2>/dev/null || true

    # Browser profile (Playwright persistent state — large, but needed for full restore)
    # Skip if >500MB to keep backup size reasonable
    BROWSER_SIZE=$(docker exec "$BOT_CONTAINER" du -sm /app/data/browser-profile/ 2>/dev/null | cut -f1 || echo "0")
    if [ "${BROWSER_SIZE:-0}" -lt 500 ]; then
        mkdir -p "$STAGING/browser-profile"
        docker cp "${BOT_CONTAINER}:/app/data/browser-profile/." "$STAGING/browser-profile/" 2>/dev/null || true
    else
        log "  OK: browser-profile skipped (${BROWSER_SIZE}MB) — persists in Docker volume, not needed in backup"
    fi

    # --- JSON state & config files ---
    for f in \
        external_chats.json wa_external_chats.json \
        chat_harnesses.json harnesses.json \
        model_overrides.json oauth_overrides.json \
        oauth_default_label.json \
        scheduled_tasks.json relay_sessions.json \
        mac_ip_cache.json external_tool_overrides.json \
        org_config.json \
        elena_google_auth_state.json ms365_auth_state.json \
        last_email_poll.txt last_proactive.txt wa_last_poll.txt \
        last_startup.txt imagen_model.txt; do
        docker cp "${BOT_CONTAINER}:/app/data/${f}" "$STAGING/${f}" 2>/dev/null || true
    done

    # --- Logs (recent only) ---
    mkdir -p "$STAGING/logs"
    docker cp "${BOT_CONTAINER}:/app/logs/bot.log" "$STAGING/logs/" 2>/dev/null || true
    docker cp "${BOT_CONTAINER}:/app/logs/bot.log.1" "$STAGING/logs/" 2>/dev/null || true

    # --- Host-side files ---
    mkdir -p "$STAGING/env"
    cp "${REPO_DIR}/.env" "$STAGING/env/dot-env" 2>/dev/null || true
    cp "${REPO_DIR}/.env.staging" "$STAGING/env/dot-env-staging" 2>/dev/null || true
    cp "${REPO_DIR}/docker-compose.override.yml" "$STAGING/env/docker-compose-override.yml" 2>/dev/null || true
    git -C "$REPO_DIR" rev-parse HEAD > "$STAGING/env/git-commit.txt" 2>/dev/null || true
    git -C "$REPO_DIR" remote get-url origin > "$STAGING/env/git-remote.txt" 2>/dev/null || true
    date -u +%Y-%m-%dT%H:%M:%SZ > "$STAGING/env/backup-timestamp.txt"

    # Deploy directory state (last_healthy_commit, watcher config)
    mkdir -p "$STAGING/deploy"
    for f in last_healthy_commit deploy_result.json last_failed_startup.log; do
        cp "${REPO_DIR}/deploy/${f}" "$STAGING/deploy/${f}" 2>/dev/null || true
    done

    # Rclone config (needed for GDrive backup on new server)
    if [ -f "$HOME/.config/rclone/rclone.conf" ]; then
        mkdir -p "$STAGING/env/rclone"
        cp "$HOME/.config/rclone/rclone.conf" "$STAGING/env/rclone/" 2>/dev/null || true
    fi

    # --- Staging volumes (optional) ---
    if $WITH_STAGING; then
        log "  Including staging data..."
        STAGING_CONTAINER="${COMPOSE_PROJECT}-staging"
        if docker ps -a --format '{{.Names}}' | grep -q "^${STAGING_CONTAINER}$"; then
            mkdir -p "$STAGING/staging"
            docker cp "${STAGING_CONTAINER}:/app/data/." "$STAGING/staging/data/" 2>/dev/null || true
            docker cp "${STAGING_CONTAINER}:/app/logs/." "$STAGING/staging/logs/" 2>/dev/null || true
        else
            log "  WARN: Staging container not found — backing up volumes via docker run"
            for vol_name in staging-bot-data staging-bot-logs; do
                vol_short="${vol_name#staging-}"
                mkdir -p "$STAGING/staging/${vol_short}"
                docker run --rm -v "${vol_name}:/src:ro" -v "$STAGING/staging/${vol_short}":/dst \
                    alpine sh -c "cp -a /src/. /dst/" 2>/dev/null || true
            done
        fi
    fi

    # --- Mac-side data (optional, via guard.sh relay-backup-collect) ---
    if $WITH_MAC; then
        log "  Collecting Mac-side data..."
        SSH_OPTS=$(ssh_opts)
        MAC_TAR="$WORK_DIR/mac-data.tar.gz"
        if ssh $SSH_OPTS "${MAC_USER}@${MAC_HOST}" "relay-backup-collect" \
                > "$MAC_TAR" 2>/dev/null; then
            if [ -s "$MAC_TAR" ]; then
                mkdir -p "$STAGING/mac"
                tar xzf "$MAC_TAR" -C "$STAGING/mac/" 2>/dev/null || true
                rm -f "$MAC_TAR"
                MAC_SIZE=$(du -sm "$STAGING/mac/" 2>/dev/null | cut -f1 || echo "?")
                log "  ✓ Mac data collected (${MAC_SIZE}MB)"
            else
                log "  WARN: Mac data tar is empty — nothing collected"
                rm -f "$MAC_TAR"
            fi
        else
            log "  ✗ Mac data collection failed (SSH/guard.sh issue)"
        fi
    fi

    # --- Write manifest ---
    log "  Writing manifest..."
    {
        echo "backup_version=1"
        echo "timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "hostname=$(hostname -s 2>/dev/null || echo unknown)"
        echo "bot_container=${BOT_CONTAINER}"
        echo "git_commit=$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
        echo "with_staging=${WITH_STAGING}"
        echo "with_mac=${WITH_MAC}"
        echo "---files---"
        find "$STAGING" -type f | sort | while read -r f; do
            REL="${f#$STAGING/}"
            SIZE=$(stat -c%s "$f" 2>/dev/null || stat -f%z "$f" 2>/dev/null || echo 0)
            echo "${REL}|${SIZE}"
        done
    } > "$STAGING/MANIFEST.txt"
fi

# ======================================================================
# Step 3: Create encrypted 7z archive
# ======================================================================
log "Step 3/5: Creating encrypted 7z archive..."

if ! $DRY_RUN; then
    # 7z doesn't support -p@file — password is briefly visible in /proc/*/cmdline.
    # Mitigation: single-user host, ephemeral variable, unset immediately after.
    # Known limitation: documented, accepted risk for single-user cron execution.
    # timeout: 10 min max (normally ~2 min for 1.2G). Prevents lock held forever
    # if 7z hangs on I/O stall, OOM, or disk full in /dev/shm.
    timeout 600 7z a -t7z -m0=lzma2 -mx=5 -mhe=on "-p${BACKUP_PASS}" \
        "$WORK_DIR/$BACKUP_NAME" "$STAGING"/* > /dev/null
    RESULT=$?

    if [ $RESULT -eq 124 ]; then
        log "  ERROR: 7z archive creation TIMED OUT (>600s) — aborting"
        exit 1
    elif [ $RESULT -ne 0 ]; then
        log "  ERROR: 7z archive creation failed (exit $RESULT)"
        exit 1
    fi

    # Verify archive integrity (5 min timeout)
    timeout 300 7z t "-p${BACKUP_PASS}" "$WORK_DIR/$BACKUP_NAME" > /dev/null 2>&1
    if [ $? -ne 0 ]; then
        log "  ERROR: Archive verification failed — corrupted or timed out!"
        exit 1
    fi

    # Password no longer needed — clear from memory
    unset BACKUP_PASS

    BACKUP_SIZE=$(du -h "$WORK_DIR/$BACKUP_NAME" | cut -f1)
    log "  ${BACKUP_NAME} (${BACKUP_SIZE}) — verified ✓"
else
    log "  [dry-run] Would create: ${BACKUP_NAME}"
fi

# ======================================================================
# Step 4: Upload to destinations
# ======================================================================
if $LOCAL_ONLY; then
    log "Step 4/5: Local only (--local-only)"
    if ! $DRY_RUN; then
        mkdir -p "${REPO_DIR}/deploy/backups"
        mv "$WORK_DIR/$BACKUP_NAME" "${REPO_DIR}/deploy/backups/"
        log "  Saved: ${REPO_DIR}/deploy/backups/${BACKUP_NAME}"
        prune_tiered "${REPO_DIR}/deploy/backups"
    fi
else
    # --- Mac via guard.sh relay-backup-put/prune ---
    log "Step 4/5a: Uploading to Mac (${MAC_HOST})..."
    if ! $DRY_RUN; then
        SSH_OPTS=$(ssh_opts)
        MAC_ERR="$WORK_DIR/mac_err.tmp"
        if timeout 600 ssh $SSH_OPTS -o ConnectTimeout=60 "${MAC_USER}@${MAC_HOST}" \
                "relay-backup-put ${BACKUP_NAME}" \
                < "$WORK_DIR/$BACKUP_NAME" 2>"$MAC_ERR"; then
            log "  ✓ Uploaded to Mac"
            # Prune: 4-tier retention (Mac-side logic in guard.sh)
            PRUNE_OUT=$(ssh $SSH_OPTS "${MAC_USER}@${MAC_HOST}" "relay-backup-prune" \
                2>/dev/null || echo "prune_failed")
            log "  ✓ Pruned (${PRUNE_OUT})"
        else
            log "  ✗ Mac upload failed: $(head -3 "$MAC_ERR" | tr '\n' ' ')"
        fi
        rm -f "$MAC_ERR"
    fi

    # --- Google Drive via rclone (OAuth, drive.file scope) ---
    log "Step 4/5b: Uploading to Google Drive..."
    if ! $DRY_RUN; then
        if ! command -v rclone &>/dev/null; then
            log "  ✗ rclone not installed — skipping GDrive"
        elif [ ! -f "$RCLONE_CONF" ]; then
            log "  ✗ rclone config not found: ${RCLONE_CONF} — skipping GDrive"
        else
            RCLONE_ERR="$WORK_DIR/rclone_err.tmp"
            if timeout 600 env RCLONE_CONFIG="$RCLONE_CONF" rclone copy "$WORK_DIR/$BACKUP_NAME" gdrive: \
                    --stats-one-line -q 2>"$RCLONE_ERR"; then
                log "  ✓ Uploaded to GDrive"
                gdrive_prune_tiered_oauth
                rm -f "$RCLONE_ERR"
            else
                log "  ✗ GDrive upload failed: $(head -5 "$RCLONE_ERR" | tr '\n' ' ')"
                rm -f "$RCLONE_ERR"
            fi
        fi
    fi
fi

# ======================================================================
# Step 5: Summary
# ======================================================================
if ! $DRY_RUN; then
    FILE_COUNT=$(grep -c '|' "$STAGING/MANIFEST.txt" 2>/dev/null || echo "?")
    log "  Files backed up: ${FILE_COUNT}"
fi
log "=== Backup complete ==="
