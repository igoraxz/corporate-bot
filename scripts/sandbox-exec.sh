#!/bin/bash
# sandbox-exec.sh — Bubblewrap (bwrap) kernel-level sandbox for coding sessions.
#
# Wraps a bash command in an isolated namespace with:
#   - Cleared environment (no API keys, tokens, secrets)
#   - Network fully isolated (--unshare-net) with proxy-based internet access
#   - New PID namespace (process isolation)
#   - /proc hidden (no parent process info, env vars, or PID enumeration)
#   - /app hidden by tmpfs (no bot source, data, logs visible)
#   - Only the sandbox directory is writable
#   - System dirs (/usr, /etc) mounted read-only for Python/bash to work
#   - Auto-activates .venv/ if present in sandbox dir
#
# Network isolation via Unix socket proxy bridge:
#   bwrap always uses --unshare-net (zero network access at kernel level).
#   When network is enabled (default), a Python HTTP proxy runs outside bwrap
#   and listens on a Unix socket. The socket is bind-mounted into the sandbox.
#   Inside the sandbox, a small Python forwarder bridges localhost:3128 to the
#   Unix socket. HTTP_PROXY/HTTPS_PROXY env vars route all traffic through it.
#   The proxy enforces port filtering: only TCP 80/443 are forwarded.
#   DNS resolution happens at the proxy level (outside sandbox) using public DNS.
#   Docker internal services are completely unreachable:
#     - No AF_INET/AF_INET6 sockets possible (--unshare-net)
#     - Proxy resolves hostnames via public DNS (not Docker's resolver)
#     - Proxy only forwards ports 80/443
#   No capabilities needed. No pasta/slirp4netns. Pure Python + bwrap.
#
# Usage: sandbox-exec.sh <sandbox_dir> <command_string>
# Environment:
#   SANDBOX_NO_NETWORK=1  — disables ALL network (no proxy, fully offline)
# Exit codes: propagates child exit code, or 1 on bwrap setup failure
#
# On merged-usr systems (Debian 12+), /bin /sbin /lib are symlinks to /usr/.
# We detect this and create matching symlinks inside the sandbox.

set -euo pipefail

SANDBOX_DIR="$1"
shift
CMD="$1"

if [ -z "$SANDBOX_DIR" ] || [ -z "$CMD" ]; then
    echo "Usage: sandbox-exec.sh <sandbox_dir> <command>" >&2
    exit 1
fi

# Verify sandbox dir exists
if [ ! -d "$SANDBOX_DIR" ]; then
    echo "sandbox-exec: sandbox dir does not exist: $SANDBOX_DIR" >&2
    exit 1
fi

# Build parent directory chain for the sandbox path (under /app)
# bwrap's --tmpfs /app creates empty tmpfs, so we need --dir for intermediates
DIR_ARGS=()
PARENT="$SANDBOX_DIR"
while true; do
    PARENT=$(dirname "$PARENT")
    if [ "$PARENT" = "/" ] || [ "$PARENT" = "." ]; then
        break
    fi
    # Only create dirs under /app (the tmpfs root)
    if [[ "$PARENT" == /app* ]]; then
        DIR_ARGS=("--dir" "$PARENT" "${DIR_ARGS[@]}")
    fi
done

# Detect merged-usr (Debian 12+): /bin is a symlink to usr/bin
SYMLINK_ARGS=()
if [ -L /bin ]; then
    SYMLINK_ARGS+=(--symlink usr/bin /bin)
fi
if [ -L /sbin ]; then
    SYMLINK_ARGS+=(--symlink usr/sbin /sbin)
fi
if [ -L /lib ]; then
    SYMLINK_ARGS+=(--symlink usr/lib /lib)
fi
if [ -L /lib64 ]; then
    SYMLINK_ARGS+=(--symlink usr/lib64 /lib64)
fi

# If /bin is NOT a symlink, bind-mount it directly
if [ ! -L /bin ] && [ -d /bin ]; then
    SYMLINK_ARGS+=(--ro-bind /bin /bin)
fi
if [ ! -L /sbin ] && [ -d /sbin ]; then
    SYMLINK_ARGS+=(--ro-bind /sbin /sbin)
fi
if [ ! -L /lib ] && [ -d /lib ]; then
    SYMLINK_ARGS+=(--ro-bind /lib /lib)
fi
if [ ! -L /lib64 ] && [ -d /lib64 ]; then
    SYMLINK_ARGS+=(--ro-bind /lib64 /lib64)
fi

# Venv auto-activation: if .venv/ exists in sandbox dir, prepend to PATH
VENV_PATH="/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"
VENV_ARGS=()
if [ -d "$SANDBOX_DIR/.venv/bin" ]; then
    VENV_PATH="$SANDBOX_DIR/.venv/bin:$VENV_PATH"
    VENV_ARGS+=(--setenv VIRTUAL_ENV "$SANDBOX_DIR/.venv")
fi

# Temp files for this sandbox instance
SANDBOX_RESOLV="/tmp/sandbox_resolv_$$"
# Socket must be under sandbox dir (not /tmp which gets tmpfs inside bwrap)
PROXY_SOCK="${SANDBOX_DIR}/.sandbox_proxy_$$.sock"
PROXY_READY="/tmp/sandbox_proxy_ready_$$"
PROXY_SCRIPT="/host-repo/scripts/sandbox-proxy.py"

# Write custom resolv.conf (public DNS — used by proxy, not by sandbox directly)
echo "nameserver 8.8.8.8" > "$SANDBOX_RESOLV"
echo "nameserver 8.8.4.4" >> "$SANDBOX_RESOLV"

# Cleanup on exit
cleanup() {
    # Kill proxy if running
    if [ -n "${PROXY_PID:-}" ]; then
        kill "$PROXY_PID" 2>/dev/null || true
        wait "$PROXY_PID" 2>/dev/null || true
    fi
    rm -f "$SANDBOX_RESOLV" "$PROXY_SOCK" "$PROXY_READY" 2>/dev/null || true
    rm -f "${SANDBOX_DIR}/.sandbox_forwarder.py" 2>/dev/null || true
}
trap cleanup EXIT

# Proxy env vars for inside the sandbox
PROXY_ARGS=()

if [ "${SANDBOX_NO_NETWORK:-0}" != "1" ] && [ -f "$PROXY_SCRIPT" ]; then
    # Start the proxy outside bwrap (has full network access)
    python3 "$PROXY_SCRIPT" "$PROXY_SOCK" "$PROXY_READY" &
    PROXY_PID=$!

    # Wait for proxy to be ready (file-based signal)
    for i in $(seq 1 50); do
        if [ -f "$PROXY_READY" ]; then
            break
        fi
        sleep 0.05
    done

    if [ -f "$PROXY_READY" ]; then
        # Bind-mount the Unix socket + inner forwarder script into the sandbox
        INNER_FORWARDER="/host-repo/scripts/sandbox-inner-forwarder.py"
        # Copy inner forwarder to sandbox dir (can't bind into /usr — read-only).
        # Socket is already under SANDBOX_DIR (bind-mounted into bwrap).
        FORWARDER_IN_SANDBOX="${SANDBOX_DIR}/.sandbox_forwarder.py"
        cp "$INNER_FORWARDER" "$FORWARDER_IN_SANDBOX" 2>/dev/null || true

        # Wrap: start inner forwarder (backgrounds), wait for ready, then run user cmd
        WRAPPED_CMD="python3 ${FORWARDER_IN_SANDBOX} $PROXY_SOCK & for i in \$(seq 1 20); do [ -f /tmp/.proxy_inner_ready ] && break; sleep 0.05; done; $CMD"

        # Set proxy env vars so pip/curl/urllib use the proxy
        PROXY_ARGS=(
            --setenv HTTP_PROXY "http://127.0.0.1:3128"
            --setenv HTTPS_PROXY "http://127.0.0.1:3128"
            --setenv http_proxy "http://127.0.0.1:3128"
            --setenv https_proxy "http://127.0.0.1:3128"
            --setenv NO_PROXY "localhost,127.0.0.1"
            --setenv no_proxy "localhost,127.0.0.1"
        )
    else
        # Proxy failed to start — fall back to offline mode
        echo "sandbox-exec: WARNING: proxy failed to start, running without network" >&2
        WRAPPED_CMD="$CMD"
    fi
else
    WRAPPED_CMD="$CMD"
fi

# NOTE: do NOT use 'exec' here — the EXIT trap must fire to kill the proxy
# process and clean up temp files. The extra shell process is acceptable.
bwrap \
    --clearenv \
    --setenv PATH "$VENV_PATH" \
    --setenv HOME "$SANDBOX_DIR" \
    --setenv LANG "C.UTF-8" \
    --setenv TERM "xterm-256color" \
    --setenv PYTHONDONTWRITEBYTECODE "1" \
    --setenv NODE_PATH "/usr/lib/node_modules" \
    "${VENV_ARGS[@]}" \
    "${PROXY_ARGS[@]}" \
    --ro-bind /usr /usr \
    "${SYMLINK_ARGS[@]}" \
    --dir /etc \
    --ro-bind /etc/ssl /etc/ssl \
    --ro-bind /etc/alternatives /etc/alternatives \
    --ro-bind /etc/ld.so.cache /etc/ld.so.cache \
    --ro-bind /etc/localtime /etc/localtime \
    --ro-bind /etc/nsswitch.conf /etc/nsswitch.conf \
    --ro-bind /etc/passwd /etc/passwd \
    --ro-bind /etc/group /etc/group \
    --ro-bind "$SANDBOX_RESOLV" /etc/resolv.conf \
    --tmpfs /app \
    "${DIR_ARGS[@]}" \
    --bind "$SANDBOX_DIR" "$SANDBOX_DIR" \
    --tmpfs /tmp \
    --dev /dev \
    --chdir "$SANDBOX_DIR" \
    --unshare-user \
    --unshare-pid \
    --unshare-net \
    --die-with-parent \
    -- bash -c "$WRAPPED_CMD"
BWRAP_EXIT=$?

# Cleanup runs via EXIT trap (kills proxy, removes temp files)
exit $BWRAP_EXIT
