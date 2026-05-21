---
description: "Microsoft 365 integration: multi-account routing, token management, Softeria MCP server, device code auth flow."
---

MULTI-ACCOUNT CALENDAR & EMAIL INTEGRATION:
(covers Google Workspace multi-account + Microsoft 365/Outlook)

The organization has multiple calendar/email systems. You MUST check ALL relevant ones for
scheduling, briefings, and event creation.

━━━ ACCOUNTS & TOOLS ━━━

GOOGLE WORKSPACE ACCOUNTS: {google_accounts}
  Tools: search_gmail_messages, get_gmail_message_content, send_gmail_message,
         get_events, manage_event, list_calendars
  Capabilities: full read/write email + calendar
  Parameter: user_google_email="<account email>"

MICROSOFT 365 ACCOUNT: {ms365_email}
  Tools: list-mail-messages, get-mail-message, list-calendar-events,
         get-calendar-view, create-calendar-event, update-calendar-event
  Capabilities: read-only email, read/write calendar
  No send-mail permission — corporate email sending is not available

━━━ TOOL ROUTING — WHICH ACCOUNT FOR WHAT ━━━

READING EMAIL:
- Identify which team member is asking from session context
- Route to their Google account (user_google_email parameter)
- "Check my work email" → MS365 tools (Outlook)
- Daily briefing / email scan → ALL accounts: all Google + Outlook

SENDING EMAIL:
- Default sender: admin's primary Google account — unless context requires otherwise
- External correspondence: use whichever team lead is the primary contact
- Corporate email: CANNOT send (read-only)

READING CALENDAR:
- "What's on today/tomorrow?" → check ALL calendars, merge chronologically
- "What's on my work calendar?" → Outlook only
- Daily briefing → ALL calendars, always

━━━ CREATING TEAM EVENTS — MANDATORY MULTI-CALENDAR ━━━

RULE: Team events MUST be created in ALL team members' Google calendars.

Team events include:
- Team meetings, all-hands, offsites, team lunches
- Shared deadlines (project milestones, submission dates)
- Cross-functional meetings and planning sessions
- Any event the whole team should know about

Workflow for creating a team event:
1. CHECK OVERLAPS against ALL calendars (Google + MS365)
   If ANY calendar has a conflict, flag it before creating.

2. CREATE in each team member's Google calendar:
   - manage_event(action="create", ..., user_google_email="<each account>")

3. Optionally CREATE a busy block in Outlook if the event falls during work hours

ERROR HANDLING: If one calendar creation succeeds but another fails, notify the user
which one failed and ask whether to retry or proceed. Never silently leave calendars
out of sync.

Personal/individual events only need to go in the respective person's calendar.

When in doubt about whether an event is "team-wide" → create in all calendars.
It's better to have a redundant entry than to have one team member unaware.

━━━ OVERLAP CHECKING — ALWAYS ALL CALENDARS ━━━

Before creating ANY event, check all relevant calendars:
- Team event → all calendars
- Individual event → that person's Google + Outlook if applicable

Use query_freebusy for quick availability checks when possible.

━━━ DAILY BRIEFING — COMPREHENSIVE VIEW ━━━

Morning briefings MUST include data from ALL sources:
1. All Google Calendars → personal/team events
2. Outlook Calendar → work meetings, corporate events
3. All Gmail accounts → email highlights
4. Outlook email → corporate email highlights (if relevant)

Present as a unified chronological schedule with source labels:
  "09:00 — Team standup (Outlook)"
  "10:30 — Swimming (Google — both calendars)"
  "15:00 — Client call (team member's calendar)"

━━━ MS365-SPECIFIC NOTES ━━━

MS365 TOOLS AVAILABLE (via Softeria MCP server):
Calendar: list-calendars, list-calendar-events, get-calendar-event, get-calendar-view,
  create-calendar-event, update-calendar-event, delete-calendar-event,
  accept-calendar-event, decline-calendar-event, tentatively-accept-calendar-event
Email (READ-ONLY): list-mail-messages, list-mail-folders, list-mail-folder-messages,
  get-mail-message, list-mail-attachments, get-mail-attachment, list-mail-child-folders
Auth: login, verify-login, logout, list-accounts, select-account, remove-account

CORPORATE EMAIL:
- Read-only (Mail.Read) — can list and read emails, download attachments
- Calendar is read-write (Calendars.ReadWrite) — can create/update/delete events
- NO send-mail permission — corporate email sending not available

AUTH & TROUBLESHOOTING:
- Token cached and auto-refreshed by Softeria MSAL. Re-auth only needed if refresh
  token expires (90 days inactive).
- If AUTH_ERROR → invoke `mcp-authentication` skill for full procedure.
- QUICK FIX (most common issue — stale MCP subprocess after deploy/restart):
  1. Copy cache: cp /app/data/ms365-mcp/* /app/data/credentials/ms365-mcp/
     (known path mismatch: login script writes to ms365-mcp/, Softeria reads from
     credentials/ms365-mcp/)
  2. /reset the session — Softeria caches the token at process startup, so the
     running subprocess won't see new files without a restart.
  3. Verify with any MS365 tool (get-calendar-view, list-mail-messages).
- If quick fix fails → full re-auth needed from HOST: python3 scripts/m365_login.py