# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Core message handlers: handle_telegram_message, handle_teams_message.

All bot.*, config.*, integrations.* imports are LAZY (inside function bodies)
to avoid circular import issues.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.pipeline.task_manager import ActiveTask
from pathlib import Path

log = logging.getLogger(__name__)


def _copy_media_to_sandbox(
    media_info: dict | None,
    text: str,
    session_key: str,
) -> str:
    """Copy media files into sandbox dir so sandboxed models can Read them.

    Media downloads land in /app/data/tmp/ which is outside the sandbox boundary.
    This copies them to <sandbox_dir>/_attachments/ and:
    - Sets media_info["prompt_path"] for the primary attachment (agent.py uses it)
    - Replaces embedded /app/data/tmp/ paths in text (for merged media)

    Returns the (possibly updated) text. media_info is mutated in-place.
    Failures are non-fatal — the model just can't Read the attachment.
    """
    try:
        from bot.core.session_state import _get_or_create_state
        import shutil

        state = _get_or_create_state(session_key)
        sandbox_dir = state.get("sandbox_dir")
        if not sandbox_dir:
            return text

        sandbox_resolved = str(Path(sandbox_dir).resolve())
        media_subdir = Path(sandbox_dir) / "_attachments"
        needs_dir = True  # create once on first use

        # Primary media — copy and set prompt_path
        if media_info and media_info.get("local_path"):
            src = Path(media_info["local_path"])
            if src.exists() and not str(src.resolve()).startswith(sandbox_resolved):
                if needs_dir:
                    media_subdir.mkdir(parents=True, exist_ok=True)
                    needs_dir = False
                dst = media_subdir / src.name
                shutil.copy2(str(src), str(dst))
                media_info["prompt_path"] = str(dst)
                log.info(f"Copied media to sandbox: {dst}")

        # Merged media paths embedded in text — copy and replace
        tmp_resolved = str(Path("/app/data/tmp").resolve())
        for tmp_path in re.findall(r"/app/data/tmp/[^\s\]\n]+", text):
            src = Path(tmp_path)
            # Validate src resolves within /app/data/tmp/ (blocks ../../../ etc traversal)
            if src.exists() and str(src.resolve()).startswith(tmp_resolved + "/"):
                if needs_dir:
                    media_subdir.mkdir(parents=True, exist_ok=True)
                    needs_dir = False
                dst = media_subdir / src.name
                shutil.copy2(str(src), str(dst))
                text = text.replace(tmp_path, str(dst))
                log.info(f"Copied merged media to sandbox: {dst}")
    except Exception:
        log.warning("Failed to copy media to sandbox for %s",
                    session_key, exc_info=True)

    return text


def _set_sandbox_for_session(session_key: str) -> None:
    """Set sandbox_dir in session state from RBAC execution scope.

    Sprint R4: sandbox flag removed from Harness — RBAC execution scope determines
    whether the session is sandboxed (filesystem=sandbox) or has full access (filesystem=full).

    When sandbox is active, ALL users (admin + non-admin) get identical
    bwrap containment — Bash/Write/Edit/Read/Grep/Glob confined to project dir.
    """
    try:
        from bot.harness import get_harness_for_chat, get_harness_cwd
        from bot.core.session_state import _get_or_create_state

        state = _get_or_create_state(session_key)
        rbac_execution = state.get("rbac_execution", {})
        fs_scope = rbac_execution.get("filesystem", "none")

        sandbox_dir = None
        if fs_scope == "sandbox":
            # RBAC says sandbox — derive project_dir from harness label
            harness = get_harness_for_chat(session_key)
            cwd = get_harness_cwd(harness.label, session_key)
            if cwd and cwd != "/app":
                sandbox_dir = cwd
                log.info("Sandbox active (RBAC filesystem=sandbox) for harness '%s': %s",
                         harness.label, sandbox_dir)
        # filesystem=full → no sandbox (admin access)
        # filesystem=none → no sandbox (no filesystem access — Layer 1b blocks tools)

        state["sandbox_dir"] = sandbox_dir
    except Exception:
        log.warning("Failed to set sandbox for session %s", session_key, exc_info=True)


async def handle_telegram_message(parsed: dict, task: "ActiveTask") -> None:
    """Process a TG message with real-time streaming in the task's placeholder."""
    from bot.agent import process_incoming
    from integrations.telegram import (
        send_chat_action, send_message, delete_message, download_media,
    )

    from config import (
        STREAM_EDIT_INTERVAL, PLACEHOLDER_DELAY,
        REPLY_QUOTE_MAX_CHARS, IS_STAGING, get_model_primary,
        MEDIA_INLINE_READ_MAX_KB, MEDIA_DESCRIBE_TIMEOUT_S,
    )
    from bot.hooks import TOOL_LABELS, SEND_TOOLS
    from bot.pipeline.task_manager import task_manager
    from bot.pipeline.streaming import (
        _kill_placeholder, _cancel_if_pending, _tg_safe_edit,
        _build_tg_status, _tg_ticker,
    )
    from bot.pipeline.staging import staging_buffer_response
    from bot.commands import _suggest_diagnose_on_failure

    user_name = parsed["user_name"]
    user_id = str(parsed["user_id"])
    text = parsed["text"]
    message_id = str(parsed.get("message_id", ""))
    # Corporate mode: chat_id must be in parsed dict. No primary group fallback.
    chat_id = parsed.get("chat_id") or ""

    # Enrich text with reply/forward/location context
    reply_to = parsed.get("reply_to")
    if reply_to:
        parts = []
        if reply_to.get("text"):
            label = "bot's previous response" if reply_to.get("is_bot") else "earlier message"
            _q = reply_to["text"]
            # Strip RELAY_FILE tags from reply-to text so the daemon
            # doesn't echo them back and cause duplicate file delivery.
            _q = re.sub(r"\[[\s\S]*?RELAY_FILE:\s*/[^\]\n]+?\s*\]", "", _q).strip()
            if len(_q) > REPLY_QUOTE_MAX_CHARS:
                _q = _q[:REPLY_QUOTE_MAX_CHARS] + "\u2026[truncated]"
            if _q:
                parts.append(f'[Replying to {label}: "{_q}"]')
        if reply_to.get("location"):
            loc = reply_to["location"]
            coords = f"{loc.get('latitude')}, {loc.get('longitude')}"
            loc_type = "live location" if loc.get("live_period") else "location"
            if loc.get("title"):
                addr = f", {loc['address']}" if loc.get("address") else ""
                parts.append(f"[Replying to {loc_type}: {loc['title']}{addr} ({coords})]")
            else:
                parts.append(f"[Replying to {loc_type}: {coords}]")
        if reply_to.get("contact"):
            c = reply_to["contact"]
            name = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip()
            phone = c.get("phone_number", "")
            parts.append(f"[Replying to shared contact: {name}" + (f", {phone}]" if phone else "]"))
        if reply_to.get("has_media"):
            parts.append("[Replying to message with media — see attached]")
        if parts:
            text = "\n".join(parts) + "\n" + text

    forward = parsed.get("forward")
    if forward:
        fwd_name = forward.get("sender_name", "someone")
        # For forwarded messages, the user's own text is empty — only forwarded content exists.
        # Ask what to do instead of blindly processing.
        text = (f"[Forwarded message from {fwd_name}]\n{text}\n\n"
                f"[SYSTEM: This is a forwarded message. The user did not add their own instructions. "
                f"Briefly acknowledge the content and ask what they'd like you to do with it.]")

    # Direct location message
    if parsed.get("location"):
        loc = parsed["location"]
        coords = f"{loc.get('latitude')}, {loc.get('longitude')}"
        if loc.get("title"):
            addr = f", {loc['address']}" if loc.get("address") else ""
            loc_desc = f"{loc['title']}{addr} ({coords})"
        else:
            loc_desc = coords
        loc_type = "Live location" if loc.get("live_period") else "Shared location"
        text = f"[{loc_type}: {loc_desc}]\n{text}" if text else f"[{loc_type}: {loc_desc}]"

    # Direct contact message
    if parsed.get("contact"):
        c = parsed["contact"]
        name = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip()
        phone = c.get("phone_number", "")
        contact_desc = f"{name}" + (f", {phone}" if phone else "")
        text = f"[Shared contact: {contact_desc}]\n{text}" if text else f"[Shared contact: {contact_desc}]"

    # Download media — from direct message, reply-to, or forwarded message
    media_info = None
    if parsed.get("has_media"):
        media_info = await download_media(parsed["raw"])
    elif reply_to and reply_to.get("has_media") and reply_to.get("raw"):
        media_info = await download_media(reply_to["raw"])
        if media_info:
            log.info(f"Downloaded media from reply-to: {media_info.get('media_type')}")

    # Synchronous voice transcription — run BEFORE agent so transcript appears in prompt
    if media_info and media_info.get("media_type") in ("voice", "audio") and media_info.get("local_path"):
        try:
            from integrations.gemini import transcribe_voice
            t0 = time.time()
            result = await transcribe_voice(media_info["local_path"])
            if result.get("transcript"):
                media_info["transcript"] = result["transcript"]
                log.info(f"Voice transcribed sync ({time.time()-t0:.1f}s): {len(result['transcript'])} chars")
            elif result.get("error"):
                log.warning(f"Voice transcription failed: {result['error']}")
        except Exception as e:
            log.warning(f"Voice transcription error: {e}")

    # Synchronous Gemini description for large files — prevents SDK 1MB JSON buffer crash.
    # Files above MEDIA_INLINE_READ_MAX_KB get an AI description instead of a file path in the
    # prompt, so the model never tries to Read them (which would base64-encode >1MB -> crash).
    if media_info and media_info.get("local_path") and not media_info.get("transcript"):
        fsize = media_info.get("file_size", 0)
        if not fsize:
            try:
                fsize = Path(media_info["local_path"]).stat().st_size
                media_info["file_size"] = fsize
            except OSError:
                pass
        if fsize > MEDIA_INLINE_READ_MAX_KB * 1024:
            try:
                from integrations.gemini import describe_media
                t0 = time.time()
                result = await asyncio.wait_for(
                    describe_media(media_info["local_path"]),
                    timeout=MEDIA_DESCRIBE_TIMEOUT_S,
                )
                if result.get("description"):
                    media_info["description"] = result["description"]
                    log.info(f"Large file described sync ({time.time()-t0:.1f}s): {len(result['description'])} chars, {fsize/1024:.0f}KB")
                else:
                    # Gemini failed — still set description to prevent path injection
                    media_info["description"] = f"[File: {(media_info.get('filename') or 'unknown').replace('[', '(').replace(']', ')')} — description unavailable]"
                    log.warning(f"Large file description failed: {result.get('error', 'unknown')}")
            except Exception as e:
                media_info["description"] = f"[File: {(media_info.get('filename') or 'unknown').replace('[', '(').replace(']', ')')} — description unavailable]"
                log.warning(f"Large file sync description error: {e}")

    # Process merged_media — additional attachments coalesced by the
    # burst-race merger in triage_and_enqueue. Each item is downloaded,
    # transcribed/described, and appended to the visible `text` as an
    # inline attachment block. Kept separate from the primary `media_info`
    # (which drives SDK media_info param, cache logging) for minimal blast
    # radius — the primary still behaves exactly as before.
    merged_media_list = parsed.get("merged_media") or []
    if merged_media_list:
        _merged_blocks: list[str] = []
        for _idx, _mm in enumerate(merged_media_list, start=1):
            try:
                _raw = _mm.get("raw")
                if not _raw:
                    continue
                _mi = await download_media(_raw)
                if not _mi or not _mi.get("local_path"):
                    _merged_blocks.append(
                        f"[Merged attachment #{_idx} — download failed]"
                    )
                    continue
                _mtype = _mi.get("media_type", "file")
                _extra = ""
                if _mtype in ("voice", "audio"):
                    try:
                        from integrations.gemini import transcribe_voice
                        _tr = await transcribe_voice(_mi["local_path"])
                        if _tr.get("transcript"):
                            _extra = f'Transcript: "{_tr["transcript"]}"'
                            _mi["transcript"] = _tr["transcript"]
                    except Exception as _te:
                        log.warning(f"Merged #{_idx} transcribe error: {_te}")
                else:
                    try:
                        _fs = _mi.get("file_size") or 0
                        if not _fs:
                            try:
                                _fs = Path(_mi["local_path"]).stat().st_size
                            except OSError:
                                _fs = 0
                        if _fs > MEDIA_INLINE_READ_MAX_KB * 1024:
                            from integrations.gemini import describe_media
                            _dr = await asyncio.wait_for(
                                describe_media(_mi["local_path"]),
                                timeout=MEDIA_DESCRIBE_TIMEOUT_S,
                            )
                            if _dr.get("description"):
                                _extra = f'AI description: {_dr["description"]}'
                                _mi["description"] = _dr["description"]
                    except Exception as _de:
                        log.warning(f"Merged #{_idx} describe error: {_de}")
                _fwd = _mm.get("forward") or {}
                _src_label = (
                    f" forwarded from {_fwd.get('sender_name', 'someone')}"
                    if _fwd else ""
                )
                _path_tag = f"{_mtype.upper()} attached: {_mi['local_path']}"
                _caption = _mm.get("caption") or ""
                _block = f"[Merged message #{_idx}{_src_label} — {_path_tag}]"
                if _caption:
                    _block += f"\nCaption: {_caption}"
                if _extra:
                    _block += f"\n{_extra}"
                _merged_blocks.append(_block)
            except Exception as _me:
                log.warning(f"Merged #{_idx} processing error: {_me}",
                            exc_info=True)
        if _merged_blocks:
            text = (text + "\n\n" + "\n\n".join(_merged_blocks)).strip()
            log.info(f"Processed {len(_merged_blocks)} merged media items "
                     f"chat={chat_id}")

    # Forum topic thread ID — must be defined before any use (registry check, send calls)
    _thread_id = parsed.get("message_thread_id")
    _thread_id_int = int(_thread_id) if _thread_id is not None else None

    # Streaming UX determined by registry mode:
    # active_plus = full streaming (placeholders, ticker)
    # active/silent = no streaming (simpler UX for 3P/multi-party chats)
    _is_external = parsed.get("is_external_chat", False)
    _chat_mode = None
    try:
        from bot.chat_registry import get_mode as _get_reg_mode
        _ck = f"telegram:{chat_id}"
        if _thread_id_int is not None:
            _ck = f"telegram:{chat_id}:{_thread_id_int}"
        _chat_mode = _get_reg_mode(_ck)
    except Exception:
        _chat_mode = "active"  # Fail-closed: no streaming until registry confirms
    skip_streaming = (
        IS_STAGING
        or (_chat_mode is not None and _chat_mode != "active_plus")
    )

    # Deferred placeholder: send "typing" immediately, create "Thinking..." after delay.
    # Avoids placeholder flash on fast cached responses (main session often completes in <1s).
    # UX invariant (v5): thread placeholder + final reply to the LATEST user
    # message this task will include in its context. When a burst of messages
    # was coalesced via merge_into_pending, merged_message_ids[-1] is newer
    # than the primary message_id — use it so the reply points at the last
    # thing the user actually sent, not an already-replied-to earlier msg.
    _merged_ids = parsed.get("merged_message_ids") or []
    _last_merged = None
    if _merged_ids:
        try:
            _last_merged = int(_merged_ids[-1])
        except (TypeError, ValueError):
            _last_merged = None
    user_msg_id = _last_merged if _last_merged else (
        int(message_id) if message_id else None
    )
    _ph_deferred: asyncio.Task | None = None

    if not skip_streaming:
        await send_chat_action("typing", chat_id=chat_id, message_thread_id=_thread_id_int)

        async def _create_placeholder_after_delay():
            """Create placeholder after PLACEHOLDER_DELAY unless response already sent."""
            try:
                await asyncio.sleep(PLACEHOLDER_DELAY)
                if task.reply_sent or task.cancel_event.is_set() or task.tg_placeholder_id:
                    return  # response already sent or placeholder already created
                ph = await send_message(
                    "\U0001f9e0 Thinking...", chat_id=chat_id, parse_mode=None,
                    reply_to_message_id=user_msg_id,
                    message_thread_id=_thread_id_int,
                )
                if ph:
                    task.tg_placeholder_id = ph["message_id"]
                    task.tg_placeholder_alive = True
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.debug(f"Deferred placeholder failed: {e}")

        _ph_deferred = asyncio.create_task(_create_placeholder_after_delay())

    # Ticker runs always (handles missing placeholder gracefully via tg_placeholder_alive check)
    ticker = asyncio.create_task(_tg_ticker(task)) if not skip_streaming else None

    async def on_thinking_start():
        if skip_streaming:
            return
        task.phase = "thinking"
        await _tg_safe_edit(task, f"\U0001f9e0 Thinking... ({task.elapsed()})")

    async def on_tool_status(tool_names: list[str], tool_input: dict | None = None):
        task.last_sdk_activity = time.time()
        ti = tool_input if isinstance(tool_input, dict) else {}
        for n in tool_names:
            if n in SEND_TOOLS:
                # Only mark reply_sent if the send targets THIS chat (not a DM to someone else)
                target_cid = ti.get("chat_id")
                if target_cid is None or str(target_cid) == str(chat_id):
                    task.reply_sent = True
                    # v5 UX fix: eagerly cancel the deferred placeholder creator
                    # once the model has actually called a send tool for this
                    # chat. Prevents the "Thinking..." placeholder from firing
                    # and then immediately needing delete — and closes a race
                    # where the placeholder fires AFTER the tool's send but
                    # before finally runs, leaving an orphan.
                    _cancel_if_pending(_ph_deferred)
                    # Capture sent text for fork result context (so main session knows what fork said)
                    sent_text = ti.get("text", "")
                    if sent_text:
                        task.sent_texts.append(sent_text)
        if task.phase == "streaming":
            return
        labels = [TOOL_LABELS.get(n, n) for n in tool_names
                  if n not in SEND_TOOLS]
        if labels:
            task.tool_labels_seen.extend(labels)
            task.tools_used.extend(tool_names)
            task.phase = "tools"
            status = _build_tg_status(task)
            await _tg_safe_edit(task, status)

    async def on_stream_chunk(text_so_far: str):
        task.last_sdk_activity = time.time()
        if skip_streaming:
            return
        if len(text_so_far) <= 5:
            return
        from bot.agent import strip_facts_update
        from bot.md_to_tg_html import md_to_tg_html
        display_text = strip_facts_update(text_so_far, streaming=True)
        if not display_text:
            # Model is outputting FACTS_UPDATE with no readable text yet — show brief status
            if "FACTS_UPDATE" in text_so_far:
                display_text = "\U0001f4be Saving..."
            else:
                return
        task.streaming_text = display_text
        if not task.cancel_event.is_set():
            task.phase = "streaming"
        now = time.time()
        if now - task.tg_last_edit < STREAM_EDIT_INTERVAL:
            return
        task.tg_last_edit = now
        # Re-create placeholder if it was lost (permanent edit error or never created)
        # v5 UX fix: send a neutral "Thinking..." placeholder — NEVER stream
        # content into a fresh message. If we leaked stream content here, the
        # model's later telegram_send_message tool call would deliver a second
        # near-identical message and finally's delete-on-reply_sent might race
        # or be too late (user sees both for several seconds).
        # Eager-cancel the deferred task so it doesn't race with us.
        if not task.tg_placeholder_id or not task.tg_placeholder_alive:
            _cancel_if_pending(_ph_deferred)
            try:
                new_ph = await send_message("\U0001f9e0 Thinking...", chat_id=chat_id,
                                            parse_mode=None,
                                            reply_to_message_id=user_msg_id,
                                            message_thread_id=_thread_id_int)
                if new_ph:
                    task.tg_placeholder_id = new_ph["message_id"]
                    task.tg_placeholder_alive = True
                    task.tg_last_status_text = "\U0001f9e0 Thinking..."
                    log.info(f"Re-created placeholder {task.tg_placeholder_id} "
                             f"for task {task.task_id} (Thinking, no content leak)")
                    # Immediately edit with current streaming content so the
                    # user sees progress rather than a stale "Thinking..." —
                    # this EDIT stays under the same msg_id, so finally's
                    # delete-on-reply_sent cleanly removes it.
                    await _tg_safe_edit(task, md_to_tg_html(display_text)[:4096],
                                        parse_mode="HTML")
            except Exception as e:
                # Upgraded from debug -> warning: failed recreate means the user
                # sees nothing during active streaming. Worth surfacing in prod
                # logs for forensic analysis.
                log.warning(f"Failed to re-create placeholder: {e}")
            return
        await _tg_safe_edit(task, md_to_tg_html(display_text)[:4096],
                            parse_mode="HTML")

    # Build context about other parallel tasks
    other_ctx = task_manager.other_tasks_summary(task.task_id)

    # Always use main session (no fork sessions)

    # Set reply-threading state for MCP tools (per-session state).
    # MUST match agent.py session_key format — includes thread_id for forum topics
    # so each topic gets its own SDK client and conversation context.
    from bot.hooks import set_reply_threading
    tg_session_key = f"telegram:{chat_id or 'default'}"
    if _thread_id_int is not None:
        tg_session_key = f"telegram:{chat_id}:{_thread_id_int}"
    set_reply_threading(
        msg_id=int(message_id) if message_id and str(message_id) != "0" else None,
        task_id=task.task_id,
        chat_id=str(chat_id),
        session_key=tg_session_key,
        is_external_chat=(_chat_mode is not None and _chat_mode != "active_plus"),
        message_thread_id=_thread_id_int,
    )

    # Set coding sandbox if harness has sandbox=True
    _set_sandbox_for_session(tg_session_key)

    # Copy media into sandbox dir so sandboxed models can Read the file
    text = _copy_media_to_sandbox(media_info, text, tg_session_key)

    response_text = ""  # Initialize before try so it's always available in finally
    try:
        response_text = await process_incoming(
            source="telegram",
            user_name=user_name,
            user_id=user_id,
            text=text,
            message_id=message_id,
            media_info=media_info,
            on_stream_chunk=on_stream_chunk,
            on_thinking_start=on_thinking_start,
            on_tool_status=on_tool_status,
            chat_id=str(chat_id),
            model=parsed.get("model", get_model_primary()),
            task_id="",  # always main session
            other_tasks_context=other_ctx,
            is_external_chat=(_chat_mode is not None and _chat_mode != "active_plus"),
            message_thread_id=_thread_id_int,
        )
        # Placeholder cleanup happens in finally block when LLM run finishes
    except Exception as e:
        log.error(f"TG message processing failed: {e}", exc_info=True)
        if task.tg_placeholder_id and task.tg_placeholder_alive:
            await _tg_safe_edit(task, f"\u26a0\ufe0f Error: {str(e)[:200]}")
            # Suggest self-diagnose on failure
            await _suggest_diagnose_on_failure(task, str(e))
    finally:
        # Cancel deferred placeholder if it hasn't fired yet, and join it so
        # CancelledError doesn't leak into the event loop.
        _cancel_if_pending(_ph_deferred)
        if _ph_deferred is not None:
            try:
                await _ph_deferred
            except asyncio.CancelledError:
                pass
            except Exception as _exc:
                log.debug("Suppressed: %s", _exc)
        if ticker:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
        # Clean up placeholder — covers ALL exit paths
        from bot.hooks import get_intentional_silence
        is_silent = get_intentional_silence(session_key=tg_session_key)
        if task.tg_placeholder_id and task.tg_placeholder_alive:
            if task.reply_sent or is_silent:
                # Reply was sent via tool OR intentional silence — delete placeholder
                try:
                    await delete_message(task.tg_placeholder_id, chat_id=chat_id)
                except Exception as e:
                    log.debug(f"Cleanup failed: {e}")
                _kill_placeholder(task)
            elif task.phase == "streaming" and not (task.streaming_text or "").strip():
                # Streaming phase but no visible text — check for salvageable content
                if response_text and response_text.strip():
                    # Content was produced but not streamed — salvage it
                    log.warning(f"TG salvage (empty stream): delivering {len(response_text)} chars")
                    try:
                        await delete_message(task.tg_placeholder_id, chat_id=chat_id)
                    except Exception as e:
                        log.debug(f"Cleanup failed: {e}")
                    _kill_placeholder(task)
                    try:
                        await send_message(response_text, chat_id=chat_id,
                                           message_thread_id=_thread_id_int)
                        from bot.hooks import mark_chat_activity
                        mark_chat_activity(str(chat_id), task.task_id)
                    except Exception as e:
                        log.error(f"TG salvage send failed: {e}")
                else:
                    try:
                        await delete_message(task.tg_placeholder_id, chat_id=chat_id)
                    except Exception as e:
                        log.debug(f"Cleanup failed: {e}")
                    _kill_placeholder(task)
            elif task.phase != "streaming":
                if task.tools_used:
                    # Tools ran but no reply — salvage response_text if available
                    # (agent.py fallback is disabled for TG because on_stream_chunk is set;
                    #  this catches content the model produced but forgot to send via tool)
                    if response_text and response_text.strip():
                        log.warning(f"TG salvage (tools, no send): delivering "
                                    f"{len(response_text)} chars from {len(task.tools_used)} tool(s)")
                        try:
                            await delete_message(task.tg_placeholder_id, chat_id=chat_id)
                        except Exception as e:
                            log.debug(f"Cleanup failed: {e}")
                        _kill_placeholder(task)
                        try:
                            await send_message(response_text, chat_id=chat_id,
                                               message_thread_id=_thread_id_int)
                            from bot.hooks import mark_chat_activity
                            mark_chat_activity(str(chat_id), task.task_id)
                        except Exception as e:
                            log.error(f"TG salvage send failed: {e}")
                    else:
                        # No content to salvage — show warning
                        elapsed = task.elapsed()
                        tools_summary = ", ".join(task.tools_used[-5:])
                        await _tg_safe_edit(task,
                            f"\u26a0\ufe0f Task finished without reply ({elapsed}, tools: {tools_summary}).\n"
                            f"Send \"diagnose\" to run self-diagnostics.")
                        _kill_placeholder(task)
                        await _suggest_diagnose_on_failure(task, "Task produced no output")
                else:
                    # No tools, no reply, no silence flag — delete placeholder
                    try:
                        await delete_message(task.tg_placeholder_id, chat_id=chat_id)
                    except Exception as e:
                        log.debug(f"Cleanup failed: {e}")
                    _kill_placeholder(task)
            else:
                # Branch 4: streaming phase WITH visible text but no reply/silence.
                # Happens when model outputs only FACTS_UPDATE (streaming shows "Saving...")
                # but never sends a reply. Delete the orphaned placeholder.
                from bot.agent import strip_facts_update
                salvage = strip_facts_update(response_text).strip() if response_text else ""
                if salvage:
                    log.warning(f"TG salvage (streaming, no send): delivering {len(salvage)} chars")
                    try:
                        await delete_message(task.tg_placeholder_id, chat_id=chat_id)
                    except Exception as e:
                        log.debug(f"Cleanup failed: {e}")
                    _kill_placeholder(task)
                    try:
                        await send_message(salvage, chat_id=chat_id,
                                           message_thread_id=_thread_id_int)
                        from bot.hooks import mark_chat_activity
                        mark_chat_activity(str(chat_id), task.task_id)
                    except Exception as e:
                        log.error(f"TG salvage send failed: {e}")
                else:
                    # Nothing to salvage — model saved facts but forgot to reply.
                    # Always inform the user (never silently delete).
                    log.warning("Model produced no reply (FACTS_UPDATE only) — sending ack")
                    try:
                        await delete_message(task.tg_placeholder_id, chat_id=chat_id)
                    except Exception as e:
                        log.debug(f"Cleanup failed: {e}")
                    _kill_placeholder(task)
                    try:
                        await send_message("\U0001f4be Noted.", chat_id=chat_id,
                                           message_thread_id=_thread_id_int)
                    except Exception as e:
                        log.debug(f"Ack send failed: {e}")
        # Staging fallback: if the model responded with text but didn't call
        # telegram_send_message tool, buffer the response_text so /test/inject
        # returns it. Without this, the completion event never fires.
        if IS_STAGING and response_text and response_text.strip() and not task.reply_sent:
            from bot.agent import strip_facts_update
            clean = strip_facts_update(response_text).strip()
            if clean:
                staging_buffer_response(
                    str(chat_id), "telegram_send_message",
                    {"text": clean, "chat_id": str(chat_id), "parse_mode": "HTML"},
                )
                log.info(f"[STAGING] Buffered {len(clean)} chars from response_text fallback")

        if media_info and media_info.get("local_path"):
            try:
                from bot.storage.memory import cache_media_file, link_media_to_message
                from bot.storage.media import generate_media_description as _generate_media_description
                cached = await cache_media_file(
                    source_path=media_info["local_path"],
                    media_type=media_info.get("media_type", "unknown"),
                    source="telegram",
                    sender_name=user_name,
                    chat_id=str(chat_id),
                    original_filename=media_info.get("filename", ""),
                    description=text or "",
                    mime_type=media_info.get("mime_type", ""),
                    tg_file_id=media_info.get("file_id", ""),
                )
                if cached:
                    log.info(f"Media cached: {cached['filename']} (id={cached.get('id')})")
                    if cached.get("id"):
                        await link_media_to_message("telegram", str(parsed["message_id"]), cached["id"])
                    # Use sync transcript or sync description if available (skip redundant Gemini call)
                    if cached.get("id") and media_info.get("transcript"):
                        from bot.storage.memory import update_media_description
                        await update_media_description(cached["id"], media_info["transcript"])
                        log.info(f"Used sync transcript as media description: id={cached['id']}")
                    elif cached.get("id") and media_info.get("description"):
                        from bot.storage.memory import update_media_description
                        await update_media_description(cached["id"], media_info["description"])
                        log.info(f"Used sync large-file description as media description: id={cached['id']}")
                    elif cached.get("id") and cached.get("file_path"):
                        # Spawn background AI description (non-blocking)
                        asyncio.create_task(_generate_media_description(
                            cached["id"], cached["file_path"], cached.get("filename", "")
                        ))
                media_path = Path(media_info["local_path"])
                if media_path.exists():
                    media_path.unlink()
            except Exception as e:
                log.warning(f"Failed to cache/clean up media file: {e}")


# ============================================================
# TEAMS HANDLER
# ============================================================

async def handle_teams_message(parsed: dict, task: "ActiveTask") -> None:
    """Process a Teams message with streaming via UpdateActivity.

    Follows the same pattern as handle_telegram_message:
    - Placeholder message ("Thinking...") sent immediately
    - Updated in real-time with tool status and streaming text
    - Deleted when final reply is sent via teams_send_message MCP tool

    Teams supports UpdateActivity (PUT) for in-place message edits —
    works in all scopes (personal, group chat, channel).
    """
    from bot.agent import process_incoming

    from config import get_model_primary, IS_STAGING
    from bot.hooks import TOOL_LABELS, SEND_TOOLS
    from bot.pipeline.task_manager import task_manager

    user_name = parsed["user_name"]
    user_id = str(parsed["user_id"])
    text = parsed["text"]
    message_id = parsed.get("message_id", "")
    chat_id = parsed.get("chat_id", "")

    # Skip streaming on staging (no Teams API available)
    skip_streaming = IS_STAGING

    # Teams placeholder state
    teams_placeholder_id: str | None = None

    # Get Teams client for placeholder/streaming
    teams_client = None
    if not skip_streaming:
        from integrations.teams import get_client
        teams_client = get_client()

    # Send typing indicator + create placeholder
    if teams_client and chat_id:
        try:
            await teams_client.send_typing(chat_id)
            result = await teams_client.send_message(
                chat_id, "\U0001f9e0 Thinking...", text_format="plain",
            )
            teams_placeholder_id = result.get("id")
            task.tg_placeholder_id = teams_placeholder_id  # reuse TG field for tracking
            task.tg_placeholder_alive = True
        except Exception as e:
            log.warning("Teams: failed to create placeholder: %s", e)

    # Streaming callbacks
    _last_edit_time: float = 0

    async def on_thinking_start():
        pass  # Already showing "Thinking..." placeholder

    async def on_tool_status(tool_names: list[str], tool_input: dict | None = None):
        nonlocal _last_edit_time
        task.last_sdk_activity = time.time()
        ti = tool_input if isinstance(tool_input, dict) else {}

        for n in tool_names:
            if n in SEND_TOOLS:
                target_cid = ti.get("chat_id")
                if target_cid is None or str(target_cid) == str(chat_id):
                    task.reply_sent = True
                    sent_text = ti.get("text", "")
                    if sent_text:
                        task.sent_texts.append(sent_text)

        if task.phase == "streaming":
            return

        labels = [TOOL_LABELS.get(n, n) for n in tool_names
                  if n not in SEND_TOOLS]
        if labels:
            task.tool_labels_seen.extend(labels)
            task.tools_used.extend(tool_names)
            task.phase = "tools"

            # Update placeholder with tool status
            if teams_client and teams_placeholder_id and not task.reply_sent:
                now = time.time()
                if now - _last_edit_time >= 2.0:  # Throttle to 0.5 Hz
                    _last_edit_time = now
                    chain = " \u2192 ".join(task.tool_labels_seen[-5:])
                    status = f"\U0001f527 {chain} ({task.elapsed()})"
                    try:
                        await teams_client.update_message(
                            chat_id, teams_placeholder_id, status,
                            text_format="plain",
                        )
                    except Exception as e:
                        log.debug("Teams placeholder update failed: %s", e)

    async def on_stream_chunk(text_so_far: str):
        nonlocal _last_edit_time
        task.last_sdk_activity = time.time()
        if skip_streaming or not teams_client or not teams_placeholder_id:
            return
        if len(text_so_far) <= 5:
            return

        from bot.agent import strip_facts_update
        display_text = strip_facts_update(text_so_far, streaming=True)
        if not display_text:
            return

        task.streaming_text = display_text
        task.phase = "streaming"

        now = time.time()
        if now - _last_edit_time < 1.5:  # Throttle to ~0.7 Hz
            return
        _last_edit_time = now

        try:
            # Teams messages can be much longer than TG (100KB vs 4KB)
            await teams_client.update_message(
                chat_id, teams_placeholder_id,
                display_text[:80000],  # 80KB safety limit
                text_format="markdown",
            )
        except Exception as e:
            log.debug("Teams stream update failed: %s", e)

    # Build context about other parallel tasks
    other_ctx = task_manager.other_tasks_summary(task.task_id)

    # Set reply-threading state for MCP tools (per-session state)
    from bot.hooks import set_reply_threading
    teams_session_key = f"teams:{chat_id or 'default'}"
    # R4: Teams external status from RBAC execution scope (not harness data_scope).
    # filesystem=full → admin (not external). Otherwise → external (scoped).
    from bot.core.session_state import _get_or_create_state
    _teams_state = _get_or_create_state(teams_session_key)
    _teams_rbac_exec = _teams_state.get("rbac_execution", {})
    _teams_is_external = _teams_rbac_exec.get("filesystem") != "full"

    # Teams media download — mirrors TG pattern (lines 209-259)
    _teams_media_info = None
    if parsed.get("has_media") and parsed.get("raw"):
        _t_attachments = parsed["raw"].get("attachments", [])
        if _t_attachments:
            try:
                from bot.teams_files import download_teams_attachment
                _t_att = _t_attachments[0]  # Handle first attachment
                _t_service_url = parsed.get("teams_service_url", "")
                _t_bot_token = ""
                if teams_client:
                    _t_bot_token = await teams_client._get_bot_token()
                _t_path = await download_teams_attachment(
                    _t_att, _t_service_url, _t_bot_token,
                )
                if _t_path:
                    _t_fname = Path(_t_path).name
                    _t_size = Path(_t_path).stat().st_size if Path(_t_path).exists() else 0
                    _t_ctype = _t_att.get("contentType", "")
                    _t_mtype = "document"
                    if _t_ctype.startswith("image/"): _t_mtype = "photo"
                    elif _t_ctype.startswith("audio/"): _t_mtype = "audio"
                    elif _t_ctype.startswith("video/"): _t_mtype = "video"
                    _teams_media_info = {
                        "media_type": _t_mtype,
                        "local_path": _t_path,
                        "filename": _t_fname,
                        "file_size": _t_size,
                        "content_type": _t_ctype,
                        "mime_type": _t_ctype,  # alias for media cache compat
                    }
                    log.info("Teams media downloaded: %s (%s, %d bytes)", _t_fname, _t_mtype, _t_size)
            except Exception as e:
                log.warning("Teams media download failed (non-blocking): %s", e)

    # Sync voice transcription + large file description (same as TG)
    if _teams_media_info and _teams_media_info.get("local_path"):
        if _teams_media_info.get("media_type") in ("voice", "audio"):
            try:
                from integrations.gemini import transcribe_voice
                _t_result = await transcribe_voice(_teams_media_info["local_path"])
                if _t_result.get("transcript"):
                    _teams_media_info["transcript"] = _t_result["transcript"]
            except Exception as e:
                log.warning("Teams voice transcription failed: %s", e)
        else:
            from config import MEDIA_INLINE_READ_MAX_KB, MEDIA_DESCRIBE_TIMEOUT_S
            _t_fsize = _teams_media_info.get("file_size", 0)
            if _t_fsize > MEDIA_INLINE_READ_MAX_KB * 1024:
                try:
                    from integrations.gemini import describe_media
                    _t_desc = await asyncio.wait_for(
                        describe_media(_teams_media_info["local_path"]),
                        timeout=MEDIA_DESCRIBE_TIMEOUT_S,
                    )
                    if _t_desc.get("description"):
                        _teams_media_info["description"] = _t_desc["description"]
                except Exception as e:
                    _teams_media_info["description"] = f"[File: {(_teams_media_info.get('filename') or 'unknown').replace('[', '(').replace(']', ')')} — description unavailable]"
                    log.warning("Teams large file description failed: %s", e)
    set_reply_threading(
        msg_id=message_id if message_id else None,
        task_id=task.task_id,
        chat_id=str(chat_id),
        session_key=teams_session_key,
        is_external_chat=_teams_is_external,
    )

    # Set coding sandbox if harness has sandbox=True
    _set_sandbox_for_session(teams_session_key)

    # Audit: log incoming message (H4 fix)
    try:
        from bot.audit import log_event
        asyncio.create_task(log_event(
            user_id=user_id,
            action="message",
            user_name=user_name,
            conversation_id=str(chat_id),
            conversation_type=parsed.get("teams_conversation_type", ""),
            details=f"Teams message: {text[:100]}",
        ))
    except Exception as _exc:
        log.debug("Suppressed: %s", _exc)

    # Copy media to sandbox (if applicable) — mirrors TG line 519
    if _teams_media_info:
        text = _copy_media_to_sandbox(_teams_media_info, text, teams_session_key)

    response_text = ""
    try:
        response_text = await process_incoming(
            source="teams",
            user_name=user_name,
            user_id=user_id,
            text=text,
            message_id=message_id,
            media_info=_teams_media_info,
            on_stream_chunk=on_stream_chunk,
            on_thinking_start=on_thinking_start,
            on_tool_status=on_tool_status,
            chat_id=str(chat_id),
            model=parsed.get("model", get_model_primary()),
            task_id="",  # always main session
            other_tasks_context=other_ctx,
            is_external_chat=_teams_is_external,
        )
    except Exception as e:
        log.error("Teams handler error: %s", e, exc_info=True)
        # Try to send error message
        if teams_client and chat_id:
            try:
                # Generic error — never expose internal details to Teams users
                await teams_client.send_message(
                    chat_id,
                    "\u26a0\ufe0f Something went wrong. Please try again.",
                    text_format="plain",
                )
            except Exception as _exc:
                log.debug("Suppressed: %s", _exc)
    finally:
        # Clean up placeholder
        if teams_placeholder_id and teams_client and chat_id:
            if task.reply_sent:
                # Final reply was sent via MCP tool — delete placeholder
                try:
                    await teams_client.delete_activity(
                        chat_id, teams_placeholder_id,
                    )
                except Exception as e:
                    log.debug("Teams: placeholder delete failed: %s", e)
            elif response_text:
                # Fallback: model didn't call teams_send_message but
                # produced response_text — update placeholder with it
                from bot.agent import strip_facts_update
                clean = strip_facts_update(response_text)
                if clean and clean.strip():
                    try:
                        await teams_client.update_message(
                            chat_id, teams_placeholder_id,
                            clean[:80000],
                            text_format="markdown",
                        )
                        task.reply_sent = True
                        # Store bot response
                        try:
                            from bot.storage.memory import store_message
                            await store_message(
                                "teams", "Bot", clean, "assistant",
                                chat_id=str(chat_id),
                            )
                        except Exception as _exc:
                            log.debug("Suppressed: %s", _exc)
                    except Exception as e:
                        log.warning("Teams: fallback update failed: %s", e)
                else:
                    # Empty visible response (FACTS_UPDATE only) — show "Noted."
                    try:
                        await teams_client.update_message(
                            chat_id, teams_placeholder_id,
                            "\u2705 Noted.",
                            text_format="plain",
                        )
                        task.reply_sent = True
                    except Exception:
                        # Fallback: delete if update fails
                        try:
                            await teams_client.delete_activity(
                                chat_id, teams_placeholder_id,
                            )
                        except Exception as _exc:
                            log.debug("Suppressed: %s", _exc)
            else:
                # No response at all — delete placeholder
                try:
                    await teams_client.delete_activity(
                        chat_id, teams_placeholder_id,
                    )
                except Exception as _exc:
                    log.debug("Suppressed: %s", _exc)
        if _teams_media_info and _teams_media_info.get("local_path"):
            try:
                from bot.storage.memory import cache_media_file
                _t_cached = await cache_media_file(
                    source="teams",
                    chat_id=str(chat_id),
                    message_id=str(message_id),
                    sender_name=user_name,
                    media_type=_teams_media_info.get("media_type", "document"),
                    mime_type=_teams_media_info.get("mime_type", ""),
                    file_name=_teams_media_info.get("filename", ""),
                    file_size=_teams_media_info.get("file_size", 0),
                    local_path=_teams_media_info["local_path"],
                    file_id="",  # Teams has no file_id equivalent
                    description=_teams_media_info.get("description") or _teams_media_info.get("transcript") or "",
                )
                if _t_cached:
                    log.debug("Teams media cached: %s", _t_cached)
            except Exception as e:
                log.debug("Teams media cache failed: %s", e)
            # Cleanup temp file
            try:
                Path(_teams_media_info["local_path"]).unlink(missing_ok=True)
            except Exception as _exc:
                log.debug("Suppressed: %s", _exc)
