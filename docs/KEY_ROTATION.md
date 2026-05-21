# Key Rotation Procedures

This document covers rotation procedures for all secrets and credentials
used by the Corporate Bot platform.

## 1. Claude OAuth Tokens

**Type:** OAuth access token (sk-ant-* prefix)
**Rotation:** Automatic via refresh token mechanism
**Expiry:** Access tokens expire in ~1 hour; refresh tokens in ~90 days
**Location:** `OAUTH_PROFILES_DIR/<label>.json` and `~/.claude/.credentials.json`

**Automatic refresh:**
- Bot scheduler pre-refreshes tokens every ~50 minutes (10 min before expiry)
- SDK auto-refreshes via `.credentials.json` on subprocess spawn
- No manual intervention needed under normal operation

**Manual rotation (if refresh token expires):**
```bash
# In-container (preferred):
/claude-auth <label>
# Follow prompts, then /reset

# Or generate new token:
docker exec -it <container> su -c 'claude setup-token' botuser
# Then use manage_oauth(action="add", label="...", token="sk-ant-...")
```

**90-day expiry prevention:** Ensure at least one bot query runs every 90 days
per OAuth profile. The scheduler's token pre-refresh keeps tokens alive
automatically as long as the bot is running.

## 2. Azure AD Client Secrets (Teams Bot)

**Type:** Client secret for Azure AD app registration
**Rotation:** Manual via Azure Entra ID portal
**Expiry:** Configurable (recommended: 24 months)
**Location:** `TEAMS_APP_SECRET` in `.env`

**Rotation procedure:**
1. Go to Azure Portal > App Registrations > Corporate Bot app
2. Certificates & Secrets > New Client Secret
3. Set description and expiry (24 months recommended)
4. Copy the new secret value (only shown once)
5. Update `.env` on the host:
   ```bash
   # Edit .env, update TEAMS_APP_SECRET=<new-secret>
   nano ~/corporate-bot/.env
   ```
6. Restart the bot container:
   ```bash
   docker compose restart bot-core
   ```
7. Verify: check `/health` endpoint for `teams_connected: true`
8. Delete the old secret from Azure Portal

**Monitoring:** Set a calendar reminder 30 days before expiry.
The bot does NOT auto-detect expiring client secrets.

## 3. Azure AD Client Secret for M365 MCP (Outlook/Calendar)

**Type:** Client secret for delegated M365 access
**Rotation:** Manual via Azure Entra ID portal
**Expiry:** Configurable (recommended: 24 months)
**Location:** `MS365_MCP_CLIENT_SECRET` in `.env`

**Rotation procedure:** Same as Teams Bot (step 2 above), but for the
M365 MCP app registration. Update `MS365_MCP_CLIENT_SECRET` in `.env`.

After rotation, the existing MSAL cache refresh token may still work.
If not, re-authenticate:
```bash
# In-container:
/m365-auth
# Then /reset
```

## 4. Token Encryption Key (Fernet)

**Type:** Fernet symmetric encryption key (base64-encoded 32 bytes)
**Rotation:** Manual — requires re-encryption of all stored tokens
**Location:** `TOKEN_ENCRYPTION_KEY` in `.env`

**WARNING:** Rotating this key invalidates ALL encrypted tokens in the
`user_tokens` and `credential_store` tables. All users must re-authenticate.

**Rotation procedure:**
1. Generate new key:
   ```bash
   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
2. (Optional) Export existing tokens with old key before rotating
3. Update `TOKEN_ENCRYPTION_KEY` in `.env`
4. Restart container
5. All users will need to re-authenticate via Teams SSO

**Recommendation:** Only rotate if the key is compromised. The key never
leaves the `.env` file (not in code, not in logs, not in git).

## 5. GitHub Webhook Secret

**Type:** HMAC-SHA256 shared secret
**Rotation:** Manual — update in both GitHub and `.env`
**Location:** `GITHUB_WEBHOOK_SECRET` in `.env`

**Rotation procedure:**
1. Generate new secret:
   ```bash
   openssl rand -hex 32
   ```
2. Update in GitHub: Repository > Settings > Webhooks > Edit > Secret
3. Update `GITHUB_WEBHOOK_SECRET` in `.env`
4. Restart container
5. Verify: push a commit and check that re-indexing triggers

## 6. Backup Encryption Password

**Type:** 7-Zip AES-256 encryption password
**Rotation:** Manual
**Location:** `.backup-password` (mode 600, host filesystem)

**Rotation procedure:**
1. Generate new password:
   ```bash
   openssl rand -base64 24 > ~/corporate-bot/.backup-password.new
   chmod 600 ~/corporate-bot/.backup-password.new
   ```
2. Keep the OLD `.backup-password` until all old backups past retention are purged
3. Rename:
   ```bash
   mv .backup-password .backup-password.old
   mv .backup-password.new .backup-password
   ```
4. Verify next backup runs successfully:
   ```bash
   sudo -u botuser /opt/corporate-bot-backup/backup.sh --local-only
   ```
5. After retention period (oldest backup expires), remove `.backup-password.old`

**WARNING:** Losing the backup password makes ALL encrypted backups
unrecoverable. Store it securely outside the server (e.g., password manager).

## 7. TOTP Secret (Desktop Relay)

**Type:** TOTP shared secret (base32-encoded)
**Rotation:** Manual
**Location:** `.relay_totp_secret` on Mac, env var on server

**Rotation procedure:**
1. On Mac, run:
   ```bash
   python3 scripts/desktop-relay/setup_totp.py
   ```
2. Copy the new secret to the server's `.env` as `RELAY_TOTP_SECRET`
3. Restart container
4. Verify: `/auth <code>` with a fresh TOTP code

## 8. SSH Deploy Key

**Type:** Ed25519 SSH key pair
**Rotation:** Manual
**Location:** Container's `~/.ssh/id_ed25519_bot`

**Rotation procedure:**
1. Generate new key pair:
   ```bash
   ssh-keygen -t ed25519 -f /tmp/new_bot_key -N ""
   ```
2. Add the new PUBLIC key to GitHub: Repository > Settings > Deploy Keys
3. Copy the new PRIVATE key into the container:
   ```bash
   docker cp /tmp/new_bot_key <container>:/home/botuser/.ssh/id_ed25519_bot
   docker exec <container> chmod 600 /home/botuser/.ssh/id_ed25519_bot
   docker exec <container> chown botuser:botuser /home/botuser/.ssh/id_ed25519_bot
   ```
4. Verify: `docker exec <container> su -c 'ssh -T git@github.com' botuser`
5. Remove the old deploy key from GitHub

## 9. Google OAuth Credentials

**Type:** OAuth 2.0 client credentials + refresh tokens
**Rotation:** Automatic for tokens; manual for client secret
**Location:** `data/google-workspace-creds/` (volume-mounted)

**Token refresh:** Automatic — bot scheduler pre-refreshes every ~50 minutes.

**Client secret rotation:**
1. Google Cloud Console > APIs & Services > Credentials
2. Edit the OAuth client > Reset Secret
3. Update the client secret in the credential JSON files
4. Restart container

## Rotation Schedule Summary

| Secret                    | Auto-Refresh | Manual Rotation     | Recommended Interval |
|---------------------------|-------------|---------------------|---------------------|
| Claude OAuth tokens       | Yes (50min) | If refresh expires  | N/A (automatic)     |
| Azure AD client secrets   | No          | Portal + .env       | 24 months           |
| M365 client secret        | No          | Portal + .env       | 24 months           |
| Token encryption key      | No          | Re-encrypt all      | Only if compromised |
| GitHub webhook secret     | No          | GitHub + .env       | 12 months           |
| Backup password           | No          | File replacement    | 12 months           |
| TOTP secret               | No          | Re-generate         | Only if compromised |
| SSH deploy key            | No          | Key pair rotation   | 24 months           |
| Google OAuth              | Yes (50min) | If client rotated   | N/A (automatic)     |
