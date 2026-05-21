# Employee Workstation Control Setup Guide

Remote management of Claude Code sessions on employee Mac workstations via SSH
over Tailscale, with Mac-side TOTP authentication for zero-trust security.

## Architecture

```
TG message → Bot (Hetzner Docker) → SSH over Tailscale → Mac
  → guard.sh (TOTP session check)
  → tmux sessions (1 per project)
  → Claude Code CLI
  → Output relay back to TG
```

**Security model**: Zero-trust. The bot server is untrusted. All authentication
happens on the Mac side. Even if both TG account AND bot server are compromised,
the attacker cannot execute commands without a valid TOTP code from the user's
authenticator app.

## Prerequisites

- Mac with macOS (tested on MacBook Pro 2021)
- Hetzner server running the bot in Docker
- Tailscale account (free tier is sufficient)
- Authenticator app (Google Authenticator, 1Password, etc.)

## Mac Setup

### 1. Install tools

```bash
brew install tmux tailscale
pip3 install pyotp
```

### 2. Enable Tailscale

Open Tailscale app → Sign in → note your Mac's Tailscale IP:

```bash
tailscale ip -4
# e.g., 100.x.y.z

tailscale status
# Shows hostname, e.g., your-mac
```

### 3. Enable SSH (Remote Login)

System Settings → General → Sharing → Remote Login → ON
- "Allow access for" → "Only these users" → add your Mac user
- "Allow full disk access for remote users" → OFF (not needed)

Optional hardening — disable password auth:
```bash
sudo nano /etc/ssh/sshd_config
# Add:
# PasswordAuthentication no
# KbdInteractiveAuthentication no
sudo launchctl unload /System/Library/LaunchDaemons/ssh.plist
sudo launchctl load /System/Library/LaunchDaemons/ssh.plist
```

### 4. Set up relay auth directory

```bash
mkdir -p ~/.relay
chmod 700 ~/.relay
```

### 5. Generate TOTP secret

```bash
python3 scripts/desktop-relay/setup_totp.py
```

Or manually:
```bash
python3 -c "
import pyotp
secret = pyotp.random_base32()
print(f'Secret: {secret}')
with open('$HOME/.relay/totp_secret', 'w') as f:
    f.write(secret)
"
```

**Add the secret to your authenticator app** (Google Authenticator / 1Password
→ add account → enter manually → paste the secret string).

### 6. Install guard and auth scripts

Copy from the repo:
```bash
cp scripts/desktop-relay/guard.sh ~/.relay/guard.sh
cp scripts/desktop-relay/auth.sh ~/.relay/auth.sh
chmod +x ~/.relay/guard.sh ~/.relay/auth.sh
```

**Install approval script** (root-owned — bot cannot overwrite):
```bash
sudo cp scripts/desktop-relay/approve-install.sh ~/.relay/approve-install.sh
sudo chown root:staff ~/.relay/approve-install.sh
sudo chmod 755 ~/.relay/approve-install.sh
```

The approval script is required for `relay-install-skills` and `relay-install-daemon` —
these commands now stage payloads for Mac-side review instead of direct-installing.
Run `~/.relay/approve-install.sh` on Mac to review diffs and approve/reject.

### 7. Add bot's SSH key

The bot's public key is:
```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFGudnyQolR6gmiRqtH+7LGJyxUYoWM1nCAngwwspdly corporate-bot-server
```

Add it with command restriction:
```bash
mkdir -p ~/.ssh
echo 'command="$HOME/.relay/guard.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFGudnyQolR6gmiRqtH+7LGJyxUYoWM1nCAngwwspdly corporate-bot-server' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

### 8. Prevent Mac sleep

System Settings → Battery → Options → Prevent automatic sleeping when display is off → ON

Or: `sudo pmset -a disablesleep 1`

### 9. Test locally

```bash
# Test auth
~/.relay/auth.sh YOUR_TOTP_CODE
# Expected: AUTH_OK:<token>:7200

# Test guard with session
export SSH_ORIGINAL_COMMAND="relay-status"
~/.relay/guard.sh
# Expected: SESSION_VALID:XXXs remaining

# Test guard blocks dangerous commands
export SSH_ORIGINAL_COMMAND="rm -rf /"
~/.relay/guard.sh
# Expected: BLOCKED:command not whitelisted

# Test ping (no auth needed)
export SSH_ORIGINAL_COMMAND="relay-ping"
~/.relay/guard.sh
# Expected: PONG:<timestamp>
```

## Hetzner Host Setup

### 1. Install Tailscale on the host

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

### 2. Verify connectivity

```bash
tailscale ping your-mac
# Should show direct or relay connection

ssh -o StrictHostKeyChecking=accept-new youruser@your-mac relay-ping
# Should show: PONG:<timestamp>
```

## Bot Configuration

Add to `.env`:
```bash
DESKTOP_ENABLED=true
DESKTOP_HOST=your-mac
DESKTOP_USER=youruser
DESKTOP_SESSION_TIMEOUT=7200  # 2 hours, optional
```

The bot container reaches the Mac via the host's Tailscale network — no
Tailscale inside Docker needed.

## Security Layers

1. **macOS Remote Login** — only your user allowed
2. **Tailscale** — only devices on your Tailscale network can reach the Mac
3. **SSH key auth** — only the bot's specific ed25519 key in authorized_keys
4. **`command="guard.sh"`** — no interactive shell, only the gatekeeper script
5. **guard.sh whitelist** — only relay-* prefix commands allowed (FB-32 hardened)
6. **TOTP auth** — Mac-side verification, secret never leaves Mac
7. **Session expiry** — 2h default, configurable
8. **Brute-force protection** — 3 failed TOTP attempts → 15 min lockout
9. **Audit log** — every command logged at `~/.relay/audit.log`

## Usage (from Telegram)

```
/mac status        → Shows Mac online/offline, auth status, tmux sessions
/mac auth <code>   → Authenticate with TOTP code
/mac lock          → Immediately revoke auth session
/mac send <project> <prompt>  → Send prompt to project session
```

## Troubleshooting

### SSH connection refused
- Check Tailscale is running on both machines: `tailscale status`
- Check Remote Login is enabled on Mac
- Verify the bot's key is in `~/.ssh/authorized_keys`

### AUTH_REQUIRED
- TOTP session expired — re-authenticate with a fresh code
- Check `~/.relay/session` exists and hasn't expired

### LOCKED_OUT
- Too many failed TOTP attempts
- Wait 15 minutes, or manually: `rm ~/.relay/fail_count`

### BLOCKED
- Command not in guard.sh whitelist
- Check `~/.relay/audit.log` for the blocked command

### Mac asleep
- Enable "Prevent automatic sleeping" in Battery settings
- Or run `caffeinate -s` in a terminal

## Phase 2 — Chat↔Project Linking

### Overview

Session registry maps TG chats to Mac relay sessions. Each chat can be linked
to a specific tmux session + project path. The bot automatically tracks which
session belongs to which chat.

### Registry

Stored at `/app/data/relay_sessions.json` (persistent Docker volume):
```json
{
  "999999999": {
    "session_name": "relay_corpbot",
    "project_path": "~/projects/corporate-bot",
    "created_at": "2026-04-14T21:00:00Z",
    "last_used": "2026-04-14T21:30:00Z"
  }
}
```

Concurrency-safe (asyncio.Lock + atomic writes via tmp+rename).

### MCP Tool Actions

**link** — Map a chat to a session:
```
desktop_relay(action="link", chat_id="999999999", session_name="corpbot", project_path="~/projects/corporate-bot")
```
- Session names auto-prefixed with `relay_`
- Project path validated: no `..`, must start with `/` or `~`

**unlink** — Remove a chat's mapping:
```
desktop_relay(action="unlink", chat_id="999999999")
```

**list_links** — Show all mappings:
```
desktop_relay(action="list_links")
```

**status** — Enhanced to include linked sessions cross-referenced with live daemons:
```
desktop_relay(action="status")
```
Returns `linked_sessions` array with `session_alive` flag for each.

### Workflow: Starting a Claude Code session

1. Authenticate: `desktop_relay(action="auth", totp_code="123456")`
2. Link chat: `desktop_relay(action="link", chat_id="999999999", session_name="myproject", project_path="~/projects/myproject")`
3. Start Claude: `desktop_relay(action="send_keys", session_name="myproject", text="claude -p .")`
4. Interact: `send_keys` to type, `capture` to read output
5. Check status: `desktop_relay(action="status")` shows all linked sessions

### Session Resume

After auth expiry:
1. Re-authenticate with TOTP
2. The persistent SDK daemon auto-respawns on next message to the relay chat
3. Claude Code picks up the last session via `--resume UUID`

### Last-Used Tracking

`send_keys` and `capture` automatically update the `last_used` timestamp for
linked sessions. This enables future cleanup of stale links.

## Phase 3 — Base64 Send + Session Discovery (REMOVED in Session C)

⚠️ **This entire phase was deleted in Session C (commit `ad6a496`, 2026-04-16).**

The `relay-send` base64 tmux command, `send_keys`/`capture` MCP actions, and
`discover` session-scan action were replaced by the Claude relay SDK daemon
pool. `relay-sdk-persistent <chat_id> <uuid> <cwd_b64>` on the Mac spawns a
long-lived daemon per chat that hosts a `ClaudeSDKClient` resumed onto the
pinned UUID. The bot no longer sends keystrokes to tmux — it sends queries
over SSH stdin to the daemon and parses stream-json output on stdout.

Historical note: the Base64 Send mechanism used `tmux load-buffer` + `tmux
paste-buffer` + `tmux send-keys Enter` to inject text. Session Discovery
scanned `~/.claude/sessions/*.json` and `~/.claude/projects/*/`. Both were
superseded by the SDK daemon pinning a single JSONL UUID per relay chat via
`create_relay_group`.

---

## Phase 14 — Automated OAuth Login (SSH Tunnel + Camoufox)

Claude Code sessions sometimes need re-authentication (`/login`). On mobile/remote use
the OAuth redirect to `localhost:PORT` fails because it hits the phone's localhost, not
the Mac's. Phase 14 solves this in two ways:

### Phase 14a — Auto-complete via Camoufox (fully automatic)

When you send `/login` in a relay chat and the OAuth URL appears in the pane:

1. Bot detects `redirect_uri=http://localhost:PORT` in the URL
2. Bot opens SSH tunnel: `bot_server:PORT → Mac:PORT`
   - `guard.sh` validates the `relay-port-forward PORT` command
   - Tunnel stays open while guard.sh sleeps (up to 5 min)
3. Bot opens URL in Camoufox (already has Google SSO session)
4. Camoufox completes Google auth → browser redirects to `localhost:PORT`
5. Redirect goes through tunnel → Mac's Claude Code callback server
6. Claude Code receives the auth code → logs in ✅

**Mac setup required** (one time):
- Remove `no-port-forwarding` from the bot's authorized_keys entry:
  ```bash
  # Edit ~/.ssh/authorized_keys on Mac
  # Change: command="...",no-port-forwarding,...
  # To:     command="...",no-X11-forwarding,no-agent-forwarding,...
  #         (remove no-port-forwarding only)
  ```
- Install latest guard.sh (adds `relay-port-forward` command)

### Phase 14b — Paste-back Fallback (manual, no Mac authorized_keys change required)

If Phase 14a fails (tunnel blocked, Camoufox session expired, etc.):

1. Open the login URL on your phone or Mac browser
2. Complete Google auth — you'll see "connection refused" (expected on phone)
3. Copy the URL from the browser address bar (it looks like `http://localhost:PORT/...?code=...`)
4. Paste it into the relay chat
5. Bot detects the `localhost:PORT` URL, opens SSH tunnel, makes HTTP GET to Mac
6. Mac's Claude Code callback server receives the auth code → logs in ✅

The bot shows "🔑 Replaying OAuth callback to Mac…" and confirms once done.

This fallback works even without removing `no-port-forwarding` from authorized_keys
(it uses the same `relay-port-forward` guard.sh command, but only for a brief moment
to make one HTTP request).

### Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| "SSH tunnel failed" | Port in use on bot server | Retry — auto-cleanup handles stale tunnels |
| "Could not reach Mac callback server" | `no-port-forwarding` still in authorized_keys | Remove that restriction (see Mac setup above) |
| "OAuth timed out (50s)" | Google SSO session expired in Camoufox | Use Phase 14b paste-back instead |
| "connection refused" on phone (expected) | localhost != Mac's localhost | Use Phase 14b — paste the URL into chat |
