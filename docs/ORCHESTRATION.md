# Docker Orchestration Reference

Complete reference for the bot's Docker infrastructure — services, volumes, networks,
staging, watchers, and configuration parametrization.

## Quick Reference

| Component | Prod | Staging |
|-----------|------|---------|
| Compose project | `${COMPOSE_PROJECT_NAME}` | `${COMPOSE_PROJECT_NAME}-staging` |
| Container | `${COMPOSE_PROJECT_NAME}` | `${COMPOSE_PROJECT_NAME}-staging` |
| Port | `${BOT_PORT}` (e.g. 8200) | Staging port (e.g. 8300) |
| Repo path | `/home/botuser/${COMPOSE_PROJECT_NAME}/` | `/home/botuser/${COMPOSE_PROJECT_NAME}-staging/` |
| Watcher | `/opt/${COMPOSE_PROJECT_NAME}-watcher/` | `/opt/${COMPOSE_PROJECT_NAME}-staging-watcher/` |
| Network | `${BOT_NETWORK:-bot-shared}` | same (shared) |

## Parametrization — Environment Variables

All instance-specific values come from `.env` or shell environment. No hardcoded
instance names in compose files or Python code.

### Required in `.env`

| Variable | Example | Purpose |
|----------|---------|---------|
| `COMPOSE_PROJECT_NAME` | `corporate-bot` | Docker project name, container name, volume prefix |
| `BOT_PORT` | `8200` | Host port mapped to container 8000 |
| `SSH_KEY_PATH` | `/home/botuser/.ssh/id_ed25519` | Deploy key for git push |
| `GIT_SSH_HOST` | `github-corp` | SSH host alias for git remote (must match `git remote -v`) |

### Optional (with defaults)

| Variable | Default | Purpose |
|----------|---------|---------|
| `BOT_NETWORK` | `bot-shared` | External Docker network name |
| `TZ` | `Europe/London` | Container timezone |
| `MEDIA_RETENTION_DAYS` | `36135` (~99yr) | Media cache retention |
| `CAMOFOX_API_KEY` | (empty) | Camoufox browser API key |
| `GIT_COMMIT` | `dev` | Build arg — baked commit hash |
| `DESKTOP_HOST` | (none) | Mac hostname for relay (Tailscale) |
| `DESKTOP_USER` | (none) | Mac username for relay |

### Staging-Specific (shell env, NOT in .env.staging)

| Variable | Required | Purpose |
|----------|----------|---------|
| `BOT_PORT` | Yes (shell) | Staging port — MUST pass as shell env var |
| `PROD_PROJECT_NAME` | No (default=`COMPOSE_PROJECT_NAME`) | Prod project name for cross-volume mounts |

**Why shell env?** Compose variable substitution (`${VAR}`) reads from shell env and
the project `.env` file — NOT from the service's `env_file` directive. The staging
`.env.staging` is loaded as service env only (runtime inside container), not during
Compose file parsing.

### Derived at Runtime

| Value | Derivation |
|-------|------------|
| Container name (staging) | `${COMPOSE_PROJECT_NAME}-staging` |
| `STAGING_URL` | `http://${COMPOSE_PROJECT_NAME}-staging:8000` (overridable) |
| Git author email | `bot@${COMPOSE_PROJECT_NAME}.local` |
| Git author name | `${COMPOSE_PROJECT_NAME}` |
| SSH host alias | `${GIT_SSH_HOST}` (extra Host entry in SSH config) |
| Watcher approval dir | `/opt/${PROJECT_NAME}-watcher/approvals` (derived from `COMPOSE_PROJECT_NAME`) |

## Services

### bot-core (primary)

The main bot application — Python 3.12, Claude Agent SDK, MCP tools.

```yaml
# docker-compose.yml
services:
  bot-core:
    build:
      context: .
      args:
        GIT_COMMIT: ${GIT_COMMIT:-dev}
    container_name: ${COMPOSE_PROJECT_NAME:-corporate-bot}
    ports:
      - "${BOT_PORT:-8000}:8000"
```

**Security hardening:**
- `cap_drop: ALL` — no Linux capabilities
- `no-new-privileges:true` — prevent privilege escalation
- `seccomp=./seccomp-bwrap.json` — custom profile for bubblewrap sandbox
- `apparmor=unconfined` — required for mount propagation in bwrap
- Memory limit: 8GB, shared memory: 2GB
- Runs as non-root `botuser` (UID 10001)

### camofox-browser (optional)

Anti-detect Firefox browser for sites that block Playwright.

```yaml
# docker-compose.yml — only starts with --profile browser
camofox-browser:
    profiles: ["browser"]
    container_name: ${COMPOSE_PROJECT_NAME:-corporate-bot}-camofox
```

**Staging override:** Alpine stub (`entrypoint: ["true"]`) — defined but harmless.
Can't use Compose profiles to disable (breaks `depends_on` resolution).

## Volumes

### Production Volumes

| Volume | Mount Point | Purpose |
|--------|-------------|---------|
| `bot-data` | `/app/data` | SQLite DB, domains, tokens, credentials, state |
| `bot-logs` | `/app/logs` | Persistent log files (10MB rotate, 5 backups) |
| `claude-sessions` | `/home/botuser/.claude` | Claude Agent SDK sessions + OAuth |
| `media-cache` | `/app/data/media_cache` | Cached Telegram media (permanent) |
| `camofox-data` | `/home/node/.camofox` | Browser profiles (prod only) |

### Staging Volumes (isolated)

| Volume | Mount Point | Purpose |
|--------|-------------|---------|
| `staging-bot-data` | `/app/data` | Isolated staging data |
| `staging-bot-logs` | `/app/logs` | Isolated staging logs |
| `staging-claude-sessions` | `/home/botuser/.claude` | Isolated SDK sessions |
| `staging-media-cache` | `/app/data/media_cache` | Isolated media cache |

### Cross-Volume Mounts (staging reads prod)

| Volume | Mount Point | Purpose |
|--------|-------------|---------|
| `prod-claude-sessions` | `/prod-claude:ro` | OAuth credential copy on startup |
| `prod-bot-data` | `/prod-data:ro` | OAuth profile sync on startup |

Cross-volume names resolve via: `${PROD_PROJECT_NAME:-corporate-bot}_claude-sessions`.
Set `PROD_PROJECT_NAME` to match your prod `COMPOSE_PROJECT_NAME`.

### Bind Mounts (host → container)

| Host Path | Container Path | Mode | Purpose |
|-----------|---------------|------|---------|
| `.env` | (env_file) | ro | Environment variables |
| `./data/org_config.json` | `/app/data/org_config.json` | ro | Organization identity |
| `./data/prompts` | `/app/data/prompts` | ro | Prompt fragments |
| `./data/domains` | `/app/data/domains` | ro | Knowledge domain configs |
| `./data/claude_md_modules` | `/app/data/claude_md_modules` | ro | Legacy module fallback (skills now in `.claude/skills/`) |
| `${SSH_KEY_PATH}` | `/home/botuser/.ssh/id_ed25519` | ro | Deploy key |
| `./deploy/triggers` | `/deploy-triggers` | rw | Deploy trigger files |
| `.` (repo root) | `/host-repo` | rw* | Self-upgrade git ops |

*Staging mounts `/host-repo:ro` (read-only).

**⚠️ Docker bind mount gotcha:** If a host file doesn't exist, Docker creates it as a
directory. This causes OCI runtime errors on restart. Always ensure bind-mounted files
exist before `docker compose up`.

## Networks

```
┌─── bot-shared (external) ───────────┐
│                                      │
│  bot-core (prod)     :${BOT_PORT}    │
│  bot-core (staging)  :staging-port   │
│  camofox-browser     :9377 (internal)│
│                                      │
└──────────────────────────────────────┘
```

**Prerequisites:** Create once per host:
```bash
docker network create ${BOT_NETWORK:-bot-shared}
```

Both prod and staging join the same external network. This enables:
- Prod → staging HTTP calls (QA testing via `/test/inject`)
- Staging → prod volume reads (OAuth credential sync)

## Staging Setup

### Architecture

- **Separate git clone** at `~/COMPOSE_PROJECT_NAME-staging/` (fully independent)
- **`.env` symlinked** from prod (shared secrets, different overrides via `.env.staging`)
- **Isolated volumes** (staging never touches prod data)
- **OAuth copied from prod** at startup (unidirectional: prod → staging only)
- **TG/WA/Vapi/Relay disabled** via `.env.staging` overrides
- **Test endpoints enabled** via `BOT_ENV=staging`

### Launch Command

```bash
sudo -u botuser BOT_PORT=<staging_port> docker compose \
  -p <project>-staging \
  -f docker-compose.yml -f docker-compose.staging.yml \
  up -d --no-deps bot-core
```

Example:
```bash
# No BOT_PORT needed — staging has ports: [] (no host ports, Docker network only)
sudo -u botuser docker compose \
  -p ${COMPOSE_PROJECT_NAME}-staging \
  -f docker-compose.yml -f docker-compose.staging.yml \
  up -d --no-deps bot-core
```

### Pitfalls (all verified in production)

1. **`-p` flag REQUIRED** — without it, staging and prod share the same Compose project;
   starting one RECREATES the other's container.

2. **`BOT_PORT` must be shell env** — Compose `${BOT_PORT}` reads shell env + project `.env`,
   NOT the service's `env_file`. Without shell override, staging inherits prod's port.

3. **`depends_on: {}` doesn't clear** in Compose v2 — it merges (appends nothing). Alpine
   stubs solve this by keeping services "defined" so depends_on resolves.

4. **Run as botuser** (`sudo -u botuser`) — repo ownership consistency.

5. **`PROD_PROJECT_NAME`** must match prod's `COMPOSE_PROJECT_NAME` for cross-volume mounts.

### OAuth on Staging

**At startup:** `main.py` lifespan copies from prod volumes:
- `/prod-claude/.credentials.json` → `~/.claude/.credentials.json`
- `/prod-data/credentials/oauth_profiles/*.json` → staging profiles dir

**At runtime (from prod):** Use the `staging_command` MCP tool:
```
staging_command(text="switch_oauth personal")
staging_command(text="manage_oauth status")
```
This injects commands into staging via `/test/inject` and returns the response.

### Test Endpoints (staging only, disabled in production)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/test/inject` | POST | Inject synthetic message |
| `/test/responses/{chat_id}` | GET/DELETE | Poll/clear responses |
| `/test/reset-clients` | POST | Reset SDK clients by prefix |

## Deploy Watchers

### How They Work

Both prod and staging watchers are systemd services that poll trigger directories.
The watcher scripts live OUTSIDE the repo at `/opt/` (root-owned — bot cannot modify).

```
Bot ──writes──→ trigger.json ──watcher reads──→ git pull + docker build + health check
                                                      │
                                              writes result.json ──bot reads──→ shows user
```

### Prod Watcher

| Item | Value |
|------|-------|
| Service | `/etc/systemd/system/${PROJECT}-watcher.service` |
| Script | `/opt/${PROJECT}-watcher/host-watcher.sh` |
| Trigger dir | `${REPO}/deploy/triggers/` |
| Approval script | `/opt/${PROJECT}-watcher/approve-deploy.sh` |

**Sensitive-file gate:** When a deploy touches Dockerfile, compose, entrypoint, service
files, or scripts — the watcher pauses and requires manual approval via `approve-deploy.sh`.
The approval script auto-detects instance name from its install path.

### Staging Watcher

| Item | Value |
|------|-------|
| Service | `/etc/systemd/system/${PROJECT}-staging-watcher.service` |
| Script | `/opt/${PROJECT}-staging-watcher/staging-watcher.sh` |
| Trigger dir | `${PROD_REPO}/deploy/staging-triggers/` |

**Note:** Staging watcher reads triggers from the PROD repo's staging-triggers dir
(bot writes there via its `/host-repo` mount).

### Installing Watchers

Both `.service` files in `deploy/` are TEMPLATES with `CHANGE_ME` placeholders.
Replace all occurrences with your instance values:

```bash
# Prod watcher
sudo mkdir -p /opt/mybot-watcher
sudo cp deploy/host-watcher.sh /opt/mybot-watcher/
sudo cp deploy/approve-deploy.sh /opt/mybot-watcher/
# Edit service file: replace CHANGE_ME
sudo cp deploy/corp-bot-watcher.service /etc/systemd/system/mybot-watcher.service
sudo systemctl daemon-reload && sudo systemctl enable --now mybot-watcher

# Staging watcher
sudo mkdir -p /opt/mybot-staging-watcher
sudo cp deploy/staging-watcher.sh /opt/mybot-staging-watcher/
# Edit service file: replace CHANGE_ME
sudo cp deploy/staging-watcher.service /etc/systemd/system/mybot-staging-watcher.service
sudo systemctl daemon-reload && sudo systemctl enable --now mybot-staging-watcher
```

### After Updating Watcher Scripts

When watcher scripts are modified in the repo, they must be manually copied to `/opt/`:
```bash
sudo cp ~/mybot/deploy/host-watcher.sh /opt/mybot-watcher/
sudo systemctl restart mybot-watcher

sudo cp ~/mybot/deploy/staging-watcher.sh /opt/mybot-staging-watcher/
sudo systemctl restart mybot-staging-watcher
```

## Entrypoint

The `entrypoint.sh` script runs on container start (as `botuser`):

1. **Creates `.claude.json`** — prevents SDK warning
2. **Ensures data directories** — defense-in-depth for bind mount edge cases
3. **Links skills/agents** — symlinks from `/host-repo/.claude/` to `/app/.claude/` and `$HOME/.claude/`
4. **Configures git** — identity from `COMPOSE_PROJECT_NAME`, safe.directory for `/host-repo`
5. **SSH setup** — deploy key, GitHub known hosts, host aliases:
   - `github-private` — always created (default deploy key)
   - `${GIT_SSH_HOST}` — created if set and different from `github-private` (e.g. `github-corp`)
   - `github-upstream` — created if upstream deploy key exists
   - `mac-desktop` — created if `DESKTOP_HOST` is set
6. **Starts bot** — `exec python main.py`

## Dockerfile

Key features:
- **Base:** Python 3.12-slim + Node.js 22
- **User:** `botuser` (UID 10001, GID 999/hostdocker)
- **Tools:** Playwright + Chromium, bubblewrap, pptxgenjs, mermaid-cli, docx
- **MCP:** `@playwright/mcp@0.0.68` pre-installed
- **Health:** `curl -f http://localhost:8000/health` (30s interval, 3 retries)
- **Build args:** `GIT_COMMIT` (baked commit hash), `TZ` (timezone)

## Multi-Instance Deployment

The same codebase supports multiple independent bot instances on one host.
Each instance has its own:
- `.env` with unique `COMPOSE_PROJECT_NAME` and `BOT_PORT`
- Git clone at `~/INSTANCE_NAME/`
- Docker volumes (prefixed by Compose project name)
- Watcher services at `/opt/INSTANCE_NAME-watcher/`
- SSH deploy key at the path specified by `SSH_KEY_PATH`

Instances share the Docker network (`bot-shared`) but are otherwise fully isolated.

## New Instance Setup Checklist

Complete steps to deploy a new bot instance. Replace `$P` with your project name (e.g. `corporate-bot`).

### 1. Host Preparation
```bash
# Create botuser (one-time per host, shared across instances)
sudo useradd -r -s /usr/sbin/nologin -u 10001 -m botuser
sudo usermod -aG docker botuser

# Create shared network (one-time per host)
docker network create bot-shared
```

### 2. Clone & Configure
```bash
sudo -u botuser git clone git@github:user/repo.git /home/botuser/$P
cd /home/botuser/$P

# Generate SSH deploy key
sudo -u botuser ssh-keygen -t ed25519 -f /home/botuser/.ssh/id_ed25519_$P -N ""
# Add public key to GitHub repo → Settings → Deploy Keys (with write access)

# Create .env (use setup wizard or manually)
# Required variables:
#   COMPOSE_PROJECT_NAME=$P
#   BOT_PORT=<unique port>
#   SSH_KEY_PATH=/home/botuser/.ssh/id_ed25519_$P
#   GIT_SSH_HOST=github-$P    # must match git remote SSH host alias
#   (plus all API keys, tokens, etc.)
```

### 3. Git Remote Setup
```bash
# The remote host alias must match GIT_SSH_HOST in .env
sudo -u botuser git -C /home/botuser/$P remote set-url origin git@github-$P:user/repo.git
```

### 4. Seccomp Profile
```bash
# Must exist before first docker compose up
cp /home/botuser/$P/seccomp-bwrap.json /home/botuser/$P/seccomp-bwrap.json
```

### 5. First Build
```bash
cd /home/botuser/$P
COMMIT=$(sudo -u botuser git rev-parse HEAD)
sudo -u botuser GIT_COMMIT=$COMMIT docker compose up -d --build bot-core
```

### 6. Install Watchers
```bash
# Prod watcher
sudo mkdir -p /opt/$P-watcher/approvals
sudo cp deploy/host-watcher.sh /opt/$P-watcher/
sudo cp deploy/approve-deploy.sh /opt/$P-watcher/
sudo chown -R root:root /opt/$P-watcher/
sudo chmod +x /opt/$P-watcher/*.sh
# Edit service file: replace CHANGE_ME with $P and correct paths
sudo cp deploy/corp-bot-watcher.service /etc/systemd/system/$P-watcher.service
sudo systemctl daemon-reload && sudo systemctl enable --now $P-watcher
```

### 7. Staging Setup
```bash
# Clone staging
sudo -u botuser git clone git@github-$P:user/repo.git /home/botuser/$P-staging
sudo -u botuser ln -s /home/botuser/$P/.env /home/botuser/$P-staging/.env
sudo -u botuser ln -s /home/botuser/$P/data/google-workspace-creds /home/botuser/$P-staging/data/google-workspace-creds
# Copy seccomp profile
cp /home/botuser/$P/seccomp-bwrap.json /home/botuser/$P-staging/

# Install staging watcher
sudo mkdir -p /opt/$P-staging-watcher/approvals
sudo cp deploy/staging-watcher.sh /opt/$P-staging-watcher/
sudo cp deploy/approve-deploy.sh /opt/$P-staging-watcher/
sudo chown -R root:root /opt/$P-staging-watcher/
sudo chmod +x /opt/$P-staging-watcher/*.sh
# Edit service file: replace CHANGE_ME
sudo cp deploy/staging-watcher.service /etc/systemd/system/$P-staging-watcher.service
sudo systemctl daemon-reload && sudo systemctl enable --now $P-staging-watcher

# First staging launch
cd /home/botuser/$P-staging
sudo -u botuser BOT_PORT=<staging_port> docker compose \
  -p $P-staging \
  -f docker-compose.yml -f docker-compose.staging.yml \
  up -d --no-deps bot-core
```

### 8. Post-Setup Verification
```bash
# Health check
curl http://localhost:<port>/health
# Verify git push works from inside container
docker exec $P bash -c "cd /host-repo && git push --dry-run origin main"
# Verify staging reachable from prod
docker exec $P curl -sf http://$P-staging:8000/health
```

## Pitfalls (All Verified in Production)

Every pitfall here was discovered through actual production incidents.

### Compose & Docker

1. **`-p` flag REQUIRED for staging** — without it, staging and prod share the same Compose
   project; starting one RECREATES the other's container.

2. **`BOT_PORT` must be shell env** — Compose `${BOT_PORT}` reads shell env + project `.env`,
   NOT the service's `env_file`. Without shell override, staging inherits prod's port and
   fails to bind.

3. **`depends_on: {}` doesn't clear** in Compose v2 — it merges (appends nothing). Alpine
   stubs keep services "defined" so depends_on resolves without running real containers.

4. **`external: true` + `driver: local` merge crash** — if a volume is `external: true` in
   base compose AND `driver: local` in staging override, Compose v2 merges into an invalid
   config → instant `build_failed`. Use separate Compose-level keys; only `name:` must match.

5. **Docker bind mount creates directories** — if a host file path doesn't exist, Docker
   creates it as a directory (not a file). This causes OCI runtime errors on next restart.
   Always ensure bind-mounted files exist before `docker compose up`.

6. **Docker bind mount intermediate dirs** — when a bind mount target nests under a
   non-existent dir, Docker creates the intermediate dir as ROOT. Code running as botuser
   then gets PermissionError. Pre-create ALL intermediate dirs in Dockerfile + entrypoint.

7. **`cap_drop: ALL` blocks in-container root ops** — `docker exec --user root` cannot
   remove files in capability-dropped containers. Fix volume permission issues from the
   host via `docker volume inspect` + `sudo chown`.

### SSH & Git

8. **`GIT_SSH_HOST` must match git remote** — the entrypoint creates SSH host aliases.
   If the git remote uses a custom host alias (e.g. `github-corp`) but `GIT_SSH_HOST` is unset, the SSH config only
   has `github-private` → git push fails with "hostname not found". Always set `GIT_SSH_HOST`
   in `.env` to match the remote's SSH host alias.

9. **SSH config is recreated on every container start** — the entrypoint writes a fresh
   SSH config from environment variables. Manual SSH config changes inside the container
   are lost on restart. All SSH customization must go through env vars.

10. **Deploy key path detection** — if `SSH_KEY_PATH` points to a nonexistent file, Docker
    creates a directory placeholder. Entrypoint detects this and warns, but git push won't work.
    Fix: create the key, then recreate the container (`docker compose down + up`).

### Watchers & Approval

11. **Watcher approval paths must be parametrized** — the watcher derives `APPROVAL_DIR`
    from `PROJECT_NAME` (which comes from `COMPOSE_PROJECT_NAME`). If the watcher script at
    `/opt/` has hardcoded paths from an older version, approval files are written to the wrong
    directory and the watcher never sees them. Always re-install watcher scripts after updates.

12. **Watcher service name ≠ project name** — the systemd service name is whatever you named
    the `.service` file. It may not follow the `$P-watcher` pattern. Use
    `systemctl list-units --type=service | grep watcher` to find the actual name.

13. **Watcher scripts live at `/opt/`** — the bot edits scripts in the git repo, but the
    running watcher reads from `/opt/`. After any watcher script change: copy to `/opt/`,
    then `systemctl restart`. The bot cannot do this itself (root-owned).

14. **Sensitive-file gate blocks silently** — when a deploy touches Dockerfile, compose,
    entrypoint, or service files, the watcher pauses for approval. The bot's deploy_review
    tool now detects `pending_approval` immediately, but the approval script must be run
    manually on the host.

15. **Approval script must be in the same `/opt/` dir as the watcher** — `approve-deploy.sh`
    auto-detects instance name from its install path. If installed in the wrong directory,
    it writes approval to the wrong location.

### Staging OAuth

16. **OAuth copy fails if target is root-owned** — `docker cp` creates root-owned files.
    The staging lifespan tries to `unlink()` + `copy2()` but fails on root-owned files.
    Fix: remove root-owned files from the host via volume mountpoint, or wipe the volume.

17. **`docker volume rm` requires container removal first** — even stopped containers
    hold volume references. Use `docker rm -f` then `docker volume rm`.

18. **OAuth profiles copy succeeds but `.credentials.json` fails separately** — check
    staging logs for both: "Copied N OAuth profile(s)" AND the credentials line. They
    can succeed/fail independently.

### General

19. **Run as botuser** — all Docker/git operations should run as botuser (UID 10001) for
    consistent file ownership. Using root or the host user causes permission mismatches.

20. **`PROD_PROJECT_NAME` for cross-volume mounts** — staging references prod volumes via
    `${PROD_PROJECT_NAME:-corporate-bot}_claude-sessions`. This variable defaults to
    `corporate-bot` — set it explicitly in the staging watcher's systemd service or shell env
    if your project name differs.

## Troubleshooting

### Container won't start
```bash
docker logs <container_name> --tail 50
# Check deploy/last_failed_startup.log for watcher-captured logs
```

### Git push fails inside container
```bash
# Check SSH config was generated correctly
docker exec $P cat /home/botuser/.ssh-config/config
# Verify GIT_SSH_HOST matches remote
docker exec $P git -C /host-repo remote -v
# Test SSH connectivity
docker exec $P ssh -F /home/botuser/.ssh-config/config github-$P -T
```

### OAuth not working on staging
```bash
# Check startup logs
docker logs $P-staging 2>&1 | grep -i "oauth\|credential\|prod-claude"
# Verify prod volume is mounted
docker exec $P-staging ls -la /prod-claude/.credentials.json
# Use staging_command from prod to manage OAuth at runtime
```

### Approval stuck / watcher not consuming approval
```bash
# Check watcher is actually running
systemctl status $P-watcher
# Check watcher logs for approval directory path
journalctl -u $P-watcher -n 50 --no-pager | grep -i approval
# Verify approval dir matches between watcher and approve script
grep APPROVAL_DIR /opt/$P-watcher/host-watcher.sh
ls -la /opt/$P-watcher/approvals/
```

### Volume permission errors
```bash
# Check volume ownership
docker volume inspect ${P}_bot-data -f '{{.Mountpoint}}'
sudo ls -la "$(docker volume inspect ${P}_bot-data -f '{{.Mountpoint}}')"
# Fix: stop container, chown, restart
docker stop $P
VOL=$(docker volume inspect ${P}_claude-sessions -f '{{.Mountpoint}}')
sudo chown -R 10001:999 "$VOL"
docker start $P
```

### Staging uses wrong container name
Verify `COMPOSE_PROJECT_NAME` is set in your `.env`. The staging container name is
derived as `${COMPOSE_PROJECT_NAME}-staging`. If unset, defaults to `corporate-bot-staging`.
