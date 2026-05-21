# Contributing to Corporate Bot

Thank you for your interest in contributing! This project is an AI-powered corporate assistant built on the Claude Agent SDK.

## Repository Model

Corporate Bot uses an **upstream/downstream fork model**:

```
corporate-bot (upstream)          — core framework, shared code, docs
    ↑ PRs
yourorg-corporate-bot (downstream) — your organization's instance, custom config
```

**How it works:**

1. **Fork** the core `corporate-bot` repo into your own `yourorg-corporate-bot`
2. **Develop** in your downstream fork — add organization-specific config, custom scheduled tasks, prompt tweaks
3. **Contribute back** — generic improvements (bug fixes, new features, docs) go upstream via Pull Request
4. **Stay synced** — periodically pull upstream changes into your fork

**What stays downstream (never PR upstream):**
- `data/org_config.json` — your organization's identity
- `.env` — your API keys and secrets
- Organization-specific scheduled tasks and prompt customizations
- Harness overrides for your chat topology

**What goes upstream (PR welcome):**
- Bug fixes in core bot code
- New integrations (MCP tools, messaging platforms)
- Security hardening
- Documentation improvements
- Test coverage

### Development Setup

#### Prerequisites
- Docker and Docker Compose v2
- Python 3.12+
- Node.js 22+ (for MCP servers)
- Git

#### Quick Start

```bash
# Fork and clone your downstream repo
git clone git@github.com:yourorg/yourorg-corporate-bot.git
cd yourorg-corporate-bot

# Add upstream remote for syncing
git remote add upstream git@github.com:igoraxz/corporate-bot.git

# Run the setup wizard to create .env and org_config.json
./scripts/setup.sh

# Start the bot
docker compose up -d bot-core

# Check health
curl http://localhost:8000/health
```

#### Syncing with Upstream

```bash
git fetch upstream
git merge upstream/main
# Resolve any conflicts in your organization-specific files
git push origin main
```

### First-Time Telegram Setup

```bash
# Run the interactive session setup (requires phone number + 2FA code)
docker exec -it corporate-bot python scripts/setup_tg_userbot.py
```

## Code Standards

### Python
- Python 3.12 typing: use `dict`, `list`, `str | None` (NOT `Dict`, `List`, `Optional`)
- Use `asyncio.create_task()` for fire-and-forget operations
- Use `log.error(..., exc_info=True)` for error reporting
- No literal newlines in f-strings (use `\n`)
- Pin all dependencies in `requirements.txt`

### Commits
- One logical change per commit
- Commit message format: `<type>: <description>`
  - Types: `feat`, `fix`, `chore`, `docs`, `security`, `refactor`, `test`
- Always push after committing (never accumulate unpushed changes)

### Testing
```bash
# Syntax check
python3 -c "import ast; ast.parse(open('<file>').read())"

# Unit tests
python -m pytest tests/ -v

# Smoke tests (verify imports + basic handler calls)
python -m pytest tests/test_smoke_*.py -v
```

## Architecture

See [CLAUDE.md](CLAUDE.md) for the full architecture reference, including:
- Module map and file structure
- Security model (flat RBAC — DM=user subject, group=chat subject)
- MCP tool system
- Deploy process and gates

## CI Pipeline

On every push and PR to `main`, GitHub Actions runs:
1. **Ruff F821** -- undefined name detection (blocking)
2. **Syntax check** -- `ast.parse()` on all .py files (blocking)
3. **Unit tests** -- knowledge domain and domain config tests
4. **Ruff style** -- E/F/W warnings (non-blocking)

## Deploy Process

1. Make changes in `/host-repo/`
2. Run `deploy_review` (syntax + imports + smoke tests + pyflakes + staging boot)
3. Run `staging_qa_run` (regression tests against staging)
4. Call `deploy_bot` (triggers host watcher rebuild)

The host watcher automatically rolls back on failed health checks.

## Security

- Never commit secrets (`.env`, tokens, credentials)
- All bash commands in sandbox are regex-checked + bwrap-isolated
- File path access is blocked for sensitive patterns
- External chats have restricted tool access (Layer 1c)

## License

This project is licensed under the terms specified in [LICENSE](LICENSE).
