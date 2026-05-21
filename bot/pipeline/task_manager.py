# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Task manager for parallel processing across TG and WA.

Manages ActiveTask lifecycle: register, complete, fail, cancel.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from config import MAX_PARALLEL_TASKS, MAX_TOTAL_TASKS

log = logging.getLogger(__name__)


@dataclass
class ActiveTask:
    """Tracks a single in-flight task across platforms."""
    task_id: str
    source: str
    chat_id: str
    user_name: str
    user_id: str
    text: str
    started_at: float = field(default_factory=time.time)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    # TG-specific
    tg_placeholder_id: int | None = None
    tg_placeholder_alive: bool = False
    tg_last_edit: float = 0.0
    tg_last_status_text: str = ""
    tg_user_message_id: int | None = None  # original user message (for checkpoint reply threading)
    tg_message_thread_id: int | None = None  # forum topic thread ID for routing replies
    reply_sent: bool = False
    sent_texts: list[str] = field(default_factory=list)  # capture text sent via MCP send tools
    # Status tracking
    phase: str = "thinking"  # thinking, tools, streaming, done, failed
    tools_used: list[str] = field(default_factory=list)
    tool_labels_seen: list[str] = field(default_factory=list)
    streaming_text: str = ""
    error: str = ""
    use_main_session: bool = True  # Always main session (fork mechanism removed)
    checkpoint_15m_sent: bool = False
    checkpoint_20m_sent: bool = False
    last_sdk_activity: float = field(default_factory=time.time)  # updated on every real SDK event; initialized to creation time so zombie timer starts immediately
    _asyncio_task: asyncio.Task | None = field(default=None, repr=False)

    def elapsed(self) -> str:
        s = int(time.time() - self.started_at)
        if s < 60:
            return f"{s}s"
        return f"{s // 60}m{s % 60:02d}s"

    def elapsed_seconds(self) -> float:
        return time.time() - self.started_at

    def elapsed_rounded(self) -> str:
        """Elapsed time rounded to minutes (e.g. '~2 min', '~5 min')."""
        m = max(1, round((time.time() - self.started_at) / 60))
        return f"~{m} min"


class TaskManager:
    """Manages parallel tasks with real-time status across TG and WA."""

    def __init__(self):
        self._tasks: dict[str, ActiveTask] = {}  # task_id -> ActiveTask

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    def get_active_tasks(self) -> list[ActiveTask]:
        return list(self._tasks.values())

    def get_task(self, task_id: str) -> ActiveTask | None:
        return self._tasks.get(task_id)

    def active_count_for_chat(self, chat_id: str) -> int:
        """Count active tasks for a specific chat."""
        return sum(1 for t in self._tasks.values() if t.chat_id == chat_id)

    def get_over_limit_chats(self) -> set[str]:
        """Return chat_ids that have reached the per-chat task limit."""
        counts: dict[str, int] = {}
        for t in self._tasks.values():
            counts[t.chat_id] = counts.get(t.chat_id, 0) + 1
        return {cid for cid, n in counts.items() if n >= MAX_PARALLEL_TASKS}

    def can_accept_task(self) -> bool:
        return self.active_count < MAX_TOTAL_TASKS

    def other_tasks_summary(self, exclude_id: str) -> str:
        """Build a summary of other active sessions — includes session type labels."""
        others = [t for t in self._tasks.values() if t.task_id != exclude_id]
        if not others:
            return ""
        lines = [
            "⛔ OTHER ACTIVE SESSIONS (do NOT process these — they are not your task):",
        ]
        for t in others:
            status = f"{t.phase}"
            if t.tool_labels_seen:
                status = t.tool_labels_seen[-1]
            session_type = "🔵 ACTIVE"
            safe_text = t.text[:120].replace('\n', ' ').replace('\r', ' ')
            lines.append(f"  - {session_type} [{t.elapsed()}] \"{safe_text}\" ({status})")
        return "\n".join(lines)

    def register_task(self, source: str, chat_id: str, user_name: str,
                      user_id: str, text: str) -> ActiveTask:
        task_id = str(uuid.uuid4())[:8]
        task = ActiveTask(
            task_id=task_id,
            source=source,
            chat_id=chat_id,
            user_name=user_name,
            user_id=user_id,
            text=text[:200],
        )
        self._tasks[task_id] = task
        log.info(f"Task {task_id} registered: {text[:60]} (active={self.active_count})")
        return task

    async def complete_task(self, task_id: str):
        task = self._tasks.pop(task_id, None)
        if task:
            task.phase = "done"
            log.info(f"Task {task_id} completed ({task.elapsed()}, tools={task.tools_used})")
        # Signal PQ to dispatch next pending row (if any)
        self._signal_pq()

    async def fail_task(self, task_id: str, error: str):
        task = self._tasks.get(task_id)
        if task:
            task.phase = "failed"
            task.error = error
        self._tasks.pop(task_id, None)
        log.error(f"Task {task_id} failed: {error[:100]}")
        # Signal PQ to dispatch next pending row (if any)
        self._signal_pq()

    def _signal_pq(self):
        """Signal the persistent queue to dispatch next pending row."""
        # Lazy import to avoid circular dependency with main.py
        try:
            import main as _main_mod
            pq = getattr(_main_mod, "persistent_queue", None)
            if pq is not None:
                pq.signal()
        except Exception:
            pass

    def find_cancel_target(self, text: str, chat_id: str | None = None) -> ActiveTask | None:
        """Find the task that a cancel message refers to.

        Handles: "cancel", "stop", exact cancel, "cancel this task and ...",
        cancel by keyword match.  Scoped to chat_id when provided — never
        cross-cancel tasks in other chats.
        """
        all_active = self.get_active_tasks()
        # Scope to the requesting chat — never cancel cross-chat
        if chat_id:
            active = [t for t in all_active if str(t.chat_id) == str(chat_id)]
        else:
            active = all_active
        if len(active) == 1:
            return active[0]
        if not active:
            return None
        # Try to match by keyword from the cancel message
        text_lower = text.lower()
        for word in ["cancel", "stop", "abort", "kill"]:
            text_lower = text_lower.replace(word, "").strip()
        # Remove common phrases
        for phrase in ["this task", "that task", "the task", "and check",
                       "and look at", "status", "logs", "please"]:
            text_lower = text_lower.replace(phrase, "").strip()
        # If no identifying text left, cancel the oldest
        if not text_lower or len(text_lower) < 3:
            return min(active, key=lambda t: t.started_at)
        # Try fuzzy match against task text
        for task in active:
            if any(w in task.text.lower() for w in text_lower.split() if len(w) > 2):
                return task
        # Default: cancel oldest in this chat
        return min(active, key=lambda t: t.started_at)


task_manager = TaskManager()
