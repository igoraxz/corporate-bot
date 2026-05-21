# Mac Claude Code — Shared Skills + Agents

This directory is the **source of truth** for the coding skills and named
agents that apply to ALL Claude Code sessions on the user's Mac (Desktop
Claude.app + TG relay). Mirrors the server's `coding-principles` skill with
bot-specific bits stripped.

## Contents

```
mac-skills/
  skills/
    coding-principles/
      SKILL.md           — full architect checklist, anti-patterns, review pipeline
    pptx-creation/
      SKILL.md           — professional PowerPoint creation via pptxgenjs
    docx-creation/
      SKILL.md           — professional Word document creation via docx npm
    diagram-creation/
      SKILL.md           — diagram creation via Mermaid CLI (mmdc)
    html-report/
      SKILL.md           — HTML reports with Chart.js + Tailwind CSS
  agents/
    architect.md         — plan review
    code-researcher.md   — deep technical research
    security-reviewer.md — vulnerability audit
    quality-reviewer.md  — code quality + testing
  CLAUDE.md.snippet      — always-on reminder, appended to ~/.claude/CLAUDE.md
  README.md              — this file
```

## Install target (Mac)

```
~/.claude/skills/coding-principles/SKILL.md
~/.claude/skills/pptx-creation/SKILL.md
~/.claude/skills/docx-creation/SKILL.md
~/.claude/skills/diagram-creation/SKILL.md
~/.claude/skills/html-report/SKILL.md
~/.claude/agents/architect.md
~/.claude/agents/code-researcher.md
~/.claude/agents/security-reviewer.md
~/.claude/agents/quality-reviewer.md
~/.claude/CLAUDE.md        (append CLAUDE.md.snippet, idempotent via markers)
```

Claude Code CLI explicitly registers `~/.claude/skills/*/SKILL.md` and
`~/.claude/agents/*.md` on startup — they apply to every project on the Mac.
`~/.claude/CLAUDE.md` loads always-on context for every session.

## Install via SSH (Option A — from TG, planned)

```
desktop_relay(action="install_skills")
```

Triggers a tar → base64 → SSH stdin → guard.sh case `relay-install-skills`
that extracts to `~/.claude/skills/` + `~/.claude/agents/` and idempotently
appends the CLAUDE.md snippet using BEGIN/END markers.

## Manual install (first-time bootstrap / Option B fallback)

```bash
# From the Mac:
REPO=~/dev/corporate-bot  # or wherever you have the repo cloned
rsync -av "$REPO/scripts/desktop-relay/mac-skills/skills/" ~/.claude/skills/
rsync -av "$REPO/scripts/desktop-relay/mac-skills/agents/" ~/.claude/agents/

# Append CLAUDE.md snippet (idempotent: checks markers first)
if ! grep -q "ALFEROV MAC CODING PRINCIPLES" ~/.claude/CLAUDE.md 2>/dev/null; then
  cat "$REPO/scripts/desktop-relay/mac-skills/CLAUDE.md.snippet" >> ~/.claude/CLAUDE.md
fi

# Verify:
claude --version   # sanity check Claude CLI still works
ls ~/.claude/skills/coding-principles/SKILL.md
ls ~/.claude/agents/
```

## Update cadence

When server-side `coding-principles` or agents change:
1. Edit in `/host-repo/.claude/{skills,agents}/`
2. Re-run the build script: `python3 /app/data/tmp/build_mac_skills.py`
   (or equivalent future make target)
3. Commit the regenerated `scripts/desktop-relay/mac-skills/` dir
4. Trigger install: `desktop_relay(action="install_skills")` from TG

## Skill selection rationale

Coding-specific skills only (consolidated 2026-04-17):
- `simplify` — built-in Claude Code skill, not in our repo
- `git-discipline` — folded into coding-principles
- `self-upgrade` — bot-specific, not portable to Mac

Document creation skills added 2026-05-05:
- `pptx-creation` — professional PowerPoint via pptxgenjs (requires: npm install -g pptxgenjs)
- `docx-creation` — professional Word docs via docx npm (requires: npm install -g docx)
- `diagram-creation` — Mermaid diagrams via mmdc (requires: npm install -g @mermaid-js/mermaid-cli)
- `html-report` — HTML reports with Chart.js + Tailwind CSS (no extra deps)

Net: 5 skills + four review agents. Mac-wide.
