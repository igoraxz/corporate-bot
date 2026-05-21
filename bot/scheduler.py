# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Flexible scheduled tasks system.

Tasks are stored in data/scheduled_tasks.json and can be managed
conversationally via MCP tools. The scheduler loop in main.py calls
check_custom_tasks() every 30s to fire due tasks.

Each task has:
  - id: unique string (auto-generated or fixed for defaults)
  - name: human-readable name
  - hour: 0-23 (for fixed-time tasks)
  - minute: 0-59
  - days: list of weekday names ["mon","tue",...] or ["daily"]
  - prompt: the system prompt to execute when task fires
  - platform: "telegram" | "teams" (where to send output)
  - enabled: bool
  - last_run: ISO date string (YYYY-MM-DD) or ISO datetime for interval tasks
  - created_at: ISO datetime
  - interval_hours: (optional) float — if set, task fires every N hours instead of at a fixed time.
    The hour/minute fields are ignored for interval tasks; only last_run matters.
"""

import json
import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from config import DATA_DIR, ORG_TIMEZONE
from bot.config_store import JsonConfigStore

log = logging.getLogger(__name__)

TASKS_FILE = DATA_DIR / "config" / "scheduled_tasks.json"
TZ = ZoneInfo(ORG_TIMEZONE)

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_MAP = {
    "monday": "mon", "tuesday": "tue", "wednesday": "wed",
    "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun",
    "mon": "mon", "tue": "tue", "wed": "wed", "thu": "thu",
    "fri": "fri", "sat": "sat", "sun": "sun",
    "daily": "daily", "weekdays": "weekdays", "weekends": "weekends",
}


def _migrate_tasks_file() -> None:
    """One-time migration: convert scheduled_tasks.json from array to dict format."""
    if not TASKS_FILE.exists():
        return
    try:
        raw = json.loads(TASKS_FILE.read_text())
        if isinstance(raw, list):
            data = {}
            for task in raw:
                tid = task.get("id", str(uuid.uuid4())[:8])
                task["id"] = tid
                data[tid] = task
            # Atomic write: tmp + rename to prevent corruption on crash
            tmp = TASKS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            tmp.replace(TASKS_FILE)
            log.info("Migrated scheduled_tasks.json from array to dict (%d tasks)", len(data))
    except Exception as e:
        log.warning("Failed to pre-migrate scheduled tasks: %s", e)


_migrate_tasks_file()
_task_store = JsonConfigStore("scheduled_tasks", TASKS_FILE)


def _coerce_task(task: dict) -> dict:
    """Coerce task field types for safety (hour/minute → int, enabled → bool).

    Returns a new dict to avoid mutating JCS internal data.
    """
    task = dict(task)  # defensive copy
    if "hour" in task:
        try:
            task["hour"] = int(task["hour"])
        except (ValueError, TypeError):
            log.warning("Task %s: invalid hour value: %s", task.get("id", "?"), task.get("hour"))
    if "minute" in task:
        try:
            task["minute"] = int(task["minute"])
        except (ValueError, TypeError):
            log.warning("Task %s: invalid minute value: %s", task.get("id", "?"), task.get("minute"))
    if "enabled" in task and not isinstance(task["enabled"], bool):
        task["enabled"] = str(task["enabled"]).lower() in ("true", "1", "yes")
    return task


def _normalize_days(days_input: list[str]) -> list[str]:
    """Normalize day names to short form. Expand 'daily', 'weekdays', 'weekends'."""
    result = []
    for d in days_input:
        d_lower = d.lower().strip()
        mapped = DAY_MAP.get(d_lower)
        if mapped == "daily":
            return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        elif mapped == "weekdays":
            result.extend(["mon", "tue", "wed", "thu", "fri"])
        elif mapped == "weekends":
            result.extend(["sat", "sun"])
        elif mapped:
            result.append(mapped)
        else:
            log.warning(f"Unknown day name: {d}")
    return list(set(result)) if result else ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def list_tasks() -> list[dict]:
    """Return all scheduled tasks."""
    return [_coerce_task(t) for t in _task_store.values()]


def get_task(task_id: str) -> dict | None:
    """Get a single task by ID."""
    t = _task_store.get(task_id)
    return _coerce_task(t) if t else None


def add_task(
    name: str,
    hour: int,
    minute: int,
    prompt: str,
    days: list[str] | None = None,
    platform: str = "",
    enabled: bool = True,
    chat_id: str = "",
    interval_hours: int | None = None,
    task_id: str = "",
) -> dict:
    """Add a new scheduled task. Returns the created task.

    platform: messaging platform (telegram|teams). If empty, inherits from ROOT_ADMIN.
    chat_id: originating chat — task inherits this chat's AccessContext at runtime.
    Empty = admin-level access (default for built-in tasks).
    task_id: optional explicit ID (for employee tasks). Auto-generated if empty.
    """
    # CB-132: Inherit platform from ROOT_ADMIN if not specified
    if not platform:
        from config import ROOT_ADMIN
        platform = ROOT_ADMIN[0] or "telegram"
    task = {
        "id": task_id or str(uuid.uuid4())[:8],
        "name": name,
        "hour": hour,
        "minute": minute,
        "days": _normalize_days(days or ["daily"]),
        "prompt": prompt,
        "platform": platform,
        "enabled": enabled,
        "last_run": "",
        "created_at": datetime.now(TZ).isoformat(),
    }
    if chat_id:
        task["chat_id"] = chat_id
    if interval_hours:
        task["interval_hours"] = interval_hours
    _task_store.set(task["id"], task)
    log.info(f"Scheduled task added: {task['name']} ({task['id']})")
    return task


def update_task(task_id: str, **kwargs) -> dict | None:
    """Update fields of an existing task. Returns updated task or None."""
    t = _task_store.get(task_id)
    if t is None:
        return None
    t = dict(t)  # work on a copy
    # M24 fix: explicit allowlist of updatable fields (not "if key in t" which drops new fields)
    _UPDATABLE = {"name", "hour", "minute", "days", "prompt", "platform", "enabled",
                  "chat_id", "interval_hours", "last_run"}
    for key, val in kwargs.items():
        if key == "id":
            continue  # never overwrite task ID
        if key not in _UPDATABLE:
            continue  # reject unknown fields
        if key == "days" and val is not None:
            val = _normalize_days(val)
        t[key] = val
    _task_store.set(task_id, t)
    log.info(f"Scheduled task updated: {t['name']} ({task_id})")
    return t


def delete_task(task_id: str) -> bool:
    """Delete a task by ID. Returns True if found and deleted."""
    if _task_store.delete(task_id):
        log.info(f"Scheduled task deleted: {task_id}")
        return True
    return False


def toggle_task(task_id: str) -> dict | None:
    """Toggle enabled/disabled. Returns updated task."""
    t = _task_store.get(task_id)
    if t is None:
        return None
    t = dict(t)  # work on a copy
    t["enabled"] = not t.get("enabled", True)
    _task_store.set(task_id, t)
    log.info(f"Task {task_id} toggled to {'enabled' if t['enabled'] else 'disabled'}")
    return t


def get_due_tasks() -> list[dict]:
    """Return tasks that are due right now.

    Fixed-time tasks: match hour, minute, day, not yet run today.
    Interval tasks: fire every interval_hours since last_run.
    """
    now = datetime.now(TZ)
    today_str = str(now.date())
    current_day = DAY_NAMES[now.weekday()]  # 0=Monday

    due = []
    for task in (_coerce_task(t) for t in _task_store.values()):
        if not task.get("enabled", True):
            continue

        interval = task.get("interval_hours")
        if interval:
            # Interval-based task
            last_run_str = task.get("last_run", "")
            if last_run_str:
                try:
                    last_run = datetime.fromisoformat(last_run_str)
                    if last_run.tzinfo is None:
                        last_run = last_run.replace(tzinfo=TZ)
                    hours_since = (now - last_run).total_seconds() / 3600
                    if hours_since < interval:
                        continue
                except (ValueError, TypeError):
                    pass  # Invalid last_run — fire it
            due.append(task)
        else:
            # Fixed-time task
            if task.get("last_run", "").startswith(today_str):
                continue
            if task["hour"] != now.hour:
                continue
            if now.minute < task["minute"]:
                continue
            task_days = task.get("days", DAY_NAMES)
            if current_day not in task_days:
                continue
            due.append(task)

    return due


def mark_task_run(task_id: str) -> None:
    """Mark a task as having run. Uses ISO datetime for interval tasks, date for fixed-time."""
    t = _task_store.get(task_id)
    if t is None:
        return
    t = dict(t)  # work on a copy
    now = datetime.now(TZ)
    if t.get("interval_hours"):
        t["last_run"] = now.isoformat()
    else:
        t["last_run"] = str(now.date())
    _task_store.set(task_id, t)


def format_task_list(tasks: list[dict]) -> str:
    """Format tasks into a readable string for display."""
    if not tasks:
        return "No scheduled tasks configured."

    lines = []
    for t in tasks:
        status = "ON" if t.get("enabled", True) else "OFF"
        interval = t.get("interval_hours")

        if interval:
            time_str = f"every {interval}h"
        else:
            days = t.get("days", ["daily"])
            if set(days) == set(DAY_NAMES):
                days_str = "daily"
            elif set(days) == {"mon", "tue", "wed", "thu", "fri"}:
                days_str = "weekdays"
            elif set(days) == {"sat", "sun"}:
                days_str = "weekends"
            else:
                days_str = ", ".join(sorted(days, key=lambda d: DAY_NAMES.index(d)))
            time_str = f"{int(t['hour']):02d}:{int(t['minute']):02d} {days_str}"

        lines.append(
            f"[{status}] {t['name']} - {time_str} "
            f"({t['platform']}) [id: {t['id']}]"
        )
    return "\n".join(lines)


# === DEFAULT TASKS ===

DEFAULT_TASKS = [
    # --- ADMIN TASKS (system scope — run with admin's email/calendar) ---
    {
        "id": "morning",
        "name": "Admin Morning Briefing",
        "hour": 7,
        "minute": 30,
        "days": ["mon", "tue", "wed", "thu", "fri"],
        "prompt": (
            "[ADMIN_MORNING] Daily admin briefing — corporate context.\n\n"
            "MANDATORY — fetch ALL fresh data before composing:\n"
            "1. get_events for today + tomorrow (both Google Calendar AND Outlook)\n"
            "2. search_gmail_messages for last 12h (client responses, team updates, urgent items)\n"
            "3. memory_search for today's date (stored deadlines, project milestones)\n"
            "4. search_facts for category 'operations' and 'projects' (active work)\n\n"
            "Compose a concise briefing:\n"
            "- Today's meetings/calls with times and participants\n"
            "- Action items from overnight emails\n"
            "- Project deadlines approaching (this week)\n"
            "- Any blockers or escalations needing attention\n\n"
            "After fetching, update:\n"
            "FACTS_UPDATE: {\"last_calendar_check\": {\"value\": \"<now_iso>\", \"category\": \"bot\"}}\n"
            "FACTS_UPDATE: {\"last_email_check\": {\"value\": \"<now_iso>\", \"category\": \"bot\"}}\n\n"
            "Send via the messaging tool specified in ROUTING. Keep it under 10 bullet points."
        ),
        "platform": "",  # CB-132: inherit from ROOT_ADMIN at runtime
        "enabled": True,
    },
    {
        "id": "email_check",
        "name": "Admin Email Monitor",
        "hour": 9,
        "minute": 0,
        "interval_hours": 3,
        "days": ["mon", "tue", "wed", "thu", "fri"],
        "prompt": (
            "[ADMIN_EMAIL] Periodic email monitoring — corporate context.\n\n"
            "MANDATORY — fetch fresh data:\n"
            "1. search_gmail_messages for the last 3 hours\n"
            "2. If emails mention dates/events: cross-check with get_events\n"
            "3. Look for URGENT items: client requests, meeting changes, deadline updates,\n"
            "   payment notifications, team escalations, vendor responses\n"
            "4. If anything URGENT: send alert via the messaging tool specified in ROUTING\n"
            "5. If nothing urgent: stay silent (don't send to chat)\n"
            "6. Store relevant info: FACTS_UPDATE for new deadlines/contacts/decisions\n"
            "7. Update: FACTS_UPDATE: {\"last_email_check\": {\"value\": \"<now_iso>\", \"category\": \"bot\"}}"
        ),
        "platform": "",  # CB-132: inherit from ROOT_ADMIN at runtime
        "enabled": True,
    },
    {
        "id": "evening1",
        "name": "Admin Next-Day Prep",
        "hour": 18,
        "minute": 0,
        "days": ["mon", "tue", "wed", "thu", "fri", "sun"],
        "prompt": (
            "[ADMIN_EVENING] Next-day preparation — corporate context.\n\n"
            "MANDATORY — fetch ALL fresh data:\n"
            "1. get_events for tomorrow (both Google Calendar AND Outlook)\n"
            "2. search_gmail_messages for last 6h (any new items about tomorrow)\n"
            "3. memory_search for tomorrow's date (stored deadlines, prep items)\n"
            "4. search_facts for category 'operations' and 'projects'\n\n"
            "Look for:\n"
            "- Tomorrow's meetings (time, participants, any prep needed)\n"
            "- Deliverables or reports due tomorrow\n"
            "- Follow-ups promised today that need action tomorrow\n"
            "- Any travel or logistics for tomorrow\n\n"
            "After fetching, update:\n"
            "FACTS_UPDATE: {\"last_calendar_check\": {\"value\": \"<now_iso>\", \"category\": \"bot\"}}\n\n"
            "Send via the messaging tool specified in ROUTING.\n"
            "Format: brief bullet list — what's tomorrow + what to prepare tonight.\n"
            "Keep concise and actionable. Skip if tomorrow is empty."
        ),
        "platform": "",  # CB-132: inherit from ROOT_ADMIN at runtime
        "enabled": True,
    },
    {
        "id": "daily_selfopt",
        "name": "Daily Self-Optimization Review",
        "hour": 9,
        "minute": 0,
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "prompt": (
            "[SELF_OPTIMIZATION] Daily bot health, performance, and observability review.\n\n"
            "SCOPE: Check ALL monitored parameters, system health, and disk space.\n\n"
            "== DISK SPACE (run FIRST) ==\n"
            "1. Run: df -h / — get total/used/avail/percent.\n"
            "2. Run: du -sh /app/data/ /app/data/media_cache/ /app/wa-data/ "
            "/home/botuser/.cache/ /home/botuser/.claude/ — bot data breakdown.\n"
            "   Note: tmp cleanup is automatic (scheduler removes files >1h old every 10min).\n"
            "3. Thresholds:\n"
            "   - <80%: OK, no action needed.\n"
            "   - 80-89%: WARN in report — mention free space.\n"
            "   - 90%+: ALERT — always send a message, suggest: docker system prune, "
            "clear old logs, review large files.\n\n"
            "== BOT HEALTH ==\n"
            "4. Check recent logs for errors and warnings (use Bash: tail -200 on log files).\n"
            "5. Run rag_stats to check RAG index health (chunk count, embedding failures).\n"
            "6. Review any failed tool calls or recurring issues.\n"
            "7. Analyze RAG search quality: check similarity score distribution if possible.\n"
            "8. Consider parameter tuning: chunk size (7), overlap (2 msgs), threshold (0.25).\n"
            "9. Check scheduler task execution: any tasks failing or timing out?\n"
            "10. Review memory system: DB size, facts count, RAG chunks.\n"
            "11. Check tool response times: any tools consistently slow?\n\n"
            "== NEW FEATURES ==\n"
            "12. Review any NEW parameters or features added since last check.\n"
            "    (Read recent git log: Bash 'cd /host-repo && git log --oneline -10')\n"
            "13. For any new feature/parameter found in recent commits:\n"
            "    - Verify it has logging/observability\n"
            "    - Check if it's working as expected from logs\n"
            "    - Note if monitoring needs to be added\n\n"
            "== REPORTING ==\n"
            "IMPORTANT: If there are NO meaningful findings — no errors, no warnings, "
            "no performance concerns, and disk is <80% — stay COMPLETELY SILENT.\n\n"
            "ALWAYS send a message if disk >= 90%. Send for 80-89% only if combined with other issues.\n\n"
            "Only if you find actionable issues or concrete tuning recommendations "
            "with supporting data:\n"
            "- Send findings + recommendations to the admin via the messaging tool specified in ROUTING.\n"
            "- Request admin approval before making any changes.\n"
            "- Include specific metrics and evidence for each recommendation."
        ),
        "platform": "",  # CB-132: inherit from ROOT_ADMIN at runtime
        "enabled": True,
    },
]


def init_default_tasks() -> None:
    """Initialize default tasks if the tasks file is empty or missing.

    Only adds defaults whose IDs don't already exist (preserves user edits).
    """
    added = 0
    for default in DEFAULT_TASKS:
        if default["id"] not in _task_store:
            task = {
                **default,
                "last_run": "",
                "created_at": datetime.now(TZ).isoformat(),
            }
            _task_store.set(task["id"], task)
            added += 1
            log.info(f"Default task added: {task['name']} ({task['id']})")

    # Migration: update stale self-opt prompt that contains blocked Bash find command
    selfopt = _task_store.get("daily_selfopt")
    if selfopt and "find /app/data/tmp/" in selfopt.get("prompt", ""):
        default_selfopt = next((d for d in DEFAULT_TASKS if d["id"] == "daily_selfopt"), None)
        if default_selfopt:
            selfopt = dict(selfopt)
            selfopt["prompt"] = default_selfopt["prompt"]
            _task_store.set("daily_selfopt", selfopt)
            log.info("Migrated daily_selfopt prompt: removed blocked find command")

    # Migration: sync admin task schedule (days/times) to canonical defaults.
    # Runs every boot — ensures tasks match the code-defined schedule.
    for default in DEFAULT_TASKS:
        tid = default["id"]
        task = _task_store.get(tid)
        if task:
            task = dict(task)  # always copy before mutation (store returns internal ref)
            changed = False
            # Sync days to canonical
            if set(task.get("days", [])) != set(default["days"]):
                task["days"] = default["days"]
                changed = True
            # Sync hour/minute to canonical (if not manually customized via name change)
            if task.get("name") == default["name"]:
                if task.get("hour") != default["hour"] or task.get("minute") != default["minute"]:
                    task["hour"] = default["hour"]
                    task["minute"] = default["minute"]
                    changed = True
            if changed:
                _task_store.set(tid, task)
                log.info(f"Synced {tid} schedule to canonical defaults")

    # Migration: assign admin tasks to root admin's DM (prevents duplicate runs
    # when multiple admins exist — self-opt etc. should run exactly once).
    try:
        from config import ROOT_ADMIN
        _root_admin = ROOT_ADMIN
        if _root_admin and _root_admin[1] != "0":
            _root_chat_id = str(_root_admin[1])  # user_id
            for tid in ("morning", "email_check", "evening1", "daily_selfopt"):
                task = _task_store.get(tid)
                if task and not task.get("chat_id"):
                    task = dict(task)
                    task["chat_id"] = _root_chat_id
                    _task_store.set(tid, task)
                    log.info(f"Assigned admin task {tid} to root admin DM: {_root_chat_id}")
    except Exception as e:
        log.debug(f"Admin task chat_id assignment failed: {e}")

    # Migration: update family-bot prompts to corporate (detects old patterns)
    _family_markers = ["school notices", "packed lunch", "pe kit", "tag both parents", "school run"]
    for tid in ("morning", "email_check", "evening1"):
        task = _task_store.get(tid)
        if task:
            prompt = task.get("prompt", "")
            if any(m in prompt.lower() for m in _family_markers):
                default = next((d for d in DEFAULT_TASKS if d["id"] == tid), None)
                if default:
                    task = dict(task)
                    task["prompt"] = default["prompt"]
                    task["name"] = default["name"]
                    _task_store.set(tid, task)
                    log.info(f"Migrated {tid} prompt: family-bot → corporate")

    if added:
        log.info(f"Initialized {added} default scheduled task(s)")


# === EMPLOYEE TASK TEMPLATES ===
# Used during onboarding to create per-user tasks scoped to their DM chat_id.

EMPLOYEE_TASK_TEMPLATES = [
    {
        "id_suffix": "morning",
        "name": "Morning Briefing",
        "hour": 8,
        "minute": 30,
        "days": ["daily"],  # 7 days/week by default (user can request weekdays only)
        "prompt": (
            "[EMPLOYEE_MORNING] Your daily briefing.\n\n"
            "Fetch fresh data using YOUR email/calendar accounts:\n"
            "1. get_events for today + tomorrow\n"
            "2. search_gmail_messages for last 12h\n"
            "3. memory_search for today's date (reminders, deadlines)\n\n"
            "Compose a brief summary:\n"
            "- Today's meetings and calls\n"
            "- Action items from recent emails\n"
            "- Upcoming deadlines this week\n\n"
            "Send via the messaging tool specified in ROUTING. Keep under 8 bullet points."
        ),
        "platform": "",  # CB-132: inherit from ROOT_ADMIN at runtime
        "enabled": True,
        "default_opt_in": True,
    },
    {
        "id_suffix": "email_monitor",
        "name": "Email Monitor",
        "hour": 10,
        "minute": 0,
        "interval_hours": 2,
        "days": ["daily"],
        "prompt": (
            "[EMPLOYEE_EMAIL] Check your recent emails.\n\n"
            "1. search_gmail_messages for the last 2 hours\n"
            "2. Look for urgent items: meeting changes, client requests, deadlines\n"
            "3. If anything urgent: send alert via the messaging tool specified in ROUTING\n"
            "4. If nothing urgent: stay silent\n"
            "5. Store relevant info via FACTS_UPDATE if needed"
        ),
        "platform": "",  # CB-132: inherit from ROOT_ADMIN at runtime
        "enabled": True,
        "default_opt_in": True,
    },
    {
        "id_suffix": "evening",
        "name": "Evening Prep",
        "hour": 17,
        "minute": 30,
        "days": ["daily"],
        "prompt": (
            "[EMPLOYEE_EVENING] End-of-day recap and next-day prep.\n\n"
            "Fetch fresh data using YOUR email/calendar accounts:\n"
            "1. get_events for tomorrow\n"
            "2. search_gmail_messages for today (last 10h)\n"
            "3. memory_search for pending action items\n\n"
            "Compose a brief summary:\n"
            "- What happened today (key outcomes)\n"
            "- Anything needing action tomorrow\n"
            "- Tomorrow's schedule highlights\n\n"
            "Send via the messaging tool specified in ROUTING. Keep concise."
        ),
        "platform": "",  # CB-132: inherit from ROOT_ADMIN at runtime
        "enabled": True,
        "default_opt_in": True,
    },
]
