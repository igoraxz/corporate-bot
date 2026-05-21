#!/bin/bash
# ============================================================
# Corporate Bot — Full Setup Wizard
# ============================================================
# End-to-end setup for a new corporate bot deployment:
#   1. Organization config (org_config.json)
#   2. Environment variables (.env)
#   3. Data directory initialization
#   4. Server-side git repo setup
#   5. Deploy watcher (systemd service) + OAuth token refresh cron
#   6. Docker build & launch
#
# PREREQUISITE: Fork/clone this repo first. Each corporate bot
# should run from its own fork so that the bot's self-upgrade
# feature (git commit + push from inside the container) pushes
# to YOUR fork, not the upstream repo.
#
# Usage:
#   ./scripts/setup.sh           # Full interactive setup
#   ./scripts/setup.sh --config  # Only generate config files
#   ./scripts/setup.sh --deploy  # Only deploy (skip config)
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DATA_DIR="$PROJECT_DIR/data"
CONFIG_FILE="$DATA_DIR/org_config.json"
ENV_FILE="$PROJECT_DIR/.env"
DEPLOY_DIR="$PROJECT_DIR/deploy"

MODE="${1:-full}"  # full, --config, --deploy

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

header() { echo -e "\n${BLUE}${BOLD}=== $1 ===${NC}\n"; }
info() { echo -e "${CYAN}$1${NC}"; }
warn() { echo -e "${YELLOW}$1${NC}"; }
success() { echo -e "${GREEN}$1${NC}"; }
errormsg() { echo -e "${RED}$1${NC}"; }

prompt_required() {
    local varname="$1" prompt="$2" default="$3"
    local value=""
    while [ -z "$value" ]; do
        if [ -n "$default" ]; then
            read -rp "$(echo -e "${BOLD}$prompt${NC} [$default]: ")" value
            value="${value:-$default}"
        else
            read -rp "$(echo -e "${BOLD}$prompt${NC}: ")" value
        fi
        [ -z "$value" ] && errormsg "  This field is required."
    done
    eval "$varname=\"$value\""
}

prompt_optional() {
    local varname="$1" prompt="$2" default="$3"
    read -rp "$(echo -e "${BOLD}$prompt${NC} [$default]: ")" value
    eval "$varname=\"${value:-$default}\""
}

prompt_yn() {
    local prompt="$1" default="$2"
    local yn=""
    read -rp "$(echo -e "${BOLD}$prompt${NC} [${default}]: ")" yn
    yn="${yn:-$default}"
    [[ "$yn" =~ ^[Yy] ]]
}

# ============================================================
# Banner
# ============================================================
echo -e "${BLUE}${BOLD}"
echo "   ____                    ____        _   "
echo "  / ___|___  _ __ _ __   | __ )  ___ | |_ "
echo " | |   / _ \\| '__| '_ \\  |  _ \\ / _ \\| __|"
echo " | |__| (_) | |  | |_) | | |_) | (_) | |_ "
echo "  \\____\\___/|_|  | .__/  |____/ \\___/ \\__|"
echo "                 |_|                       "
echo -e "${NC}"
echo -e "${BOLD}Corporate Bot — Full Setup Wizard${NC}"
echo ""

# ============================================================
# STEP 0: Prerequisites Check
# ============================================================
header "Step 0: Prerequisites"

# Check we're in a git repo
if [ ! -d "$PROJECT_DIR/.git" ]; then
    errormsg "This directory is not a git repository."
    errormsg "Please fork/clone the corporate-bot repo first."
    echo ""
    echo "  How to set up:"
    echo "  1. Fork the repo on GitHub → Fork"
    echo "  2. Name your fork (e.g. 'smith-corporate-bot')"
    echo "  3. Clone YOUR fork, using the fork name as folder:"
    echo "     git clone git@github.com:YOU/smith-corporate-bot.git ~/smith-corporate-bot"
    echo "  4. Run this script from inside the clone:"
    echo "     cd ~/smith-corporate-bot && ./scripts/setup.sh"
    echo ""
    echo "  Why a fork? The bot can self-upgrade by committing and pushing."
    echo "  A fork ensures those changes go to YOUR repo, not upstream."
    echo "  Name the folder after your fork to avoid confusion."
    exit 1
fi

# Check git remote
REMOTE_URL=$(git -C "$PROJECT_DIR" remote get-url origin 2>/dev/null || echo "")
if [ -n "$REMOTE_URL" ]; then
    success "Git repo: $REMOTE_URL"
else
    warn "No git remote 'origin' configured."
    warn "The bot's self-upgrade feature needs a remote to push to."
    echo ""
    if prompt_yn "Add a git remote now?" "y"; then
        prompt_required GIT_REMOTE "  Git remote URL (SSH recommended)" ""
        git -C "$PROJECT_DIR" remote add origin "$GIT_REMOTE" 2>/dev/null || \
            git -C "$PROJECT_DIR" remote set-url origin "$GIT_REMOTE"
        success "  Set origin to $GIT_REMOTE"
    fi
fi

echo ""
info "IMPORTANT: Each corporate bot should run from its own fork."
info "The bot self-upgrades by committing + pushing to this repo."
info "Using a fork ensures those changes go to YOUR repo, not upstream."
info "Name your clone folder after the fork (e.g. ~/smith-corporate-bot)"
info "to distinguish it from the upstream corporate-bot repo."

# Check Docker
if command -v docker &>/dev/null; then
    success "Docker: $(docker --version | head -1)"
else
    errormsg "Docker not found. Install Docker first: https://docs.docker.com/get-docker/"
    exit 1
fi

if command -v docker compose &>/dev/null || docker compose version &>/dev/null 2>&1; then
    success "Docker Compose: available"
else
    errormsg "Docker Compose not found. Install Docker Compose v2."
    exit 1
fi

# Check python3
if command -v python3 &>/dev/null; then
    success "Python 3: $(python3 --version)"
else
    warn "Python 3 not found locally. Config generation may fail."
    warn "Python 3 is only needed for this setup script; the bot runs in Docker."
fi

if [ "$MODE" = "--deploy" ]; then
    # Skip config, jump to deploy
    if [ ! -f "$CONFIG_FILE" ]; then
        errormsg "No org_config.json found. Run without --deploy first."
        exit 1
    fi
    if [ ! -f "$ENV_FILE" ]; then
        errormsg "No .env found. Run without --deploy first."
        exit 1
    fi
    # Jump to deploy section
    SKIP_CONFIG=true
    SKIP_ENV=true
fi

# ============================================================
# STEP 1: Organization Config
# ============================================================
if [ "${SKIP_CONFIG:-}" != "true" ]; then

    if [ -f "$CONFIG_FILE" ]; then
        warn "org_config.json already exists at $CONFIG_FILE"
        if ! prompt_yn "Overwrite it?" "n"; then
            info "Keeping existing config."
            SKIP_CONFIG=true
        fi
    fi
fi

if [ "${SKIP_CONFIG:-}" != "true" ]; then
    header "Step 1: Organization Information"

    prompt_required ORG_NAME "Organization name (e.g. Acme Corp)" ""
    prompt_optional BOT_NAME "Bot name" "$ORG_NAME Corporate Bot"
    prompt_optional TIMEZONE "Timezone" "Europe/London"
    prompt_optional LOCATION "Home address (optional, helps with directions)" ""

    # --- Administrators ---
    header "Step 1a: Administrators"
    info "Add administrators. These are the primary bot users."
    echo ""

    PARENTS_JSON="[]"
    PARENT_NUM=0
    while true; do
        PARENT_NUM=$((PARENT_NUM + 1))
        if [ $PARENT_NUM -gt 1 ]; then
            prompt_yn "Add another administrator?" "y" || break
        fi

        echo -e "\n${BOLD}Administrator $PARENT_NUM:${NC}"
        prompt_required P_NAME "  Full name" ""
        prompt_required P_ROLE "  Role (manager/director/admin)" ""
        prompt_required P_EMAIL "  Email" ""

        prompt_optional P_TG_ID "  Telegram user ID (number, or blank)" ""
        prompt_optional P_TG_USERNAME "  Telegram username (without @, or blank)" ""
        prompt_optional P_WA_PHONE "  WhatsApp phone (intl, e.g. 447700900001, or blank)" ""

        IS_ADMIN="false"
        prompt_yn "  Is this person an admin (can self-upgrade bot)?" "y" && IS_ADMIN="true"

        P_JSON="{"
        P_JSON+="\"name\":\"$P_NAME\","
        P_JSON+="\"role\":\"$P_ROLE\","
        P_JSON+="\"email\":\"$P_EMAIL\","
        [ -n "$P_TG_ID" ] && P_JSON+="\"telegram_id\":$P_TG_ID,"
        [ -n "$P_TG_USERNAME" ] && P_JSON+="\"telegram_username\":\"$P_TG_USERNAME\","
        [ -n "$P_WA_PHONE" ] && P_JSON+="\"whatsapp_phone\":\"$P_WA_PHONE\","
        P_JSON+="\"is_admin\":$IS_ADMIN"
        P_JSON+="}"

        if [ "$PARENTS_JSON" = "[]" ]; then
            PARENTS_JSON="[$P_JSON]"
        else
            PARENTS_JSON="${PARENTS_JSON%]}, $P_JSON]"
        fi
    done

    # --- Team Members ---
    header "Step 1b: Team Members"
    CHILDREN_JSON="[]"
    if prompt_yn "Add team members?" "y"; then
        CHILD_NUM=0
        while true; do
            CHILD_NUM=$((CHILD_NUM + 1))
            [ $CHILD_NUM -gt 1 ] && { prompt_yn "Add another team member?" "y" || break; }

            echo -e "\n${BOLD}Team Member $CHILD_NUM:${NC}"
            prompt_required C_NAME "  Full name" ""
            prompt_optional C_ROLE "  Role/title (or blank)" ""
            prompt_optional C_EMAIL "  Email (or blank)" ""

            C_JSON="{\"name\":\"$C_NAME\""
            [ -n "$C_ROLE" ] && C_JSON+=",\"role\":\"$C_ROLE\""
            [ -n "$C_EMAIL" ] && C_JSON+=",\"email\":\"$C_EMAIL\""
            C_JSON+="}"

            if [ "$CHILDREN_JSON" = "[]" ]; then
                CHILDREN_JSON="[$C_JSON]"
            else
                CHILDREN_JSON="${CHILDREN_JSON%]}, $C_JSON]"
            fi
        done
    fi

    # --- Other members ---
    header "Step 1c: Other Organization Members"
    OTHER_JSON="[]"
    if prompt_yn "Add other organization members (contractor, consultant, etc.)?" "n"; then
        OTHER_NUM=0
        while true; do
            OTHER_NUM=$((OTHER_NUM + 1))
            [ $OTHER_NUM -gt 1 ] && { prompt_yn "Add another member?" "n" || break; }
            prompt_required O_NAME "  Name" ""
            prompt_required O_ROLE "  Role (nanny/au pair/etc.)" ""
            O_JSON="{\"name\":\"$O_NAME\",\"role\":\"$O_ROLE\"}"
            if [ "$OTHER_JSON" = "[]" ]; then
                OTHER_JSON="[$O_JSON]"
            else
                OTHER_JSON="${OTHER_JSON%]}, $O_JSON]"
            fi
        done
    fi

    # --- Goals ---
    header "Step 1d: Organization Goals"
    info "What should the bot help with? Enter goals one per line."
    info "Press Enter on an empty line when done."
    echo ""
    GOALS_JSON="[]"
    GOAL_NUM=0
    while true; do
        GOAL_NUM=$((GOAL_NUM + 1))
        read -rp "$(echo -e "${BOLD}Goal $GOAL_NUM (or Enter to finish)${NC}: ")" GOAL
        [ -z "$GOAL" ] && break
        GOAL_ESC=$(echo "$GOAL" | sed 's/"/\\"/g')
        if [ "$GOALS_JSON" = "[]" ]; then
            GOALS_JSON="[\"$GOAL_ESC\"]"
        else
            GOALS_JSON="${GOALS_JSON%]}, \"$GOAL_ESC\"]"
        fi
    done
    if [ "$GOALS_JSON" = "[]" ]; then
        GOALS_JSON='["Assist team with operations","Manage organizational knowledge","Support development workflows"]'
    fi

    # --- Phone agent ---
    header "Step 1e: Phone Agent (AI voice calls)"
    prompt_optional PHONE_SURNAME "Organization name for phone agent" "$ORG_NAME"
    prompt_optional PHONE_GENDER "Default voice gender (male/female)" "male"

    # --- Email config ---
    header "Step 1f: Email"
    FIRST_EMAIL=$(echo "$PARENTS_JSON" | grep -o '"email":"[^"]*"' | head -1 | cut -d'"' -f4)
    FIRST_NAME=$(echo "$PARENTS_JSON" | grep -o '"name":"[^"]*"' | head -1 | cut -d'"' -f4 | cut -d' ' -f1)
    prompt_optional PRIMARY_EMAIL "Primary email for sending" "$FIRST_EMAIL"
    prompt_optional PRIMARY_EMAIL_USER "Sender display name" "$FIRST_NAME"

    # --- Write org_config.json ---
    header "Writing org_config.json"
    mkdir -p "$DATA_DIR"

    python3 -c "
import json

config = {
    'org_name': '''$ORG_NAME''',
    'bot_name': '''$BOT_NAME''',
    'timezone': '''$TIMEZONE''',
    'location': '''$LOCATION''',
    'members': {
        'admins': json.loads('''$PARENTS_JSON'''),
        'team_members': json.loads('''$CHILDREN_JSON'''),
        'other': json.loads('''$OTHER_JSON''')
    },
    'goals': json.loads('''$GOALS_JSON'''),
    'phone_agent': {
        'org_surname': '''$PHONE_SURNAME''',
        'default_gender': '''$PHONE_GENDER'''
    },
    'email': {
        'primary_address': '''$PRIMARY_EMAIL''',
        'primary_user_name': '''$PRIMARY_EMAIL_USER'''
    }
}

with open('$CONFIG_FILE', 'w') as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
print('OK')
" && success "  Saved: $CONFIG_FILE" || { errormsg "  Failed to write config!"; exit 1; }
fi

# ============================================================
# STEP 2: Environment Variables (.env)
# ============================================================
if [ "${SKIP_ENV:-}" != "true" ]; then
    header "Step 2: Environment Variables (.env)"

    if [ -f "$ENV_FILE" ]; then
        warn ".env already exists at $ENV_FILE"
        if ! prompt_yn "Overwrite it?" "n"; then
            info "Keeping existing .env."
            SKIP_ENV=true
        fi
    fi
fi

if [ "${SKIP_ENV:-}" != "true" ]; then
    info "We'll ask for key values. Press Enter to leave blank (configure later)."
    echo ""

    # Auth
    echo -e "${BOLD}Authentication (pick one):${NC}"
    prompt_optional AUTH_TOKEN "  CLAUDE_CODE_OAUTH_TOKEN (recommended)" ""
    prompt_optional API_KEY "  ANTHROPIC_API_KEY (alternative)" ""

    # Telegram (Pyrogram MTProto userbot)
    echo -e "\n${BOLD}Telegram (Pyrogram userbot — get from https://my.telegram.org/apps):${NC}"
    prompt_optional TG_API_ID "  TG_API_ID" ""
    prompt_optional TG_API_HASH "  TG_API_HASH" ""
    prompt_optional TG_CHAT "  TG_CHAT_ID (group chat, negative number)" ""

    # Google
    echo -e "\n${BOLD}Google Workspace (optional):${NC}"
    prompt_optional G_CLIENT_ID "  GOOGLE_OAUTH_CLIENT_ID" ""
    prompt_optional G_CLIENT_SECRET "  GOOGLE_OAUTH_CLIENT_SECRET" ""

    # Vapi
    echo -e "\n${BOLD}Phone calls — Vapi (optional):${NC}"
    prompt_optional V_API_KEY "  VAPI_API_KEY" ""
    prompt_optional V_PHONE_ID "  VAPI_PHONE_NUMBER_ID" ""
    prompt_optional V_WEBHOOK "  WEBHOOK_BASE_URL (public URL)" ""

    # Gemini
    echo -e "\n${BOLD}Image generation (optional):${NC}"
    prompt_optional GEM_KEY "  GEMINI_API_KEY" ""

    # Camoufox (anti-detect browser)
    echo -e "\n${BOLD}Camoufox anti-detect browser:${NC}"
    info "  Anti-detect Firefox browser for sites that block Playwright"
    info "  (airlines, flight aggregators, CAPTCHA-heavy sites)"
    prompt_optional CAMOFOX_KEY "  CAMOFOX_API_KEY (generate: openssl rand -base64 32)" ""

    # Docker
    echo -e "\n${BOLD}Docker:${NC}"
    COMPOSE_DEFAULT="${ORG_NAME:-corporate}"
    COMPOSE_DEFAULT=$(echo "$COMPOSE_DEFAULT" | tr '[:upper:]' '[:lower:]' | tr ' ' '-')
    prompt_optional D_PROJECT "  COMPOSE_PROJECT_NAME" "${COMPOSE_DEFAULT}-bot"
    prompt_optional D_PORT "  BOT_PORT" "8000"
    prompt_optional D_TZ "  TZ (timezone)" "${TIMEZONE:-Europe/London}"

    info ""
    info "Note: Scheduled tasks (morning reminders, email checks) are managed"
    info "conversationally via the bot — no env vars needed."
    info ""
    info "Note: TG_ALLOWED_USERS and ADMIN_USERS"
    info "are auto-populated from org_config.json at runtime."

    # --- Write .env ---
    header "Writing .env"
    cat > "$ENV_FILE" << ENVEOF
# ============================================================
# Corporate Bot — Environment Configuration
# Generated by setup wizard on $(date +%Y-%m-%d)
# ============================================================

# === AUTHENTICATION ===
CLAUDE_CODE_OAUTH_TOKEN=$AUTH_TOKEN
# ANTHROPIC_API_KEY=$API_KEY

# === TELEGRAM (Pyrogram MTProto userbot) ===
TG_API_ID=$TG_API_ID
TG_API_HASH=$TG_API_HASH
TG_CHAT_ID=$TG_CHAT

# Auth lists — auto-populated from org_config.json if empty
# TG_ALLOWED_USERS=
# ADMIN_USERS=

# === GOOGLE WORKSPACE ===
GOOGLE_OAUTH_CLIENT_ID=$G_CLIENT_ID
GOOGLE_OAUTH_CLIENT_SECRET=$G_CLIENT_SECRET

# === VAPI PHONE ===
VAPI_API_KEY=$V_API_KEY
VAPI_PHONE_NUMBER_ID=$V_PHONE_ID
WEBHOOK_BASE_URL=$V_WEBHOOK

# === IMAGE GENERATION ===
GEMINI_API_KEY=$GEM_KEY

# === CAMOUFOX (anti-detect browser) ===
CAMOFOX_API_KEY=$CAMOFOX_KEY

# === DOCKER ===
COMPOSE_PROJECT_NAME=$D_PROJECT
BOT_PORT=$D_PORT
TZ=$D_TZ
MEDIA_RETENTION_DAYS=36135
ENVEOF

    success "  Saved: $ENV_FILE"
fi

# ============================================================
# STEP 3: Initialize Data Files
# ============================================================
header "Step 3: Initializing Data Files"

for dir in "$DATA_DIR/prompts" "$DATA_DIR/google-workspace-creds" "$DATA_DIR/media_cache" "$DATA_DIR/tmp"; do
    mkdir -p "$dir"
done
success "  Created data directories"

# Goals are loaded from org_config.json — no separate file needed

if [ "$MODE" = "--config" ]; then
    header "Config Setup Complete!"
    echo "  Run ./scripts/setup.sh --deploy to continue with deployment."
    exit 0
fi

# ============================================================
# STEP 4: Server-Side Git Setup
# ============================================================
header "Step 4: Server-Side Git Setup"

info "The bot needs a git repo on the server for self-upgrade."
info "The bot commits changes inside its container and pushes to origin."
echo ""
info "Architecture:"
info "  YOUR FORK (e.g. github.com/you/smith-corporate-bot)"
info "       |"
info "  SERVER (~/smith-corporate-bot — named after fork) <-- Docker runs here"
info "       |"
info "  CONTAINER (/host-repo mount) <-- bot edits code here"
echo ""
info "Tip: Name the server folder after your fork to avoid confusion"
info "with the upstream 'corporate-bot' repo."
echo ""

if prompt_yn "Set up server-side git (SSH deploy key, safe.directory)?" "y"; then

    # Check if we have a git remote
    REMOTE_URL=$(git -C "$PROJECT_DIR" remote get-url origin 2>/dev/null || echo "")
    if [ -z "$REMOTE_URL" ]; then
        prompt_required REMOTE_URL "  Git remote URL (SSH format, e.g. git@github.com:you/yourname-corporate-bot.git)" ""
        git -C "$PROJECT_DIR" remote add origin "$REMOTE_URL" 2>/dev/null || \
            git -C "$PROJECT_DIR" remote set-url origin "$REMOTE_URL"
    fi

    info ""
    info "For the bot to push changes, you need an SSH deploy key."
    info "This is a key pair where the public key is added to your"
    info "git host as a deploy key with WRITE access."
    echo ""

    DEPLOY_KEY_PATH="$HOME/.ssh/corp_bot_deploy"
    if [ ! -f "$DEPLOY_KEY_PATH" ]; then
        if prompt_yn "Generate a new deploy key now?" "y"; then
            ssh-keygen -t ed25519 -f "$DEPLOY_KEY_PATH" -N "" -C "corporate-bot-deploy-$(hostname -s)"
            success "  Generated: $DEPLOY_KEY_PATH"
            echo ""
            echo -e "${BOLD}Add this PUBLIC KEY to your git host as a deploy key (with write access):${NC}"
            echo ""
            cat "${DEPLOY_KEY_PATH}.pub"
            echo ""
            info "GitHub: Settings → Deploy Keys → Add Key → paste the above"
            info "GitLab: Settings → Repository → Deploy Keys → paste the above"
            echo ""
            read -rp "$(echo -e "${BOLD}Press Enter when done...${NC}")"
        fi
    else
        success "  Deploy key exists: $DEPLOY_KEY_PATH"
    fi

    # Create a container-readable copy of the deploy key.
    # SSH rejects keys with group-readable perms (requires ≤ 600), so we can't
    # just chmod the original — that would break host SSH. Instead, keep the
    # original at 600 for host use and create a .container copy at 640 (readable
    # by docker group / container GID 999).
    CONTAINER_KEY="${DEPLOY_KEY_PATH}.container"
    cp "$DEPLOY_KEY_PATH" "$CONTAINER_KEY"
    cp "${DEPLOY_KEY_PATH}.pub" "${CONTAINER_KEY}.pub"
    chgrp docker "$CONTAINER_KEY" "${CONTAINER_KEY}.pub" 2>/dev/null && \
    chmod 640 "$CONTAINER_KEY" 2>/dev/null && \
        success "  Created container-readable key copy (640, docker group)" || \
        warn "  Could not set container key group — container may not be able to push"

    # Configure git to use the deploy key (host-side, original 600 key)
    git -C "$PROJECT_DIR" config core.sshCommand "ssh -i $DEPLOY_KEY_PATH -o IdentitiesOnly=yes"
    success "  Configured git to use deploy key"

    # Mark repo as safe for bot user
    git -C "$PROJECT_DIR" config --global --add safe.directory "$PROJECT_DIR"
    success "  Marked repo as safe directory"

    # Generate docker-compose.override.yml mounting the .container copy
    OVERRIDE_FILE="$PROJECT_DIR/docker-compose.override.yml"
    if [ ! -f "$OVERRIDE_FILE" ]; then
        cat > "$OVERRIDE_FILE" << OVERRIDEEOF
# Auto-generated by setup wizard — mounts deploy key for self-upgrade
# Uses .container copy (640 perms) so container GID 999 can read it
# while original key stays 600 for host SSH
# IMPORTANT: paths must be absolute (not ~/) — deploy watcher runs as botuser
services:
  bot-core:
    volumes:
      - ${CONTAINER_KEY}:/home/botuser/.ssh/id_ed25519:ro
      - ${CONTAINER_KEY}.pub:/home/botuser/.ssh/id_ed25519.pub:ro
OVERRIDEEOF
        success "  Created docker-compose.override.yml (deploy key mount)"
    else
        warn "  docker-compose.override.yml already exists — not overwriting."
        info "  Ensure it mounts the .container key copy to the botuser home dir:"
        echo "    - ${CONTAINER_KEY}:/home/botuser/.ssh/id_ed25519:ro"
        echo "    - ${CONTAINER_KEY}.pub:/home/botuser/.ssh/id_ed25519.pub:ro"
    fi
else
    info "Skipping git setup. Self-upgrade will need manual configuration."
fi

# ============================================================
# STEP 5: Deploy Watcher (systemd) — Security-Hardened
# ============================================================
header "Step 5: Deploy Watcher Service"

info "The deploy watcher is a host-side systemd service that watches"
info "for deploy triggers from the bot. When the bot calls deploy_bot(),"
info "it writes a trigger file. The watcher picks it up, pulls code,"
info "rebuilds, and restarts the container."
echo ""
info "Security model:"
info "  - Watcher script is owned by root in /opt/ (not editable by bot)"
info "  - Runs as a dedicated 'botuser' (UID 10001, same as in-container user)"
info "  - botuser is in the 'docker' group (can manage containers)"
info "  - Bot cannot modify the watcher script via /host-repo mount"
echo ""

if prompt_yn "Install the deploy watcher service now?" "y"; then
    BOT_DIR="$PROJECT_DIR"

    # Determine service name (unique per org to support multiple bots)
    D_PROJECT_VAL=$(grep '^COMPOSE_PROJECT_NAME=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "corporate-bot")
    SERVICE_NAME="${D_PROJECT_VAL:-corporate-bot}-watcher"

    # --- Step 6a: Create host botuser ---
    header "Step 6a: Host botuser"
    if id botuser &>/dev/null; then
        success "  Host user 'botuser' already exists (UID $(id -u botuser))"
    else
        info "Creating host user 'botuser' (UID 10001) for watcher isolation."
        info "This user has no login shell and no password — it only runs the watcher."
        echo ""
        echo "  sudo useradd -r -s /usr/sbin/nologin -u 10001 -m botuser"
        echo "  sudo usermod -aG docker botuser"
        echo ""
        if prompt_yn "Create botuser now (requires sudo)?" "y"; then
            sudo useradd -r -s /usr/sbin/nologin -u 10001 -m botuser 2>/dev/null && \
                success "  Created botuser (UID 10001)" || \
                warn "  useradd failed (user may already exist with different UID)"
            sudo usermod -aG docker botuser && \
                success "  Added botuser to docker group" || \
                warn "  Failed to add botuser to docker group"
        fi
    fi

    # Ensure botuser has SSH dir for deploy key
    if [ -d /home/botuser ]; then
        if [ ! -d /home/botuser/.ssh ]; then
            echo ""
            info "Creating SSH directory for botuser..."
            echo "  sudo mkdir -p /home/botuser/.ssh"
            echo "  sudo chmod 700 /home/botuser/.ssh"
            echo "  sudo chown botuser:botuser /home/botuser/.ssh"
            if prompt_yn "Create SSH dir now (requires sudo)?" "y"; then
                sudo mkdir -p /home/botuser/.ssh && \
                sudo chmod 700 /home/botuser/.ssh && \
                sudo chown botuser:botuser /home/botuser/.ssh && \
                success "  Created /home/botuser/.ssh" || \
                warn "  Failed to create SSH dir"
            fi
        fi

        # Copy deploy key if it exists and botuser SSH dir is ready
        DEPLOY_KEY_PATH="${DEPLOY_KEY_PATH:-$HOME/.ssh/corp_bot_deploy}"
        if [ -f "$DEPLOY_KEY_PATH" ] && [ -d /home/botuser/.ssh ]; then
            echo ""
            info "Copying deploy key to botuser's SSH dir..."
            BOTUSER_KEY="/home/botuser/.ssh/$(basename "$DEPLOY_KEY_PATH")"
            echo "  sudo cp $DEPLOY_KEY_PATH $BOTUSER_KEY"
            echo "  sudo chown botuser:botuser $BOTUSER_KEY"
            echo "  sudo chmod 600 $BOTUSER_KEY"
            if prompt_yn "Copy deploy key now (requires sudo)?" "y"; then
                sudo cp "$DEPLOY_KEY_PATH" "$BOTUSER_KEY" && \
                sudo cp "${DEPLOY_KEY_PATH}.pub" "${BOTUSER_KEY}.pub" 2>/dev/null; \
                sudo chown botuser:botuser "$BOTUSER_KEY" "${BOTUSER_KEY}.pub" 2>/dev/null && \
                sudo chmod 600 "$BOTUSER_KEY" && \
                success "  Copied deploy key to botuser" || \
                warn "  Failed to copy deploy key"
            fi
        fi
    fi

    # --- Step 6b: Install watcher to /opt/ ---
    header "Step 6b: Watcher Script Installation"
    OPT_WATCHER_DIR="/opt/${SERVICE_NAME}"
    WATCHER_SCRIPT="$DEPLOY_DIR/host-watcher.sh"

    info "Installing watcher script to $OPT_WATCHER_DIR/"
    info "Root owns the script — botuser can only execute it, not modify."
    echo ""
    echo "  sudo mkdir -p $OPT_WATCHER_DIR"
    echo "  sudo cp $WATCHER_SCRIPT $OPT_WATCHER_DIR/host-watcher.sh"
    echo "  sudo chown root:root $OPT_WATCHER_DIR/host-watcher.sh"
    echo "  sudo chmod 755 $OPT_WATCHER_DIR/host-watcher.sh"
    echo ""

    if prompt_yn "Install watcher to /opt/ now (requires sudo)?" "y"; then
        sudo mkdir -p "$OPT_WATCHER_DIR" && \
        sudo cp "$WATCHER_SCRIPT" "$OPT_WATCHER_DIR/host-watcher.sh" && \
        sudo chown root:root "$OPT_WATCHER_DIR/host-watcher.sh" && \
        sudo chmod 755 "$OPT_WATCHER_DIR/host-watcher.sh" && \
        success "  Installed watcher to $OPT_WATCHER_DIR/" || \
        warn "  Failed to install watcher. Run the commands manually."
    fi

    # --- Step 6c: Generate and install systemd service ---
    header "Step 6c: Systemd Service"

    # SSH config for botuser (git operations need deploy key)
    BOTUSER_SSH_CMD="ssh -i /home/botuser/.ssh/$(basename "${DEPLOY_KEY_PATH:-corp_bot_deploy}") -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

    SERVICE_FILE="/tmp/${SERVICE_NAME}.service"
    cat > "$SERVICE_FILE" << SVCEOF
[Unit]
Description=Corporate Bot Deploy Watcher ($(basename "$BOT_DIR"))
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=botuser
Group=docker
WorkingDirectory=$BOT_DIR
ExecStart=$OPT_WATCHER_DIR/host-watcher.sh
Restart=always
RestartSec=10
Environment=COMPOSE_DIR=$BOT_DIR
Environment=COMPOSE_PROJECT_NAME=${D_PROJECT_VAL:-corporate-bot}
Environment=HOME=/home/botuser
Environment=GIT_SSH_COMMAND=$BOTUSER_SSH_CMD

[Install]
WantedBy=multi-user.target
SVCEOF

    info "To install the systemd service, run these commands:"
    echo ""
    echo "  sudo cp $SERVICE_FILE /etc/systemd/system/${SERVICE_NAME}.service"
    echo "  sudo systemctl daemon-reload"
    echo "  sudo systemctl enable --now ${SERVICE_NAME}"
    echo ""
    info "Check status: sudo systemctl status ${SERVICE_NAME}"

    if prompt_yn "Run these commands now (requires sudo)?" "y"; then
        sudo cp "$SERVICE_FILE" "/etc/systemd/system/${SERVICE_NAME}.service" && \
        sudo systemctl daemon-reload && \
        sudo systemctl enable --now "${SERVICE_NAME}" && \
        success "  Deploy watcher installed and started!" || \
        warn "  Failed to install service. Run the commands manually."
    fi

    # Ensure botuser can read the repo dir (for git pull)
    echo ""
    header "Step 6d: Repo Access"
    info "botuser needs read/write access to the repo (for git pull + self-upgrade writes)."
    info "We set the repo's group to 'docker' — both your user and botuser are in this group."
    info "Container botuser is in GID 999 (hostdocker) to match the host docker group."
    echo ""
    REPO_OWNER=$(stat -c "%U" "$BOT_DIR" 2>/dev/null || stat -f "%Su" "$BOT_DIR" 2>/dev/null)
    echo "  sudo chgrp -R docker $BOT_DIR"
    echo "  sudo chmod -R g+rwX $BOT_DIR"
    echo "  sudo chmod 750 /home/$REPO_OWNER  # let docker group traverse"
    echo "  sudo chgrp docker /home/$REPO_OWNER"
    echo ""
    if prompt_yn "Fix repo permissions now (requires sudo)?" "y"; then
        sudo chgrp -R docker "$BOT_DIR" && \
        sudo chmod -R g+rwX "$BOT_DIR" && \
            success "  Set repo group to docker (read/write by botuser)" || \
            warn "  Failed to set repo group"
        # Let docker group traverse the owner's home dir
        HOME_DIR=$(dirname "$BOT_DIR")
        sudo chmod 750 "$HOME_DIR" 2>/dev/null
        sudo chgrp docker "$HOME_DIR" 2>/dev/null && \
            success "  Set $HOME_DIR traversable by docker group" || \
            warn "  Failed to update home dir permissions"
        # Mark repo as safe for botuser
        sudo -u botuser git config --global --add safe.directory "$BOT_DIR" 2>/dev/null && \
            success "  Marked repo as safe directory for botuser" || \
            warn "  Failed to mark repo as safe (run: sudo -u botuser git config --global --add safe.directory $BOT_DIR)"
    fi
    # --- Step 6e: Token refresh cron ---
    header "Step 6e: OAuth Token Refresh Cron"
    info "The Claude Code CLI uses OAuth tokens that expire every ~3 hours."
    info "The CLI auto-refreshes during active API calls, but if no one messages"
    info "the bot during the refresh window, the token expires silently."
    echo ""
    info "This cron job runs every 2 hours on the HOST and uses the refresh token"
    info "already in the container to get a fresh access token from Anthropic."
    info "No macOS dependency — fully server-side."
    echo ""
    info "Security: the cron runs as YOUR admin user (not botuser), because it"
    info "needs docker exec to read/write credentials inside the container."
    info "The refresh token never leaves the container's credentials file."
    echo ""

    REFRESH_SCRIPT="$PROJECT_DIR/scripts/refresh_token_cron.sh"
    REFRESH_LOG="$DEPLOY_DIR/token_refresh.log"

    if [ -f "$REFRESH_SCRIPT" ]; then
        chmod +x "$REFRESH_SCRIPT"

        # Check if cron already has this entry
        if crontab -l 2>/dev/null | grep -q "refresh_token_cron.sh"; then
            success "  Token refresh cron already installed."
        else
            echo "  Will add to crontab:"
            echo "    0 */2 * * * $REFRESH_SCRIPT >> $REFRESH_LOG 2>&1"
            echo ""
            if prompt_yn "Install token refresh cron now?" "y"; then
                (crontab -l 2>/dev/null; echo "0 */2 * * * $REFRESH_SCRIPT >> $REFRESH_LOG 2>&1") | crontab - && \
                    success "  Token refresh cron installed (every 2 hours)!" || \
                    warn "  Failed to install cron. Add manually: crontab -e"
            fi
        fi
    else
        warn "  refresh_token_cron.sh not found at $REFRESH_SCRIPT"
        warn "  Skipping cron setup. Copy the script and run setup again."
    fi

else
    info "Skipping watcher install."
    info "Install later: see docs/HOST_SECURITY.md for manual setup instructions."
fi

# ============================================================
# STEP 7: Docker Build & Launch
# ============================================================
header "Step 7: Build & Launch"

echo -e "${BOLD}Ready to build and start the bot?${NC}"
echo ""
echo "  This will:"
echo "  - Build Docker images (bot-core + wa-bridge)"
echo "  - Start all containers (bot, WA bridge, Camoufox)"
echo "  - The first build takes 3-5 minutes (downloads, installs)"
echo ""

if prompt_yn "Build and start now?" "y"; then
    cd "$PROJECT_DIR"

    info "Building Docker images..."
    docker compose build 2>&1 | tail -5
    success "  Build complete!"

    info "Starting containers..."
    docker compose up -d 2>&1
    success "  Containers started!"

    # Wait for health
    D_PORT_VAL=$(grep '^BOT_PORT=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "8000")
    D_PORT_VAL="${D_PORT_VAL:-8000}"

    echo ""
    info "Waiting for health check..."
    for i in $(seq 1 30); do
        if curl -sf "http://localhost:${D_PORT_VAL}/health" &>/dev/null; then
            success "  Bot is healthy!"
            echo ""
            curl -s "http://localhost:${D_PORT_VAL}/health" | python3 -m json.tool 2>/dev/null || \
                curl -s "http://localhost:${D_PORT_VAL}/health"
            break
        fi
        sleep 2
    done

    if ! curl -sf "http://localhost:${D_PORT_VAL}/health" &>/dev/null; then
        warn "  Bot not responding yet. Check logs:"
        D_PROJECT_VAL=$(grep '^COMPOSE_PROJECT_NAME=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "corporate-bot")
        echo "    docker logs ${D_PROJECT_VAL:-corporate-bot} --tail 30"
    fi
else
    info "Skipping Docker launch."
    echo ""
    echo "  To build and start later:"
    echo "    cd $PROJECT_DIR"
    echo "    docker compose build"
    echo "    docker compose up -d"
fi

# ============================================================
# STEP 8: Telegram Pyrogram Session
# ============================================================
header "Step 8: Telegram Session Setup"

TG_API_ID_VAL=$(grep '^TG_API_ID=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)

if [ -n "$TG_API_ID_VAL" ]; then
    info "Telegram uses Pyrogram MTProto (userbot, not Bot API)."
    info "A one-time interactive session setup is required after first deploy."
    echo ""
    echo "  After 'docker compose up -d', run:"
    D_PROJECT_VAL=$(grep '^COMPOSE_PROJECT_NAME=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "corporate-bot")
    echo "    docker exec -it ${D_PROJECT_VAL:-corporate-bot} python scripts/setup_tg_userbot.py"
    echo ""
    info "This will ask for your phone number and a verification code."
    info "The session file is stored in a Docker volume — set up once only."
else
    info "Telegram not configured. Set TG_API_ID and TG_API_HASH in .env"
    info "(get them from https://my.telegram.org/apps)"
fi

# ============================================================
# Summary
# ============================================================
header "Setup Complete!"

echo -e "${GREEN}${BOLD}Your corporate bot is configured!${NC}"
echo ""
echo "  Files:"
[ -f "$CONFIG_FILE" ] && echo "    data/org_config.json     — Organization members, goals, config"
[ -f "$ENV_FILE" ] && echo "    .env                     — API keys, Docker settings"
echo "    conversations.db          — SQLite DB with facts, knowledge, messages"
echo ""

echo "  Services:"
D_PROJECT_VAL=$(grep '^COMPOSE_PROJECT_NAME=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "corporate-bot")
D_PORT_VAL=$(grep '^BOT_PORT=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "8000")
echo "    Bot:       docker logs ${D_PROJECT_VAL:-corporate-bot}"
echo "    WA Bridge: docker logs ${D_PROJECT_VAL:-corporate-bot}-wa-bridge"
echo "    Camoufox:  docker logs ${D_PROJECT_VAL:-corporate-bot}-camofox"
echo "    Health:    curl http://localhost:${D_PORT_VAL:-8000}/health"
echo ""

echo -e "  ${BOLD}Multi-instance note:${NC}"
echo "    Each organization runs from its own fork with its own .env and"
echo "    org_config.json. Multiple bots can run on the same server"
echo "    using different COMPOSE_PROJECT_NAME and BOT_PORT values."
echo ""

echo -e "  ${BOLD}Useful commands:${NC}"
echo "    docker compose logs -f bot-core    # Follow bot logs"
echo "    docker compose restart bot-core    # Restart bot"
echo "    docker compose down                # Stop everything"
echo "    docker compose up -d               # Start everything"
echo ""
success "Happy botting!"
