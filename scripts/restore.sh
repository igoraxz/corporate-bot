#!/usr/bin/env bash
# ============================================================================
# Corporate Bot — Full Restore from Encrypted Backup
# ============================================================================
# Restores a complete bot environment from a single .7z backup file.
# Can set up a BARE Ubuntu server from scratch (Docker, git, deps) or
# restore onto an existing server.
#
# Usage:
#   ./scripts/restore.sh <backup-file.7z>        # restore from local file
#   ./scripts/restore.sh --from-mac              # fetch latest from Mac
#   ./scripts/restore.sh --list-mac              # list backups on Mac
#   ./scripts/restore.sh --setup-only            # install deps, skip restore
#   ./scripts/restore.sh --verify <file.7z>      # verify archive integrity
#
# After restore, ALL sessions work as before:
#   ✓ Telegram (MTProto session restored)
#   ✓ WhatsApp (device keys + message history restored)
#   ✓ Google OAuth (all accounts), MS365 / Outlook
#   ✓ Claude SDK sessions (full JSONL history)
#   ✓ All facts, media cache, scheduled tasks, harness configs
#   ✓ Deploy watcher state (last_healthy_commit)
#   ✓ Staging data (if --with-staging was used during backup)
#   ✓ Mac-side relay + Claude data (if --with-mac was used during backup)
#
# Password: .backup-password file or interactive prompt
# ============================================================================
set -uo pipefail

# --- Configuration ---
REPO_DIR="${REPO_DIR:-/home/botuser/corporate-bot}"
MAC_USER="${BACKUP_MAC_USER:-your-mac-user}"
MAC_HOST="${BACKUP_MAC_HOST:-100.x.y.z}"
MAC_SSH_KEY="${BACKUP_MAC_SSH_KEY:-$HOME/.ssh/id_ed25519_bot}"
PASSWORD_FILE="${REPO_DIR}/.backup-password"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

log()     { echo -e "[$(date -u +%H:%M:%S)] $*"; }
success() { echo -e "${GREEN}✓ $*${NC}"; }
warn()    { echo -e "${YELLOW}⚠ $*${NC}"; }
fail()    { echo -e "${RED}✗ $*${NC}"; }

get_password() {
    if [ -f "$PASSWORD_FILE" ]; then cat "$PASSWORD_FILE"; return; fi
    echo -e "${BOLD}Enter backup password:${NC}" >&2
    read -rs pw; echo "$pw"
}

ssh_opts() {
    local opts="-o ConnectTimeout=10 -o StrictHostKeyChecking=yes"
    [ -n "$MAC_SSH_KEY" ] && opts="$opts -i $MAC_SSH_KEY"
    echo "$opts"
}

# =====================================================================
# Parse arguments
# =====================================================================
MODE="restore"
BACKUP_FILE=""
FORCE=false
for arg in "$@"; do
    case "$arg" in
        --list-mac)   MODE="list-mac" ;;
        --from-mac)   MODE="from-mac" ;;
        --setup-only) MODE="setup-only" ;;
        --verify)     MODE="verify" ;;
        --force)      FORCE=true ;;
        *)            BACKUP_FILE="$arg" ;;
    esac
done

if [ "$MODE" = "list-mac" ]; then
    log "Backups on Mac (${MAC_HOST}):"
    ssh $(ssh_opts) "${MAC_USER}@${MAC_HOST}" "relay-backup-list"
    exit 0
fi

if [ "$MODE" = "verify" ] && [ -z "$BACKUP_FILE" ]; then
    fail "verify requires a file argument: $0 --verify <backup.7z>"
    exit 1
fi

if [ "$MODE" = "verify" ] && [ -n "$BACKUP_FILE" ]; then
    log "Verifying archive: $BACKUP_FILE"
    BACKUP_PASS=$(get_password)
    if 7z t "-p${BACKUP_PASS}" "$BACKUP_FILE" > /dev/null 2>&1; then
        success "Archive is valid"
    else
        fail "Archive is corrupt or wrong password"
        exit 1
    fi
    exit 0
fi

echo -e "${BLUE}${BOLD}"
echo "  ____            _ _         ____        _   "
echo " |  _ \\ __ _ _ __ (_) |_   _  | __ )  ___ | |_ "
echo " | |_) / _\` | '_ \\| | | | | | |  _ \\ / _ \\| __|"
echo " |  __/ (_| | | | | | | |_| | | |_) | (_) | |_ "
echo " |_|   \\__,_|_| |_|_|_|\\__, | |____/ \\___/ \\__|"
echo "                        |___/                    "
echo -e "${NC}"
echo -e "${BOLD}Full Restore from Backup${NC}"
echo ""

# =====================================================================
# Step 1: Install system dependencies
# =====================================================================
log "${BOLD}Step 1/8: System dependencies${NC}"

# Docker
if command -v docker &>/dev/null; then
    success "Docker $(docker --version 2>/dev/null | grep -oP '\d+\.\d+\.\d+' | head -1)"
else
    log "Installing Docker..."
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER"
    success "Docker installed"
fi

if docker compose version &>/dev/null 2>&1; then
    success "Docker Compose available"
else
    sudo apt-get update -qq && sudo apt-get install -y -qq docker-compose-plugin
    success "Docker Compose plugin installed"
fi

for dep in "git:git" "7z:p7zip-full" "rclone:rclone" "curl:curl" "python3:python3"; do
    cmd="${dep%%:*}"; pkg="${dep##*:}"
    if command -v "$cmd" &>/dev/null; then
        success "$cmd available"
    else
        log "Installing $pkg..."
        sudo apt-get update -qq 2>/dev/null
        sudo apt-get install -y -qq "$pkg"
        success "$pkg installed"
    fi
done

if [ "$MODE" = "setup-only" ]; then
    success "System dependencies installed."
    exit 0
fi

# =====================================================================
# Step 2: Get backup file
# =====================================================================
log "${BOLD}Step 2/8: Backup file${NC}"

# Ensure dirs created with restrictive perms (eliminates TOCTOU race on mktemp)
umask 077

# Use /dev/shm if available (RAM-backed, no disk writes of decrypted data)
if [ -d /dev/shm ] && [ -w /dev/shm ]; then
    WORK_DIR=$(mktemp -d "/dev/shm/bot-restore-XXXXXX")
else
    WORK_DIR=$(mktemp -d "/tmp/bot-restore-XXXXXX")
fi
trap 'rm -rf "$WORK_DIR"' EXIT INT TERM HUP

if [ "$MODE" = "from-mac" ]; then
    log "Fetching latest backup from Mac..."
    LATEST_BASE=$(ssh $(ssh_opts) "${MAC_USER}@${MAC_HOST}" "relay-backup-latest")
    if [ -z "$LATEST_BASE" ]; then
        fail "No backups found on Mac"
        exit 1
    fi
    log "  Latest: $LATEST_BASE"
    ssh $(ssh_opts) -o ConnectTimeout=120 "${MAC_USER}@${MAC_HOST}" \
        "relay-backup-get ${LATEST_BASE}" > "$WORK_DIR/$LATEST_BASE"
    BACKUP_FILE="$WORK_DIR/$LATEST_BASE"
    success "Downloaded $(du -h "$BACKUP_FILE" | cut -f1)"
elif [ -z "$BACKUP_FILE" ]; then
    fail "No backup file specified."
    echo "  Usage: $0 <backup.7z> | --from-mac | --list-mac | --setup-only | --verify <file>"
    exit 1
fi

if [ ! -f "$BACKUP_FILE" ]; then
    fail "File not found: $BACKUP_FILE"
    exit 1
fi

log "  Source: $(basename "$BACKUP_FILE") ($(du -h "$BACKUP_FILE" | cut -f1))"

# =====================================================================
# Step 3: Decrypt + extract
# =====================================================================
log "${BOLD}Step 3/8: Decrypt + extract${NC}"

BACKUP_PASS=$(get_password)

# Verify first
if ! 7z t "-p${BACKUP_PASS}" "$BACKUP_FILE" > /dev/null 2>&1; then
    fail "Archive verification failed — wrong password or corrupt file"
    exit 1
fi
success "Archive verified"

EXTRACT_DIR="$WORK_DIR/extracted"
mkdir -p "$EXTRACT_DIR"

7z x "-p${BACKUP_PASS}" -o"$EXTRACT_DIR" "$BACKUP_FILE" -y > /dev/null 2>&1
if [ $? -ne 0 ]; then
    fail "Extraction failed"
    exit 1
fi

# Find the data — 7z extracts STAGING/* which is WORK_DIR/data/*
# So content lands under $EXTRACT_DIR/data/ (matching $STAGING dir name)
RESTORE_DIR="$EXTRACT_DIR"
if [ -f "$EXTRACT_DIR/data/MANIFEST.txt" ]; then
    RESTORE_DIR="$EXTRACT_DIR/data"
elif [ -f "$EXTRACT_DIR/MANIFEST.txt" ]; then
    RESTORE_DIR="$EXTRACT_DIR"
else
    warn "MANIFEST.txt not found — archive may have unexpected structure"
    # Try data/ subdir as fallback
    [ -d "$EXTRACT_DIR/data" ] && RESTORE_DIR="$EXTRACT_DIR/data"
fi

if [ -f "$RESTORE_DIR/MANIFEST.txt" ]; then
    log "  Manifest:"
    head -6 "$RESTORE_DIR/MANIFEST.txt" | while read -r line; do log "    $line"; done
fi
success "Extracted"

# =====================================================================
# Step 4: Safety check for existing data
# =====================================================================
log "${BOLD}Step 4/8: Pre-restore safety check${NC}"

if docker volume ls -q 2>/dev/null | grep -q "^bot-data$"; then
    if ! $FORCE; then
        warn "Docker volume 'bot-data' already exists with data!"
        echo -e "  ${BOLD}This will OVERWRITE existing bot data.${NC}"
        echo -e "  Use --force to skip this prompt."
        echo -n "  Continue? [y/N] "
        read -r confirm
        if [[ ! "$confirm" =~ ^[Yy] ]]; then
            log "Aborted."
            exit 0
        fi
    fi
    warn "Overwriting existing volumes"
else
    success "Clean install — no existing volumes"
fi

# =====================================================================
# Step 5: Clone repo + restore host files
# =====================================================================
log "${BOLD}Step 5/8: Git repo + host files${NC}"

GIT_REMOTE=""
[ -f "$RESTORE_DIR/env/git-remote.txt" ] && GIT_REMOTE=$(cat "$RESTORE_DIR/env/git-remote.txt")

if [ -d "$REPO_DIR/.git" ]; then
    success "Git repo exists at $REPO_DIR"
    cd "$REPO_DIR"
    git pull --ff-only 2>/dev/null || warn "git pull failed"
else
    if [ -z "$GIT_REMOTE" ]; then
        echo -e "${BOLD}Git repo URL (SSH format):${NC}"
        read -r GIT_REMOTE
    else
        echo -e "  Backup specifies git remote: ${BOLD}${GIT_REMOTE}${NC}"
        echo -n "  Use this URL? [Y/n] "
        read -r confirm
        if [[ "$confirm" =~ ^[Nn] ]]; then
            echo -e "${BOLD}Enter git URL:${NC}"
            read -r GIT_REMOTE
        fi
    fi
    log "Cloning $GIT_REMOTE → $REPO_DIR..."
    # Disable hooks during clone for safety
    if ! git clone --config core.hooksPath=/dev/null "$GIT_REMOTE" "$REPO_DIR"; then
        fail "git clone failed — check URL and SSH keys"
        exit 1
    fi
    success "Repo cloned"
    cd "$REPO_DIR"
    # Restore hooks
    git config --unset core.hooksPath 2>/dev/null || true

    if [ -f "$RESTORE_DIR/env/git-commit.txt" ]; then
        BACKUP_COMMIT=$(cat "$RESTORE_DIR/env/git-commit.txt")
        log "  Backup from commit: ${BACKUP_COMMIT:0:10}"
    fi
fi

# Restore .env
if [ -f "$RESTORE_DIR/env/dot-env" ]; then
    cp "$RESTORE_DIR/env/dot-env" "$REPO_DIR/.env"
    chmod 600 "$REPO_DIR/.env"
    success ".env restored"
fi
if [ -f "$RESTORE_DIR/env/dot-env-staging" ]; then
    cp "$RESTORE_DIR/env/dot-env-staging" "$REPO_DIR/.env.staging"
    chmod 600 "$REPO_DIR/.env.staging"
    success ".env.staging restored"
fi

# docker-compose.override.yml (deploy key mount)
if [ -f "$RESTORE_DIR/env/docker-compose-override.yml" ]; then
    cp "$RESTORE_DIR/env/docker-compose-override.yml" "$REPO_DIR/docker-compose.override.yml"
    success "docker-compose.override.yml restored"
fi

# Deploy directory state
if [ -d "$RESTORE_DIR/deploy" ]; then
    mkdir -p "$REPO_DIR/deploy"
    cp "$RESTORE_DIR/deploy/"* "$REPO_DIR/deploy/" 2>/dev/null || true
    success "Deploy state restored (last_healthy_commit, etc.)"
fi

# Rclone config
if [ -f "$RESTORE_DIR/env/rclone/rclone.conf" ]; then
    mkdir -p "$HOME/.config/rclone"
    cp "$RESTORE_DIR/env/rclone/rclone.conf" "$HOME/.config/rclone/"
    chmod 600 "$HOME/.config/rclone/rclone.conf"
    success "Rclone config restored"
fi

# Backup password
if [ ! -f "$REPO_DIR/.backup-password" ]; then
    printf '%s' "$BACKUP_PASS" > "$REPO_DIR/.backup-password"
    chmod 600 "$REPO_DIR/.backup-password"
    success "Backup password saved"
fi

# =====================================================================
# Step 6: Create Docker volumes + populate
# =====================================================================
log "${BOLD}Step 6/8: Docker volumes${NC}"

sudo systemctl start docker 2>/dev/null || true

for vol in bot-data bot-logs claude-sessions media-cache wa-data shared-media camofox-data; do
    docker volume create "$vol" > /dev/null 2>&1 || true
done
success "Docker volumes created"

vol_put() {
    local src="$1" vol="$2" dst="$3"
    [ -f "$src" ] || return 0
    local src_dir; src_dir=$(cd "$(dirname "$src")" && pwd)
    local src_base; src_base=$(basename "$src")
    docker run --rm -v "$vol":/vol -v "$src_dir":/src:ro \
        alpine sh -c "mkdir -p /vol/$(dirname "$dst") && cp /src/${src_base} /vol/${dst}"
}

vol_put_dir() {
    local src_dir="$1" vol="$2" dst="$3"
    [ -d "$src_dir" ] || return 0
    local abs_dir; abs_dir=$(cd "$src_dir" && pwd)
    docker run --rm -v "$vol":/vol -v "$abs_dir":/src:ro \
        alpine sh -c "mkdir -p /vol/${dst} && cp -a /src/. /vol/${dst}/"
}

log "  Populating bot-data..."
vol_put "$RESTORE_DIR/conversations.db" bot-data "conversations.db"
vol_put "$RESTORE_DIR/task_queue.db" bot-data "task_queue.db"
[ -f "$RESTORE_DIR/pm.db" ] && vol_put "$RESTORE_DIR/pm.db" bot-data "pm.db"
vol_put_dir "$RESTORE_DIR/tg_session" bot-data "tg_session"
vol_put_dir "$RESTORE_DIR/google-workspace-creds" bot-data "google-workspace-creds"
vol_put_dir "$RESTORE_DIR/ms365-mcp" bot-data "ms365-mcp"
vol_put_dir "$RESTORE_DIR/oauth_profiles" bot-data "oauth_profiles"
vol_put_dir "$RESTORE_DIR/camofox-profiles" bot-data "camofox-profiles"
vol_put_dir "$RESTORE_DIR/harness-projects" bot-data "harness-projects"
vol_put_dir "$RESTORE_DIR/prompts" bot-data "prompts"
vol_put_dir "$RESTORE_DIR/browser-profile" bot-data "browser-profile"

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
    vol_put "$RESTORE_DIR/$f" bot-data "$f"
done
success "bot-data populated"

log "  Populating media-cache..."
vol_put_dir "$RESTORE_DIR/media_cache" media-cache "."
success "media-cache populated"

log "  Populating claude-sessions..."
vol_put_dir "$RESTORE_DIR/claude" claude-sessions "."
success "claude-sessions populated"

log "  Populating wa-data..."
vol_put "$RESTORE_DIR/wa-messages.db" wa-data "messages.db"
vol_put "$RESTORE_DIR/wa-whatsapp.db" wa-data "whatsapp.db"
success "wa-data populated"

log "  Populating camofox-data..."
vol_put_dir "$RESTORE_DIR/camofox-data" camofox-data "."
success "camofox-data populated"

log "  Restoring logs..."
vol_put_dir "$RESTORE_DIR/logs" bot-logs "."
success "bot-logs populated"

# Staging volumes (if present in backup)
if [ -d "$RESTORE_DIR/staging" ]; then
    log "  Restoring staging volumes..."
    for vol_name in staging-bot-data staging-bot-logs staging-claude-sessions staging-media-cache; do
        docker volume create "$vol_name" > /dev/null 2>&1 || true
    done
    vol_put_dir "$RESTORE_DIR/staging/bot-data" staging-bot-data "."
    vol_put_dir "$RESTORE_DIR/staging/bot-logs" staging-bot-logs "."
    success "Staging volumes populated"
fi

# Mac-side data (if present in backup)
if [ -d "$RESTORE_DIR/mac" ]; then
    log "  Restoring Mac-side data..."
    echo -e "  ${BOLD}Mac data found in backup (.relay, .claude)${NC}"
    echo -e "  Target: ${MAC_USER}@${MAC_HOST}:~/"
    echo -n "  Restore Mac data now? [y/N] "
    if $FORCE; then
        echo "(--force, skipping prompt)"
        MAC_RESTORE=true
    else
        read -r confirm
        MAC_RESTORE=false
        [[ "$confirm" =~ ^[Yy] ]] && MAC_RESTORE=true
    fi
    if $MAC_RESTORE; then
        SSH_OPTS=$(ssh_opts)
        # Create a tar of Mac data and push via SSH
        MAC_TAR="$WORK_DIR/mac-restore.tar.gz"
        tar czf "$MAC_TAR" -C "$RESTORE_DIR/mac" . 2>/dev/null
        if ssh $SSH_OPTS "${MAC_USER}@${MAC_HOST}" \
                "cat > /tmp/mac-restore.tar.gz" < "$MAC_TAR" 2>/dev/null && \
           ssh $SSH_OPTS "${MAC_USER}@${MAC_HOST}" bash -s <<'MAC_RESTORE_EOF'
cd "$HOME" || exit 1
# Backup existing dirs before overwrite
for d in .relay .claude; do
    [ -d "$d" ] && cp -a "$d" "${d}.pre-restore-$(date +%Y%m%d%H%M)" 2>/dev/null || true
done
tar xzf /tmp/mac-restore.tar.gz -C "$HOME/" 2>/dev/null
rm -f /tmp/mac-restore.tar.gz
echo "OK"
MAC_RESTORE_EOF
        then
            success "Mac data restored (existing dirs backed up with .pre-restore-* suffix)"
        else
            warn "Mac data restore failed — check SSH/Tailscale connectivity"
            echo "  Manual restore: tar xzf <backup>/mac-data.tar.gz -C ~/"
        fi
        rm -f "$MAC_TAR"
    else
        warn "Mac data skipped — restore manually later:"
        echo "  scp -r $RESTORE_DIR/mac/.relay $RESTORE_DIR/mac/.claude ${MAC_USER}@${MAC_HOST}:~/"
    fi
fi

# Fix permissions (botuser UID=10001 GID=10001, set in Dockerfile)
log "  Fixing permissions..."
docker run --rm \
    -v bot-data:/d1 -v bot-logs:/d2 -v claude-sessions:/d3 -v media-cache:/d4 \
    alpine sh -c "
        chown -R 10001:10001 /d1 /d2 /d3 /d4 2>/dev/null
        chmod 600 /d1/tg_session/* 2>/dev/null
        chmod -R 700 /d1/google-workspace-creds /d1/ms365-mcp /d1/oauth_profiles 2>/dev/null
    " 2>/dev/null || true
success "Permissions fixed"

# =====================================================================
# Step 7: Build + start
# =====================================================================
log "${BOLD}Step 7/8: Build + launch${NC}"

cd "$REPO_DIR"
log "Building Docker images (3-5 min on first build)..."
docker compose build 2>&1 | tail -3
success "Build complete"

log "Starting containers..."
docker compose up -d
success "Containers started"

# =====================================================================
# Step 8: Health check
# =====================================================================
log "${BOLD}Step 8/8: Health check${NC}"

BOT_PORT=$(grep '^BOT_PORT=' "$REPO_DIR/.env" 2>/dev/null | cut -d= -f2- || echo "8000")
BOT_PORT="${BOT_PORT:-8000}"

PROJ=$(grep '^COMPOSE_PROJECT_NAME=' "$REPO_DIR/.env" 2>/dev/null | cut -d= -f2- || echo "corporate-bot")

log "Waiting for health (up to 60s)..."
HEALTHY=false
for i in $(seq 1 30); do
    if curl -sf "http://localhost:${BOT_PORT}/health" > /dev/null 2>&1; then
        HEALTHY=true; break
    fi
    sleep 2
done

if $HEALTHY; then
    success "Bot is healthy!"
    curl -s "http://localhost:${BOT_PORT}/health" | python3 -m json.tool 2>/dev/null || true
else
    warn "Not responding after 60s — check: docker logs ${PROJ:-corporate-bot} --tail 50"
fi

# =====================================================================
# Summary
# =====================================================================
echo ""
echo -e "${GREEN}${BOLD}=== Restore Complete ===${NC}"
echo ""
[ -f "$RESTORE_DIR/env/backup-timestamp.txt" ] && \
    echo -e "  Backup from: ${BOLD}$(cat "$RESTORE_DIR/env/backup-timestamp.txt")${NC}"
[ -f "$RESTORE_DIR/env/git-commit.txt" ] && \
    echo -e "  Git commit:  ${BOLD}$(head -c 10 "$RESTORE_DIR/env/git-commit.txt")${NC}"
echo ""
echo -e "${BOLD}Post-restore checklist:${NC}"
echo "  1. Send a test message on Telegram"
echo "  2. Check WhatsApp: docker logs ${PROJ}-wa-bridge --tail 20"
echo "  3. Ask bot: 'what's on today?' (tests calendar + facts)"
echo "  4. If TG expired: docker exec -it ${PROJ} python scripts/setup_tg_userbot.py"
echo "  5. If WA disconnected: check wa-bridge logs for QR code"
echo "  6. If Mac relay needed: verify Mac data restored (--with-mac in backup)"
echo ""
echo -e "${BOLD}Next steps:${NC}"
echo "  • Deploy watcher: ./scripts/setup.sh --deploy"
echo "  • Install backup:   sudo mkdir -p /opt/corporate-bot-backup && sudo cp $REPO_DIR/scripts/backup.sh /opt/corporate-bot-backup/backup.sh && sudo chown -R botuser:botuser /opt/corporate-bot-backup && sudo chmod 755 /opt/corporate-bot-backup/backup.sh"
echo "  • Hourly backups:   echo '0 * * * * /opt/corporate-bot-backup/backup.sh >> $REPO_DIR/deploy/backup.log 2>&1' | sudo -u botuser crontab -"
echo "  • Test backup:    sudo -u botuser /opt/corporate-bot-backup/backup.sh --dry-run"
echo ""
success "All sessions and chats should work as before."
