# Corporate Bot

This file is intentionally empty. Each harness composes its own CLAUDE.md from
auto-loaded skills defined in `bot/harness.py` (`claude_md_modules` field).

Skills are in `.claude/skills/*/SKILL.md`. The harness system loads them via
`load_module_content()` and writes composed CLAUDE.md to per-harness project dirs.

For developer documentation, see:
- `docs/ARCHITECTURE.md` — system architecture
- `.claude/skills/bot-architecture/` — architecture skill (auto-loaded for admin/coding)
- `.claude/skills/bot-patterns/` — design patterns
- `.claude/skills/bot-deploy/` — deploy process
- `.claude/skills/bot-pitfalls/` — common pitfalls
