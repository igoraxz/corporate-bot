# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Centralized chat key builder — single source of truth for key construction.

All chat/session key construction MUST go through these functions to ensure
forum topic thread_id is consistently included. Ad-hoc f-string construction
(e.g. f"{source}:{chat_id}") misses thread_id and causes misrouted messages.

Key formats:
  Chat key:    source:chat_id           (group-level)
               source:chat_id:thread_id (forum topic)
  Session key: source:chat_id[:thread_id][:task-task_id]
"""


def normalize_thread_id(value) -> int | None:
    """Safely convert a thread_id value to int, or return None.

    Handles None, empty strings, and invalid types gracefully.
    Used to deduplicate thread_id int-casting across dispatch, commands, and agent.
    """
    try:
        return int(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def build_chat_key(source: str, chat_id: str | int,
                   thread_id: int | str | None = None) -> str:
    """Build a chat key from components.

    Returns source:chat_id or source:chat_id:thread_id for forum topics.
    """
    key = f"{source}:{chat_id}"
    if thread_id is not None:
        key = f"{key}:{thread_id}"
    return key


def build_session_key(source: str, chat_id: str | int,
                      thread_id: int | str | None = None,
                      task_id: str | None = None) -> str:
    """Build a session key from components.

    Session keys extend chat keys with optional task suffix.
    """
    key = build_chat_key(source, chat_id, thread_id)
    if task_id:
        key = f"{key}:task-{task_id}"
    return key


def build_chat_key_from_task(task) -> str:
    """Build a chat key from an ActiveTask, including thread_id if available."""
    tid = getattr(task, "tg_message_thread_id", None)
    return build_chat_key(task.source, task.chat_id, tid)


def parse_chat_key(key: str) -> tuple[str, str, int | None]:
    """Parse a chat key into (source, chat_id, thread_id | None).

    Handles all formats: source:chat_id, source:chat_id:thread_id,
    and session keys with :task- suffix (stripped before parsing).
    """
    # Strip task suffix if present
    base = key.split(":task-")[0] if ":task-" in key else key
    parts = base.split(":")
    source = parts[0] if parts else ""
    chat_id = parts[1] if len(parts) >= 2 else ""
    thread_id = None
    if len(parts) >= 3:
        try:
            thread_id = int(parts[2])
        except (ValueError, TypeError):
            pass
    return source, chat_id, thread_id
