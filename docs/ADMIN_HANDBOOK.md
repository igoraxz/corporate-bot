# Admin Handbook — Corporate Bot Setup & Operations

Complete guide for deploying, configuring, and operating a Corporate Bot instance.
Covers both Teams-only and dual-platform (Teams + Telegram) deployments.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Server Setup](#2-server-setup)
3. [Azure AD Setup (Teams + M365)](#3-azure-ad-setup)
4. [Environment Configuration](#4-environment-configuration)
5. [First Boot & Verification](#5-first-boot--verification)
6. [Chat Registration](#6-chat-registration)
7. [RBAC & Permissions](#7-rbac--permissions)
8. [Harness Configuration](#8-harness-configuration)
9. [Knowledge Domains](#9-knowledge-domains)
10. [Employee Onboarding](#10-employee-onboarding)
11. [Scheduled Tasks](#11-scheduled-tasks)
12. [Backup & Restore](#12-backup--restore)
13. [Self-Upgrade (Bug Fixes & Features)](#13-self-upgrade)
14. [Troubleshooting](#14-troubleshooting)
15. [Security Checklist](#15-security-checklist)

---

## 1. Prerequisites

### Hardware
- Linux server (Ubuntu 22.04+ recommended)
- 4GB+ RAM, 2+ CPU cores
- 20GB+ disk (SSD preferred)
- Public HTTPS endpoint (for Teams webhook)

### Accounts
- **Azure AD** — App registration (free tier)
- **Claude AI** — Subscription with API access (claude.ai/claude-code)
- **Google Gemini** — API key for RAG + media descriptions (free tier available at aistudio.google.com)
- **GitHub** (optional) — For knowledge domain sync

### Software
- Docker + Docker Compose v2
- Git
- Public HTTPS proxy (Cloudflare Tunnel, nginx + Let's Encrypt, or similar)

---

## 2. Server Setup

### 2.1 Create bot user
```bash
sudo useradd -m -s /bin/bash -u 10001 botuser
sudo usermod -aG docker botuser
```

### 2.2 Clone repository
```bash
sudo -u botuser -i
git clone <your-repo-url> ~/corporate-bot
cd ~/corporate-bot
```

### 2.3 Configure environment
```bash
cp .env.example .env
# Edit .env with your values (see Section 4)
nano .env
```

### 2.4 Generate encryption key
```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Copy output to TOKEN_ENCRYPTION_KEY in .env
```

### 2.5 Build and start
```bash
docker compose build
docker compose up -d
```

### 2.6 Verify health
```bash
curl http://localhost:8000/health
# Expected: {"status": "ok", "model": "claude-sonnet-4-6", ...}
```

### 2.7 Public HTTPS endpoint (required)

The bot needs a public HTTPS URL for:
- **Teams webhook** (`/api/teams-messages`)
- **Telegram webhook** (`/webhook/telegram`)
- **OAuth callbacks** (`/auth/google/callback`, `/auth/m365/callback`) — Google and M365 auth flows redirect here after user authorization

**Simplest option — Cloudflare Tunnel:**
```bash
cloudflared tunnel --url http://localhost:8000 --hostname bot.your-domain.com
```

**Alternative — nginx + Let's Encrypt:**
```nginx
server {
    server_name bot.your-domain.com;
    listen 443 ssl;
    ssl_certificate /etc/letsencrypt/live/bot.your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bot.your-domain.com/privkey.pem;

    location / { proxy_pass http://localhost:8000; }
}
```

Set in `.env`:
```bash
TG_WEBHOOK_URL=https://bot.your-domain.com    # For Telegram
WEBHOOK_BASE_URL=https://bot.your-domain.com  # For OAuth callbacks + other webhooks
```

The Teams messaging endpoint URL is configured in Azure Bot Service (Section 3.4), not `.env`.

---

## 3. Azure AD Setup (Teams + M365)

### 3.1 Register Azure AD App
1. Go to [Azure Portal](https://portal.azure.com) > Azure Active Directory > App registrations
2. Click **New registration**
   - Name: "Corporate Bot"
   - Supported account types: **Single tenant** (your organization only)
   - Redirect URI: leave blank
3. Note the **Application (client) ID** and **Directory (tenant) ID**

### 3.2 Create Client Secret
1. In the app registration > Certificates & secrets > New client secret
2. Description: "Bot secret", Expiry: 24 months
3. Copy the secret value immediately (shown only once)

### 3.3 API Permissions
1. API permissions > Add a permission > Microsoft Graph > Delegated permissions
2. Add: `Mail.Read`, `Calendars.ReadWrite`, `User.Read`, `offline_access`
3. Click **Grant admin consent** for your organization

### 3.4 Create Azure Bot Service
1. Azure Portal > Create a resource > Search "Azure Bot"
2. Bot handle: "corporate-bot"
3. Pricing: Free (F0)
4. Microsoft App ID: Use the app registration from step 3.1
5. Messaging endpoint: `https://your-domain.com/api/teams-messages`

### 3.5 Teams Channel
1. In the Bot Service > Channels > Microsoft Teams
2. Enable the Teams channel

### 3.6 Teams App Manifest
1. Create a `teams-app/` directory with `manifest.json`:
```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/teams/v1.16/MicrosoftTeams.schema.json",
  "manifestVersion": "1.16",
  "version": "1.0.0",
  "id": "YOUR-APP-ID",
  "developer": {
    "name": "Your Company",
    "websiteUrl": "https://your-company.com",
    "privacyUrl": "https://your-company.com/privacy",
    "termsOfUseUrl": "https://your-company.com/terms"
  },
  "name": { "short": "Corporate Bot", "full": "Corporate AI Assistant" },
  "description": {
    "short": "AI assistant for your organization",
    "full": "AI-powered assistant for team operations, planning, and knowledge management."
  },
  "icons": { "outline": "outline.png", "color": "color.png" },
  "accentColor": "#4F6BED",
  "bots": [{
    "botId": "YOUR-APP-ID",
    "scopes": ["personal", "team", "groupChat"],
    "supportsFiles": true
  }],
  "permissions": ["identity", "messageTeamMembers"],
  "validDomains": ["your-domain.com"]
}
```
2. Add icons (192x192 color.png, 32x32 outline.png)
3. Zip the manifest: `cd teams-app && zip ../corporate-bot.zip *`
4. Upload to Teams Admin Center > Manage apps > Upload custom app

### 3.7 Update .env
```bash
TEAMS_ENABLED=true
TEAMS_APP_ID=<from step 3.1>
TEAMS_APP_SECRET=<from step 3.2>
TEAMS_TENANT_ID=<from step 3.1>
ROOT_ADMIN=teams:<your-aad-object-id>
```

To find your AAD Object ID: Azure Portal > Users > Your profile > Object ID

---

## 3A. Telegram Setup (Alternative to Teams)

If deploying Telegram-only (no MS Teams), skip Section 3 and do this instead.

### 3A.1 Create Bot via BotFather
1. Open [@BotFather](https://t.me/BotFather) in Telegram
2. Send `/newbot`, choose a name and username (must end in `bot`)
3. Copy the **bot token** (format: `123456:ABC-DEF...`)

### 3A.2 Configure Bot Settings
In BotFather:
- `/setprivacy` → **Disable** (bot sees all group messages)
- `/setjoingroups` → **Enable** (bot can be added to groups)

### 3A.3 Set Up Webhook
Your bot needs a public HTTPS endpoint. Options:
- **Cloudflare Tunnel**: `cloudflared tunnel --url http://localhost:8000` (simplest)
- **nginx + Let's Encrypt**: Traditional reverse proxy
- **Tailscale Funnel**: `tailscale funnel 8000` (if using Tailscale)

### 3A.4 Find Your User ID
Message [@userinfobot](https://t.me/userinfobot) — it replies with your numeric user ID.

### 3A.5 Update .env
```bash
TG_MODE=bot
TG_BOT_TOKEN=<from step 3A.1>
TG_WEBHOOK_URL=https://your-domain.com/webhook/telegram
TG_WEBHOOK_SECRET=$(openssl rand -hex 32)
ROOT_ADMIN=telegram:<your-user-id>
ADMIN_USERS=telegram:<your-user-id>
```

---

## 4. Environment Configuration

See `.env.example` for all variables with descriptions.

### Minimum viable config (Teams-only):
```bash
# Auth
CLAUDE_CODE_OAUTH_TOKEN=<your-token>
TOKEN_ENCRYPTION_KEY=<generated-fernet-key>
GEMINI_API_KEY=<your-gemini-key>

# Teams
TEAMS_ENABLED=true
TEAMS_APP_ID=<azure-app-id>
TEAMS_APP_SECRET=<azure-app-secret>
TEAMS_TENANT_ID=<azure-tenant-id>

# Admin
ROOT_ADMIN=teams:<your-aad-object-id>
ADMIN_USERS=teams:<your-aad-object-id>
BOT_ADMIN_AAD_IDS=<your-aad-object-id>

# M365 (uses same Azure app)
MS365_EMAIL=admin@yourcompany.com

# Docker
COMPOSE_PROJECT_NAME=corporate-bot
TZ=Europe/London
```

### Minimum viable config (Telegram-only):
```bash
# Auth
CLAUDE_CODE_OAUTH_TOKEN=<your-token>
TOKEN_ENCRYPTION_KEY=<generated-fernet-key>
GEMINI_API_KEY=<your-gemini-key>

# Telegram
TG_MODE=bot
TG_BOT_TOKEN=<bot-token-from-botfather>
TG_WEBHOOK_URL=https://your-domain.com/webhook/telegram
TG_WEBHOOK_SECRET=<random-hex-string>

# Admin
ROOT_ADMIN=telegram:<your-user-id>
ADMIN_USERS=telegram:<your-user-id>

# Docker
COMPOSE_PROJECT_NAME=corporate-bot
TZ=Europe/London
```

### Port Configuration
The bot exposes two HTTP ports:
- **API port** (`BOT_PORT`, default 8000): Main bot API + webhooks
- **Reports port** (`REPORTS_PORT`, default 8210): Hosted HTML reports

Docker maps host port to container port 8000:
```yaml
ports:
  - "${BOT_PORT:-8000}:8000"  # API
```

**Multi-instance example** (prod + staging on same host):
| Instance | BOT_PORT | REPORTS_PORT | Compose Project |
|----------|----------|-------------|-----------------|
| Production | 8200 | 8210 | corporate-bot |
| Staging | 8300 | 8310 | corporate-bot-staging |

Set in `.env`:
```bash
BOT_PORT=8200
REPORTS_PORT=8210
```

### Staging Environment
Staging is a separate Docker instance for testing changes before production.

**Setup staging:**
```bash
# Clone repo to staging directory
git clone <repo-url> ~/corporate-bot-staging
cd ~/corporate-bot-staging

# Create staging .env (copy from prod, change ports)
cp ../corporate-bot/.env .env
# Edit: BOT_PORT=8300, REPORTS_PORT=8310, BOT_ENV=staging
```

**Start staging:**
```bash
BOT_PORT=8300 docker compose -p corporate-bot-staging up -d
```

**Staging watcher** (auto-rebuilds on git push):
- Install: copy `deploy/staging-watcher.sh` to `/opt/<project>-staging-watcher/`
- Configure systemd service with `Environment=BOT_PORT=8300`
- The watcher monitors `deploy/staging-triggers/` for rebuild requests

---

## 5. First Boot & Verification

### 5.1 Start the bot
```bash
docker compose up -d
docker compose logs -f bot-core  # Watch logs
```

### 5.2 Expected startup sequence
```
[Auth] OAuth token tables initialized
[Auth] Universal credential store initialized
[RBAC] Seeded 4 built-in roles
[Domains] Seeded 3 canonical domains (core, admin, teamwork)
[Harness] 6 canonical harnesses initialized
[Teams] Bot Framework adapter initialized
Startup smoke test passed
```

### 5.3 Verify Teams connectivity
1. Open Microsoft Teams
2. Search for "Corporate Bot" in the app catalog
3. Install the app and send "hi" in a 1:1 chat
4. Bot should respond with a greeting

### 5.4 Set up M365 access
In your Teams DM with the bot:
```
/m365-auth
```
Follow the device code flow to connect your Microsoft 365 account.

### 5.5 Run initial health check
```
/status
```
Should show: model, harness, connected accounts, and system health.

---

## 6. Chat Registration

The bot only responds in registered chats. Register chats via admin DM:

### Register a Teams group chat
```
manage_chat(action="register", chat_key="teams:<conversation-id>",
            title="Engineering", mode="active_plus", harness="teamwork")
```

### Register a Telegram group chat
```
manage_chat(action="register", chat_key="telegram:<chat-id>",
            title="Engineering", mode="active_plus", harness="teamwork")
```
Note: Telegram group IDs are negative numbers (e.g., `telegram:-1001234567890`).
For forum topics: `telegram:<chat-id>:<thread-id>`

### Chat modes
- **active_plus** — Full UX: streaming, placeholders, all tools (default)
- **active** — Bot participates, no streaming (for multi-party/external chats)
- **silent** — Listen only, store messages (monitoring)

### View registered chats
```
/chats
```

---

## 7. RBAC & Permissions

### Built-in roles
| Role | Access Level | Use Case |
|------|-------------|----------|
| `bot_admin` | Full access to all tools, credentials, deploy | Admin DMs, coding chats |
| `teamwork` | R/W teamwork domain, shared OAuth, team tools | Team group chats |
| `employee` | Own DM data, read teamwork, personal OAuth | Employee DMs |
| `external` | Restricted: no filesystem, no credentials, no PM | Third-party chats |

### Assign roles
Roles are auto-assigned when you register a chat with a harness:
```
manage_chat(action="register", chat_key="teams:<id>",
            title="Sales", mode="active_plus", harness="teamwork")
# → auto-assigns "teamwork" role
```

### Check permissions
```
/permissions
```

---

## 8. Harness Configuration

Harnesses control model, effort, budget, and domain access per chat.

### Canonical harnesses
| Harness | Model | Domains | Role | Use Case |
|---------|-------|---------|------|----------|
| `admin` | Opus | admin + teamwork | bot_admin | Admin DMs |
| `coding` | Opus | admin + teamwork | bot_admin | Coding sessions |
| `teamwork` | Sonnet | teamwork | teamwork | Team group chats |
| `personal` | Sonnet | (none) | employee | Employee DMs |
| `external_chat` | Sonnet | (none) | external | Third-party chats |

### View harnesses
```
/harnesses
```

### Override per-chat
```
manage_harness(action="update", label="teamwork",
               fields={"model": "claude-opus-4-6[1m]", "max_turns": 100})
```

---

## 9. Knowledge Domains

Domains organize your company's knowledge for AI-powered search and RAG.

### Canonical domains
- **core** — Bot identity, base capabilities (always active, all chats)
- **admin** — Bot infrastructure, deploy history, operational runbooks (admin only)
- **teamwork** — Team knowledge, processes, business decisions (team chats)

### Set up GitHub knowledge sync

1. **Create a private GitHub repo** for your domain:
```bash
gh repo create yourorg/company-knowledge --private
```

2. **Register the domain** with GitHub source:
```
manage_domain(action="register", domain_id="teamwork",
              config_yaml="domain:\n  source:\n    type: github\n    repo: yourorg/company-knowledge\n    branch: main")
```

3. **Set up deploy keys** for secure sync:
```
manage_domain(action="setup_deploy_key", domain_id="teamwork")
```

4. **Trigger initial sync**:
```
manage_domain(action="sync", domain_id="teamwork")
```

5. **Push docs to the repo** — they'll be automatically indexed:
```bash
cd company-knowledge
echo "# Sales Process\n\nOur sales cycle is..." > sales-process.md
git add . && git commit -m "Add sales process" && git push
```

### Search knowledge
The bot automatically searches knowledge when relevant. You can also explicitly:
```
search_knowledge("sales process")
```

---

## 10. Employee Onboarding

### Steps for each new employee:

1. **Add to Teams** — Install the bot app in the employee's Teams

2. **Register their DM chat** — From admin DM:
```
manage_chat(action="register", chat_key="teams:<employee-aad-id>",
            title="John Smith", mode="active_plus", harness="personal")
```

3. **Employee authenticates** — In their DM with the bot:
```
/m365-auth     # Connect their Microsoft 365 account
/google-auth   # Connect their Google account (if applicable)
```

4. **Set up credentials** — The bot auto-grants credential access per harness

5. **Verify** — Employee sends "hi" and the bot responds with their personalized setup

### Remove employee access
```
manage_chat(action="unregister", chat_key="teams:<employee-aad-id>")
```

---

## 11. Scheduled Tasks

### Built-in admin tasks (auto-created)
- **Daily Briefing** — 7:30 AM weekdays (calendar + emails summary)
- **Email Monitor** — Every 3 hours (urgent email alerts)
- **Evening Prep** — 6:00 PM (next-day preparation)

### Create custom tasks
Tell the bot:
> Create a scheduled task that runs at 9am every Monday to summarize last week's team activity

Or via MCP:
```
manage_scheduled_task(action="add", name="Weekly Summary",
                      hour=9, minute=0, days=["monday"],
                      prompt="Summarize team activity from the past week")
```

### View tasks
```
/tasks
```
Or: `list_scheduled_tasks`

### Manage tasks
```
manage_scheduled_task(action="edit", task_id="<id>", enabled=false)   # Disable
manage_scheduled_task(action="delete", task_id="<id>")                 # Delete
```

---

## 12. Backup & Restore

### Automatic backups
The bot includes a backup script at `scripts/backup.sh` that:
- Creates WAL-safe SQLite snapshots
- Encrypts with AES-256 (7z)
- Supports remote destinations (Mac via SSH, GDrive via rclone)
- Retains: 12x2h + 7 daily + 4 weekly + monthly forever

### Set up hourly backups
```bash
# Install backup script
sudo cp scripts/backup.sh /opt/corporate-bot-backup/
sudo chmod +x /opt/corporate-bot-backup/backup.sh

# Add to crontab (every 2 hours)
echo "0 */2 * * * /opt/corporate-bot-backup/backup.sh" | sudo tee -a /etc/crontab
```

### Manual backup
```bash
/opt/corporate-bot-backup/backup.sh
```

### Restore from backup
```bash
scripts/restore.sh /path/to/backup.7z
```

---

## 13. Self-Upgrade (Bug Fixes & Features)

The bot can fix its own bugs and add features through the **coding chat**.

### How it works
1. Open a coding-capable chat (admin DM or `/coding` topic)
2. Describe the bug or feature you want
3. The bot researches the codebase, proposes a plan, gets your approval
4. Implements the change with full review pipeline (security + quality + architecture)
5. You review and say `/deploy` to ship

### Example: Fix a bug
```
User: The /status command shows the wrong timezone. Fix it.
Bot:  [researches code, finds the bug, proposes fix, implements, reviews]
Bot:  Ready to deploy. Send /deploy to ship.
User: /deploy
Bot:  Deploy #65 triggered. Rebuilding...
```

### Safety nets
- **Staging environment** — Changes tested on staging before production
- **Automated reviews** — Security, quality, and architecture agents review every change
- **Auto-rollback** — Failed deploys automatically rollback to last healthy commit
- **Pipeline gates** — Syntax check, import check, integration tests, QA scenarios

### Deploy process
1. Bot edits code in `/host-repo/` (git repo mounted in container)
2. Commits and pushes to GitHub
3. Runs `deploy_review` (6 automated checks)
4. Runs `staging_qa_run` (regression test suite)
5. You send `/deploy` to authorize
6. `deploy_bot` triggers Docker rebuild via host watcher
7. Health check verifies new build → live in ~60 seconds

---

## 13A. Reports Endpoint (Optional)

The bot can generate and host HTML reports (dashboards, audits, visualizations) at a separate HTTP port.

### Setup
Add to `.env`:
```bash
REPORTS_PORT=8210                    # Separate port for hosted reports
REPORTS_BASE_URL=https://reports.your-domain.com  # Public URL (proxy this port)
REPORTS_MAX_AGE_DAYS=365             # Auto-cleanup after 1 year
```

### How it works
- Bot generates reports via `save_report` tool → stored as HTML files
- Each report gets a unique UUID URL (unguessable)
- Served on `REPORTS_PORT` (separate from main API for security)
- Reports are per-chat scoped (each chat has its own report directory)

### Public access
To make reports accessible externally, proxy `REPORTS_PORT`:
```bash
# Cloudflare Tunnel example:
cloudflared tunnel --url http://localhost:8210 --hostname reports.your-domain.com

# Or nginx:
server {
    server_name reports.your-domain.com;
    location / { proxy_pass http://localhost:8210; }
}
```

Without a proxy, reports are only accessible on the host network.

---

## 14. Troubleshooting

### Bot doesn't respond in Teams
1. Check health: `curl http://localhost:8000/health`
2. Check logs: `docker compose logs -f bot-core | tail -50`
3. Verify chat is registered: send `/chats` in admin DM
4. Check RBAC: send `/permissions` in the problematic chat

### M365 auth fails
1. Verify Azure AD permissions (Section 3.3)
2. Check client secret hasn't expired
3. Ensure admin consent is granted
4. Try: `docker compose restart bot-core` then re-run `/m365-auth`

### Bot is slow
1. Check if it's a model issue: try switching to Sonnet (`/model sonnet`)
2. Check rate limits: `curl http://localhost:8000/health | jq .rate_limits`
3. Check for stuck tasks: `/cancel` then `/reset`

### Deploy fails
1. Check deploy status: tell bot "check deploy status"
2. Read failed logs: `cat deploy/last_failed_startup.log`
3. Auto-rollback should have restored the previous version
4. If stuck: `docker compose down && docker compose up -d`

### Messages aren't arriving
1. Check webhook: `docker compose logs bot-core | grep "teams-messages"`
2. Verify public HTTPS endpoint is working
3. Check Azure Bot Service messaging endpoint URL
4. Ensure chat is registered (not in silent mode)

### Database issues
1. Check disk space: `df -h /app/data`
2. Check DB integrity: `sqlite3 /app/data/conversations.db "PRAGMA integrity_check"`
3. Restore from backup if corrupted (Section 12)

---

## 15. Security Checklist

### Initial setup
- [ ] TOKEN_ENCRYPTION_KEY generated and set
- [ ] .env file has 600 permissions (`chmod 600 .env`)
- [ ] .env is in .gitignore
- [ ] Azure AD app uses single-tenant (not multi-tenant)
- [ ] Admin consent granted for M365 permissions
- [ ] Teams webhook uses HTTPS
- [ ] BOT_ADMIN_AAD_IDS set (restricts admin commands)

### Ongoing
- [ ] Rotate Azure AD client secret before expiry (check every 6 months)
- [ ] Review RBAC assignments quarterly: `/permissions`
- [ ] Monitor audit log: check for unauthorized access attempts
- [ ] Keep Docker images updated: `docker compose build --no-cache`
- [ ] Test backup restoration periodically
- [ ] Review connected accounts: `/accounts`

### If compromised
1. Rotate Azure AD client secret immediately
2. Rotate TOKEN_ENCRYPTION_KEY (requires re-authentication for all users)
3. Check audit log for unauthorized actions
4. Revoke all user OAuth tokens: `/revoke all`
5. Restart bot: `docker compose down && docker compose up -d`

---

*Generated by Corporate Bot documentation system. Last updated: 2026-05-20.*
