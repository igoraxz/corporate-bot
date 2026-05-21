# Employee Workstation Control — Operator Guide

Full setup flow for linking the corporate-bot (Docker container) to Claude Code
sessions on employee Mac workstations via SSH-over-Tailscale with zero-trust TOTP auth.

**Audience**: IT operator bringing up workstation control from scratch, or rebuilding
after infrastructure change (new employee Mac, new server host, new Tailscale tenant).

**Prerequisites**: Mac, Hetzner VPS running the bot, Tailscale account, an
authenticator app (Google Authenticator, 1Password, etc.).

---

## Overview — The 5 Layers

```
┌─────────────────────────────────────────────────────────────┐
│  TG message → Bot (Hetzner)                                 │
│                    ↓                                         │
│  Layer 1: Mac IP resolver (5-tier chain)                    │
│           env pin → Tailscale OAuth API → DNS → disk → hardcoded │
│                    ↓                                         │
│  Layer 2: SSH over Tailscale (direct or DERP relay)         │
│                    ↓                                         │
│  Layer 3: guard.sh on Mac (command whitelist)               │
│                    ↓                                         │
│  Layer 4: TOTP session check (2h sliding TTL)               │
│                    ↓                                         │
│  Layer 5: Persistent SDK daemon per chat                    │
│           (claude CLI subprocess, pinned to a JSONL UUID)   │
└─────────────────────────────────────────────────────────────┘
```

**Security model**: zero-trust. Bot server is untrusted. All authentication
decisions happen on the Mac side. Even if the TG account AND bot server are
both compromised, the attacker cannot execute anything without a valid TOTP
code from the user's authenticator app.

---

## Part 1 — Mac Setup (one-time)

### 1.1 Install tools

```bash
brew install tmux
pip3 install pyotp  # Use pyenv python — see note below
```

**pyotp install note**: `auth.sh` uses pyenv's python3 at
`/Users/youruser/.pyenv/versions/3.12.7/bin/python3`. Homebrew/anaconda pythons
do NOT have pyotp. Install with pyenv:

```bash
pyenv shell 3.12.7
pip install pyotp
```

### 1.2 Install Tailscale (Mac)

Install the Tailscale app from App Store → sign in → confirm the Mac appears
at <https://login.tailscale.com/admin/machines>.

Get the Mac's tailnet IP + hostname:

```bash
tailscale ip -4          # e.g. 100.x.y.z
tailscale status | head  # hostname, e.g. your-mac
```

### 1.3 Enable Remote Login

System Settings → General → Sharing → Remote Login → **ON**
- "Allow access for" → "Only these users" → add your Mac user
- "Allow full disk access for remote users" → **OFF** (not needed)

Optional hardening (disable password auth, require SSH key only):

```bash
sudo nano /etc/ssh/sshd_config
# PasswordAuthentication no
# KbdInteractiveAuthentication no
sudo launchctl unload /System/Library/LaunchDaemons/ssh.plist
sudo launchctl load /System/Library/LaunchDaemons/ssh.plist
```

### 1.4 Prevent Mac sleep

System Settings → Battery → Options → "Prevent automatic sleeping when display is off" → **ON**

Or: `sudo pmset -a disablesleep 1`

### 1.5 Create `~/.relay` directory + TOTP secret

```bash
mkdir -p ~/.relay
chmod 700 ~/.relay

# Generate TOTP secret
python3 -c "
import pyotp
secret = pyotp.random_base32()
print(f'Secret: {secret}')
with open('$HOME/.relay/totp_secret', 'w') as f:
    f.write(secret)
"
```

**Add the printed secret to your authenticator app** (Google Authenticator →
Add account → Enter a setup key → paste secret → save).

### 1.6 Install guard.sh + auth.sh from repo

```bash
cd ~/dev/corporate-bot  # or wherever the repo clone is
cp scripts/desktop-relay/guard.sh ~/.relay/guard.sh
cp scripts/desktop-relay/auth.sh ~/.relay/auth.sh
chmod +x ~/.relay/guard.sh ~/.relay/auth.sh
```

### 1.7 Add the bot's SSH public key to `~/.ssh/authorized_keys`

The bot's public key (canonical):

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFGudnyQolR6gmiRqtH+7LGJyxUYoWM1nCAngwwspdly corporate-bot-server
```

Append with `command=` restriction — this forces every SSH connection to go
through guard.sh:

```bash
mkdir -p ~/.ssh
cat >> ~/.ssh/authorized_keys <<'EOF'
command="$HOME/.relay/guard.sh",no-X11-forwarding,no-agent-forwarding ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFGudnyQolR6gmiRqtH+7LGJyxUYoWM1nCAngwwspdly corporate-bot-server
EOF
chmod 600 ~/.ssh/authorized_keys
```

**Note**: `no-port-forwarding` is **NOT** in the restrictions — port forwarding
is needed for Claude CLI OAuth re-auth flow. Port-forward scope is further
restricted inside `guard.sh` to specific whitelisted commands.

### 1.8 Sanity check (local Mac)

```bash
# TOTP works
~/.relay/auth.sh 123456  # use a real code from authenticator
# Expected: AUTH_OK:<token>:7200

# Ping works without auth
SSH_ORIGINAL_COMMAND="relay-ping" ~/.relay/guard.sh
# Expected: PONG:<timestamp>

# Dangerous command blocked
SSH_ORIGINAL_COMMAND="rm -rf /" ~/.relay/guard.sh
# Expected: BLOCKED:...
```

---

## Part 2 — Tailscale Permanent Setup (bot side)

This section is the **2026-04 addition** that replaces the old hardcoded IP
pin. Makes the bot survive Mac IP changes (Wi-Fi moves, re-auth, tailnet
expiry) without any code change or restart.

### 2.1 Disable Mac node key expiry

Tailscale by default expires a node's key after 180 days of non-interaction.
When that happens the Mac silently drops off the tailnet and all SSH breaks.

1. Go to <https://login.tailscale.com/admin/machines>
2. Find `your-mac` (or your Mac's hostname)
3. Three-dot menu → **Disable key expiry**
4. Confirm

Once disabled the Mac stays on the tailnet forever (until you manually revoke
it).

### 2.2 Create a Tailscale OAuth client

The bot uses this to query the tailnet API for the Mac's current IP. Read-only
scope — the bot cannot modify anything.

1. Go to <https://login.tailscale.com/admin/settings/oauth>
2. Click **Generate OAuth client...**
3. Name: `corporate-bot-server` (or similar)
4. Scope: **tick ONLY `devices:read`** — no `devices:write`, no `routes:*`,
   nothing else
5. **Save** — copy the Client ID and Client Secret that appear (shown once)

### 2.3 Enable MagicDNS (optional but recommended)

Makes `your-mac` resolvable as a hostname tailnet-wide.
Acts as a backup tier in the bot's resolver chain.

1. Go to <https://login.tailscale.com/admin/dns>
2. Toggle **MagicDNS** → **ON**
3. Under "Nameservers" → add `1.1.1.1` (or Google `8.8.8.8`) if none present
4. Save

**Limitation**: the bot Docker container itself is NOT a tailnet node, so
MagicDNS doesn't work from inside the container. This is why we have the
OAuth API tier (2.2) — that's the authoritative source.

### 2.4 Write env vars to bot's `.env`

On the Hetzner host, edit `/home/botuser/corporate-bot/.env` (or wherever your repo
lives) and append:

```bash
# Tailscale OAuth (read-only devices scope)
TAILSCALE_OAUTH_CLIENT_ID=<paste Client ID from step 2.2>
TAILSCALE_OAUTH_CLIENT_SECRET=<paste Client Secret from step 2.2>

# Optional — pin a specific IP if you want to skip the OAuth API tier
# DESKTOP_HOST_IP=100.x.y.z

# Relay configuration
DESKTOP_ENABLED=true
DESKTOP_HOST=your-mac
DESKTOP_USER=youruser
```

### 2.5 Rebuild bot-core to pick up env

```bash
cd /home/botuser/corporate-bot
docker compose up -d --force-recreate bot-core
```

**Important**: `docker compose restart` does NOT reload `.env` — you must
`--force-recreate` or fully `down` + `up`. This caught us in production;
symptom is the env vars appear to be set on the host but `docker exec env`
doesn't show them in the container.

### 2.6 Verify Tier 2 (Tailscale API) is firing

Wait ~60s after restart, then check bot logs:

```bash
docker logs corporate-bot --since 2m 2>&1 | grep -iE 'mac_resolver|provider='
```

Expected line:

```
mac_resolver: IP unchanged provider=tailscale ip=100.x.y.z elapsed=4.80s
```

If you see `provider=dns` or `provider=default` instead, Tier 2 failed:

- Check env actually reached container: `docker exec corporate-bot env | grep TAILSCALE`
- If missing: step 2.5 wasn't done (`restart` instead of `--force-recreate`)
- Check for trailing newline in secret: `tail -c 80 .env | od -c | tail`
- Check OAuth scope is `devices:read` only (step 2.2)

### 2.7 Force-test the resolver

```bash
docker exec corporate-bot python3 -c "
import asyncio
from bot import mac_resolver
r = asyncio.run(mac_resolver.refresh_mac_ip(force=True))
print(r)
"
```

Expected output: `RefreshResult(ip='100.x.y.z', provider='tailscale', changed=False, elapsed_s=0.18)`

First call after restart takes ~4-5s (OAuth token fetch), subsequent calls
~0.2s (token cached).

---

## Part 3 — End-to-End Smoke Test

From any team chat in Telegram, send:

```
/mac
```

Bot should reply with status + auth remaining time. If `🟢 authenticated` →
working. If `🔒 not authenticated` → send TOTP code via `/auth <6 digits>`.

Test a relay-mode group:

```
# In Telegram, to the bot DM:
desktop_relay: create_relay_group, group_title: "🖥 my-project", pinned_uuid: <optional>
```

Bot creates the TG group, pins a JSONL, spawns a daemon on Mac, sends a
catchup digest of the last 10 turns.

---

## Part 4 — Operational Playbook

### Routine: TOTP re-auth

TOTP session is 2h **sliding** — every successful relay activity resets the
clock. Expect to manually re-auth once or twice per coding day of heavy use:

```
/auth 123456
```

### Mac IP changed

Bot auto-detects within ~60s (watchdog tick). No manual action needed if
Part 2 is configured. If you see SSH failures longer than 2min:

```bash
docker exec corporate-bot python3 -c "
import asyncio
from bot import mac_resolver
print(asyncio.run(mac_resolver.refresh_mac_ip(force=True)))
"
```

### Mac is offline for hours

Watchdog alerts admin when Mac goes offline (≥60s) and again when it comes
back. On recovery, the bot kicks all relay daemons to force a fresh spawn +
delta catchup digest — no manual re-auth or re-pin needed.

### Upgrade guard.sh

Edit `scripts/desktop-relay/guard.sh` in the repo, commit, push, deploy.
Then sync to Mac manually:

```bash
# On Mac
cp ~/dev/corporate-bot/scripts/desktop-relay/guard.sh ~/.relay/guard.sh
chmod +x ~/.relay/guard.sh
```

(The bot container can push via `relay-push-file` SSH command, but first-time
sync must be done by hand.)

### Claude CLI needs re-login

If `claude` on the Mac says "401 Unauthorized" or similar, the Claude CLI
OAuth token expired. From Telegram:

```
/mac-reauth
```

Bot spawns `claude setup-token` on Mac, captures the OAuth URL, you complete
the flow in your browser, paste the one-time code back to the chat. Helper
captures the 1-year `sk-ant-oat...` token and writes it to
`~/.relay/oauth_token` — bot never sees the token.

---

## Part 5 — Security Layers (Audit Reference)

| # | Layer | Enforcement | Bypass requires |
|---|-------|-------------|-----------------|
| 1 | Mac Remote Login | macOS sharing preferences — user allowlist | Physical access + admin password |
| 2 | Tailscale tailnet | Device must be on your tailnet | Tailscale SSO compromise |
| 3 | SSH key auth | `~/.ssh/authorized_keys` specific ed25519 key | Bot server root access |
| 4 | `command="guard.sh"` | No interactive shell; only gatekeeper runs | Editing `authorized_keys` (needs Mac access) |
| 5 | guard.sh whitelist | Only `tmux`/`claude`/`relay-*` patterns match | See caveat below ⚠️ |
| 6 | TOTP auth | Secret at `~/.relay/totp_secret`, Mac only | Physical Mac access |
| 7 | Session TTL | 2h sliding, 3-fail 15min lockout | Brute force (impractical with 6-digit TOTP + lockout) |
| 8 | Audit log | `~/.relay/audit.log` — every command logged | None — tamper requires Mac access |
| 9 | Tailscale API read-only | OAuth client scoped to `devices:read` | Client secret compromise → read-only tailnet device list (no write) |

✅ **Hardened (FB-32, 2026-04-29)**: guard.sh accepts ONLY `relay-*` prefix
commands. Generic `tmux *` / `claude *` / `cat` / `ls` catch-all removed —
no `eval "$CMD"` anywhere. `relay-fetch` restricted to path allowlist
(`~/dev/*`, `~/.claude/*`, `~/.relay/inbox/*`, `~/.relay/outbox/*`, `/tmp/*`). After bot deploy,
sync guard.sh to Mac: `scp scripts/desktop-relay/guard.sh mac:~/.relay/guard.sh`

---

## Part 6 — Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `ENOTFOUND api.anthropic.com` on Mac | MagicDNS broken on Mac | `sudo tailscale up --reset` or toggle MagicDNS off/on in admin |
| Bot logs: `mac_resolver: DNS tier failed: [Errno -3]` | Normal in container — DNS tier not expected to work | Ignore if Tier 2 (tailscale) works |
| Bot logs: `provider=default` | Tier 2 failed, falling back to hardcoded | Check Part 2 — env vars + restart + OAuth scope |
| `AUTH_REQUIRED` on every command | TOTP session expired (2h sliding) | `/auth <6 digits>` |
| `LOCKED_OUT` | 3+ failed TOTP attempts | Wait 15min OR `rm ~/.relay/fail_count` on Mac |
| `BLOCKED:command not whitelisted` | Command not in guard.sh whitelist | Check `~/.relay/audit.log` + update guard.sh if legitimate |
| Mac asleep | Caffeinate / disable auto-sleep | System Settings → Battery → Prevent automatic sleeping |
| Relay chat placeholder hangs forever | Daemon died | `desktop_relay action=kill_daemon chat_id=<id>` → next message respawns |
| `/mac` shows red (offline) | Mac unreachable or Tailscale down | Check Tailscale app on Mac, restart if stuck |
| First message in new relay chat has no catchup | JSONL UUID not pinned or doesn't exist on Mac | `/session list` → `/session pin <uuid>` |

---

## Part 7 — File Reference

**On Mac (`~/.relay/`)**:

| File | Purpose | Perms |
|------|---------|-------|
| `guard.sh` | Gatekeeper script; command whitelist + TOTP check | 755 |
| `auth.sh` | TOTP verification helper (pyotp) | 755 |
| `totp_secret` | TOTP seed — **never leaves Mac** | 600 |
| `session` | Active session token + expiry | 600 (auto-managed) |
| `fail_count` | TOTP failure counter (lockout logic) | 600 (auto-managed) |
| `audit.log` | Every command logged with timestamp + user | 600 |
| `oauth_token` | Claude CLI 1-year OAuth token (created by `/mac-reauth`) | 600 |
| `daemons/<chat_id>.pid` | Running SDK daemon PID + metadata | 600 (auto-managed) |
| `inbox/<sha>.<ext>` | Media pushed from TG for Mac Claude to read | 644 |

**In repo**:

| File | Purpose |
|------|---------|
| `scripts/desktop-relay/guard.sh` | Canonical guard.sh (sync to Mac manually or via `relay-push-file`) |
| `scripts/desktop-relay/auth.sh` | Canonical auth.sh |
| `scripts/desktop-relay/sdk_persistent_daemon.py` | Daemon Python source (run on Mac via `relay-sdk-persistent`) |
| `bot/desktop_relay.py` | SSH + TOTP + watchdog + MCP tool actions |
| `bot/relay/pool.py` | Persistent SDK daemon client pool |
| `bot/relay/mac_resolver.py` | 5-tier Mac IP resolver (Part 2 magic) |

**Bot `.env` keys**:

```bash
DESKTOP_ENABLED=true
DESKTOP_HOST=your-mac
DESKTOP_USER=youruser
DESKTOP_HOST_IP=                       # Optional pin (Tier 1)
TAILSCALE_OAUTH_CLIENT_ID=             # Tier 2
TAILSCALE_OAUTH_CLIENT_SECRET=         # Tier 2
DESKTOP_SESSION_TIMEOUT=7200           # Optional, default 2h
TG_ADMIN_CHAT_IDS=                     # Admin-only /mac commands
```

---

## Appendix — How the 5-tier resolver decides

```
┌─────────────────────────────────────────────────────────────┐
│ Tier 1: DESKTOP_HOST_IP env var                             │
│   → If set, use literally. Admin override for emergencies.  │
├─────────────────────────────────────────────────────────────┤
│ Tier 2: Tailscale OAuth API                                 │
│   → POST oauth2/token (client_credentials)                  │
│   → GET /api/v2/tailnet/-/devices                           │
│   → Match by DESKTOP_HOST (hostname)                        │
│   → Validate IP is in CGNAT 100.64.0.0/10 range             │
├─────────────────────────────────────────────────────────────┤
│ Tier 3: DNS lookup (MagicDNS)                               │
│   → socket.gethostbyname(DESKTOP_HOST)                      │
│   → Requires container on tailnet (not currently the case)  │
├─────────────────────────────────────────────────────────────┤
│ Tier 4: Disk cache (data/mac_ip_cache.json)                 │
│   → Cold-start only, not re-read on every refresh           │
│   → Prevents thundering-herd on startup                     │
├─────────────────────────────────────────────────────────────┤
│ Tier 5: Hardcoded fallback                                  │
│   → 100.x.y.z compiled into source                      │
│   → Last-ditch when everything above fails                  │
└─────────────────────────────────────────────────────────────┘

Refresh cadence:
  • Every 60s from mac_health_watchdog (background task)
  • On-demand via mac_resolver.refresh_mac_ip(force=True)
  • Cached result reused for 60s unless force=True
```

---

See also:
- `CLAUDE.md` — full bot architecture + relay-specific details
- `docs/DESKTOP_RELAY_SETUP.md` — original (pre-2026-04) setup guide; some
  sections superseded by this doc but Phase 14 (OAuth re-auth flow) is still
  the reference
- `.claude/skills/relay-chats/SKILL.md` — on-demand skill for relay operations
