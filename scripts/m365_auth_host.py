#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""
M365 device code auth — run on HOST machine (outside Docker).

Avoids container security hooks and process lifecycle issues.
Uses only stdlib (no pip install needed).

Usage:
  cd /path/to/corporate-bot-repo
  python3 scripts/m365_auth_host.py

After auth completes, copies token cache into the Docker container automatically.
"""
import json
import os
import sys
import time
import base64
import subprocess
import urllib.request
import urllib.parse
import urllib.error

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
ENV_PATH = os.path.join(REPO_DIR, ".env")

# Where to save locally before copying into container
OUTPUT_DIR = os.path.join(REPO_DIR, "data", "ms365-auth-output")
CACHE_FILE = os.path.join(OUTPUT_DIR, "msal_cache.json")
ACCOUNT_FILE = os.path.join(OUTPUT_DIR, "selected_account.json")

# Container paths (where Softeria MCP expects the files)
CONTAINER_NAME = "corporate-bot"
CONTAINER_CACHE_PATH = "/app/data/ms365-mcp/msal_cache.json"
CONTAINER_ACCOUNT_PATH = "/app/data/ms365-mcp/selected_account.json"

SCOPES = "https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Calendars.ReadWrite https://graph.microsoft.com/User.Read offline_access"


def parse_env(path):
    """Parse .env file manually — no python-dotenv needed."""
    vals = {}
    if not os.path.exists(path):
        return vals
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Remove surrounding quotes if present
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            vals[key] = value
    return vals


def http_post(url, data):
    """POST form data, return parsed JSON."""
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=encoded, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"error": "http_error", "error_description": f"HTTP {e.code}: {body[:500]}"}


def request_device_code(client_id, authority):
    """Request a new device code from Azure AD."""
    print("\nRequesting device code...")
    result = http_post(
        f"{authority}/oauth2/v2.0/devicecode",
        {"client_id": client_id, "scope": SCOPES},
    )
    if "error" in result:
        print(f"ERROR: {result.get('error')}")
        print(f"Description: {result.get('error_description', '')[:300]}")
        sys.exit(1)

    return result


def poll_for_token(flow, client_id, client_secret, authority):
    """Poll Azure AD until user authorizes or code expires."""
    device_code = flow["device_code"]
    interval = flow.get("interval", 5)
    expires_in = flow.get("expires_in", 900)
    deadline = time.time() + expires_in

    print(f"\nPolling for authorization (timeout: {expires_in}s, interval: {interval}s)...")

    while time.time() < deadline:
        time.sleep(interval)

        # Device code flow — do NOT send client_secret (AADSTS700025).
        result = http_post(
            f"{authority}/oauth2/v2.0/token",
            {
                "client_id": client_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
            },
        )

        error = result.get("error", "")
        if error == "authorization_pending":
            print(".", end="", flush=True)
            continue
        elif error == "slow_down":
            interval += 5
            print(f"\n(slowing to {interval}s)", flush=True)
            continue
        elif error == "expired_token":
            print("\n\nDevice code expired. Please re-run the script.")
            return None
        elif error:
            print(f"\n\nFAILED: {error}")
            print(f"Description: {result.get('error_description', '')[:500]}")
            return None
        else:
            print("\n\nToken acquired!")
            return result

    print("\n\nTimeout — device code expired.")
    return None


def build_msal_cache(token_result, client_id, tenant_id):
    """Build MSAL-compatible cache JSON from raw token response."""
    access_token = token_result["access_token"]

    # Decode JWT payload
    payload_b64 = access_token.split(".")[1]
    payload_b64 += "=" * (4 - len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))

    user_oid = payload.get("oid", "")
    user_tid = payload.get("tid", tenant_id)
    username = payload.get("preferred_username", payload.get("upn", "unknown"))
    name = payload.get("name", username)
    home_account_id = f"{user_oid}.{user_tid}"

    print(f"User: {username} ({name})")
    print(f"Account ID: {home_account_id}")

    now = int(time.time())
    expires_on = now + token_result.get("expires_in", 3600)
    ext_expires_on = now + token_result.get("ext_expires_in", token_result.get("expires_in", 3600))
    scopes_no_offline = SCOPES.replace("offline_access", "").strip()

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
                ).decode().rstrip("="),
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

    # Refresh token
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
        print("Refresh token: present")
    else:
        print("WARNING: No refresh token — access will expire without renewal")

    # ID token
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

    selected = {"homeAccountId": home_account_id, "username": username}

    return cache_data, selected


def copy_to_container(local_path, container_path):
    """Copy a file into the Docker container."""
    cmd = ["docker", "cp", local_path, f"{CONTAINER_NAME}:{container_path}"]
    print(f"  docker cp → {container_path}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  WARNING: docker cp failed: {result.stderr.strip()}")
        return False
    return True


def main():
    # Load env
    env = parse_env(ENV_PATH)
    client_id = env.get("MS365_MCP_CLIENT_ID", "")
    tenant_id = env.get("MS365_MCP_TENANT_ID", "")
    client_secret = env.get("MS365_MCP_CLIENT_SECRET", "")

    if not all([client_id, tenant_id, client_secret]):
        print("ERROR: Missing MS365_MCP_CLIENT_ID, MS365_MCP_TENANT_ID, or MS365_MCP_CLIENT_SECRET in .env")
        sys.exit(1)

    print(f"Client ID: {client_id[:8]}...")
    print(f"Tenant ID: {tenant_id[:8]}...")
    print(f"Secret length: {len(client_secret)}")

    authority = f"https://login.microsoftonline.com/{tenant_id}"

    # Step 1: Get device code
    flow = request_device_code(client_id, authority)

    user_code = flow.get("user_code", "")
    verification_uri = flow.get("verification_uri", "https://login.microsoft.com/device")

    print(f"\n{'='*60}")
    print(f"  Go to: {verification_uri}")
    print(f"  Enter code: {user_code}")
    print(f"{'='*60}")
    print(f"\nWaiting for you to authorize...")

    # Step 2: Poll for token
    token_result = poll_for_token(flow, client_id, client_secret, authority)
    if not token_result:
        sys.exit(1)

    # Step 3: Build MSAL cache
    cache_data, selected_account = build_msal_cache(token_result, client_id, tenant_id)

    # Step 4: Save locally
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache_data, f, indent=2)
    with open(ACCOUNT_FILE, "w") as f:
        json.dump(selected_account, f, indent=2)
    print(f"\nSaved locally to {OUTPUT_DIR}/")

    # Step 5: Copy into container
    print("\nCopying into Docker container...")
    # Ensure target dir exists in container
    subprocess.run(
        ["docker", "exec", CONTAINER_NAME, "mkdir", "-p", "/app/data/ms365-mcp"],
        capture_output=True,
    )
    ok1 = copy_to_container(CACHE_FILE, CONTAINER_CACHE_PATH)
    ok2 = copy_to_container(ACCOUNT_FILE, CONTAINER_ACCOUNT_PATH)
    # Fix ownership
    subprocess.run(
        ["docker", "exec", CONTAINER_NAME, "chown", "-R", "botuser:botuser", "/app/data/ms365-mcp"],
        capture_output=True,
    )

    if ok1 and ok2:
        print("\n" + "="*60)
        print("  M365 auth complete!")
        print("  Token cache is in the container.")
        print("  Softeria MCP server will pick it up on next SDK client connect.")
        print("="*60)
    else:
        print(f"\nManual copy needed:")
        print(f"  docker cp {CACHE_FILE} {CONTAINER_NAME}:{CONTAINER_CACHE_PATH}")
        print(f"  docker cp {ACCOUNT_FILE} {CONTAINER_NAME}:{CONTAINER_ACCOUNT_PATH}")


if __name__ == "__main__":
    main()
