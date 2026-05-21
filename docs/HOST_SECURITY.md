# Host Security & Watcher Isolation Guide

## Overview

The bot runs in a Docker container with self-upgrade capabilities. This creates
a potential privilege escalation path: bot modifies code → triggers deploy →
watcher executes on host. This guide documents how to isolate the watcher and
harden the host against container escape.

## Threat Model

**Attack chain without isolation:**
1. Compromised AI model (via prompt injection) gains admin access
2. Bot writes malicious code to `/host-repo/deploy/host-watcher.sh`
3. Watcher executes modified script with host-level docker group privileges
4. Attacker has full host access (docker group ≈ root)

**Mitigations (defense-in-depth):**
- Layer 1: Bot hooks block `docker` CLI, `systemctl`, `git push --force`
- Layer 2: Watcher's sensitive-file gate detects infra changes, requires host approval
- Layer 3: Approval files at `/opt/` (outside Docker mounts — bot cannot reach)
- Layer 4: Commit-bound approvals (approval_id = baseline..target SHA pair)
- Layer 5: Watcher + approval scripts live outside repo, root-owned
- Layer 6: Docker-compose hardening (cap_drop ALL, no-new-privileges)
- Layer 7: `last_healthy_commit` + `deploy_trigger.json` + approval files path-blocklisted

## Step 1: Install Watcher Outside Repo

```bash
# Create isolated watcher directory
sudo mkdir -p /opt/bot-watcher
sudo cp ~/corporate-bot/deploy/host-watcher.sh /opt/bot-watcher/
sudo chown root:root /opt/bot-watcher/host-watcher.sh
sudo chmod 755 /opt/bot-watcher/host-watcher.sh
```

## Step 2: Update Systemd Service

Edit the service file to use the isolated watcher path:

```bash
sudo nano /etc/systemd/system/bot-watcher.service
```

Change `ExecStart` to:
```ini
ExecStart=/opt/bot-watcher/host-watcher.sh
```

Then reload:
```bash
sudo systemctl daemon-reload
sudo systemctl restart bot-watcher
```

## Step 3: Upgrading the Watcher (Manual Review)

When the bot proposes changes to `host-watcher.sh`:

1. Bot shows the diff in chat (it can still READ the file)
2. Review the proposed changes on the host:
   ```bash
   diff /opt/bot-watcher/host-watcher.sh ~/corporate-bot/deploy/host-watcher.sh
   ```
3. If approved, copy and restart:
   ```bash
   sudo cp ~/corporate-bot/deploy/host-watcher.sh /opt/bot-watcher/
   sudo systemctl restart bot-watcher
   ```

## Sensitive-File Approval Gate (Apr 29 2026)

The deploy watcher automatically detects changes to infrastructure files and
pauses for manual operator approval before proceeding with the build.

**Protected files** (auto-executing on deploy):
- `Dockerfile*`, `docker-compose*`, `entrypoint.sh`, `seccomp*`, `.github/workflows/*`

**How it works:**
1. Watcher does `git fetch` (does NOT merge yet)
2. Diffs `last_healthy_commit..origin/main` for sensitive patterns
3. If found → writes `approval_request.json` (with commit-bound approval_id)
4. Writes `pending_approval` to result → pauses
5. Operator runs `sudo /opt/bot-watcher/approve-deploy.sh`
6. Script shows diff, asks y/N, writes commit-bound approval to `/opt/.../approvals/`
7. Watcher validates approval_id matches, proceeds with merge + build

**Approval directory** (`/opt/bot-watcher/approvals/`):
- Owned by `botuser:botuser` (mode 700) — watcher can read/delete, sudo can write
- Outside all Docker mounts — bot cannot reach it
- Contains `approved.json` or `rejected.json` with `approval_id` field

**Setup (one-time):**
```bash
# Create approval directories
sudo mkdir -p /opt/bot-watcher/approvals
sudo chown botuser:botuser /opt/bot-watcher/approvals
sudo mkdir -p /opt/bot-staging-watcher/approvals
sudo chown botuser:botuser /opt/bot-staging-watcher/approvals

# Install approval scripts
sudo cp deploy/approve-deploy.sh /opt/bot-watcher/
sudo cp deploy/approve-deploy.sh /opt/bot-staging-watcher/approve-staging-deploy.sh

# Git safe.directory (approval script runs as root via sudo)
sudo git config --global --add safe.directory /home/botuser/corporate-bot
sudo git config --global --add safe.directory /home/botuser/corporate-bot-staging
```

**Updating approval scripts** (same manual-copy pattern as watcher):
```bash
sudo cp deploy/approve-deploy.sh /opt/bot-watcher/
sudo cp deploy/approve-deploy.sh /opt/bot-staging-watcher/approve-staging-deploy.sh
```

**Backup password** moved to `/opt/bot-backup/.backup-password` (root:botuser 640,
outside all container mounts). Added to `.dockerignore` to prevent baking into Docker image.

## Backup Script Isolation

The hourly backup cron runs `/opt/bot-backup/backup.sh` as `botuser`. If the bot could
write to this script, it could inject host-level commands that run outside the Docker container.
This isolation applies the same strategy as the deploy watcher.

**Threat model:**
1. Compromised AI model (via prompt injection) gains admin access
2. Bot modifies `/opt/bot-backup/backup.sh` (which lives outside the repo)
3. Cron executes the modified script as `botuser` on the host — arbitrary host-level code runs

**Mitigations:**
- Bot path blocklist blocks writes to any path containing `bot-backup`
- Bot bash blocklist blocks shell commands referencing `bot-backup`
- Script lives at `/opt/bot-backup/` (outside repo mount — not writable even if hooks fail)
- `.backup-password` file is also path-blocked (encryption key must not be read/modified by bot)

**One-time host setup:**
```bash
sudo mkdir -p /opt/bot-backup
sudo cp /home/botuser/corporate-bot/scripts/backup.sh /opt/bot-backup/backup.sh
sudo chown -R botuser:botuser /opt/bot-backup
sudo chmod 755 /opt/bot-backup/backup.sh
# Update crontab (as botuser) to point to /opt/bot-backup/backup.sh
sudo -u botuser crontab -e
# Change: /home/botuser/corporate-bot/scripts/backup.sh → /opt/bot-backup/backup.sh
```

**Upgrading the backup script (manual review — same process as watcher):**

1. Bot proposes changes to `scripts/backup.sh` in chat (it can READ the repo copy)
2. Review the proposed changes on the host:
   ```bash
   diff /opt/bot-backup/backup.sh /home/botuser/corporate-bot/scripts/backup.sh
   ```
3. If approved, copy:
   ```bash
   sudo -u botuser cp /home/botuser/corporate-bot/scripts/backup.sh /opt/bot-backup/backup.sh
   ```
   No service restart needed — cron picks up the new script on the next run.

## Step 4: Docker-Compose Hardening (Recommended)

Add these security settings to `docker-compose.yml` under the `bot-core` service:

```yaml
bot-core:
  # ... existing config ...
  cap_drop:
    - ALL
  security_opt:
    - no-new-privileges:true
```

Note: No `cap_add` needed — the entrypoint runs as `botuser` (non-root), so CHOWN/SETUID/SETGID are unnecessary. Port 8000 is unprivileged (>1024).

## Step 5: Deploy Key Restrictions (Recommended)

If using SSH deploy key for `git push`, restrict it on GitHub:

1. Use a **read-write deploy key** (not personal access token)
2. Set the key as repository-scoped (not org-wide)
3. Consider using short-lived GitHub tokens instead:
   ```bash
   # Generate a 1-hour token via GitHub CLI
   gh auth token --expiry 1h
   ```

## Step 6: Docker Socket Proxy (Advanced, Optional)

Replace direct Docker socket mount with a restricted proxy:

```yaml
services:
  docker-proxy:
    image: tecnativa/docker-socket-proxy:edge
    environment:
      CONTAINERS: 1    # allow ps, logs
      SERVICES: 1      # allow compose
      IMAGES: 1        # allow build
      BUILD: 1         # allow compose build
      POST: 1          # allow restart
      # Everything else denied by default
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks:
      - internal

  bot-core:
    # Replace socket mount with proxy:
    # - /var/run/docker.sock:/var/run/docker.sock  # REMOVE THIS
    environment:
      - DOCKER_HOST=tcp://docker-proxy:2375
```

This restricts the bot to only the Docker operations it needs (build, restart, logs)
while blocking dangerous operations (run, exec, cp, save, load).

## Step 7: OAuth Token Refresh Cron

The Claude Code CLI uses OAuth tokens (~3h TTL). The CLI auto-refreshes during
active API calls, but if no requests arrive near the expiry window, the token
expires silently and the bot returns `401 authentication_error`.

**Fix:** A host-side cron job that refreshes the token every 2 hours using the
refresh token already stored inside the container.

```bash
# Install (runs as your admin user, NOT botuser — needs docker exec)
chmod +x ~/corporate-bot/scripts/refresh_token_cron.sh
(crontab -l 2>/dev/null; echo "0 */2 * * * $HOME/corporate-bot/scripts/refresh_token_cron.sh >> $HOME/corporate-bot/deploy/token_refresh.log 2>&1") | crontab -
```

**Security model:**
- Runs as your admin user (needs `docker exec` to read/write container credentials)
- The refresh token never leaves the container's `~/.claude/.credentials.json`
- Calls `https://platform.claude.com/v1/oauth/token` with the standard OAuth2 refresh flow
- Skips refresh if token has >30 minutes remaining (avoids unnecessary API calls)
- Logs to `deploy/token_refresh.log`

**If the refresh token itself expires** (happens after extended outages), you must
re-authenticate from a machine with Claude Code logged in:
```bash
# From macOS (or any machine with valid Claude Code login):
SERVER=user@your-server SSH_KEY=~/.ssh/key CONTAINER=corporate-bot \
  ./scripts/refresh_server_token.sh
```

## Bot-Side Protections (Already Implemented)

The bot's `hooks.py` blocks:

**File writes blocked for:**
- `.env`, credentials, tokens, secrets, SSH keys
- `host-watcher.sh`, `bot-watcher.service`
- `bot-backup` (backup script at `/opt/bot-backup/`)
- `.backup-password` (backup encryption key)
- `dockerfile`, `docker-compose.yml`
- `deploy_trigger.json` (only writable via `deploy_bot` tool, not directly)

**Bash commands blocked:**
- `docker run` (container escape vector)
- `docker build` (except via compose)
- `docker cp/save/load/import/export`
- `docker inspect` (env var leak)
- `git reset --hard`, `git push --force` (destructive git)
- `systemctl` (systemd control)
- `sudo`, `su`, `chmod +s` (privilege escalation)
- `ssh` (except keyscan)
- `curl` with file upload or POST to non-localhost
- `wget`, `nc`, `netcat`
- `/proc/`, `/sys/` access
- `deploy_trigger.json` (prevents manual trigger file creation via bash)
- `bot-backup` (backup script at `/opt/bot-backup/`)
- `.backup-password` (backup encryption key — read AND write blocked)

## Verification Checklist

After setup, verify isolation:

```bash
# 1. Watcher is outside repo
ls -la /opt/bot-watcher/host-watcher.sh
# Should be: -rwxr-xr-x root root

# 2. Systemd uses isolated path
systemctl cat bot-watcher | grep ExecStart
# Should show: /opt/bot-watcher/host-watcher.sh

# 3. Bot cannot write to watcher (test from inside container)
docker exec corporate-bot python3 -c "
from bot.hooks import _check_file_path
print(_check_file_path('/host-repo/deploy/host-watcher.sh'))
"
# Should print: Access denied: path contains 'host-watcher.sh'

# 4. Bot cannot run docker run
docker exec corporate-bot python3 -c "
from bot.hooks import _check_bash_command
print(_check_bash_command('docker run --rm alpine echo hello'))
"
# Should print: Access denied: command matches security blocklist
```

## Infrastructure Update Flows

Three categories of infrastructure files require different update flows. All share
the same principle: **the operator manually installs to the live location**, which
provides a built-in human review gate.

### 1. Server Infra (Dockerfile, compose, entrypoint)

**Flow**: Bot edits repo → `git push` → deploy → watcher detects sensitive-file
change → `approve-deploy.sh` → operator reviews diff → approve/reject → watcher
continues or rolls back.

**Live location**: Docker image (rebuilt from repo by watcher).
**Human gate**: `approve-deploy.sh` on the host.
**No manual install step** — Docker build handles it automatically after approval.

### 2. Backup Script (`scripts/backup.sh`)

**Flow**: Bot edits `/host-repo/scripts/backup.sh` → deploy → watcher rebuilds
container (backup.sh is in repo but executed from `/opt/`).

**Live location**: `/opt/bot-backup/backup.sh` (root-owned, outside repo).
**Human gate**: Operator manually copies after reviewing:
```bash
sudo cp /home/botuser/corporate-bot/scripts/backup.sh /opt/bot-backup/backup.sh
sudo chown botuser:botuser /opt/bot-backup/backup.sh
sudo chmod 755 /opt/bot-backup/backup.sh
```

### 3. Guard.sh + Relay Scripts (`scripts/desktop-relay/`)

**Flow**: Bot edits `/host-repo/scripts/desktop-relay/guard.sh` → deploy →
container has updated repo copy, but Mac still runs old version.

**Live location**: `~/.relay/guard.sh` on Mac (user-owned).
**Human gate**: Operator manually syncs after reviewing:
```bash
# From server (as botuser):
scp /home/botuser/corporate-bot/scripts/desktop-relay/guard.sh youruser@mac:~/.relay/guard.sh

# Or from Mac (if repo is pulled):
cp ~/dev/corporate-bot/scripts/desktop-relay/guard.sh ~/.relay/guard.sh
```

Same flow for daemon and other relay scripts:
```bash
# Daemon (also auto-installable via desktop_relay install_daemon action):
scp scripts/desktop-relay/sdk_persistent_daemon.py youruser@mac:~/.relay/sdk_persistent_daemon.py
```

**Post-deploy reminder**: `deploy_status` auto-detects when the deployed commit
range touches `scripts/desktop-relay/*` and reminds the operator to sync.

### Why No Automated Guard.sh Updates

Guard.sh is the SSH security gate on the Mac — it validates every command the bot
can execute remotely. A self-update mechanism (`relay-install-guard`) would be a
**circular trust violation**: the entity being gated could replace its own gate.
Even with content validation (shebang, size, mandatory patterns), a structurally
valid guard.sh can still be permissive. Manual sync is intentional — the operator
reviews the diff before installing.

### Host Watcher Scripts

The watchers themselves follow the same manual-install pattern:
```bash
# After updating deploy/host-watcher.sh in repo:
sudo cp /home/botuser/corporate-bot/deploy/host-watcher.sh /opt/bot-watcher/host-watcher.sh
sudo systemctl restart bot-watcher

# Staging watcher:
sudo cp /home/botuser/corporate-bot/deploy/staging-watcher.sh /opt/bot-staging-watcher/staging-watcher.sh
sudo systemctl restart staging-watcher
```
