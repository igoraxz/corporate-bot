#!/bin/bash
# Mac-side launcher for `claude setup-token` remote re-auth flow (v7).
#
# Invoked via guard.sh `relay-claude-login`. Starts the Python helper
# detached (survives SSH disconnect), polls the state file for the OAuth
# URL, and echoes it back on stdout. The helper stays alive in the
# background, polling ~/.relay/claude-login.code for the bot-delivered
# OAuth code, then captures the 1-year token to ~/.relay/oauth_token.
#
# stdout format on success:   https://...|HELPER_PID
# stdout format on error:     ERROR:<reason>

set -uo pipefail

RELAY_DIR="$HOME/.relay"
STATE_FILE="$RELAY_DIR/claude-login.state"
PID_FILE="$RELAY_DIR/claude-login.pid"
HELPER="$RELAY_DIR/claude_login_helper.py"
LOG_FILE="$RELAY_DIR/claude-login.log"

mkdir -p "$RELAY_DIR"
chmod 700 "$RELAY_DIR" 2>/dev/null || true

# Resolve Python (prefer pyenv, same as auth.sh)
PYENV_PYTHON="/Users/youruser/.pyenv/versions/3.12.7/bin/python3"
if [ -x "$PYENV_PYTHON" ]; then
    PYTHON="$PYENV_PYTHON"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
else
    echo "ERROR:no_python3"
    exit 1
fi

# Helper must be installed
if [ ! -f "$HELPER" ]; then
    echo "ERROR:helper_not_installed_at_$HELPER"
    exit 1
fi

# Kill any stale helper from previous run
if [ -f "$PID_FILE" ]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        kill -TERM "$OLD_PID" 2>/dev/null || true
        sleep 0.3
        kill -KILL "$OLD_PID" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
fi

# Clear state + start fresh
: > "$STATE_FILE"

# Launch detached helper (disown so it survives SSH disconnect)
nohup "$PYTHON" "$HELPER" </dev/null >>"$LOG_FILE" 2>&1 &
HELPER_PID=$!
echo "$HELPER_PID" > "$PID_FILE"
disown "$HELPER_PID" 2>/dev/null || true

# Poll for state (up to ~50s — helper captures URL within 45s usually)
for i in $(seq 1 100); do
    sleep 0.5
    if [ -s "$STATE_FILE" ]; then
        FIRST_LINE="$(head -n1 "$STATE_FILE")"
        case "$FIRST_LINE" in
            https://*\|*)
                echo "$FIRST_LINE"
                exit 0
                ;;
            ERROR:*)
                echo "$FIRST_LINE"
                rm -f "$PID_FILE"
                exit 1
                ;;
        esac
    fi
    # If helper died before writing state, abort
    if ! kill -0 "$HELPER_PID" 2>/dev/null; then
        echo "ERROR:helper_died_early"
        rm -f "$PID_FILE"
        exit 1
    fi
done

# Timeout — kill helper
kill -TERM "$HELPER_PID" 2>/dev/null || true
sleep 0.3
kill -KILL "$HELPER_PID" 2>/dev/null || true
rm -f "$PID_FILE"
echo "ERROR:url_capture_timeout"
exit 1
