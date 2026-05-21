# Your First Self-Upgrade — A Concrete Walkthrough

This guide walks you through fixing a real bug using the bot's self-upgrade capability.
By the end, you'll understand the full cycle: describe → research → plan → implement → review → deploy.

---

## Prerequisites

- You're an admin with access to the bot's DM (admin harness)
- The bot is running and healthy (`/status` shows green)
- You've identified something to fix or improve

---

## Example: "The /status command doesn't show the timezone"

### Step 1: Describe the Problem

In your admin DM, simply describe what you want:

```
The /status command should show the bot's timezone (e.g. "Timezone: Europe/London")
alongside the other status info. It currently doesn't show it.
```

### Step 2: The Bot Researches

The bot will:
1. Search the codebase for the `/status` command handler
2. Read the relevant code
3. Understand the current output format
4. Identify where to add the timezone display

You'll see it thinking and reading files. This takes 30-60 seconds.

### Step 3: The Bot Proposes a Plan

The bot presents its plan:

> **Plan:**
> - File: `bot/commands.py`, function `_lightweight_status()`
> - Add timezone line after the uptime display
> - Use `config.ORG_TIMEZONE` which is already imported
> - Classification: SMALL (1 file, single function)
>
> **Approve to implement?**

### Step 4: You Approve

Simply say:
```
yes
```
or
```
approve
```

### Step 5: The Bot Implements

The bot:
1. Edits the file in `/host-repo/` (the git repository)
2. Runs syntax checks
3. Runs a quality review (inline for SMALL changes)
4. Shows you the exact change made

### Step 6: You Deploy

The bot says the change is ready. You type:

```
/deploy
```

The bot then:
1. Commits the change to git
2. Pushes to GitHub
3. Runs `deploy_review` (syntax + imports + smoke tests)
4. Runs staging QA (regression tests)
5. Triggers Docker rebuild
6. Health check confirms the new version is live (~60 seconds)

### Step 7: Verify

Send `/status` again — the timezone should now appear.

---

## What If Something Goes Wrong?

### Deploy fails health check
The host watcher automatically rolls back to the previous commit.
Check what happened:

```
Check the deploy status and show me the failed startup logs.
```

### The change breaks something
Tell the bot:

```
The last deploy broke X. Revert and fix it.
```

The bot will investigate, create a fix, and re-deploy.

### You want to undo a change
```
Revert the last commit and deploy.
```

---

## Change Complexity Tiers

The bot automatically classifies changes:

| Tier | Scope | Review Level | Example |
|------|-------|-------------|---------|
| SMALL | 1-3 files, single module | Inline review | Fix a typo, add a status field |
| MEDIUM | 2-5 files, cross-module | Quality agent review | Add a new command, modify a flow |
| SIGNIFICANT | 5+ files or security-sensitive | Full agent suite (security + quality + architecture) | Auth changes, new MCP tools, DB schema |

The bot decides the tier before implementing. If you disagree, say:
```
This should be SIGNIFICANT — it touches auth code.
```

---

## Common Self-Upgrade Tasks

### Add a new scheduled task
```
Create a scheduled task that checks our team calendar every morning
at 8am and sends a summary of today's meetings.
```

### Fix formatting
```
The /accounts command truncates long email addresses. Fix the display
to show the full email.
```

### Add a new feature
```
I want a /notes command that lets me save and retrieve quick notes.
Store them as facts with category "notes".
```

### Improve a prompt
```
The bot's morning briefing is too verbose. Make it more concise —
max 5 bullet points, focus on actionable items only.
```

### Debug an issue
```
The bot sometimes doesn't respond to messages in the Engineering chat.
Investigate and fix.
```

For each of these, the bot follows the same cycle:
**Research → Plan → Approve → Implement → Review → Deploy**

---

## Tips

1. **Be specific** — "Fix the bug in /status" is better than "something is broken"
2. **Mention files if you know them** — "The issue is in bot/commands.py" speeds up research
3. **Start small** — Your first self-upgrade should be simple (typo fix, display tweak)
4. **Trust the reviews** — The bot runs security and quality reviews on every change
5. **You control deploys** — Nothing goes live without your `/deploy` command
6. **Rollback is automatic** — If a deploy breaks the health check, it rolls back

---

*This guide is part of the Admin Handbook. See also: [ADMIN_HANDBOOK.md](ADMIN_HANDBOOK.md)*
