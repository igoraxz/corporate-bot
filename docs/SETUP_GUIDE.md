# Corporate Bot — Comprehensive Setup Guide

From bare server to running bot with full auth, tunnels, and employee onboarding.

**Time estimate**: ~45 min for a basic Telegram-only deployment, ~90 min with Teams + staging.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Host Setup](#2-host-setup)
3. [Repository Setup](#3-repository-setup)
4. [Environment Configuration](#4-environment-configuration)
5. [Organization Config](#5-organization-config)
6. [Docker Build & Start](#6-docker-build--start)
7. [Public HTTPS Endpoint](#7-public-https-endpoint)
8. [Telegram Bot Setup](#8-telegram-bot-setup)
9. [MS Teams Setup](#9-ms-teams-setup-optional)
10. [First Boot & Admin Initialization](#10-first-boot--admin-initialization)
11. [Claude OAuth](#11-claude-oauth)
12. [Google Workspace](#12-google-workspace-optional)
13. [Microsoft 365 Mail/Calendar](#13-microsoft-365-mailcalendar-optional)
14. [Staging Environment](#14-staging-environment-optional)
15. [Deploy Watcher](#15-deploy-watcher-optional)
16. [Backup](#16-backup-optional)
17. [Security Hardening](#17-security-hardening)
18. [Ports & Networking](#18-ports--networking)
19. [Troubleshooting](#19-troubleshooting)

---

## 1. Prerequisites

**Server requirements:**
- Ubuntu 22.04+ (or any Linux with Docker support)
- 4+ GB RAM (8 GB recommended — Claude SDK + browser use memory)
- 20+ GB disk (Docker images + data volumes)
- Public IP OR Cloudflare account (for tunnels)

**Software:**
- Docker Engine 24+ with Compose v2 (`docker compose` — NOT `docker-compose`)
- git
- curl

**Accounts needed (minimum):**
- [Anthropic Claude](https://console.anthropic.com/) — Claude subscription or API key
- [Telegram](https://telegram.org/) — for TG Bot API (recommended, simplest)
- [GitHub](https://github.com/) — to clone the repo

**Optional accounts (enable more features):**
- [Azure AD](https://portal.azure.com/) — for MS Teams integration
- [Google Cloud Console](https://console.cloud.google.com/) — for Gmail/Calendar
- [Cloudflare](https://dash.cloudflare.com/) — for persistent HTTPS tunnels
- [Gemini API](https://ai.google.dev/) — for document analysis + voice transcription

---

## 2. Host Setup

### Create the bot user

The bot runs as a non-root user (`botuser`, UID 10001) inside Docker. Create a matching
host user for file ownership consistency:

```bash
# Create botuser (no login shell — service account only)
sudo useradd -r -m -s /bin/bash -u 10001 botuser
sudo usermod -aG docker botuser

# Create working directory
sudo mkdir -p /home/botuser
sudo chown botuser:botuser /home/botuser
```

### Firewall

Only the bot's HTTP port needs to be accessible. If using Cloudflare Tunnel, no inbound
ports are needed at all — the tunnel connects outbound.

```bash
# If using direct port exposure (no tunnel):
sudo ufw allow 8000/tcp    # or your chosen BOT_PORT

# If using Cloudflare Tunnel:
# No inbound rules needed — tunnel connects outbound on 443
```

---

## 3. Repository Setup

### Clone the repo

```bash
# Switch to botuser
sudo -iu botuser

# Option A: Fork on GitHub, then clone your fork
git clone git@github.com:your-org/your-bot.git ~/my-bot
cd ~/my-bot

# Option B: Clone directly (if you have access)
git clone git@github.com:your-org/corporate-bot.git ~/my-bot
cd ~/my-bot
```

### Deploy key (recommended for CI/CD)

If the repo is private, create a dedicated deploy key:

```bash
# Generate deploy key
ssh-keygen -t ed25519 -C "bot-deploy" -f ~/.ssh/id_ed25519_bot -N ""

# Show public key — add to GitHub repo → Settings → Deploy keys (enable write access)
cat ~/.ssh/id_ed25519_bot.pub

# Add SSH config
cat >> ~/.ssh/config << 'EOF'
Host github-bot
    HostName github.com
    IdentityFile ~/.ssh/id_ed25519_bot
    StrictHostKeyChecking accept-new
EOF

# Set remote to use the alias
git remote set-url origin git@github-bot:your-org/your-bot.git
```

---

## 4. Environment Configuration

```bash
cd ~/my-bot
cp .env.example .env
nano .env    # or your preferred editor
```

### Required variables

| Variable | Example | Description |
|----------|---------|-------------|
| `COMPOSE_PROJECT_NAME` | `corporate-bot` | Docker container name. **Must be unique per bot instance.** |
| `BOT_PORT` | `8200` | Host port to expose. Default: 8000. |
| `BOT_ENV` | `production` | `production` or `staging` |
| `ADMIN_USERS` | `telegram:123456789` | Admin users. Format: `telegram:<user_id>` or `teams:<aad_id>`. Comma-separated. |
| `TG_MODE` | `bot` | `bot` for Bot API (corporate). `userbot` for Pyrogram (personal). |
| `TG_BOT_TOKEN` | `7123456:AAF...` | From @BotFather (required if TG_MODE=bot) |
| `TG_WEBHOOK_URL` | `https://bot.example.com` | Public HTTPS URL (NO trailing `/webhook`). |
| `TG_WEBHOOK_SECRET` | `a1b2c3...` | Generate: `openssl rand -hex 32` |

### Optional variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TG_CHAT_ID` | `0` | Primary team group chat ID. Set to `0` to disable. |
| `ADMIN_USERS` | *(empty)* | Format: `123456789:John,987654321:Jane`. Auto-populated from org_config if empty. |
| `CLAUDE_CODE_OAUTH_TOKEN` | *(empty)* | Not needed if using `/claude-auth` flow (recommended). |
| `GEMINI_API_KEY` | *(empty)* | For document analysis, voice transcription, image generation. |
| `TOKEN_ENCRYPTION_KEY` | *(empty)* | For encrypting user OAuth tokens. Generate: `openssl rand -base64 32` |
| `RBAC_PRIMARY` | `true` | RBAC as primary access control. Keep `true` for corporate. |
| `TEAMS_ENABLED` | `false` | Enable MS Teams integration. |
| `TEAMS_APP_ID` | *(empty)* | Azure AD Application ID (for Teams). |
| `TEAMS_APP_SECRET` | *(empty)* | Azure AD client secret (for Teams). |
| `TEAMS_TENANT_ID` | *(empty)* | Azure AD tenant ID (for Teams). |
| `DESKTOP_ENABLED` | `false` | Enable Mac desktop relay (personal use only). |
| `VAPI_API_KEY` | *(empty)* | For AI phone calls via Vapi. |
| `MODEL_PRIMARY` | `claude-sonnet-4-6` | Default Claude model. |
| `MAX_BUDGET_DEFAULT` | `5` | Session budget cap in USD (Sonnet). |
| `MAX_BUDGET_HEAVY` | `50` | Session budget cap in USD (Opus). |

### ⚠️ Common pitfalls

- **`TG_CHAT_ID=`** (empty string) causes a startup crash. Set to `0` if you don't have a group yet.
- **`TG_WEBHOOK_URL`** must NOT include `/webhook` — the code appends `/webhook/telegram` automatically.
- **`BOT_PORT`** must be set as a shell environment variable for `docker compose`, not just in `.env`.
  Run: `BOT_PORT=8200 docker compose -p my-bot up -d`
- **`ADMIN_USERS`** auto-populates from `org_config.json` if left empty, but only if the org config
  has `is_admin: true` on member entries.

---

## 5. Organization Config

Create `data/org_config.json` to define your organization:

```json
{
  "org_name": "YourOrganization",
  "bot_name": "Corporate Bot",
  "timezone": "Europe/London",
  "members": {
    "admins": [
      {
        "name": "Admin",
        "role": "administrator",
        "telegram_id": 123456789,
        "is_admin": true
      }
    ],
    "team_members": [
      {
        "name": "Alice",
        "department": "Engineering",
        "telegram_id": 123456789
      }
    ]
  },
  "goals": [
    "Assist team with research and operations",
    "Manage organizational knowledge domains",
    "Support development workflows"
  ]
}
```

**Key fields:**
- `is_admin: true` — marks user as admin (auto-assigned `bot_admin` RBAC role on first boot)
- `telegram_id` — required for Telegram auth. Find yours by messaging [@userinfobot](https://t.me/userinfobot)
- `teams_aad_id` — Azure AD Object ID (for Teams deployments)

**Legacy format**: The config also supports `members.parents`/`members.children` keys for
backward compatibility with personal deployments. Corporate deployments should use the flat
`members` array shown above.

---

## 6. Docker Build & Start

```bash
cd ~/my-bot

# Build and start (note: BOT_PORT as shell env var)
BOT_PORT=8200 docker compose -p my-bot up -d --build bot-core

# Check health (wait ~30s for startup)
curl http://localhost:8200/health

# Check logs
docker logs my-bot --tail 50
```

**Expected health output:**
```json
{"status":"ok","version":"agent-sdk","commit":"...","tg_connected":true,...}
```

**Expected log lines on first boot:**
```
[RBAC] Initialized (dual enforcement, log-only)
RBAC builtin role seeded: bot_admin (N policies)
RBAC builtin role seeded: employee (N policies)
RBAC: auto-assigned bot_admin to telegram:123456789 (first boot)
[TG] Bot API client initialized
[TG] Webhook set: https://bot.example.com/webhook/telegram
[TG] Bot menu commands registered
Startup smoke test passed
```

### ⚠️ Build troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `seccomp=./seccomp-bwrap.json` not found | Missing seccomp profile | Copy from repo: `cp seccomp-bwrap.json .` (should be in repo root) |
| Port already in use | Another service on the port | Change `BOT_PORT` in `.env` |
| Permission denied on volumes | UID mismatch | Ensure host `botuser` has UID 10001 |

---

## 7. Public HTTPS Endpoint

The bot needs a public HTTPS URL for Telegram/Teams webhooks. Choose one:

### Option A: Cloudflare Tunnel (recommended — free, persistent)

```bash
# Install cloudflared
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared

# Login to Cloudflare (opens browser)
cloudflared tunnel login

# Create named tunnel
cloudflared tunnel create my-bot

# Note the tunnel UUID, then create config
sudo mkdir -p /etc/cloudflared
sudo tee /etc/cloudflared/config.yml << EOF
tunnel: <TUNNEL_UUID>
credentials-file: /home/botuser/.cloudflared/<TUNNEL_UUID>.json

ingress:
  - hostname: bot.example.com
    service: http://localhost:8200
  - service: http_status:404
EOF

# Add DNS route
cloudflared tunnel route dns my-bot bot.example.com

# Install as systemd service (runs on boot)
sudo cloudflared service install
sudo systemctl enable cloudflared
sudo systemctl start cloudflared

# Verify
curl https://bot.example.com/health
```

**Adding a second bot to the same tunnel** (e.g., you already have a tunnel for another service):

```yaml
# Just add another ingress rule to /etc/cloudflared/config.yml:
ingress:
  - hostname: existing-service.example.com
    service: http://localhost:8003
  - hostname: new-bot.example.com          # NEW
    service: http://localhost:8200          # NEW
  - service: http_status:404

# Add DNS route and restart:
cloudflared tunnel route dns <TUNNEL_NAME> new-bot.example.com
sudo systemctl restart cloudflared
```

### Option B: Tailscale Funnel

```bash
sudo tailscale funnel --bg 8200
```

⚠️ Requires Tailscale Funnel to be enabled on your tailnet (admin console).

### Option C: Nginx + Let's Encrypt

```bash
sudo apt install nginx certbot python3-certbot-nginx
sudo certbot --nginx -d bot.example.com
```

Then add proxy config to `/etc/nginx/sites-available/bot`:
```nginx
server {
    listen 443 ssl;
    server_name bot.example.com;
    location / {
        proxy_pass http://localhost:8200;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## 8. Telegram Bot Setup

### Create the bot via BotFather

1. Open Telegram, message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`
3. Name: `Corporate Bot` (or your bot's name)
4. Username: `your_org_bot` (must end in `bot`)
5. Copy the **token** → set as `TG_BOT_TOKEN` in `.env`

### Configure bot settings

```
/setprivacy → @your_bot → Disable
```

This allows the bot to see all group messages (not just commands).

### Set webhook URL in .env

```
TG_WEBHOOK_URL=https://bot.example.com
```

⚠️ Do NOT append `/webhook` — the code adds `/webhook/telegram` automatically.

### Rebuild after .env changes

```bash
cd ~/my-bot
docker compose -p my-bot up -d --force-recreate bot-core
```

### Test

1. Open Telegram, find your bot by username
2. Send `/start` then `Hello`
3. Bot should respond with a greeting

### Create a team group (optional)

1. Create a new TG group
2. Add your bot to the group
3. Make the bot admin (so it can see all messages and manage topics)
4. Enable Topics in group settings (optional, for forum-style channels)
5. Get the group chat ID from bot logs or by sending a message and checking:
   ```bash
   docker logs my-bot --tail 5 | grep chat_id
   ```
6. Set `TG_CHAT_ID=<negative_number>` in `.env` and recreate the container

---

## 9. MS Teams Setup (optional)

Skip this section if using Telegram only.

### 9a. Register Azure AD Application

1. Go to [Azure Portal → App registrations](https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps)
2. Click "New registration"
3. Name: "Corporate Bot"
4. Supported account types: "Single tenant"
5. Note the **Application (client) ID** and **Directory (tenant) ID**
6. Go to "Certificates & secrets" → New client secret → note the **secret value**

### 9b. Configure API Permissions

Add these delegated permissions under Microsoft Graph:
- `Mail.Read`
- `Calendars.ReadWrite`
- `User.Read`

Click "Grant admin consent".

### 9c. Set .env variables

```
TEAMS_ENABLED=true
TEAMS_APP_ID=<application-client-id>
TEAMS_APP_SECRET=<client-secret-value>
TEAMS_TENANT_ID=<directory-tenant-id>
```

### 9d. Create and deploy Teams App Package

See the existing `teams-app/` directory. Edit `manifest.json`, add icons, zip, and
upload to Teams Admin Center.

### 9e. Set messaging endpoint

In Azure Bot Service → Configuration → Messaging endpoint:
```
https://bot.example.com/api/teams-messages
```

---

## 10. First Boot & Admin Initialization

On first startup, the bot automatically:

1. **Creates database tables** — SQLite DB at `/app/data/conversations.db`
2. **Seeds RBAC roles** — `bot_admin`, `employee`, `guest` with baseline policies
3. **Auto-assigns `bot_admin`** to all users listed in `ADMIN_USERS` env var
   (or users with `is_admin: true` in org_config.json)
4. **Registers webhook** with Telegram (if `TG_WEBHOOK_URL` is set)
5. **Registers bot menu commands** in Telegram UI

**Verify admin role was assigned:**
```bash
docker exec my-bot sqlite3 /app/data/conversations.db \
  "SELECT * FROM rbac_assignments;"
```

You should see an entry like:
```
user | telegram:123456789 | bot_admin | system:seed | 2026-...
```

If the assignment is missing, check:
- `ADMIN_USERS` format: must be `telegram:123456789` (with the `telegram:` prefix)
- org_config.json: must have `"is_admin": true` on the admin entry
- Logs: `docker logs my-bot | grep RBAC`

---

## 11. Claude OAuth

The bot needs Claude API access. The recommended flow uses OAuth profiles:

### Via /claude-auth (in-chat, recommended)

1. DM your bot on Telegram
2. Send: `/claude-auth main`
3. Bot responds with an OAuth URL
4. Open the URL in your browser, authorize with your Anthropic account
5. Copy the authorization code
6. Paste it back in the Telegram chat
7. Bot saves the token as profile "main" and auto-sets it as default

**First profile is auto-defaulted** — no manual step needed for the first auth.

### Via environment variable (alternative)

Set `CLAUDE_CODE_OAUTH_TOKEN=<token>` in `.env` and recreate the container.
This is less flexible (no profile management, no auto-refresh).

### ⚠️ Pitfalls

- Label `default` is reserved — use `main`, `primary`, or any other name
- The "Mac sync: failed" message is harmless when `DESKTOP_ENABLED=false`
- After auth, the bot needs a session reset to pick up the new token:
  send `/reset` to the bot

---

## 12. Google Workspace (optional)

For Gmail search and Google Calendar integration:

1. Create OAuth credentials in [Google Cloud Console](https://console.cloud.google.com/)
2. Set in `.env`:
   ```
   GOOGLE_OAUTH_CLIENT_ID=<client-id>
   GOOGLE_OAUTH_CLIENT_SECRET=<client-secret>
   ```
3. Run the credential setup script:
   ```bash
   docker exec -it my-bot python3 /app/scripts/setup_google_credentials.py
   ```
4. Follow the OAuth flow in your browser

---

## 13. Microsoft 365 Mail/Calendar (optional)

For Outlook mail (read-only) and calendar (read-write) integration:

1. Set in `.env`:
   ```
   MS365_MCP_CLIENT_ID=<azure-app-id>
   MS365_MCP_TENANT_ID=<azure-tenant-id>
   MS365_MCP_CLIENT_SECRET=<azure-client-secret>
   ```
2. DM the bot: `/m365-auth`
3. Bot provides a device code URL + code
4. Open the URL, sign in, enter the code
5. Bot polls automatically and saves the token
6. Send `/reset` to restart the session with fresh M365 access

---

## 14. Staging Environment (optional)

Run an isolated staging instance for testing changes before production.

### Setup

```bash
# Clone a separate copy
sudo -u botuser git clone git@github-bot:your-org/your-bot.git \
  /home/botuser/my-bot-staging

# Symlink shared secrets
ln -s /home/botuser/my-bot/.env /home/botuser/my-bot-staging/.env

# Start staging (MUST use -p flag for isolation)
# No BOT_PORT needed — staging has ports: [] (no host ports, Docker network only)
cd /home/botuser/my-bot-staging
docker compose \
  -p my-bot-staging \
  -f docker-compose.yml -f docker-compose.staging.yml \
  up -d --build bot-core
```

### ⚠️ Critical pitfalls

- **`-p` flag is REQUIRED** — without it, staging and prod share the same Docker project,
  starting one KILLS the other
- **`BOT_PORT` must be shell env var** — Docker Compose reads it from shell environment,
  NOT from the `.env.staging` file
- **Run as botuser** — permission errors occur if run as a different user

---

## 15. Deploy Watcher (optional)

Auto-deploy on `git push` with health check + auto-rollback:

```bash
# Install watcher outside repo (security isolation)
sudo mkdir -p /opt/my-bot-watcher
sudo cp deploy/host-watcher.sh /opt/my-bot-watcher/host-watcher.sh
sudo chown -R botuser:botuser /opt/my-bot-watcher
sudo chmod 755 /opt/my-bot-watcher/host-watcher.sh

# Create systemd service
sudo cp deploy/corp-bot-watcher.service /etc/systemd/system/my-bot-watcher.service
# Edit the service file: update paths and COMPOSE_PROJECT_NAME
sudo systemctl daemon-reload
sudo systemctl enable my-bot-watcher
sudo systemctl start my-bot-watcher
```

The watcher:
1. Polls `deploy/triggers/` for trigger files
2. Runs `git pull` + `docker compose build` + `docker compose up -d`
3. Checks `/health` endpoint (120s timeout)
4. On failure: captures logs → auto-rolls back to last healthy commit

---

## 16. Backup (optional)

Encrypted hourly backups with auto-retention:

```bash
# Install dependencies
sudo apt install p7zip-full

# Generate encryption password
openssl rand -base64 24 > /home/botuser/my-bot/.backup-password
chmod 600 /home/botuser/my-bot/.backup-password

# Install backup script outside repo
sudo mkdir -p /opt/my-bot-backup
sudo cp scripts/backup.sh /opt/my-bot-backup/backup.sh
sudo chown -R botuser:botuser /opt/my-bot-backup
sudo chmod 755 /opt/my-bot-backup/backup.sh

# Test
sudo -u botuser /opt/my-bot-backup/backup.sh --dry-run

# Add to cron (hourly, self-throttles to 2h)
sudo -u botuser crontab -e
# Add: 0 * * * * /opt/my-bot-backup/backup.sh >> /home/botuser/my-bot/deploy/backup.log 2>&1
```

---

## 17. Security Hardening

The Docker container uses defense-in-depth:

- **`cap_drop: ALL`** — no Linux capabilities
- **`no-new-privileges: true`** — prevents privilege escalation
- **`seccomp=./seccomp-bwrap.json`** — custom seccomp profile for bubblewrap sandboxing
- **`apparmor=unconfined`** — required for bwrap mount operations
- **Non-root user** — `botuser` (UID 10001) inside the container
- **RBAC** — role-based access control for all tool operations
- **Bubblewrap sandbox** — kernel-level namespace isolation for code execution

### seccomp profile

The `seccomp-bwrap.json` file must exist in the repo root. It allows the syscalls
needed for bubblewrap (user namespaces, mount). Without it, container startup fails with:
```
no such file or directory: seccomp-bwrap.json
```

---

## 18. Ports & Networking

| Port | Bind | Service | Public? |
|------|------|---------|---------|
| `BOT_PORT` (default 8000) | `0.0.0.0` | Bot HTTP API | Via tunnel only |
| 9377 | Docker internal | Camoufox browser | No |
| 3001 | Docker internal | Playwright MCP | No |

**All inter-service communication** uses Docker's default bridge network.
The bot port should be behind a tunnel (Cloudflare/Tailscale) or reverse proxy — never
exposed directly to the internet without HTTPS.

---

## 19. Troubleshooting

Common issues encountered during deployment and operation:

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ValueError: invalid literal for int()` on startup | `TG_CHAT_ID=` (empty string) | Set `TG_CHAT_ID=0` |
| Bot doesn't respond to messages | Webhook URL wrong or not registered | Check logs for `[TG] Webhook set:`. URL must NOT include `/webhook`. |
| `Not logged in · Please run /login` | No Claude OAuth token | Run `/claude-auth main` in DM with the bot |
| `Access denied (RBAC): No roles assigned` | Admin RBAC role not seeded | Check `ADMIN_USERS` format is `telegram:123456789`. Restart container. |
| `/harnesses` crashes with AttributeError | Stale harness JSON with removed fields | Delete `/app/data/harnesses.json` and restart |
| `Mac sync: failed` after `/claude-auth` | `DESKTOP_ENABLED=false` (expected) | Harmless — Mac sync only runs when relay is enabled |
| OAuth profile saved but bot still says "Not logged in" | Profile not set as default | First profile auto-defaults since v3.0. For manual fix: `/reset` |
| Health check passes but bot doesn't process messages | RBAC blocking telegram_send_message | Check admin role assignment in DB |
| Build fails with `seccomp` error | Missing seccomp profile | Ensure `seccomp-bwrap.json` exists in repo root |
| Container keeps restarting | SyntaxError or import error | Check logs: `docker logs my-bot --tail 50` |
| `502 Bad Gateway` from Cloudflare | Bot container not running or wrong port | Verify container is up and port matches tunnel config |
| Staging starts but kills prod | Missing `-p` flag | Always use `docker compose -p my-bot-staging` |

### Useful diagnostic commands

```bash
# Container status
docker ps -a | grep my-bot

# Recent logs
docker logs my-bot --tail 100

# Health check
curl http://localhost:8200/health | python3 -m json.tool

# Deep health check (exercises pre-SDK paths)
curl http://localhost:8200/health/deep

# RBAC status
docker exec my-bot sqlite3 /app/data/conversations.db \
  "SELECT subject_type, subject_id, role_id FROM rbac_assignments;"

# OAuth profiles
docker exec my-bot ls -la /app/data/oauth-profiles/

# Webhook info
curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
```

---

## Quick Start Checklist

For the fastest path to a working bot (Telegram only):

- [ ] Create `botuser` on host
- [ ] Clone repo, `cp .env.example .env`
- [ ] Set in `.env`: `COMPOSE_PROJECT_NAME`, `TG_MODE=bot`, `TG_BOT_TOKEN`, `TG_WEBHOOK_SECRET`, `ADMIN_USERS`, `TG_CHAT_ID=0`
- [ ] Create `data/org_config.json` with admin entry
- [ ] Create TG bot via @BotFather, set privacy off
- [ ] Set up Cloudflare tunnel or public HTTPS endpoint
- [ ] Set `TG_WEBHOOK_URL` in `.env` (base URL only, no `/webhook`)
- [ ] `BOT_PORT=8200 docker compose -p my-bot up -d --build bot-core`
- [ ] Wait 30s, check health: `curl http://localhost:8200/health`
- [ ] DM the bot: `/claude-auth main` → complete OAuth flow
- [ ] Send `/reset` then `Hello` — bot should respond 🎉
