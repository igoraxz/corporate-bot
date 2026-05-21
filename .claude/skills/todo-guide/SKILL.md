---
description: "TODO/action item tracking: TodoWrite tool usage, capture rules, follow-up patterns."
---

TODO / ACTION ITEM MANAGEMENT:

The team uses the bot as their task tracker. When someone mentions a task, action item,
or TODO — capture it and track it until explicitly marked as done.

CAPTURING TODOs:
When a user mentions something that needs to be done (explicitly or implicitly):
- "I need to book..." / "Don't forget to..." / "We should..." / "TODO: ..." / "Remind me to..."
- Or when YOU identify an action item during research, planning, or conversation
→ Store it as a fact with category="todo" and a descriptive key:

FACTS_UPDATE: {{"schedule_vendor_meeting": {{"value": "TODO: Schedule meeting with Acme Corp to discuss Q2 contract renewal", "subject": "procurement", "category": "todo"}}}}
FACTS_UPDATE: {{"submit_expense_report": {{"value": "TODO: Submit Q1 expense report. Deadline: by end of month", "subject": "john", "category": "todo"}}}}

KEY FORMAT: snake_case descriptive key. Prefix with entity if person-specific.
VALUE FORMAT: Always start with "TODO: " prefix for easy identification.
Include deadline if known: "Deadline: <date/description>"
Include context: what, why, any reference numbers or links.

DEADLINES:
- If a deadline is mentioned or implied, include it in the TODO value
- For time-sensitive items, also create a calendar event or reminder
- In daily briefings, highlight TODOs with approaching deadlines (within 3 days)
- Overdue TODOs should be flagged prominently

DAILY BRIEFING INTEGRATION:
During every morning briefing / proactive check:
1. Search for all active TODO facts: search_facts(action="list", category="todo")
2. Present open TODOs grouped by urgency:
   - OVERDUE (deadline passed)
   - DUE SOON (deadline within 3 days)
   - OPEN (no deadline or deadline further out)
3. If a TODO has been open for >7 days without a deadline, gently remind about it

COMPLETING TODOs:
- NEVER expire/complete a TODO without explicit user confirmation
- When user says "done", "completed", "finished", "sorted" about a specific task:
  → Expire the TODO fact via manage_fact or FACTS_UPDATE with updated value
- When completing, optionally store the outcome as a regular fact if useful:
  FACTS_UPDATE: {{"ba_complaint_filed": {{"value": "Filed BA complaint on 5 Apr, ref XYZ123", "subject": "john", "category": "travel"}}}}

DROPPING vs COMPLETING:
- "Drop this task" / "cancel" / "never mind" = expire the TODO (user decided not to do it)
- "Done" / "completed" = expire TODO + optionally record outcome
- Both require explicit user statement — bot never auto-completes

PROACTIVE TODO CREATION:
When you discover actionable items during any task (email scanning, research, planning):
- Store them as TODOs automatically
- Mention them in your reply: "I've added a TODO to [action]"
- Example: scanning email finds a compliance form deadline → create TODO with deadline

RULES:
1. TODOs are NEVER expired without explicit user confirmation (see fact_cleanup_rule_action_items)
2. Every TODO must have enough context to be actionable without looking up history
3. Prefer fewer, well-described TODOs over many vague ones
4. When a TODO is blocked by something, note the blocker in the value
5. If user asks "what's on my list?" or "any open tasks?" → show all active TODOs
