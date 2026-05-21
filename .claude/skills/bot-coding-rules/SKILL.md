---
description: "Bot-specific coding discipline: f-string rules, async patterns, typing, git flow, container paths, dependency pinning."
---

## Coding Rules

0. **Understand before coding (ALL tasks)**: Before writing a single line of code, read the relevant
   component knowledge file(s) in `docs/components/`, CLAUDE.md, and the source of every module you
   will touch. For bot self-upgrade: read the component file + anti-pattern list first. For any system:
   verify architecture from source before proposing a solution. Confirm ambiguities with the user —
   never implement on hypotheses. See coding-principles skill Stage SM for full protocol.
1. **Read before writing**: Always `Grep`/`Read` to understand existing code before editing
2. **Minimal changes**: Don't refactor surrounding code. Don't add comments or docstrings to code you didn't change
3. **No f-string newlines**: NEVER use literal newlines in f-strings — use `\n` escape sequences. SyntaxError crashes the bot with no recovery
4. **Test mentally**: Walk through your edit for syntax errors before writing. A SyntaxError = infinite restart loop
5. **Pin dependencies**: All versions are pinned in requirements.txt. NEVER update without explicit approval
6. **Admin-only operations**: deploy_bot, deploy_review, deploy_status, Bash, Write, Edit are admin-gated
7. **Python 3.12 typing**: Use `dict`, `list`, `str | None` — NOT `Dict`, `List`, `Optional`. Use `Callable` from typing (not `callable`)
8. **Async patterns**: Use `asyncio.create_task()` for fire-and-forget. Use `asyncio.Lock` per session key
9. **Error handling**: Report errors to chat (never fail silently). Use `log.error(..., exc_info=True)`
9a. **TG chat identity = chat_id + thread_id (EVERYWHERE)**: Forum topics are separate chats.
    The composite key `chat_id:thread_id` MUST be used for ALL operations: session keys,
    sending, harness resolution, timeout, RBAC, message storage, chat registry auth.
    Use `build_chat_key()` from `bot/chat_key.py`. `try: int(chat_id) except` not `isdigit()`.
10. **File paths in container**: Edit in `/host-repo/`, NOT `/app/` (which is read-only image copy)
11. **Git discipline**: Each change = separate commit + push. Never accumulate uncommitted changes
12. **Standard git flow only**: ALWAYS use `git add` + `git commit` + `git push`. NEVER manipulate `.git/` internals (object dirs, index, refs). If git fails, report the error — don't attempt workarounds
13. **Docs update is part of the change (ALL tiers — no exceptions)**: Every code change MUST update
    all affected documentation IN THE SAME COMMIT. Stale docs = bugs. Checklist:
    - `CLAUDE.md` — architecture, patterns, anti-patterns, deploy process
    - `README.md` — keep accurate and up to date at all times
    - `docs/ARCHITECTURE.md` — module map and data flow
    - `docs/components/<name>.md` — the component knowledge file for any subsystem you touched;
      update features, design principles, pitfalls, or anti-patterns that changed
    - `data/prompts/*.txt` — especially `26_self_awareness.txt` for new bot features
    - `.claude/skills/*/SKILL.md` — if skill behavior or pipeline changed
    - `.env` — document any new/removed config vars
    - Grep for removed/renamed symbols across `*.md` + `*.txt` before committing.
    - Expire stale facts via `manage_fact` when stored info changes.
    Create a new component knowledge file in `docs/components/` when adding a substantial new subsystem.
