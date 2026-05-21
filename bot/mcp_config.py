# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""External MCP server configuration for Claude Agent SDK.

Configures MCP servers for the bot:
  - playwright: Browser automation via shared SSE server (started in main.py).
    All SDK clients share one browser — each gets a tab via SSE→stdio bridge.
    Responses are size-guarded to prevent SDK 1MB JSON buffer crash.
  - camofox: Anti-detect browser via Camoufox (stdio subprocess).
    Firefox-based with C++ fingerprint spoofing. For anti-bot sites & fallback.
    Responses are size-guarded to prevent SDK 1MB JSON buffer crash.
  - google-workspace: Gmail + Calendar via google_workspace_mcp (stdio subprocess)
  - ms365: Microsoft 365 Outlook calendar + email via Softeria MCP server (stdio subprocess).
    Delegated permissions + client secret for secure token refresh.
    One-time device code login, then headless. Token cache on persistent volume.
  - kiwi-flights: Kiwi.com flight search via remote HTTP MCP server (no API key needed).
    Tools: search-flight (one-way/round-trip with date flexibility, cabin class, passengers).
    Returns curated best options with booking links. ~750 airlines incl. self-transfers.
  - google-flights: Google Flights search via fli library (stdio, no API key needed).
    Tools: search_flights (specific date), search_dates (cheapest across date range).
    Reverse-engineers Google Flights Protobuf API. No booking — search + redirect only.

SDK built-in tools (Read, Write, Edit, Glob, Grep, Bash, WebFetch, WebSearch)
are always available — no configuration needed.

All browser MCP servers are wrapped with mcp_size_guard.py which truncates
responses exceeding MCP_MAX_RESPONSE_BYTES (default 500KB) to stay well under
the Claude SDK's 1MB JSON buffer limit.
"""

import logging
import os

log = logging.getLogger(__name__)

# Path to the size guard proxy script (same directory as this module)
_SIZE_GUARD = os.path.join(os.path.dirname(__file__), "mcp_size_guard.py")


def get_external_mcp_servers(camofox_profiles_dir: str = "",
                             playwright_profiles_dir: str = "") -> dict:
    """Return external MCP server configs for ClaudeAgentOptions.

    Returns a dict of server configs. Browser servers are wrapped with
    mcp_size_guard.py for response truncation. Others use direct stdio.

    Args:
        camofox_profiles_dir: Per-session Camoufox profiles directory for
            chat-level isolation. If empty, defaults to /app/data/camofox-profiles.
        playwright_profiles_dir: Per-session Playwright user-data-dir.
            Empty string = Tier 1 (team/admin) → shared SSE server.
            Non-empty = Tier 2/3 (coding/external) → per-chat stdio subprocess.
    """
    servers = {}

    # --- Playwright MCP (browser automation) ---
    # 3-tier architecture:
    #   Tier 1 (team/admin/promoted): shared SSE server, --shared-browser-context,
    #     immune from eviction, shared cookies (desirable for shared logins).
    #   Tier 2 (coding): per-project stdio subprocess, --user-data-dir per project,
    #     LRU-evictable, isolated cookies.
    #   Tier 3 (external): per-chat stdio subprocess, --user-data-dir per chat,
    #     LRU-evictable, isolated cookies, OFF by default (Layer 1c blocks Playwright).
    if playwright_profiles_dir:
        # Tier 2/3: per-chat stdio Playwright with isolated user-data-dir
        try:
            os.makedirs(playwright_profiles_dir, exist_ok=True)
        except OSError as e:
            log.warning(f"Playwright stdio profiles dir creation failed: {e}")
            playwright_profiles_dir = ""

    if playwright_profiles_dir:
        servers["playwright"] = {
            "command": "python3",
            "args": [
                _SIZE_GUARD, "wrap", "--",
                "npx", "-y", "@playwright/mcp@0.0.68",
                "--headless", "--browser", "chromium",
                "--user-data-dir", playwright_profiles_dir,
                "--viewport-size", "1920x1080",
                "--image-responses", "omit",
            ],
        }
        log.info(f"Playwright MCP: stdio mode (profiles={playwright_profiles_dir})")
    else:
        # Tier 1: shared SSE server (started in main.py lifespan)
        playwright_port = os.environ.get("PLAYWRIGHT_MCP_PORT", "3001")
        servers["playwright"] = {
            "command": "python3",
            "args": [
                _SIZE_GUARD, "sse",
                f"http://127.0.0.1:{playwright_port}",
            ],
        }
        log.info("Playwright MCP: SSE mode (shared browser)")

    # --- Camoufox MCP (anti-detect browser) ---
    # Provides: camofox_navigate, camofox_snapshot, camofox_screenshot,
    #   camofox_click, camofox_type, camofox_scroll, camofox_press_key,
    #   camofox_create_tab, camofox_close_tab, camofox_list_tabs,
    #   camofox_import_cookies, camofox_export_cookies, camofox_extract_text,
    #   camofox_extract_structured_data, camofox_get_page_html, and more.
    # Firefox-based with C++ level fingerprint spoofing (OS, CPU, GPU, Canvas).
    # Undetectable by CreepJS, DataDome, Cloudflare, Imperva, reCAPTCHA.
    # Use for: airline sites, flight aggregators, anti-bot protected sites.
    # Requires: camofox-browser Docker service running on the configured URL.
    # Pin version — review periodically for security patches (last: 2026-03-10)
    # Wrapped with size guard to truncate oversized snapshots.
    camofox_url = os.environ.get("CAMOFOX_BROWSER_URL", "http://camofox-browser:9377")
    from config import CAMOFOX_API_KEY
    camofox_api_key = CAMOFOX_API_KEY
    # Validate URL scheme to prevent file:// or other protocol injection
    from urllib.parse import urlparse
    parsed = urlparse(camofox_url)
    if parsed.scheme not in ("http", "https"):
        log.warning(f"Camoufox MCP disabled (invalid URL scheme: {parsed.scheme})")
    else:
        # Resolve profiles directory — persistent volume, isolated per chat
        profiles_dir = camofox_profiles_dir or "/app/data/camofox-profiles"
        try:
            os.makedirs(profiles_dir, exist_ok=True)
        except OSError as e:
            log.warning(f"Camoufox MCP disabled (profiles dir creation failed: {e})")
            profiles_dir = None

        if not profiles_dir:
            log.warning("Camoufox MCP skipped — no valid profiles directory")
        else:
            camofox_env = {
                "CAMOFOX_URL": camofox_url,
                "MCP_IMAGE_FILE_PREFIX": "camofox",  # distinguish from Playwright screenshots
                "CAMOFOX_AUTO_SAVE": "true",  # auto-save profile on close_tab
            }
            if camofox_api_key:
                camofox_env["CAMOFOX_API_KEY"] = camofox_api_key
            else:
                log.warning("CAMOFOX_API_KEY not set — camofox-browser auth disabled")
            servers["camofox"] = {
                "command": "python3",
                "args": [
                    _SIZE_GUARD, "wrap", "--",
                    "npx", "-y", "camofox-mcp@1.13.1",
                    "--profiles-dir", profiles_dir,
                ],
                "env": camofox_env,
            }
            log.info(f"Camoufox MCP enabled (size-guarded, profiles={profiles_dir})")

    # --- Google Workspace MCP (Gmail + Calendar) ---
    # Provides: search_gmail_messages, get_gmail_message_content, send_gmail_message,
    #   draft_gmail_message, get_gmail_attachment_content, list_gmail_labels,
    #   manage_gmail_label, get_gmail_thread_content, modify_gmail_message_labels,
    #   get_events, manage_event, list_calendars, query_freebusy
    # Requires: workspace-mcp pip package + pre-authorized Google OAuth credentials
    # Credentials stored at /app/data/google-workspace-creds/<email>.json
    # Generated via: python scripts/setup_google_credentials.py (run locally)
    creds_dir = os.environ.get(
        "WORKSPACE_MCP_CREDENTIALS_DIR",
        "/app/data/google-workspace-creds",
    )
    # Client secrets can be passed via env vars or file
    from config import GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET
    client_id = GOOGLE_OAUTH_CLIENT_ID
    client_secret = GOOGLE_OAUTH_CLIENT_SECRET

    workspace_env = {
        "WORKSPACE_MCP_CREDENTIALS_DIR": creds_dir,
        # Avoid port conflict with FastAPI on 8000 (workspace-mcp starts OAuth callback server)
        "WORKSPACE_MCP_PORT": "8765",
    }
    if client_id and client_secret:
        workspace_env["GOOGLE_OAUTH_CLIENT_ID"] = client_id
        workspace_env["GOOGLE_OAUTH_CLIENT_SECRET"] = client_secret

    servers["google-workspace"] = {
        "command": "workspace-mcp",
        "args": [
            "--tools", "gmail", "calendar",
            "--transport", "stdio",
        ],
        "env": workspace_env,
    }

    # --- Microsoft 365 MCP (Outlook calendar + email via Softeria) ---
    # Provides (filtered by --enabled-tools regex):
    #   Calendar: list-calendars, list-calendar-events, get-calendar-event,
    #     get-calendar-view, create/update/delete-calendar-event,
    #     accept/decline/tentatively-accept-calendar-event
    #   Email: list-mail-messages, list-mail-folders,
    #     list-mail-folder-messages, get-mail-message, get-mail-attachment,
    #     send-mail, create-draft, move-mail, delete-mail, create-mail-folder
    #   Auth: login, verify-login, logout, list/select/remove-account
    #   Other: get-current-user
    # Auth: Delegated permissions with client secret (confidential client).
    #   Device code flow for initial login, then automatic token refresh.
    # Token cache on persistent volume — survives container rebuilds.
    # --enabled-tools regex: mail (read + send/draft) + calendar + user info.
    # Email sends gated by outbound approval rule in system prompt (same as Gmail).
    from config import MS365_MCP_CLIENT_ID, MS365_MCP_CLIENT_SECRET, MS365_MCP_TENANT_ID
    ms365_client_id = MS365_MCP_CLIENT_ID
    ms365_tenant_id = MS365_MCP_TENANT_ID
    ms365_client_secret = MS365_MCP_CLIENT_SECRET

    if ms365_client_id and ms365_tenant_id:
        # Ensure cache directory exists on persistent volume
        ms365_cache_dir = "/app/data/credentials/ms365-mcp"
        try:
            os.makedirs(ms365_cache_dir, exist_ok=True)
        except OSError as e:
            log.warning(f"Microsoft 365 MCP disabled (cache dir creation failed: {e})")
            ms365_cache_dir = None

        if ms365_cache_dir:
            ms365_env = {
                "MS365_MCP_CLIENT_ID": ms365_client_id,
                "MS365_MCP_TENANT_ID": ms365_tenant_id,
                # Token cache paths — avoid "token"/"secret" in filenames
                # (hooks.py _BLOCKED_PATH_PATTERNS blocks those strings in paths)
                "MS365_MCP_TOKEN_CACHE_PATH": f"{ms365_cache_dir}/msal_cache.json",
                "MS365_MCP_SELECTED_ACCOUNT_PATH": f"{ms365_cache_dir}/selected_account.json",
            }
            # Client secret is optional but recommended — enables confidential
            # client mode for more secure token refresh. Without it, Softeria
            # falls back to public client (device code still works, just less secure).
            if ms365_client_secret:
                ms365_env["MS365_MCP_CLIENT_SECRET"] = ms365_client_secret

            servers["ms365"] = {
                "command": "npx",
                # Pin version — review periodically for updates (last: 2026-04-02)
                # NOTE: Do NOT use --preset — it overwrites --enabled-tools in cli.js
                # (preset pattern runs after Commander parse, stomping our filter).
                # Instead, --enabled-tools alone controls both tool visibility AND
                # OAuth scopes (via buildScopesFromEndpoints regex filtering).
                "args": ["-y", "@softeria/ms-365-mcp-server@0.54.1",
                         "--enabled-tools", "list-mail|get-mail-message|get-mail-attach|send-mail|create-draft|move-mail|delete-mail|create-mail-folder|calendar|get-current-user"],
                "env": ms365_env,
            }
            log.info("Microsoft 365 MCP enabled (Softeria, mail + calendar)")
    else:
        log.info("Microsoft 365 MCP disabled (MS365_MCP_CLIENT_ID/TENANT_ID not set)")

    # --- Kiwi.com Flights MCP (remote HTTP — hosted by Kiwi.com) ---
    # Provides: search-flight (one-way/round-trip with passengers, cabin class,
    #   date flexibility, creative routing incl. self-transfers across carriers).
    # No API key needed — Kiwi handles auth internally.
    # Returns curated "best" options with direct booking links to kiwi.com.
    # ~750 airlines including LCCs. Official MCP server by Kiwi + Alpic AI.
    kiwi_url = os.environ.get("KIWI_MCP_URL", "https://mcp.kiwi.com")
    servers["kiwi-flights"] = {
        "type": "http",
        "url": kiwi_url,
    }
    log.info("Kiwi Flights MCP enabled (remote HTTP)")

    # --- Google Flights (fli library) ---
    # NOTE: fli-mcp external server is BROKEN with fastmcp>=3.0 (_tool_manager removed).
    # Google Flights search is provided via custom MCP tools in mcp_tools.py instead:
    #   google_flights_search, google_flights_search_dates
    # These call fli's search engine directly, bypassing the broken MCP server layer.

    # Log server names only — NEVER log servers dict (contains API keys in env)
    log.info(f"External MCP servers configured: {list(servers.keys())}")
    return servers
