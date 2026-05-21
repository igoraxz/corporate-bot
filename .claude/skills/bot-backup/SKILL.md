---
description: "Backup & restore procedures: hourly cron, WAL-safe SQLite, encrypted 7z, Mac/GDrive sync, bare-server restore wizard."
---

## Backup & Restore

Encrypted backups (hourly cron, 2h effective cadence) of ALL bot data (single 7z file, AES-256, `scripts/backup.sh`).
Full bare-server restore wizard (`scripts/restore.sh`).
**Canonical install path: `/opt/corporate-bot-backup/backup.sh`** (outside repo — bot cannot modify it).
See `docs/HOST_SECURITY.md` "Backup Script Isolation" for one-time setup.

### Backup (`scripts/backup.sh` → installed at `/opt/corporate-bot-backup/backup.sh`)
- Runs hourly via cron on HOST machine; self-throttles to 2h (`BACKUP_INTERVAL_S`, default 7200s)
- WAL-safe SQLite snapshots via `VACUUM INTO`
- Backs up: all DBs, TG/WA sessions, Google/MS365/Claude OAuth, SDK sessions, media cache, configs, .env, deploy state
- `--with-staging`: include staging Docker volumes
- `--with-mac`: collect Mac-side data (`~/.relay/`, `~/.claude/`) via guard.sh
- `--local-only`: skip upload to Mac/GDrive
- `--dry-run`: show what would be backed up
- Destinations: Mac via SSH guard.sh + GDrive via rclone — symmetric retention: 12×2h + 7 daily + 4 weekly + monthly forever
- Mac transport: `relay-backup-put/list/prune/collect` commands in guard.sh (TOTP-exempt for cron)
- Security: /dev/shm temp dir, chmod 700, password from file only, signal trapping, TOTP secret excluded from Mac collect
- Password: `.backup-password` (mode 600, gitignored). Generate: `openssl rand -base64 24 > .backup-password`
- Naming: `bot-backup-YYYY-MM-DD-HH00.7z`

### Restore (`scripts/restore.sh`)
- From bare Ubuntu to running bot in one command
- Installs Docker, git, 7z, rclone, python3, curl
- `--from-mac`: fetch latest backup from Mac via SSH guard.sh
- `--list-mac`: list available backups on Mac
- `--verify <file>`: verify archive integrity without extracting
- `--force`: skip safety prompts
- Restores: all Docker volumes, .env, git repo, deploy state, rclone config
- Mac-side data restored interactively (prompts before overwriting)
- Post-restore: health check + checklist (TG session, WA QR, test message)

### Host Setup (one-time)

All backup infrastructure runs as `botuser`. All files must be owned by `botuser:botuser`.

```bash
# Prerequisites
sudo apt install p7zip-full rclone

# Encryption password
sudo -u botuser bash -c 'openssl rand -base64 24 > /home/botuser/corporate-bot/.backup-password'
sudo chown botuser:botuser /home/botuser/corporate-bot/.backup-password
chmod 600 /home/botuser/corporate-bot/.backup-password

# SSH key: uses bot's id_ed25519_bot (same key guard.sh trusts, TOTP-exempt for backup cmds)
# Ensure /home/botuser/.ssh/id_ed25519_bot exists (copied from container's botuser key)

# GDrive: set up rclone (see GDrive Setup below)

# Install backup script outside repo (security isolation — mirrors deploy watcher)
sudo mkdir -p /opt/corporate-bot-backup
sudo cp /home/botuser/corporate-bot/scripts/backup.sh /opt/corporate-bot-backup/backup.sh
sudo chown -R botuser:botuser /opt/corporate-bot-backup
sudo chmod 755 /opt/corporate-bot-backup/backup.sh

# Test (using the installed copy)
sudo -u botuser /opt/corporate-bot-backup/backup.sh --dry-run
sudo -u botuser /opt/corporate-bot-backup/backup.sh --local-only

# Cron (hourly, as botuser — self-throttles to 2h)
sudo -u botuser crontab -e
# Add: 0 * * * * /opt/corporate-bot-backup/backup.sh >> /home/botuser/corporate-bot/deploy/backup.log 2>&1
```

### GDrive Setup — `drive.file` scoped OAuth (personal Google account)

**Why not service account?** Service accounts have no storage quota on personal Google Drive
(`storageQuotaExceeded` 403). Domain-wide delegation (SA impersonating a user) requires Google
Workspace. `drive.file` scoped OAuth is the correct approach for personal Gmail.

**What `drive.file` scope means:** token stored on server can ONLY see/modify files rclone itself
uploaded — zero access to existing Drive content. If token leaks, attacker sees only backup files.

**One-time setup:**

1. GCP Console → APIs & Services → Credentials → Create Credentials → OAuth client ID
   - Application type: **Desktop app**, Name: "rclone-backup"
   - Note the Client ID and Client Secret

2. Configure OAuth consent screen: add scope `https://www.googleapis.com/auth/drive.file`

3. **Run `rclone config` on Mac** (NOT on the headless server — Google deprecated OOB OAuth):
   - `n` → new remote, name: `gdrive`, storage: `drive`
   - Enter Client ID and Client Secret from step 1
   - Scope: `drive.file` (option 2 in rclone's scope list)
   - `root_folder_id`: GDrive folder ID from URL (e.g. `1ifIErFCSvdAV3UyDK8eXyuv8gD9FrCnq`)
   - `service_account_file`: (blank)
   - Edit advanced config: `n`
   - **Use auto config: `y`** (opens browser on Mac for OAuth consent)
   - Authorize in browser, rclone captures the token via local redirect

4. Copy config to server:
   ```bash
   scp ~/.config/rclone/rclone.conf tao-backup:/tmp/rclone.conf
   # On server:
   sudo mkdir -p /home/botuser/.config/rclone
   sudo mv /tmp/rclone.conf /home/botuser/.config/rclone/
   sudo chown -R botuser:botuser /home/botuser/.config/rclone
   sudo chmod 600 /home/botuser/.config/rclone/rclone.conf
   ```

5. Test (as botuser):
   ```bash
   sudo -u botuser rclone ls gdrive:   # empty = OK (first run)
   sudo -u botuser rclone copy /tmp/test.txt gdrive: -v   # should succeed
   ```

**rclone config file location:** `/home/botuser/.config/rclone/rclone.conf` (mode 600, owned by
botuser). Backed up automatically by `backup.sh` (step 2 copies it into the archive).
The backup script uses this config directly — no service account file needed.

### Pitfalls (learned the hard way)

- **Google deprecated OOB OAuth** (2022): `rclone config` with `auto config = n` on a headless
  server fails with `Error 400: invalid_request`. Workaround: run `rclone config` on a machine
  with a browser (Mac), then scp the config to the server.
- **Wrong scope on first try**: If you accidentally configured `drive.readonly`, edit the remote
  in `rclone config` → change scope to `drive.file` → re-authorize.
- **All files must be botuser-owned**: backup.sh, .backup-password, rclone.conf, lock file,
  SSH keys. If any are owned by root or another user, the cron job (running as botuser) will
  fail silently or hang.
- **Lock file (`/tmp/bot-backup.lock`)**: Uses flock. If a previous run was killed, the lock
  file may remain but flock handles it correctly. If permission-denied: `sudo rm -f /tmp/bot-backup.lock`.
- **833MB archive**: Full backup takes ~2.5 min on tao-backup. GDrive upload takes additional
  time depending on bandwidth. Mac upload via Tailscale is slower (~5-10 min for 833MB).
- **Cron runs as botuser**: Use `sudo -u botuser crontab -e`, NOT root crontab.
- **`drive.file` scope**: rclone can ONLY see/modify files it created — not other Drive files.
  This is a security feature, not a limitation.
