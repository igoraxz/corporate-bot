#!/bin/bash
# Server-side OAuth token refresh cron job.
# Runs ON THE SERVER (not macOS) — uses the refresh token already in the container
# to get a fresh access token from Anthropic's OAuth endpoint.
#
# SETUP:
#   chmod +x ~/corporate-bot/scripts/refresh_token_cron.sh
#   crontab -e  # add:
#   0 */2 * * * $HOME/<project-dir>/scripts/refresh_token_cron.sh >> $HOME/<project-dir>/deploy/token_refresh.log 2>&1
#
# Runs every 2 hours. Tokens last ~3h, so this ensures we always have a valid one.

set -euo pipefail

CONTAINER="${CONTAINER:-corporate-bot}"
CREDS_PATH="/home/botuser/.claude/.credentials.json"
TOKEN_URL="https://platform.claude.com/v1/oauth/token"
# Claude Code's public OAuth client ID (not a secret — same for all Claude Code installations)
CLIENT_ID="${CLAUDE_OAUTH_CLIENT_ID:-9d1c250a-e61b-44d9-88ed-5944d1962f5e}"
SCOPES="user:profile user:inference user:sessions:claude_code user:mcp_servers"

log() { echo "$(date -Iseconds) — $*"; }

# Read current credentials from container
CREDS_JSON=$(docker exec "$CONTAINER" cat "$CREDS_PATH" 2>/dev/null) || {
    log "ERROR: Cannot read credentials from container"
    exit 1
}

# Check if token is still valid (with 30min buffer)
NEEDS_REFRESH=$(echo "$CREDS_JSON" | python3 -c "
import sys, json, time
d = json.load(sys.stdin)
oauth = d.get('claudeAiOauth', {})
exp = oauth.get('expiresAt', 0)
# expiresAt is in milliseconds
remaining = (exp / 1000) - time.time()
# Refresh if less than 30 minutes remaining
print('yes' if remaining < 1800 else 'no')
print(f'{remaining/3600:.1f}h remaining')
")

REMAINING=$(echo "$NEEDS_REFRESH" | tail -1)
NEEDS_REFRESH=$(echo "$NEEDS_REFRESH" | head -1)

if [ "$NEEDS_REFRESH" = "no" ]; then
    log "Token still valid ($REMAINING) — skipping refresh"
    exit 0
fi

log "Token expiring soon ($REMAINING) — refreshing..."

# Extract refresh token
REFRESH_TOKEN=$(echo "$CREDS_JSON" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d['claudeAiOauth']['refreshToken'])
")

if [ -z "$REFRESH_TOKEN" ] || [ "$REFRESH_TOKEN" = "None" ]; then
    log "ERROR: No refresh token available — need manual refresh from macOS"
    exit 1
fi

# Call OAuth token endpoint
RESPONSE=$(curl -sf --max-time 15 "$TOKEN_URL" \
    -H "Content-Type: application/json" \
    -d "{
        \"grant_type\": \"refresh_token\",
        \"refresh_token\": \"$REFRESH_TOKEN\",
        \"client_id\": \"$CLIENT_ID\",
        \"scope\": \"$SCOPES\"
    }" 2>&1) || {
    log "ERROR: Token refresh request failed: $RESPONSE"
    exit 1
}

# Parse response and update credentials
UPDATED_CREDS=$(echo "$RESPONSE" | python3 -c "
import sys, json, time

resp = json.load(sys.stdin)
if 'access_token' not in resp:
    print('ERROR: ' + json.dumps(resp), file=sys.stderr)
    sys.exit(1)

# Read existing creds to preserve structure
existing = json.loads('''$(echo "$CREDS_JSON" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin)))")''')

oauth = existing.get('claudeAiOauth', {})
oauth['accessToken'] = resp['access_token']
# Use new refresh token if provided, otherwise keep the old one
if resp.get('refresh_token'):
    oauth['refreshToken'] = resp['refresh_token']
# expiresAt in milliseconds
oauth['expiresAt'] = int((time.time() + resp.get('expires_in', 3600)) * 1000)

existing['claudeAiOauth'] = oauth
print(json.dumps(existing))
") || {
    log "ERROR: Failed to parse refresh response"
    exit 1
}

# Write updated credentials back to container
echo "$UPDATED_CREDS" | docker exec -i "$CONTAINER" tee "$CREDS_PATH" > /dev/null
docker exec "$CONTAINER" chown botuser:botuser "$CREDS_PATH"
docker exec "$CONTAINER" chmod 600 "$CREDS_PATH"

# Verify
NEW_EXPIRY=$(echo "$UPDATED_CREDS" | python3 -c "
import sys, json, time
d = json.load(sys.stdin)
exp = d['claudeAiOauth']['expiresAt'] / 1000
print(f'{(exp - time.time())/3600:.1f}h')
")

log "Token refreshed successfully — valid for $NEW_EXPIRY"
