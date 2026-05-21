#!/bin/bash
# Entrypoint: setup config then run the bot as botuser (no root, no gosu)
set -e

# Ensure Claude CLI config exists (prevents warning about missing .claude.json)
[ -f "$HOME/.claude.json" ] || echo '{}' > "$HOME/.claude.json" 2>/dev/null || true

# Ensure all data subdirectories exist with correct ownership (defense-in-depth).
# Dockerfile pre-creates these, but bind mounts can create intermediate dirs as root.
# Running as botuser here, so mkdir works if parent is botuser-owned (normal case).
# If a parent was root-created (bind mount edge case), this fails gracefully.
for d in /app/data/config /app/data/credentials /app/data/credentials/oauth_profiles \
         /app/data/credentials/google-workspace-creds /app/data/media_cache \
         /app/data/tmp /app/data/prompts /app/data/browser-profile /app/data/reports /app/logs; do
    mkdir -p "$d" 2>/dev/null || true
done

# Project-level .claude config (SDK discovers skills and settings from CWD/.claude/)
mkdir -p /app/.claude "$HOME/.claude"
for subdir in skills agents; do
    if [ -d /host-repo/.claude/$subdir ]; then
        ln -sfn /host-repo/.claude/$subdir "$HOME/.claude/$subdir" 2>/dev/null || true
        ln -sfn /host-repo/.claude/$subdir /app/.claude/$subdir 2>/dev/null || true
    fi
done
[ -f /host-repo/.claude/settings.json ] && cp /host-repo/.claude/settings.json /app/.claude/settings.json 2>/dev/null || true

# Configure git for self-upgrade commits (inside container)
git config --global user.email "bot@${COMPOSE_PROJECT_NAME:-corporate-bot}.local"
git config --global user.name "${COMPOSE_PROJECT_NAME:-Corporate Bot}"
git config --global --add safe.directory /host-repo

# SSH setup for git push (deploy key mounted read-only from host)
# Docker creates a directory placeholder if the source file doesn't exist at
# container creation time — detect this and warn instead of silently skipping.
if [ -d "$HOME/.ssh/id_ed25519" ]; then
    echo "WARNING: $HOME/.ssh/id_ed25519 is a directory (Docker placeholder)." >&2
    echo "  The deploy key file was missing when the container was created." >&2
    echo "  Fix: create the key, then recreate the container (docker compose down + up)." >&2
    echo "  Self-upgrade git push will not work until this is fixed." >&2
elif [ -f "$HOME/.ssh/id_ed25519" ]; then
    # Write config/known_hosts to a writable dir (key mount may be :ro)
    SSH_DIR="$HOME/.ssh-config"
    mkdir -p "$SSH_DIR"
    # GitHub SSH host keys (avoid ssh-keyscan timing issues)
    cat > "$SSH_DIR/known_hosts" << 'KEYS'
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=
KEYS
    # SSH config: map host aliases to github.com, use writable known_hosts
    # Restore upstream deploy key from persistent volume if available
    UPSTREAM_KEY_SRC="/app/data/credentials/ssh_upstream/id_ed25519_upstream"
    if [ -f "$UPSTREAM_KEY_SRC" ]; then
        cp "$UPSTREAM_KEY_SRC" "$SSH_DIR/id_ed25519_upstream"
        chmod 600 "$SSH_DIR/id_ed25519_upstream"
    fi
    # GIT_SSH_HOST: custom SSH alias for the git remote (e.g. github-corp).
    # If set and different from github-private, an extra Host entry is created.
    GIT_SSH_HOST="${GIT_SSH_HOST:-}"
    cat > "$SSH_DIR/config" << SSHCONF
Host github-private
    HostName github.com
    IdentityFile $HOME/.ssh/id_ed25519
    UserKnownHostsFile $SSH_DIR/known_hosts
    StrictHostKeyChecking accept-new
SSHCONF
    # Add custom alias if configured (e.g. github-corp for your instance)
    if [ -n "$GIT_SSH_HOST" ] && [ "$GIT_SSH_HOST" != "github-private" ]; then
        cat >> "$SSH_DIR/config" << SSHCONF

Host $GIT_SSH_HOST
    HostName github.com
    IdentityFile $HOME/.ssh/id_ed25519
    UserKnownHostsFile $SSH_DIR/known_hosts
    StrictHostKeyChecking accept-new
SSHCONF
    fi
    cat >> "$SSH_DIR/config" << SSHCONF

Host github-upstream
    HostName github.com
    IdentityFile $SSH_DIR/id_ed25519_upstream
    UserKnownHostsFile $SSH_DIR/known_hosts
    StrictHostKeyChecking accept-new

# Desktop Relay — Mac via Tailscale (host must be on same Tailscale network)
Host mac-desktop
    HostName ${DESKTOP_HOST:-your-mac}
    User ${DESKTOP_USER:-user}
    IdentityFile $HOME/.ssh/id_ed25519
    UserKnownHostsFile $SSH_DIR/known_hosts
    StrictHostKeyChecking accept-new
    ControlMaster auto
    ControlPath /tmp/ssh-relay-%r@%h:%p
    ControlPersist 600
    ServerAliveInterval 30
    ServerAliveCountMax 3
    ConnectTimeout 10
SSHCONF
    chmod 600 "$SSH_DIR/config"
    # Point git SSH to our config
    git config --global core.sshCommand "ssh -F $SSH_DIR/config"
fi

exec python main.py
