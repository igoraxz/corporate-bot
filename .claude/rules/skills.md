---
paths:
  - ".claude/skills/**/*.md"
---

# Skill File Rules

- Skills use YAML frontmatter with `description:` field (required for SDK discovery)
- Skills are loaded on-demand via the `Skill` tool — not always in context
- Keep skills focused and self-contained — they should work without assuming other context is loaded
- Skills awareness prompt (`data/prompts/19_skills_awareness.txt`) must list every skill with trigger conditions
- Admin-only skills (self-upgrade, coding-principles) are protected by hook-level tool gating
- Skill files are located at `/host-repo/.claude/skills/` and symlinked to `/app/.claude/skills/` in entrypoint.sh
- Maximum recommended skill size: ~20KB (larger skills risk system prompt overflow if auto-loaded)
