#!/bin/bash
# Use pyenv python3 (has pyotp installed) — NOT Homebrew/anaconda python3
PYENV_PYTHON="/Users/youruser/.pyenv/versions/3.12.7/bin/python3"
# Fallback: also add Homebrew to PATH for tmux and other tools
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"

# Desktop Relay Auth — Mac-side TOTP verification
#
# Install: place at ~/.relay/auth.sh on Mac
# Called by guard.sh when it receives a "relay-auth <code>" command.
#
# Verifies TOTP code using pyotp, creates a time-limited session token.
# Session format: {token}:{last_activity_timestamp}
# TTL is sliding window — guard.sh updates last_activity on each command.
# Brute-force protection: 3 failed attempts = 15 minute lockout.

CODE="$1"
SECRET=$(cat ~/.relay/totp_secret 2>/dev/null)
FAIL_FILE=~/.relay/fail_count
SESSION_FILE=~/.relay/session

# Check lockout
if [ -f "$FAIL_FILE" ]; then
    FAILS=$(cat "$FAIL_FILE")
    if [ "$FAILS" -ge 3 ]; then
        # macOS stat uses -f %m, Linux uses -c %Y
        LOCK_TIME=$(stat -f %m "$FAIL_FILE" 2>/dev/null || stat -c %Y "$FAIL_FILE")
        NOW=$(date +%s)
        DIFF=$((NOW - LOCK_TIME))
        if [ "$DIFF" -lt 900 ]; then
            echo "LOCKED_OUT:$((900 - DIFF))s remaining"
            exit 1
        fi
        rm -f "$FAIL_FILE"
    fi
fi

# Verify TOTP
RESULT=$($PYENV_PYTHON -c "
import pyotp, sys
secret = '$SECRET'
code = '$CODE'
totp = pyotp.TOTP(secret)
if totp.verify(code, valid_window=1):
    print('OK')
else:
    print('FAIL')
")

if [ "$RESULT" = "OK" ]; then
    TOKEN=$($PYENV_PYTHON -c "import secrets; print(secrets.token_hex(16))")
    # Store token with current timestamp as initial last_activity
    # guard.sh uses sliding window: checks (now - last_activity) > TTL
    NOW=$(date +%s)
    echo "${TOKEN}:${NOW}" > "$SESSION_FILE"
    chmod 600 "$SESSION_FILE"
    rm -f "$FAIL_FILE"
    echo "AUTH_OK:${TOKEN}:7200"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) AUTH_OK" >> ~/.relay/audit.log
else
    FAILS=$(cat "$FAIL_FILE" 2>/dev/null || echo 0)
    echo $((FAILS + 1)) > "$FAIL_FILE"
    echo "AUTH_FAILED:attempt $((FAILS + 1))/3"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) AUTH_FAILED attempt $((FAILS + 1))" >> ~/.relay/audit.log
    exit 1
fi
