# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""OAuth callback handlers — HTTP endpoints for OAuth redirect flows.

Handles the server-side code exchange after a user authorizes via browser.
Currently supports Google OAuth (web redirect flow). M365 uses device code
(no callback needed).

These handlers run on the reports server (port 8210/8310) — registered
via register_oauth_routes(app) called from bot/reports.py.
"""

import logging
import re
import time

log = logging.getLogger(__name__)

# Shared state for active Google OAuth flows.
# Keyed by (source, chat_id) for chat lookup, and "nonce:<nonce>" for callback lookup.
# Canonical location — commands.py and oauth_callbacks.py both import from here.
pending_google_auth: dict[tuple | str, dict] = {}


# ============================================================
# OAuth state signing (HMAC-SHA256)
# ============================================================

def sign_oauth_state(data: dict) -> str:
    """Create HMAC-signed state token for OAuth flow."""
    import hashlib, hmac, json as _j, base64
    from config import TOKEN_ENCRYPTION_KEY
    if not TOKEN_ENCRYPTION_KEY:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY not configured — cannot sign OAuth state")
    payload = base64.urlsafe_b64encode(_j.dumps(data).encode()).decode()
    sig = hmac.new(TOKEN_ENCRYPTION_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def verify_oauth_state(state: str) -> dict | None:
    """Verify HMAC-signed state token. Returns payload dict or None."""
    import hashlib, hmac, json as _j, base64
    from config import TOKEN_ENCRYPTION_KEY
    if not TOKEN_ENCRYPTION_KEY:
        return None
    if "." not in state:
        return None
    payload, sig = state.rsplit(".", 1)
    expected = hmac.new(TOKEN_ENCRYPTION_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        log.warning("OAuth state signature mismatch")
        return None
    try:
        data = _j.loads(base64.urlsafe_b64decode(payload + "=="))
    except Exception:
        return None
    # Check expiry (10 min)
    if time.time() - data.get("ts", 0) > 600:
        log.warning("OAuth state expired")
        return None
    return data


# ============================================================
# Google OAuth callback
# ============================================================

async def google_oauth_callback(code: str = "", state: str = "", error: str = ""):
    """Google OAuth callback — exchanges code for tokens and notifies user.

    Security:
    - State is HMAC-signed with TOKEN_ENCRYPTION_KEY (prevents forgery)
    - State contains source, chat_id, user_id, thread_id, nonce, timestamp
    - 10-minute expiry on state token
    - Code exchange happens server-side (client_secret never exposed)
    """
    from fastapi.responses import Response

    _html_page = lambda title, msg: Response(
        content=f"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title><style>body{{font-family:system-ui;display:flex;justify-content:center;align-items:center;
min-height:100vh;margin:0;background:#1a1a2e;color:#e0e0e0}}div{{text-align:center;padding:2rem;max-width:400px}}
h1{{font-size:2rem}}p{{color:#aaa}}</style></head><body><div><h1>{title}</h1><p>{msg}</p></div></body></html>""",
        media_type="text/html",
    )

    import html as _html_esc

    if error:
        log.warning("Google OAuth error callback: %s", error)
        return _html_page("\u274c Auth Error", f"Google returned: {_html_esc.escape(error)}. Close this tab and try again.")

    if not code or not state:
        return _html_page("\u274c Invalid Request", "Missing code or state parameter.")

    # Verify state
    data = verify_oauth_state(state)
    if not data:
        return _html_page("\u274c Invalid State", "Link expired or tampered. Run /google-auth again.")

    source = data.get("source", "telegram")
    chat_id = data.get("chat_id", "")
    user_id = data.get("user_id", "")
    thread_id = data.get("thread_id")
    nonce = data.get("nonce", "")

    # Atomic lookup + consume by nonce (one-time use, race-safe)
    nonce_key = f"nonce:{nonce}"
    flow = pending_google_auth.pop(nonce_key, None)
    if not flow:
        return _html_page("\u274c Expired", "Auth flow not found or already used. Run /google-auth again.")
    # Also clean up the chat-key entry
    chat_key = (source, chat_id)
    pending_google_auth.pop(chat_key, None)

    # Cross-validate state fields against server-side flow data (defense-in-depth)
    if flow.get("chat_id") != chat_id or flow.get("source") != source:
        log.warning("OAuth state mismatch: state=%s/%s flow=%s/%s",
                    source, chat_id, flow.get("source"), flow.get("chat_id"))
        return _html_page("\u274c State Mismatch", "Auth flow doesn't match. Run /google-auth again.")

    client_id = flow["client_id"]
    client_secret = flow["client_secret"]
    scopes = flow["scopes"]
    redirect_uri = flow["redirect_uri"]

    if not chat_id or not client_id:
        return _html_page("\u274c Invalid State", "Missing required fields. Run /google-auth again.")

    # Exchange code for tokens
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code.strip(),
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            if resp.status_code != 200:
                try:
                    err = resp.json().get("error_description", resp.text[:200])
                except Exception:
                    err = resp.text[:200]
                log.error("Google OAuth token exchange failed: %s", err)
                return _html_page("\u274c Token Exchange Failed", f"{_html_esc.escape(str(err))}<br>Close this tab and try /google-auth again.")
            token_data = resp.json()
    except Exception as e:
        log.error("Google OAuth token exchange error: %s", e)
        return _html_page("\u274c Error", "Token exchange failed. Close this tab and try again.")

    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")

    if not refresh_token:
        log.warning("Google OAuth: no refresh token received")
        return _html_page("\u26a0\ufe0f No Refresh Token",
            "Google didn't issue a refresh token. Revoke access at "
            "<a href='https://myaccount.google.com/permissions'>myaccount.google.com</a> "
            "then try /google-auth again.")

    # Get user email
    email = "unknown"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code == 200:
                email = resp.json().get("email", "unknown")
    except Exception:
        pass

    # Save credentials in workspace-mcp format
    import json as _j
    from config import DATA_DIR
    creds_dir = DATA_DIR / "google-workspace-creds"
    creds_dir.mkdir(parents=True, exist_ok=True)
    safe_email = re.sub(r"[^a-zA-Z0-9@._-]", "_", email)
    creds_file = creds_dir / f"{safe_email}.json"
    if not creds_file.resolve().is_relative_to(creds_dir.resolve()):
        return _html_page("\u274c Error", "Invalid email.")

    creds_data = {
        "token": access_token,
        "refresh_token": refresh_token,
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": client_id,
        "client_secret": client_secret,
        "scopes": scopes,
    }
    tmp = creds_file.with_suffix(".tmp")
    import os as _os_cb
    _fd_cb = _os_cb.open(str(tmp), _os_cb.O_WRONLY | _os_cb.O_CREAT | _os_cb.O_TRUNC, 0o600)
    with _os_cb.fdopen(_fd_cb, "w") as _f_cb:
        _f_cb.write(_j.dumps(creds_data, indent=2))
    tmp.rename(creds_file)
    log.info("Google OAuth callback: saved credentials for %s", safe_email)

    # Register credential metadata
    try:
        from bot.auth.credentials import register_credential_metadata
        await register_credential_metadata(
            cred_id=f"google:{safe_email}",
            cred_type="google-workspace",
            account=safe_email,
            scope="user" if user_id else "system",
            owner=f"{source}:{user_id}" if user_id else "",
            file_path=str(creds_file),
            extra_metadata={"scopes": scopes, "registered_via": "/google-auth-callback"},
        )
    except Exception as e:
        log.warning("Failed to register Google credential metadata: %s", e)

    # Store email in user oauth tokens for session-level auto-routing
    if user_id:
        try:
            from bot.auth.oauth_flow import store_user_oauth_token
            await store_user_oauth_token(
                user_id=f"{source}:{user_id}",
                provider="google",
                token=refresh_token or access_token,
                metadata={"email": safe_email, "scopes": scopes, "registered_via": "/google-auth-callback"},
            )
        except Exception as e:
            log.warning("Failed to store Google oauth token (callback): %s", e)

    # Auto-add to allowed_credentials so the user can actually use Google tools
    if user_id:
        try:
            from bot.harness import get_harness_for_chat, set_chat_override, resolve_field, get_chat_overrides, is_credential_unrestricted
            _g_chat_key = f"{source}:{user_id}"
            _g_h = get_harness_for_chat(_g_chat_key)
            _g_existing = get_chat_overrides(_g_chat_key).get("allowed_credentials")
            _g_ac = _g_existing if _g_existing is not None else resolve_field(_g_h, "allowed_credentials", _g_chat_key) or []
            _g_cred_id = f"google:{safe_email}"
            if is_credential_unrestricted(_g_ac):
                pass  # wildcard — no need to add individual creds
            elif _g_cred_id not in _g_ac:
                set_chat_override(_g_chat_key, "allowed_credentials", list(_g_ac) + [_g_cred_id])
                log.info("Auto-granted Google credential %s to chat %s (callback)", _g_cred_id, _g_chat_key)
        except Exception as e:
            log.warning("Failed to auto-grant Google credential (callback): %s", e)

    # Mark onboarding step done + auto-reset session for fresh env
    try:
        from bot.commands import _mark_onboarding_step_done
        await _mark_onboarding_step_done(source, chat_id, user_id, "google_auth")
        from bot.auth.oauth_flow import auto_reset_session_after_auth
        await auto_reset_session_after_auth(source, chat_id, "google")
    except Exception as e:
        log.debug("Post-auth onboarding/reset failed (non-blocking): %s", e)

    # Notify user via their platform (Telegram or Teams)
    try:
        from bot.commands import _send_to_platform_simple
        msg = (
            f"\u2705 <b>Google account connected: {_html_esc.escape(safe_email)}</b>\n\n"
            f"Scopes: Gmail + Calendar\n"
            f"Run <code>/reset</code> to pick up the fresh credentials."
        )
        _thread = int(thread_id) if thread_id else None
        await _send_to_platform_simple(source, chat_id, msg, parse_mode="HTML",
                                        message_thread_id=_thread)
    except Exception as e:
        log.warning("Failed to notify user of Google OAuth success: %s", e)

    return _html_page("\u2705 Success!", f"Google account <b>{_html_esc.escape(safe_email)}</b> connected.<br>You can close this tab and return to Telegram.")


# ============================================================
# Route registration
# ============================================================

def register_oauth_routes(app):
    """Register OAuth callback routes on a FastAPI app."""

    @app.get("/oauth/google/callback")
    async def _google_callback(code: str = "", state: str = "", error: str = ""):
        return await google_oauth_callback(code, state, error)

    log.info("OAuth callback routes registered on app")
