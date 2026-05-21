---
paths:
  - "data/prompts/**/*.txt"
---

# System Prompt Rules

- Use double braces `{{` `}}` for literal braces — Python `str.format()` interprets single braces as placeholders
- Available placeholders: `{facts_summary}`, `{pending_actions_context}` — defined in `prompts.py`
- Total system prompt size limit: ~128KB (Linux MAX_ARG_STRLEN). Base prompt is ~57K chars
- Skills are loaded on-demand via the Skill tool, or pre-loaded via harness `claude_md_modules`
- Keep prompts concise — every byte counts against the 128KB limit
- No emojis unless the section specifically requires them (e.g. tier indicators)
