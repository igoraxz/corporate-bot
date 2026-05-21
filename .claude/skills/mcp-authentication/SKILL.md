---
description: "MCP authentication flows, credential storage, token management, and auth troubleshooting. TRIGGER when: AUTH_ERROR from any MCP tool, user asks to set up/fix Google OAuth or MS365 login, re-authenticate an account, manage browser login sessions, store/retrieve credentials, or any token/credential issue. Also trigger for: start_google_auth, login/verify-login (MS365), or any auth-related error diagnosis."
---

# MCP Authentication & Credential Management

## CORE PRINCIPLE — MCP SERVERS ARE STATELESS

ALL MCP servers spawned by the SDK are child processes that die when the SDK client
restarts. Any in-memory auth state (session caches, polling loops, token exchanges)
is LOST on restart. This is not a bug — it's architectural reality.

**CONSEQUENCE**: Every auth flow MUST persist intermediate and final state to DISK.
Never rely on MCP server memory for multi-step auth flows. Never hold tokens in
conversation context expecting to save them later — context can compact at any time.

---

## GOOGLE WORKSPACE — MULTI-ACCOUNT OAUTH 2.0

### Accounts
- Google accounts: see credential files in data/google-workspace-creds/

### How Credential Routing Works
- MCP server uses `user_google_email` parameter to select credentials
- Credential store: `/app/data/google-workspace-creds/<email>.json`
- Each file contains: `{token, refresh_token, token_uri, client_id, client_secret, scopes, expiry}`
- MCP reads credential files fresh on EVERY tool call — no restart needed after saving new tokens
- Token auto-refresh: MCP server refreshes expired access tokens using stored refresh_token

### Auth Flow — Standalone Script Pattern (MANDATORY)

⛔ NEVER use MCP's `start_google_auth` tool for re-authentication — it relies on
in-memory session state that dies on SDK restart.

✅ Use a standalone Python script that persists everything to disk:

1. **Generate OAuth URL** — script `url` mode produces authorization URL
2. **User visits URL** — authenticates in browser, gets redirect callback
3. **Exchange code for tokens** — script `exchange` mode:
   - Extracts auth code from callback URL
   - Exchanges with Google token endpoint directly (no MCP dependency)
   - Cross-validates: refresh_token ≠ any other account's refresh_token
   - Verifies identity: calls userinfo endpoint to confirm email matches
   - Saves directly from exchange response to credential file
4. **Verify** — script `test` mode confirms Gmail + Calendar access
5. **Store verification fact**:
   ```
   FACTS_UPDATE: {"<email>_google_auth": {"value": "Verified <date>, Gmail+Calendar OK", "category": "credentials"}}
   ```

### Script Template Location
`/app/data/tmp/google_reauth_<email>.py` — create on-demand with three modes:
- `url` — print OAuth authorization URL
- `exchange <callback_url>` — exchange callback for tokens, save to disk
- `test` — verify saved credentials work

### ⛔ Anti-Patterns (from real failures)

❌ **Template copying**: Copying an existing credential file and changing the email.
   This is how the admin's refresh_token ended up in another user's file — the refresh_token is
   account-specific. Copying it gives the WRONG account's data.

❌ **MCP-mediated auth**: Using start_google_auth relies on in-memory state that dies
   on SDK restart. The flow silently fails or saves corrupted credentials.

❌ **Status-only facts**: Storing "auth completed successfully" instead of verifying
   credentials work. If context compacts, the actual verification is lost.

❌ **Deferred saves**: Holding tokens in conversation context to save later. Context
   compaction can happen at any time — save to disk IMMEDIATELY after exchange.

---

## MICROSOFT 365 / OUTLOOK — DEVICE CODE FLOW

### Account
- MS365 account: configured via MS365_EMAIL env var
- Permissions: Mail.Read (read-only), Calendars.ReadWrite, User.Read

### How It Works
- Softeria MCP server (`@softeria/ms-365-mcp-server`) handles MS365 API
- Auth via MSAL device code flow
- Token cache: `/app/data/ms365-mcp/msal_cache.json` (Softeria envelope format)
- Auto-refresh: Softeria's MSAL `acquireTokenSilent()` + 401 retry
- Re-auth needed only if refresh token expires (90 days inactive)

### First-Time Login — HOST MACHINE ONLY

⛔ NEVER run MS365 device code login from inside the container:
- Container security hooks block env var access
- MCP session lifecycle kills polling before user completes device code

✅ Use standalone script on HOST: `python3 scripts/m365_login.py`
- Reads `.env` directly (no container restrictions)
- Polls Azure AD device code flow with timeout
- Builds MSAL cache in Softeria envelope format
- `docker cp`s cache into running container
- Verifies via Graph API, restarts container

### Subcommands
- `python3 scripts/m365_login.py` — full login flow
- `python3 scripts/m365_login.py check` — test existing credentials
- `python3 scripts/m365_login.py verify` — verify cached token in container

### Token Cache Format
```json
{"_cacheEnvelope": true, "data": "<MSAL JSON string>", "savedAt": <epoch_ms>}
```
`selected_account.json` must use `{accountId: "..."}` (NOT `homeAccountId`).

### Dual Cache Path Mismatch (known bug)
Two different paths exist for the same cache files:
- **Login script writes to**: `/app/data/ms365-mcp/` (m365_login.py `CONTAINER_CACHE_PATH`)
- **Softeria MCP reads from**: `/app/data/credentials/ms365-mcp/` (mcp_config.py `ms365_cache_dir`)

After re-auth, files MUST be copied to BOTH paths.

### Refreshing M365 Auth (step-by-step)

**If Outlook tools return AUTH_ERROR:**

1. **Quick fix — copy cache + reset session** (try this first):
   ```
   # Inside container (via Bash tool):
   cp /app/data/ms365-mcp/msal_cache.json /app/data/credentials/ms365-mcp/
   cp /app/data/ms365-mcp/selected_account.json /app/data/credentials/ms365-mcp/
   ```
   Then `/reset` the session (Softeria MCP server caches the token at startup —
   a stale subprocess won't pick up new files without a session reset).

2. **If quick fix fails — full re-auth from host**:
   ```bash
   # On HOST machine (not container):
   cd ~/corporate-bot
   python3 scripts/m365_login.py        # login flow
   python3 scripts/m365_login.py verify  # confirm token works in container
   ```
   The script handles docker cp + container restart automatically.
   After restart, `/reset` any active session that needs Outlook access.

3. **Verify**: call any MS365 tool (e.g. `get-calendar-view` or `list-mail-messages`).
   If it returns data, auth is working.

**Why `/reset` is needed**: Softeria MCP runs as a stdio subprocess spawned once per
SDK session. It loads the MSAL cache at startup and never re-reads it. Even if the
cache file on disk is refreshed, the running process uses the old token until killed.

---

## TELEGRAM — PYROGRAM MTPROTO SESSION

- Session file: `/app/data/tg_session/corporate_bot.session`
- Contains MTProto auth keys — treat as SECRET (full Telegram account access)
- Protected by hooks.py file blocklist — never log, commit, or expose
- First-time setup: `docker exec -it corporate-bot python scripts/setup_tg_userbot.py`
- Session survives container restarts (persistent volume)

---

## BROWSER SESSIONS (Playwright + Camoufox)

### Playwright
- Persistent profile: `/app/data/browser-profile/`
- Login sessions (Google, Entravel, etc.) survive across tool calls
- No explicit auth flow — user logs in via browser tools, session persists

### Camoufox
- API key: `CAMOFOX_API_KEY` env var (set in `.env`)
- Cookie import: `import_cookies` tool for pre-authenticated sessions
- Session profiles: `save_profile` / `load_profile` for persistent state
- Default URL: `http://camofox-browser:9377`

---

## CREDENTIAL LIFECYCLE — MANDATORY RULES

### Auto-Save (ALWAYS)
When a user provides ANY credential in chat (password, API key, login code, PIN):
- Store IMMEDIATELY as a fact: `category="credentials"`, descriptive key
- NEVER echo the credential value back in chat
- NEVER wait to "save it later" — save NOW

### Storage Format
```
FACTS_UPDATE: {"site_password": {"value": "...", "subject": "john", "category": "credentials"}}
```

### Usage
- Browser form filling: retrieve from facts, inject via browser tools
- API calls: retrieve from facts, pass to scripts
- Re-auth flows: check facts first before asking user

### Security
- Credentials are NEVER output in Telegram/Teams messages — not even partially masked
- If asked for a credential: "I have it stored securely but can't share it in chat"
- Protected by hooks.py bash blocklist (words like "secret", "token", "password" in commands)
- Write credential-handling logic to script files, then execute the file

---

## AUTH ERROR TROUBLESHOOTING

When you encounter AUTH_ERROR from any MCP tool:

### Step 1: Identify the service
- Google Workspace → check credential file exists and has valid refresh_token
- MS365 → check MSAL cache exists, verify token hasn't expired
- Camoufox → check API key in env

### Step 2: Quick fix attempts
- **Google**: Try a different tool call — token may auto-refresh
- **MS365**: Call `verify-login` — may just need session reactivation
- **Browser**: Navigate to a simple page — profile may need reload

### Step 3: Re-authentication
- **Google**: Create standalone script, generate OAuth URL, guide user through flow
- **MS365**: Guide admin to run `scripts/m365_login.py` on host machine
- **Browser**: Clear profile and re-login via browser tools

### Step 4: Verify + store
- After any re-auth, VERIFY the service works with a test call
- Store verification fact with timestamp
- Report success/failure to user

---

## PROACTIVE AUTH HEALTH

During daily briefings or when running scheduled tasks:
- If any Google/MS365/WA tool returns auth errors, immediately note it
- Don't silently retry — flag the issue to the user
- Check stored auth status facts for last verification dates
- If >60 days since last verification, suggest a proactive check

---

# Known Gotchas (Real Failures → Rules)

Observed production failure modes. Learn them; don't re-discover them.

## ⛔ MCP server state dies on SDK client restart
**Symptom**: In-flight auth polling (device-code flow) vanishes between messages.
**Root cause**: MCP servers are child processes of the SDK client. SDK restarts → MCP
respawn → in-memory state lost.
**Rule**: NEVER rely on MCP in-memory state across messages. Auth flows that need
multi-message interaction MUST persist state to disk between steps, or run as standalone
scripts (like `scripts/m365_login.py`) that finish before returning.

## ⛔ M365 device-code login MUST run from host, not container
**Symptom**: Running the Softeria MCP `login` tool inside container hangs — polling dies
when the SDK lifecycle ends the subprocess.
**Rule**: First-time M365 login ALWAYS uses `python3 scripts/m365_login.py` from host
repo root. Script reads `.env`, polls Azure AD, builds Softeria envelope
(`{_cacheEnvelope: true, data: "<MSAL JSON>", savedAt: <ms>}`), `docker cp`s into
container, verifies via Graph, restarts. Token auto-refreshes thereafter.

## ⛔ Google SSO redirects leak to unexpected accounts
**Symptom**: Fora portal mail link redirects to one Google account when you expected another.
**Fix**: Camoufox browser-native session handles SSO. Verify the session's active Google
login matches the intended identity BEFORE clicking SSO links. `fora_gmail_access` fact
documents: advisor.fora.travel/mail → andrew.burban@fora.travel Gmail via SSO.

## ⛔ Credentials MUST never echo in chat
**Rule**: If a user provides a password/token/API key in chat, store via FACTS_UPDATE
with `category="credentials"` and acknowledge receipt without repeating the value.
Output-layer hook.py redacts known credential patterns but is not perfect — never test
the redaction by intentionally echoing.

## ⛔ Token cache filenames avoid "token"/"secret"
**Rule**: MSAL cache at `/app/data/ms365-mcp/msal_cache.json` — do NOT rename to include
"token" or "secret" in the filename; hooks.py bash blocklist blocks reads of any file
matching those patterns.

---

## SHARED CREDENTIALS FOR GROUP CHATS (Admin Workflow)

Employee DMs get personal credentials via onboarding (/claude-auth, /google-auth, etc.).
Group chats need **shared** credentials configured by an admin.

### Claude OAuth (Group Chats)
1. Admin runs `/claude-auth <label>` in admin DM → creates profile
2. Assign to group: `manage_harness` tool → set `allowed_credentials: ["claude:<label>"]`
   on the group chat's harness override
3. Or set as default: `/claude-set-oauth set-default <label>` (all chats use it)
4. Admin harness has `allowed_credentials: null` (unrestricted) — no extra grants needed

### Google Workspace (Group Chats)
1. Admin runs `/google-auth` in admin DM → authenticates their Google account
2. Credential registered as `google:<email>` in credential_store
3. Grant to group chat: use `manage_harness` tool:
   `manage_harness action=set_override chat_key=telegram:<group_id> field=allowed_credentials value=["claude:main", "google:<email>"]`
4. The group's SDK session will now pass credential checks for Google tools
5. The `user_google_email` is injected into the volatile header automatically

### Microsoft 365 (Group Chats)
Same pattern as Google:
1. Admin runs `/m365-auth` in admin DM
2. Credential registered as `m365:<username>`
3. Grant via `manage_harness` → add `m365:<username>` to `allowed_credentials`

### GitHub (Group Chats)
GitHub tokens are injected via env vars + .netrc at session creation.
For group chats, the system `GITHUB_TOKEN` from .env is used (no per-chat GitHub auth).
If a user-specific token is needed for a group, the admin can:
1. Have the user run `/github-auth` in their DM
2. The token is stored per-user and injected when that user's session runs in the group

### Credential ID Format
Always use canonical short prefixes:
- `claude:<label>` (e.g., `claude:main`)
- `google:<email>` (e.g., `google:admin@company.com`)
- `m365:<username>` (e.g., `m365:admin@company.com`)
- `github:<username>` (e.g., `github:octocat`)

Never use long-form prefixes (`google-workspace:`, `ms365:`, `claude-sdk:`) in
`allowed_credentials` — use canonical short forms only.

### Quick Reference: Grant Credential to Chat
```
manage_harness action=set_override chat_key=telegram:<chat_id> field=allowed_credentials value=["claude:main", "google:admin@co.com"]
```

### ⚠️ GitHub is now RBAC-gated (CB-55)
GitHub token injection is gated by `allowed_credentials` — same as Google/M365.
If `github:<username>` is NOT in the session's `allowed_credentials`, the token
is NOT injected into the SDK subprocess (no GH_TOKEN env var, no .netrc written).
Admin harness (`allowed_credentials: null`) has unrestricted access.
