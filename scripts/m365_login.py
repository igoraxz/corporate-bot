#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""
Microsoft 365 device code login — run from HOST MACHINE.

Performs Azure AD device code OAuth2 flow, builds MSAL-compatible token cache,
and copies it into the running bot container so the Softeria MCP server picks it up.

Why host machine?
  - Bot container security hooks block commands containing 'secret'/'token'/'password'
  - MCP server subprocess lifecycle is tied to SDK sessions (dies between messages)
  - Running on host avoids both issues: direct .env access, no process lifecycle problems

Usage:
    cd /path/to/corporate-bot
    pip install requests python-dotenv   # one-time
    python3 scripts/m365_login.py        # interactive device code login
    python3 scripts/m365_login.py check  # test Azure AD credentials only
    python3 scripts/m365_login.py verify # verify cached token works (Graph API call)

Flow:
    1. Reads Azure AD app credentials from .env
    2. Requests device code from Azure AD
    3. User visits https://login.microsoft.com/device and enters the code
    4. Script polls for token completion (up to 15 min, resumable)
    5. Builds MSAL-compatible cache in Softeria envelope format
    6. Copies cache into running bot container via docker cp
    7. Verifies token by making a real Graph API call
    8. Restarts bot container so MCP server loads new cache

Token refresh:
    - Softeria MCP server auto-refreshes via MSAL acquireTokenSilent()
    - On 401 responses, auto-retries with refreshed token
    - Refreshed tokens are saved back to disk automatically
    - Re-auth only needed if refresh token expires (90 days of inactivity)

Cache format (Softeria envelope):
    - msal_cache.json: {_cacheEnvelope: true, data: "<MSAL JSON>", savedAt: <ms>}
    - selected_account.json: {_cacheEnvelope: true, data: '{"accountId": "..."}', savedAt: <ms>}

State persistence:
    - Flow state saved to data/m365_auth_state.json (resumable after interruption)
    - If interrupted, re-run the script — it resumes polling with the same code
"""
import sys
import json
import os
import time
import subprocess
import base64

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip install requests python-dotenv")
    sys.exit(1)

try:
    from dotenv import dotenv_values
except ImportError:
    print("ERROR: 'python-dotenv' not installed. Run: pip install requests python-dotenv")
    sys.exit(1)

# All paths relative to repo root
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(REPO_ROOT, ".env")
STATE_PATH = os.path.join(REPO_ROOT, "data", "m365_auth_state.json")

# Temp output dir (on host) — files are docker-cp'd into container after
OUTPUT_DIR = os.path.join(REPO_ROOT, "data", "m365_auth_output")

# Paths inside the container (where Softeria MCP reads from)
CONTAINER_CACHE_PATH = "/app/data/ms365-mcp/msal_cache.json"
CONTAINER_SELECTED_PATH = "/app/data/ms365-mcp/selected_account.json"

SCOPES = (
    "https://graph.microsoft.com/Mail.Read "
    "https://graph.microsoft.com/Calendars.ReadWrite "
    "https://graph.microsoft.com/User.Read "
    "offline_access"
)

# Load credentials from .env
vals = dotenv_values(ENV_PATH)
client_id = vals.get("MS365_MCP_CLIENT_ID", "")
tenant_id = vals.get("MS365_MCP_TENANT_ID", "")
client_secret = vals.get("MS365_MCP_CLIENT_SECRET", "")
CONTAINER_NAME = vals.get("COMPOSE_PROJECT_NAME", "corporate-bot")

AUTHORITY = f"https://login.microsoftonline.com/{tenant_id}"


def log(msg):
    print(msg, flush=True)


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return None


def clear_state():
    if os.path.exists(STATE_PATH):
        os.remove(STATE_PATH)


def request_device_code():
    """Request a new device code from Azure AD."""
    log(f"\nClient ID: {client_id[:8]}...")
    log(f"Tenant ID: {tenant_id[:8]}...")
    log(f"Client secret length: {len(client_secret)}")
    log("\nRequesting device code...")

    resp = requests.post(
        f"{AUTHORITY}/oauth2/v2.0/devicecode",
        data={"client_id": client_id, "scope": SCOPES},
    )
    if resp.status_code != 200:
        log(f"ERROR getting device code: {resp.status_code}")
        log(resp.text)
        sys.exit(1)

    flow = resp.json()
    state = {
        "device_code": flow["device_code"],
        "user_code": flow.get("user_code", ""),
        "interval": flow.get("interval", 5),
        "expires_at": time.time() + flow.get("expires_in", 900),
        "created_at": time.time(),
    }
    save_state(state)

    log(f"\n{'='*60}")
    log(f"  Go to:  {flow.get('verification_uri', 'https://login.microsoft.com/device')}")
    log(f"  Enter code:  {state['user_code']}")
    log(f"{'='*60}")
    log("\nWaiting for you to complete sign-in in the browser...")
    return state


def poll_for_token(state):
    """Poll Azure AD for token completion. Resumable from saved state."""
    device_code = state["device_code"]
    interval = state.get("interval", 5)
    expires_at = state["expires_at"]

    remaining = int(expires_at - time.time())
    log(f"Polling for token (expires in {remaining}s, interval {interval}s)...")

    while time.time() < expires_at:
        time.sleep(interval)

        # Device code flow as PUBLIC client — do NOT send client_secret.
        # Azure AD rejects client_secret in device code flow for public clients
        # (AADSTS700025). Refresh token requests (separate flow) may use it.
        token_resp = requests.post(
            f"{AUTHORITY}/oauth2/v2.0/token",
            data={
                "client_id": client_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
            },
        )

        result = token_resp.json()
        error = result.get("error", "")

        if error == "authorization_pending":
            print(".", end="", flush=True)
            continue
        elif error == "slow_down":
            interval += 5
            log(f"\n(slowing down to {interval}s)")
            continue
        elif error == "expired_token":
            log("\n\n❌ Device code expired. Run the script again for a new code.")
            clear_state()
            return None
        elif error:
            log(f"\n\n❌ FAILED: {error}")
            log(f"Description: {result.get('error_description', 'none')}")
            clear_state()
            return None
        else:
            log("\n\n✅ Token acquired!")
            clear_state()
            return result

    log("\n\n❌ TIMEOUT: polling expired. Run the script again.")
    clear_state()
    return None


def save_token(token_result):
    """Build MSAL-compatible cache, save locally, then docker cp into container."""
    access_token = token_result["access_token"]

    # Decode JWT payload to extract user info
    payload_b64 = access_token.split(".")[1]
    # Pad to multiple of 4 for base64 decoding (avoid adding 4 when already aligned)
    pad = len(payload_b64) % 4
    if pad:
        payload_b64 += "=" * (4 - pad)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))

    user_oid = payload.get("oid", "")
    user_tid = payload.get("tid", tenant_id)
    username = payload.get("preferred_username", payload.get("upn", "unknown"))
    name = payload.get("name", username)
    home_account_id = f"{user_oid}.{user_tid}"

    log(f"User: {username} ({name})")
    log(f"Account ID: {home_account_id}")

    now = int(time.time())
    expires_on = now + token_result.get("expires_in", 3600)
    ext_expires_on = now + token_result.get(
        "ext_expires_in", token_result.get("expires_in", 3600)
    )
    scopes_no_offline = SCOPES.replace("offline_access", "").strip()

    # Build MSAL-compatible cache structure
    cache_data = {
        "Account": {
            f"{home_account_id}-login.microsoftonline.com-{user_tid}": {
                "home_account_id": home_account_id,
                "environment": "login.microsoftonline.com",
                "realm": user_tid,
                "local_account_id": user_oid,
                "username": username,
                "name": name,
                "authority_type": "MSSTS",
                "client_info": base64.urlsafe_b64encode(
                    json.dumps({"uid": user_oid, "utid": user_tid}).encode()
                )
                .decode()
                .rstrip("="),
            }
        },
        "AccessToken": {
            f"{home_account_id}-login.microsoftonline.com-accesstoken-{client_id}-{user_tid}-{scopes_no_offline}": {
                "home_account_id": home_account_id,
                "environment": "login.microsoftonline.com",
                "credential_type": "AccessToken",
                "client_id": client_id,
                "secret": access_token,
                "realm": user_tid,
                "target": scopes_no_offline,
                "cached_at": str(now),
                "expires_on": str(expires_on),
                "extended_expires_on": str(ext_expires_on),
            }
        },
        "RefreshToken": {},
        "IdToken": {},
        "AppMetadata": {
            f"appmetadata-login.microsoftonline.com-{client_id}": {
                "client_id": client_id,
                "environment": "login.microsoftonline.com",
            }
        },
    }

    # Add refresh token if present
    rt = token_result.get("refresh_token", "")
    if rt:
        cache_data["RefreshToken"][
            f"{home_account_id}-login.microsoftonline.com-refreshtoken-{client_id}--{scopes_no_offline}"
        ] = {
            "home_account_id": home_account_id,
            "environment": "login.microsoftonline.com",
            "credential_type": "RefreshToken",
            "client_id": client_id,
            "secret": rt,
            "target": scopes_no_offline,
        }
        log("Refresh token: ✅ present")
    else:
        log("⚠️  Refresh token: MISSING (token will expire without renewal)")

    # Add ID token if present
    id_token = token_result.get("id_token", "")
    if id_token:
        cache_data["IdToken"][
            f"{home_account_id}-login.microsoftonline.com-idtoken-{client_id}-{user_tid}-"
        ] = {
            "home_account_id": home_account_id,
            "environment": "login.microsoftonline.com",
            "credential_type": "IdToken",
            "client_id": client_id,
            "secret": id_token,
            "realm": user_tid,
        }

    # Save locally first — wrapped in Softeria's cache envelope format
    # Softeria MCP server expects: {_cacheEnvelope: true, data: "<json string>", savedAt: <ms>}
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    cache_file = os.path.join(OUTPUT_DIR, "msal_cache.json")
    selected_file = os.path.join(OUTPUT_DIR, "selected_account.json")

    now_ms = int(time.time() * 1000)

    cache_wrapped = json.dumps({
        "_cacheEnvelope": True,
        "data": json.dumps(cache_data),
        "savedAt": now_ms,
    })
    with open(cache_file, "w") as f:
        f.write(cache_wrapped)

    # Softeria expects {accountId: "..."} (not homeAccountId)
    selected_wrapped = json.dumps({
        "_cacheEnvelope": True,
        "data": json.dumps({"accountId": home_account_id}),
        "savedAt": now_ms,
    })
    with open(selected_file, "w") as f:
        f.write(selected_wrapped)

    log(f"\nLocal cache saved to: {cache_file}")

    # Copy into running container
    log(f"\nCopying into container '{CONTAINER_NAME}'...")
    try:
        # Ensure target directory exists in container
        subprocess.run(
            ["docker", "exec", CONTAINER_NAME, "mkdir", "-p", "/app/data/ms365-mcp"],
            check=True,
            capture_output=True,
        )
        # Copy cache file
        subprocess.run(
            ["docker", "cp", cache_file, f"{CONTAINER_NAME}:{CONTAINER_CACHE_PATH}"],
            check=True,
            capture_output=True,
        )
        # Copy selected account file
        subprocess.run(
            ["docker", "cp", selected_file, f"{CONTAINER_NAME}:{CONTAINER_SELECTED_PATH}"],
            check=True,
            capture_output=True,
        )
        log(f"✅ Cache copied to container at {CONTAINER_CACHE_PATH}")
        log(f"✅ Selected account copied to {CONTAINER_SELECTED_PATH}")
    except subprocess.CalledProcessError as e:
        log(f"\n⚠️  docker cp failed: {e.stderr.decode() if e.stderr else str(e)}")
        log(f"Container '{CONTAINER_NAME}' may not be running.")
        log(f"Manual copy: docker cp {cache_file} {CONTAINER_NAME}:{CONTAINER_CACHE_PATH}")
        return

    # Verify token works with Graph API
    verified = verify_graph_api(access_token)

    # Clean up local output
    try:
        os.remove(cache_file)
        os.remove(selected_file)
        os.rmdir(OUTPUT_DIR)
    except OSError:
        pass

    if not verified:
        log("\n⚠️  Token was saved but Graph API verification failed.")
        log("The token may still work — try asking the bot: 'check my Outlook calendar'")
        return

    # Restart container so MCP server loads new cache
    log("\n" + "=" * 60)
    restart_container()
    log("")
    log("✅ M365 auth complete!")
    log("After restart (~15-30s), test: ask the bot 'check my Outlook calendar'")
    log("")
    log("Token refresh is automatic:")
    log("  • Softeria auto-refreshes via MSAL when access token expires")
    log("  • Refresh token lasts 90 days without activity")
    log("  • No re-auth needed unless you don't use M365 for 90+ days")
    log("=" * 60)


def verify_graph_api(access_token):
    """Make a real Graph API call to verify the token works end-to-end."""
    log("\nVerifying token with Microsoft Graph API...")
    try:
        resp = requests.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"$select": "displayName,mail,userPrincipalName"},
            timeout=15,
        )
        if resp.status_code == 200:
            user = resp.json()
            log(f"✅ Graph API verified: {user.get('displayName', '?')} ({user.get('mail', user.get('userPrincipalName', '?'))})")
            return True
        else:
            log(f"⚠️  Graph API returned HTTP {resp.status_code}")
            log(f"   {resp.text[:300]}")
            return False
    except Exception as e:
        log(f"⚠️  Graph API call failed: {e}")
        return False


def restart_container():
    """Restart the bot container so MCP server loads the new cache."""
    log(f"\nRestarting container '{CONTAINER_NAME}' so MCP server picks up new cache...")
    try:
        subprocess.run(
            ["docker", "restart", CONTAINER_NAME],
            check=True,
            capture_output=True,
            timeout=60,
        )
        log(f"✅ Container '{CONTAINER_NAME}' restarted. Bot will be back in ~15-30s.")
        return True
    except subprocess.CalledProcessError as e:
        log(f"⚠️  docker restart failed: {e.stderr.decode() if e.stderr else str(e)}")
        log(f"   Restart manually: docker restart {CONTAINER_NAME}")
        return False
    except subprocess.TimeoutExpired:
        log("⚠️  Restart timed out (60s). Container may still be restarting.")
        return False


def verify_cached_token():
    """Read cached token from container and verify it works."""
    log("Checking cached M365 token in container...")

    # Try to read the cache from container
    try:
        result = subprocess.run(
            ["docker", "exec", CONTAINER_NAME, "cat", CONTAINER_CACHE_PATH],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            log(f"❌ Cannot read cache file from container: {result.stderr.strip()}")
            return False
    except Exception as e:
        log(f"❌ Cannot access container: {e}")
        return False

    # Parse the envelope
    try:
        envelope = json.loads(result.stdout)
        if envelope.get("_cacheEnvelope"):
            cache_data = json.loads(envelope["data"])
            log("✅ Cache format: Softeria envelope (correct)")
        else:
            cache_data = envelope
            log("⚠️  Cache format: raw MSAL (missing Softeria envelope — will not work!)")
            log("   Re-run: python3 scripts/m365_login.py")
            return False
    except json.JSONDecodeError as e:
        log(f"❌ Cache file is not valid JSON: {e}")
        return False

    # Check required sections
    has_account = bool(cache_data.get("Account"))
    has_refresh = bool(cache_data.get("RefreshToken"))
    has_access = bool(cache_data.get("AccessToken"))

    log(f"   Account: {'✅' if has_account else '❌'}")
    log(f"   AccessToken: {'✅' if has_access else '❌'}")
    log(f"   RefreshToken: {'✅' if has_refresh else '❌ (token will expire without renewal!)'}")

    if not has_account:
        log("❌ No account in cache. Re-run: python3 scripts/m365_login.py")
        return False

    # Check selected_account.json format
    try:
        sa_result = subprocess.run(
            ["docker", "exec", CONTAINER_NAME, "cat", CONTAINER_SELECTED_PATH],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if sa_result.returncode == 0:
            sa = json.loads(sa_result.stdout)
            if sa.get("_cacheEnvelope"):
                inner = json.loads(sa["data"])
                if "accountId" in inner:
                    log("   Selected account: ✅ (correct Softeria format)")
                else:
                    log("   Selected account: ❌ (missing 'accountId' key — has wrong format)")
                    return False
            else:
                log("   Selected account: ⚠️  (missing Softeria envelope)")
                return False
    except Exception:
        log("   Selected account: ⚠️  (could not read)")

    # Try to verify with Graph API using the cached access token
    if has_access:
        at_key = list(cache_data["AccessToken"].keys())[0]
        at_secret = cache_data["AccessToken"][at_key].get("secret", "")
        expires_on = int(cache_data["AccessToken"][at_key].get("expires_on", "0"))
        now = int(time.time())

        if expires_on > now:
            log(f"   Token expires in: {(expires_on - now) // 60} minutes")
            return verify_graph_api(at_secret)
        else:
            log(f"   Access token expired {(now - expires_on) // 60} min ago")
            if has_refresh:
                log("   ✅ Refresh token present — Softeria will auto-refresh on first use")
                return True
            else:
                log("   ❌ No refresh token — re-auth needed: python3 scripts/m365_login.py")
                return False

    return has_refresh  # If we have refresh but no access, Softeria can acquire


def check_cred():
    """Test credential validity with client_credentials grant (no user interaction)."""
    log("Testing Azure AD credentials...")
    log(f"  Client ID: {client_id[:8]}... (len={len(client_id)})")
    log(f"  Tenant ID: {tenant_id[:8]}... (len={len(tenant_id)})")
    log(f"  Client secret: {'present' if client_secret else 'MISSING'} (len={len(client_secret)})")

    resp = requests.post(
        f"{AUTHORITY}/oauth2/v2.0/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
    )

    j = resp.json()
    if resp.status_code == 200:
        log("\n✅ CREDENTIALS VALID — Azure AD accepted the client secret.")
        log("You can proceed with device code login: python3 scripts/m365_login.py")
    else:
        log(f"\n❌ FAILED (HTTP {resp.status_code})")
        log(f"Error: {j.get('error', 'unknown')}")
        desc = j.get("error_description", "")
        log(f"Description: {desc[:400]}")
        if "AADSTS7000215" in desc:
            log("\n→ The client secret is invalid or expired. Generate a new one in Azure Portal.")
        elif "AADSTS700016" in desc:
            log("\n→ The client ID is not found in the tenant. Check MS365_MCP_CLIENT_ID.")
        elif "AADSTS90002" in desc:
            log("\n→ The tenant ID is invalid. Check MS365_MCP_TENANT_ID.")


def main():
    if not os.path.exists(ENV_PATH):
        log(f"ERROR: .env file not found at {ENV_PATH}")
        log("Run this script from the corporate-bot repo root.")
        sys.exit(1)

    if not all([client_id, tenant_id, client_secret]):
        missing = []
        if not client_id:
            missing.append("MS365_MCP_CLIENT_ID")
        if not tenant_id:
            missing.append("MS365_MCP_TENANT_ID")
        if not client_secret:
            missing.append("MS365_MCP_CLIENT_SECRET")
        log(f"ERROR: Missing env vars in .env: {', '.join(missing)}")
        sys.exit(1)

    # Check for resumable state
    state = load_state()
    if state and time.time() < state.get("expires_at", 0):
        remaining = int(state["expires_at"] - time.time())
        log(f"Resuming poll for code {state['user_code']} ({remaining}s remaining)")
    else:
        if state:
            log("Previous device code expired, requesting new one...")
            clear_state()
        state = request_device_code()

    token_result = poll_for_token(state)
    if token_result:
        save_token(token_result)
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "check":
        check_cred()
    elif cmd == "verify":
        verify_cached_token()
    else:
        main()
