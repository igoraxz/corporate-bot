# Corporate Bot -- Admin Deployment Guide

A step-by-step guide for deploying Corporate Bot in your organization's Microsoft Teams environment. Covers infrastructure setup, Azure AD configuration, knowledge domain registration, and ongoing operations.

---

## Prerequisites

### Infrastructure

| Requirement | Minimum | Recommended |
|-------------|---------|-------------|
| OS | Any Docker-capable Linux host | Ubuntu 22.04 LTS |
| RAM | 4 GB | 16 GB (Camoufox uses 2 GB shared memory) |
| Disk | 10 GB free | 50 GB (Docker images + Chromium + data) |
| Docker | Engine 24+ with Compose v2 | Latest stable |
| Network | Public HTTPS endpoint | Cloudflare Tunnel or nginx reverse proxy |

### Accounts and Credentials

| Account | Purpose | Where to get it |
|---------|---------|-----------------|
| Azure AD tenant | Teams bot registration, SSO | [Azure Portal](https://portal.azure.com/) |
| Anthropic | Claude AI engine | [Claude Pro/Team](https://claude.ai) (OAuth) or [API key](https://console.anthropic.com/) |
| Telegram | Admin control channel | [my.telegram.org/apps](https://my.telegram.org/apps) |
| GitHub | Knowledge domain sync | [PAT](https://github.com/settings/tokens) with `repo` read access |
| Google AI (optional) | Embeddings, media descriptions, image generation | [AI Studio](https://aistudio.google.com/apikey) |

---

## Quick Start (5 Steps)

### Step 1: Clone and Configure

```bash
# Clone your organization's fork
git clone git@github.com:your-org/corporate-bot.git ~/corporate-bot
cd ~/corporate-bot

# Copy the example environment file
cp .env.example .env
```

### Step 2: Set Required Environment Variables

Open `.env` and fill in the required values:

```bash
# Claude AI access (pick one)
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-...       # OAuth token from Claude subscription
# OR: ANTHROPIC_API_KEY=sk-ant-api-...       # Pay-per-token API key

# Telegram admin channel
TG_API_ID=12345678                            # From my.telegram.org/apps
TG_API_HASH=abc123...                         # From my.telegram.org/apps
TG_CHAT_ID=-100XXXXXXXXXX                     # Your team group chat ID
ADMIN_USERS=telegram:123456789          # Authorized users
ADMIN_USERS=telegram:123456789                # Bot admin user IDs

# Azure AD / Teams
TEAMS_ENABLED=true
TEAMS_APP_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
TEAMS_APP_SECRET=your-client-secret
TEAMS_TENANT_ID=yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy
BOT_ADMIN_AAD_IDS=zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz

# Token encryption for per-user SSO vault
TOKEN_ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# Knowledge domains (GitHub sync)
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
GITHUB_WEBHOOK_SECRET=$(openssl rand -hex 32)
```

### Step 3: Start the Bot

```bash
docker compose up -d
```

Wait for the health check to pass (~30 seconds):

```bash
curl http://localhost:8000/health
# Expected: {"status":"healthy", ...}
```

### Step 4: Register the Teams App

1. Edit `teams-app/manifest.json` -- replace `${TEAMS_APP_ID}` and `${BOT_DOMAIN}`
2. Add icon files to `teams-app/`:
   - `color.png` (192x192, full-color logo)
   - `outline.png` (32x32, white outline on transparent background)
3. Create the app package:
   ```bash
   cd teams-app && zip -r ../corporate-bot.zip * && cd ..
   ```
4. Upload to [Teams Admin Center](https://admin.teams.microsoft.com/policies/manage-apps) > Upload new app

### Step 5: First Health Check

1. Open Microsoft Teams and find "Corporate Bot" in the apps catalog
2. Start a DM with the bot
3. Send `hello` -- the bot should respond
4. Send `/status` -- verify the bot reports healthy
5. Send `/domains` -- should show an empty domain list (none registered yet)

---

## Azure AD Setup (Detailed)

### App Registration

1. Navigate to [Azure Portal > App registrations](https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps)
2. Click **New registration**
   - Name: `Corporate Bot`
   - Supported account types: **Single tenant** (Accounts in this organizational directory only)
   - Redirect URI: leave blank
3. Record the **Application (client) ID** and **Directory (tenant) ID**
4. Go to **Certificates & secrets** > **New client secret**
   - Description: `corporate-bot-prod`
   - Expiry: 24 months (recommended)
   - Record the **secret value** (shown only once)

### API Permissions

In the app registration, go to **API permissions** and add:

| Permission | Type | Purpose |
|-----------|------|---------|
| `Mail.Read` | Delegated | Read employee email (DM only) |
| `Calendars.ReadWrite` | Delegated | Manage employee calendar (DM only) |
| `User.Read` | Delegated | Read user profile for identity |
| `Files.Read.All` | Delegated | Read SharePoint/OneDrive files for knowledge domain indexing |
| `Sites.Read.All` | Delegated | List SharePoint sites and document libraries |

Click **Grant admin consent for [Your Tenant]** (requires Azure AD admin role).

> **Note**: `Files.Read.All` and `Sites.Read.All` are only required if you plan to use Graph API document sources (SharePoint, Teams channel files) in knowledge domains. They can be omitted if all domains use GitHub sources.

### Expose an API (for Teams SSO)

1. Go to **Expose an API**
2. Set **Application ID URI**: `api://botid-{your-app-id}`
3. **Add a scope**:
   - Scope name: `access_as_user`
   - Who can consent: Admins and users
   - Admin consent display name: "Access Corporate Bot as user"
   - Admin consent description: "Allow the bot to access resources on behalf of the user"
4. Under **Authorized client applications**, add these Teams client IDs:
   - `1fec8e78-bce4-4aaf-ab1b-5451cc387264` (Teams desktop and mobile)
   - `5e3ce6c0-2b1f-4285-8d4b-75ee78787346` (Teams web)

### Azure Bot Service

1. Go to [Azure Portal > Create > Azure Bot](https://portal.azure.com/#create/Microsoft.AzureBot)
   - Bot handle: `corporate-bot`
   - Pricing tier: **F0** (free -- sufficient for most organizations)
   - Type of app: **Single Tenant**
   - App ID: paste your Application (client) ID
2. After creation, go to **Channels** > **Microsoft Teams** > **Enable**
3. Go to **Configuration** > set **Messaging endpoint**:
   ```
   https://bot.your-company.example.com/api/teams-messages
   ```

### HTTPS Endpoint

The bot requires a public HTTPS endpoint for Teams webhooks. Choose one:

**Option A: Cloudflare Tunnel (easiest, free)**
```bash
cloudflared tunnel create corporate-bot
cloudflared tunnel route dns corporate-bot bot.your-company.example.com
cloudflared tunnel run --url http://localhost:8000 corporate-bot
```

**Option B: nginx reverse proxy**
```nginx
server {
    listen 443 ssl;
    server_name bot.your-company.example.com;
    ssl_certificate /etc/letsencrypt/live/bot.your-company.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bot.your-company.example.com/privkey.pem;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

## Environment Variables Reference

All variables are documented in `.env.example`. Below is the complete reference grouped by category.

### Authentication (Required)

| Variable | Required | Description |
|----------|----------|-------------|
| `CLAUDE_CODE_OAUTH_TOKEN` | Yes* | Claude OAuth token from subscription plan |
| `ANTHROPIC_API_KEY` | Yes* | Alternative: Anthropic API key (pay-per-token) |

*One of these two is required.

### Telegram (Required for Admin Channel)

The bot supports two Telegram modes selected by `TG_MODE`:

**Bot API mode (default, recommended for corporate)**:
Create a bot via [@BotFather](https://t.me/botfather), get the token, then configure the
webhook URL to point to your bot's HTTPS endpoint at `/webhook/telegram`.

| Variable | Required | Description |
|----------|----------|-------------|
| `TG_MODE` | No | `"bot"` (default) for Bot API, `"userbot"` for Pyrogram MTProto |
| `TG_BOT_TOKEN` | Yes (bot mode) | Bot token from @BotFather |
| `TG_WEBHOOK_URL` | Yes (bot mode) | Public HTTPS URL, e.g. `https://bot.company.com` |
| `TG_WEBHOOK_SECRET` | **REQUIRED** | Random string for webhook verification (`openssl rand -hex 32`) |
| `TG_CHAT_ID` | Yes | Team group chat ID (negative number) |
| `ADMIN_USERS` | Yes | Root admin: `"user_id:Name,user_id:Name"` |
| `ADMIN_USERS` | Yes | Admin users: `"telegram:user_id"` |
| `TG_ADMIN_CHAT_IDS` | No | Admin-only DMs and promoted groups |
| `TG_BOT_USER_ID` | No | Bot's own Telegram user ID |

**Userbot mode (personal/family deployments only)**:

| Variable | Required | Description |
|----------|----------|-------------|
| `TG_API_ID` | Yes (userbot) | API ID from my.telegram.org |
| `TG_API_HASH` | Yes (userbot) | API hash from my.telegram.org |

**Bot API features** (not available in userbot mode):
- **Inline keyboards**: Interactive button menus on messages via `build_inline_keyboard()`
- **Forum topics**: Create, close, reopen, and delete forum topics in supergroups via `create_forum_topic()`, `close_forum_topic()`, `reopen_forum_topic()`, `delete_forum_topic()`
- **Menu commands**: Register bot slash commands visible in the TG UI via `set_bot_commands()`
- **Webhook-based**: No polling -- receives updates via HTTPS webhook
- **Callback queries**: Handle inline keyboard button presses via `answer_callback_query()`

### MS Teams (Required for Corporate Use)

| Variable | Required | Description |
|----------|----------|-------------|
| `TEAMS_ENABLED` | Yes | Set to `true` to enable Teams integration |
| `TEAMS_APP_ID` | Yes | Azure AD application (client) ID |
| `TEAMS_APP_SECRET` | Yes | Azure AD client secret |
| `TEAMS_TENANT_ID` | Yes | Azure AD tenant ID |
| `TEAMS_ALLOWED_TENANTS` | No | Additional tenant IDs (comma-separated) |
| `TEAMS_DEFAULT_HARNESS` | No | Default harness for new Teams chats (default: `employee`) |

### Corporate Auth and SSO

| Variable | Required | Description |
|----------|----------|-------------|
| `TOKEN_ENCRYPTION_KEY` | For SSO | Fernet key for per-user OAuth token vault |
| `BOT_ADMIN_AAD_IDS` | Yes | Azure AD Object IDs with full bot access |
| `MS365_CLIENT_ID` | No | Override for Graph API app (defaults to `TEAMS_APP_ID`) |
| `MS365_CLIENT_SECRET` | No | Override for Graph API secret |
| `MS365_TENANT_ID` | No | Override for Graph API tenant |
| `MS365_SCOPES` | No | Override Graph API scopes (default: `Mail.Read,Calendars.ReadWrite,User.Read,Files.Read.All,Sites.Read.All`) |

### Knowledge Domains

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_TOKEN` | For domains | GitHub PAT with `repo` read access |
| `GITHUB_WEBHOOK_SECRET` | For webhooks | HMAC-SHA256 secret for webhook verification |

### AI Services (Optional)

| Variable | Description |
|----------|-------------|
| `GEMINI_API_KEY` | Google AI API key (embeddings, media descriptions, document parsing, knowledge maps, image generation) |
| `GEMINI_MODEL_DESCRIBE` | Gemini model for document parsing and knowledge map generation (default: `gemini-2.5-flash-lite`) |
| `VAPI_API_KEY` | Vapi AI voice agent key |
| `VAPI_PHONE_NUMBER_ID` | Vapi phone number ID |
| `WEBHOOK_BASE_URL` | Public URL for Vapi callbacks |

### Telegram Bot API Setup (@BotFather)

Step-by-step setup for Bot API mode (`TG_MODE=bot`):

1. **Create the bot**: Message [@BotFather](https://t.me/botfather) on Telegram
   - Send `/newbot`
   - Choose a display name (e.g. "Corporate Bot")
   - Choose a username (e.g. `your_company_bot`) -- must end in `bot`
   - Copy the **HTTP API token** -- this is your `TG_BOT_TOKEN`

2. **Configure the webhook**: Set `TG_WEBHOOK_URL` in `.env` to your public HTTPS endpoint.
   The bot registers the webhook automatically on startup via `set_webhook()`.
   ```
   TG_WEBHOOK_URL=https://bot.company.com
   ```
   The webhook endpoint is at `/webhook/telegram` (appended automatically).

3. **Set the webhook secret**: Generate a random string and set it in `.env`.
   This is **REQUIRED** -- webhook updates without a valid secret are rejected.
   ```bash
   TG_WEBHOOK_SECRET=$(openssl rand -hex 32)
   ```

4. **Configure forum topics** (optional): If your admin group is a supergroup with
   Topics enabled, the bot can route messages to specific forum topics via
   `message_thread_id`. Enable topics in the TG group settings, then ensure the bot
   has `can_manage_topics` admin permission.

5. **Register menu commands** (optional): Call `set_bot_commands()` to register
   slash commands visible in the Telegram UI. These appear in the bot's command menu.

### Google Workspace (Optional)

| Variable | Description |
|----------|-------------|
| `GOOGLE_OAUTH_CLIENT_ID` | Google OAuth 2.0 Client ID |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Google OAuth client secret |
| `GOOGLE_ACCOUNTS` | Comma-separated email list for Google API routing |

### Claude SDK Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL_PRIMARY` | `claude-sonnet-4-6` | Default model (Sonnet ~5x cheaper than Opus) |
| `MAX_BUDGET_DEFAULT` | `5` | Sonnet session budget cap (USD) |
| `MAX_BUDGET_HEAVY` | `50` | Opus session budget cap (USD) |
| `MAX_EXTEND_DELTA_USD` | `100` | Max single budget extension |
| `MAX_TOTAL_BUDGET_CAP` | `500` | Absolute session budget ceiling |
| `QUERY_OUTPUT_CAP_DEFAULT` | `25000` | Sonnet per-query output token cap |
| `QUERY_OUTPUT_CAP_HEAVY` | `50000` | Opus per-query output token cap |

### Browser Automation (Optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `CAMOFOX_API_KEY` | -- | Shared secret for Camoufox container |
| `CAMOFOX_BROWSER_URL` | `http://camofox-browser:9377` | Camoufox endpoint |
| `PLAYWRIGHT_MCP_PORT` | `3001` | Playwright SSE server port |
| `BROWSER_POOL_IMMUNE_SLOTS` | `6` | Admin/team browser tabs (immune from eviction) |
| `BROWSER_POOL_LRU_SLOTS` | `6` | Coding/discovered browser tabs (LRU-evictable) |

### RBAC (Role-Based Access Control)

| Variable | Default | Description |
|----------|---------|-------------|
| `RBAC_PRIMARY` | `true` | When `true`, RBAC is the authoritative enforcement layer for tool access. Legacy hooks (Layers 1b, 1b2, 1b3, 1c) run log-only. Set to `false` to revert to legacy enforcement (RBAC runs log-only instead). |

The `manage_rbac` MCP tool (admin-only) provides runtime RBAC management: create/delete roles, add/remove policies, assign/revoke roles to users/teams/chats, test access decisions, and query the audit log. See `docs/RBAC_DESIGN.md` for the full design.

### Docker and Deployment

| Variable | Default | Description |
|----------|---------|-------------|
| `COMPOSE_PROJECT_NAME` | `corporate-bot` | Docker container name prefix |
| `BOT_PORT` | `8000` | Host port to expose |
| `TZ` | `Europe/London` | Timezone for container |
| `BOT_ENV` | `production` | Environment (`production` or `staging`) |

---

## Knowledge Domains Setup

Knowledge domains are composable knowledge bases sourced from GitHub repositories, Microsoft Graph API (SharePoint/Teams/OneDrive), or local filesystems. Each domain has its own YAML configuration file.

### Creating a Domain Config

Create a YAML file in `data/domains/`:

```yaml
# data/domains/engineering-runbooks.yaml
domain:
  id: engineering-runbooks
  name: Engineering Runbooks
  description: >
    Deployment guides, operational runbooks, incident response procedures,
    and infrastructure documentation for the engineering team.

  owner: devops@your-company.example.com

  source:
    type: github
    repo: your-org/engineering-docs        # GitHub repository
    branch: main                            # Branch to index
    paths:                                  # Directories to include
      - docs/
      - runbooks/
    exclude:                                # Directories to skip
      - drafts/
      - .github/

  indexing:
    chunking_strategy: markdown_headers     # Split on markdown headers
    chunk_size: 1500                        # Target chunk size (chars)
    chunk_overlap: 200                      # Overlap between chunks
    poll_interval_minutes: 5                # Polling fallback interval

  access:
    teams:                                  # Teams that can query this domain
      - engineering
      - sre
      - devops
    admins:                                 # Domain admins (can reindex)
      - platform-lead@company.com
      - cto@company.com

  knowledge:
    prompt_modules:                         # System prompt modules to inject
      - deploy
      - pitfalls
    skills:                                 # Relevant skills for this domain
      - coding-principles
      - browser-automation
    auto_load_skills:                       # Skills pre-loaded into CLAUDE.md
      - coding-principles
    facts_categories:                       # Fact categories owned by this domain
      - infrastructure
      - deploy
      - monitoring
```

### Registering a Domain

Use the `manage_domain` MCP tool from a bot admin chat:

```
manage_domain(action="register", domain_id="engineering-runbooks")
```

This reads the YAML config, creates the domain in the registry, and triggers the initial sync.

### GitHub Webhook for Auto-Sync

For automatic re-indexing when documentation changes:

1. Go to your GitHub repo > **Settings** > **Webhooks** > **Add webhook**
2. **Payload URL**: `https://bot.your-company.example.com/webhooks/github`
3. **Content type**: `application/json`
4. **Secret**: your `GITHUB_WEBHOOK_SECRET` value from `.env`
5. **Events**: select "Just the push event"

When a push lands on the configured branch, the bot fetches changed files, re-chunks, and re-embeds only modified content (content-hash gating reduces embedding cost by 80-95%).

### Graph API Document Source (SharePoint/Teams)

For indexing documents from SharePoint sites or Teams channel files, use the `graph_api` source type:

```yaml
# data/domains/company-policies.yaml
domain:
  id: company-policies
  name: Company Policies
  description: Corporate policies and compliance documents from SharePoint.
  owner: compliance@company.com

  source:
    type: graph_api
    site_id: "contoso.sharepoint.com,guid1,guid2"    # SharePoint site ID
    paths: [Policies, Compliance]
    exclude: ["*/drafts/*"]

  # ... indexing, access, knowledge sections as usual
```

For Teams channel files, use `team_id` and `channel_id` instead of `site_id`:

```yaml
  source:
    type: graph_api
    team_id: "team-guid-here"
    channel_id: "channel-guid-here"
```

Requires `Files.Read.All` and `Sites.Read.All` Azure AD permissions (see API Permissions above). The bot uses the M365 auth token to call the Microsoft Graph API directly. See [DOMAIN_COOKBOOK.md](DOMAIN_COOKBOOK.md) for more examples.

### Verifying Domain Health

```
manage_domain(action="health", domain_id="engineering-runbooks")
```

Returns: sync status, chunk count, last sync timestamp, error messages.

### Testing Search

```
search_knowledge(query="how to deploy to production", domains=["engineering-runbooks"])
```

---

## Harness Configuration

Harnesses are named configuration profiles that control what each Teams channel or chat can access. Each channel is assigned one harness.

### Creating Team Harnesses

Via bot admin chat using the `manage_harness` MCP tool:

```
# Engineering team -- access to eng + shared domains
manage_harness(action="create", label="engineering",
  fields='{"domains": ["engineering-runbooks", "shared-policies"],
           "write_domain": "engineering-runbooks",
           "default_role": "teamwork"}')

# HR team -- access to HR domain only
manage_harness(action="create", label="hr-team",
  fields='{"domains": ["hr-policies"],
           "write_domain": "hr-policies",
           "default_role": "teamwork"}')

# Sales team -- CRM knowledge, email access for shared inbox
manage_harness(action="create", label="sales",
  fields='{"domains": ["sales-playbooks", "shared-policies"],
           "write_domain": "sales-playbooks",
           "default_role": "teamwork",
           "allowed_credentials": ["sales-team@company.com"]}')
```

> **Note**: Tool access and sandbox settings are now controlled by RBAC roles
> (assigned via `default_role`), not harness fields. Domain access (`knowledge_domains`,
> `sandbox`, `allowed_tools`, `allowed_emails`) has been replaced by `domains`,
> `write_domain`, `default_role`, and `allowed_credentials`.

### Assigning Harnesses to Channels

```
# From the target channel, or by specifying chat_id:
/set-harness engineering
```

Or via the harness rules engine (`bot/harness_rules.py`) for automatic assignment:

```json
// data/harness_rules.json
[
  {"match": {"conversation_type": "channel", "team_name": "Engineering*"}, "harness": "engineering"},
  {"match": {"conversation_type": "channel", "team_name": "HR*"}, "harness": "hr-team"},
  {"match": {"conversation_type": "personal"}, "harness": "employee"}
]
```

### Reviewing Harness Configuration

```
/harnesses                    -- List all harnesses
/harnesses engineering        -- Show detailed config for one harness
/chats                        -- Show all active chats with assigned harnesses
```

---

## Backup Configuration

### Encrypted Backups (Default)

The bot includes an hourly backup script that creates AES-256 encrypted archives of all data.

```bash
# Generate encryption password (one-time)
openssl rand -base64 24 > .backup-password
chmod 600 .backup-password

# Install backup script outside repo (security isolation)
sudo mkdir -p /opt/corporate-bot-backup
sudo cp scripts/backup.sh /opt/corporate-bot-backup/backup.sh
sudo chown -R botuser:botuser /opt/corporate-bot-backup
sudo chmod 755 /opt/corporate-bot-backup/backup.sh

# Test backup
sudo -u botuser /opt/corporate-bot-backup/backup.sh --dry-run
sudo -u botuser /opt/corporate-bot-backup/backup.sh --local-only

# Add to cron (hourly, self-throttles to 2-hour intervals)
sudo -u botuser crontab -e
# Add: 0 * * * * /opt/corporate-bot-backup/backup.sh >> ~/corporate-bot/deploy/backup.log 2>&1
```

### Backup Destinations

| Destination | How to Configure | Retention |
|-------------|-----------------|-----------|
| Local disk | Automatic (default) | 12x2h + 7 daily + 4 weekly + monthly forever |
| Google Drive | Configure `rclone` with `drive.file` scope | Same retention policy |
| S3 / S3-compatible | Configure `rclone` with S3 remote | Same retention policy |
| Mac via SSH | Set `DESKTOP_HOST` and SSH key | Same retention policy |

### What Gets Backed Up

- SQLite databases (`conversations.db`, `pm.db`)
- Telegram sessions
- Google and MS365 OAuth credentials
- Claude SDK sessions
- Media cache
- Configuration files (harnesses, scheduled tasks, domain configs)
- Deploy state

### Restore

```bash
# Full restore from backup archive
./scripts/restore.sh path/to/bot-backup-2026-05-10-1400.7z

# Restore from Mac
./scripts/restore.sh --from-mac

# List available Mac backups
./scripts/restore.sh --list-mac
```

---

## Employee Workstation Control

Bridge dedicated Telegram channels to employee Mac Claude Code sessions via SSH. Each employee gets a managed channel scoped to their workstation.

### Prerequisites

- Employee Mac with macOS
- [Tailscale](https://tailscale.com/) on both the server and the Mac
- SSH key pair for bot-to-Mac connection

### Server-Side Configuration

```bash
# In .env:
DESKTOP_ENABLED=true
DESKTOP_HOST=employee-mac-hostname       # Tailscale hostname
DESKTOP_USER=employee-username           # Mac username
DESKTOP_SSH_KEY=~/.ssh/id_ed25519        # SSH key path

# Optional: Tailscale dynamic IP resolution
TAILSCALE_OAUTH_CLIENT_ID=...
TAILSCALE_OAUTH_CLIENT_SECRET=...
TAILSCALE_TAILNET=your-tailnet.ts.net
TAILSCALE_HOSTNAME=employee-mac
```

### Mac-Side Setup

See [DESKTOP_RELAY_SETUP.md](DESKTOP_RELAY_SETUP.md) for detailed instructions covering:

1. Tailscale installation and enrollment
2. SSH authorized keys configuration
3. `guard.sh` installation (hardened command proxy)
4. TOTP authentication setup
5. Claude Code CLI installation

### Creating a Relay Channel

From the admin Telegram chat:

```
/mac-sessions                            -- List available Mac projects
/create-relay 3                          -- Create relay for project #3
```

This creates a dedicated Telegram group bridged to the employee's Mac Claude Code session.

---

## Monitoring

### Health Endpoints

| Endpoint | Purpose | Expected Response |
|----------|---------|-------------------|
| `GET /health` | Basic HTTP health check | `{"status": "healthy", ...}` |
| `GET /health/deep` | Exercises pre-SDK pipeline (catches NameError/import issues) | `{"status": "healthy", ...}` |

Use `/health/deep` in monitoring dashboards -- it catches crashes that `/health` alone would miss.

### Audit Logs

Every Teams message generates an audit event stored in SQLite:

```
# Summary of last 24 hours:
get_audit_summary(hours=24)

# Export CSV for compliance:
export_audit_csv(hours=168)        # Last 7 days

# GDPR data deletion:
delete_user_data(aad_object_id="...")
```

### Usage Reports

```
# Cost summary by time period:
usage_report(action="summary")

# Top queries by cost:
usage_report(action="top_queries")

# Per-OAuth-profile cost breakdown:
usage_report(action="by_profile")
```

### Bot Logs

Container logs are stored in the `bot-logs` Docker volume at `/app/logs/bot.log`:
- Rotating file handler (10 MB, 5 backups)
- DEBUG level to file, INFO level to stdout
- Persists across container restarts

```bash
# View recent logs
docker compose logs bot-core --tail 100

# Search for errors
docker compose exec bot-core grep -i error /app/logs/bot.log | tail -20
```

---

## Troubleshooting

### Common Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| Bot does not respond in Teams | Messaging endpoint incorrect | Check Azure Bot Config > Configuration > endpoint URL matches your HTTPS domain |
| 401 Unauthorized on webhook | JWT validation failure | Verify `TEAMS_APP_ID` and `TEAMS_APP_SECRET` match the Azure app registration |
| No knowledge search results | Domains not synced | Run `manage_domain(action="reindex", domain_id="...")`, verify `GITHUB_TOKEN` has repo access |
| SSO fails in Teams DM | API permissions not consented | Go to Azure Portal > App Registration > API permissions > Grant admin consent |
| Bot timeout (15 seconds) | Proactive messaging not configured | Verify serviceUrl is stored correctly on first message receipt |
| Container fails to start | Missing `.env` values | Check `docker compose logs bot-core` for the specific missing variable |
| Health check fails after deploy | Code error in new deployment | Check `deploy_status()` for failed startup logs; auto-rollback should restore previous version |
| Domain sync fails | GitHub token expired or insufficient permissions | Regenerate PAT with `repo` read scope, update `GITHUB_TOKEN` in `.env` |
| Token vault encryption error | Wrong or missing `TOKEN_ENCRYPTION_KEY` | Ensure the key matches what was used to encrypt existing tokens; rotating requires re-authentication |

### Diagnostic Commands

From the admin Telegram chat:

```
/status            -- Bot health, uptime, active sessions
/diagnose          -- Full diagnostic dump (admin only)
/chats             -- All active chats with harness assignments
/harnesses         -- All harness configurations
/domains           -- All registered knowledge domains
deploy_status()    -- Last deploy result + failed logs if any
```

### Emergency Recovery

```bash
# Rollback to last healthy deploy
/rollback confirm

# Manual rollback (from host)
cd ~/corporate-bot
git log --oneline -5                      # Find working commit
git reset --hard <commit>
docker compose build --no-cache bot-core
docker compose up -d bot-core

# Full restore from backup
./scripts/restore.sh --from-mac
```

---

## Next Steps

- [DOMAIN_COOKBOOK.md](DOMAIN_COOKBOOK.md) -- Practical examples for knowledge domain configuration
- [ACCESS_MANAGEMENT.md](ACCESS_MANAGEMENT.md) -- Detailed permission model and credential tiers
- [ENCRYPTION.md](ENCRYPTION.md) -- Encryption at rest options
- [KEY_ROTATION.md](KEY_ROTATION.md) -- Secret rotation procedures
- [ARCHITECTURE_DIAGRAM.md](ARCHITECTURE_DIAGRAM.md) -- Visual architecture diagrams
