# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Employee workstation control MCP tool — manages Claude Code sessions on employee Macs via SSH+Tailscale.

Extracted from bot/mcp_tools.py (Phase 1B refactor). Pure extraction — no behavior changes.
"""

import asyncio
import logging
from typing import Any

from claude_agent_sdk import tool

from bot.mcp import _safe_int, _to_bool, _text_response

log = logging.getLogger(__name__)


# ============================================================
# EMPLOYEE WORKSTATION CONTROL TOOL
# ============================================================

@tool(
    name="desktop_relay",
    description="Control Claude Code sessions on employee Mac workstations via SSH+Tailscale. "
    "Actions: ping (health check), auth (TOTP login), status (full dashboard + linked sessions), "
    "lock (revoke session), "
    "link (map channel\u2192session+project), unlink (remove mapping), list_links (show all), get_link (lookup one channel), "
    "list_all_jsonls (enumerate all JSONL sessions across all employee Mac projects, sorted newest-first \u2014 for picking workstation targets), "
    "create_relay_group (create TG workstation channel linked to employee Mac session via SDK daemon), "
    "delete_relay_group (clean up workstation channel: unlink, disable, leave TG group), "
    "daemons (list persistent SDK daemons on Mac + bot-side pool health), "
    "kill_daemon (force-kill a specific chat's Mac daemon, release pool client, and eagerly respawn), "
    "reap_orphans (sweep dead daemon pidfiles on Mac), install_skills (push mac-skills/ to Mac — requires Mac-side approval via approve-install.sh), "
    "install_daemon (push updated daemon to Mac — requires Mac-side approval; auto-respawn on approval), "
    "install_status (check pending install status on Mac + auto-respawn daemons if approved). "
    "Requires TOTP authentication before most commands.",
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["ping", "auth", "status", "lock",
                         "list_all_jsonls",
                         "link", "unlink", "list_links", "get_link",
                         "set_relay_mode", "create_relay_group", "delete_relay_group",
                         "daemons", "kill_daemon", "reap_orphans", "install_skills", "install_daemon", "install_status",
                         "promote_account_token", "list_accounts", "set_chat_account"],
                "description": "Action to perform",
            },
            "totp_code": {
                "type": "string",
                "description": "6-digit TOTP code (required for 'auth' action)",
            },
            "session_name": {
                "type": "string",
                "description": "Session name for link action (relay_ prefix auto-added)",
            },
            "chat_id": {
                "type": "string",
                "description": "Chat ID for link/unlink actions (from session metadata)",
            },
            "project_path": {
                "type": "string",
                "description": "Mac project path for link action (e.g. ~/projects/myproject)",
            },
            "account_label": {
                "type": "string",
                "description": "OAuth account label for multi-account support. Used by promote_account_token, list_accounts, set_chat_account. Matches ^[a-z0-9_-]{1,32}$.",
            },
            "enabled": {
                "type": "boolean",
                "description": "Enable/disable relay_mode (for set_relay_mode action)",
            },
            "group_title": {
                "type": "string",
                "description": "TG group title (for create_relay_group action, e.g. '\U0001f5a5 tao-diet')",
            },
            "pinned_uuid": {
                "type": "string",
                "description": "Optional: specific JSONL uuid (or prefix \u22654 hex chars) to pin for create_relay_group. If omitted, newest JSONL for the project is auto-pinned.",
            },
        },
        "required": ["action"],
    },
)
async def desktop_relay_tool(args: dict[str, Any]) -> dict:
    """Employee workstation control MCP tool — manages employee Mac sessions via SSH."""
    from bot import desktop_relay

    if not desktop_relay.is_enabled():
        return _text_response({"error": "Employee workstation control is disabled. Set DESKTOP_ENABLED=true in .env"})

    action = args["action"]
    _sk = args.pop("_session_key", "")

    try:
        if action == "ping":
            result = await desktop_relay.ping()
            return _text_response(result)

        elif action == "auth":
            code = args.get("totp_code", "")
            if not code or len(code) != 6:
                return _text_response({"error": "totp_code must be a 6-digit code"})
            result = await desktop_relay.authenticate(code)
            return _text_response(result)

        elif action == "status":
            result = await desktop_relay.status()
            return _text_response(result)

        elif action == "lock":
            result = await desktop_relay.lock()
            return _text_response(result)

        elif action == "list_all_jsonls":
            # Enumerate all JSONL sessions across all Mac projects, sorted
            # newest-first. Used from admin DM to pick a relay target.
            jsonls = await desktop_relay.list_all_jsonls(limit=30)
            # Group by project for friendlier display.
            by_project: dict[str, list[dict]] = {}
            for jl in jsonls:
                by_project.setdefault(jl["project_path"], []).append(jl)
            return _text_response({
                "success": bool(jsonls),
                "count": len(jsonls),
                "projects": list(by_project.keys()),
                "jsonls": jsonls,
            })

        elif action == "link":
            chat_id = args.get("chat_id", "")
            session_name = args.get("session_name", "")
            project_path = args.get("project_path", "")
            if not chat_id or not session_name or not project_path:
                return _text_response({"error": "chat_id, session_name, and project_path are required"})
            # Detect source platform from session key (CB-132: explicit detection)
            _relay_source = _sk.split(":")[0] if ":" in _sk else ""
            if _relay_source not in ("telegram", "teams"):
                from config import ROOT_ADMIN
                _relay_source = ROOT_ADMIN[0] or "telegram"
            # CB-140: Pass user_id for per-user workstation config lookup
            # Extract from session key (format: "source:user_id")
            _link_uid_key = _sk if ":" in _sk else ""
            result = await desktop_relay.link_session(chat_id, session_name, project_path,
                                                       source=_relay_source, user_id=_link_uid_key)
            if not result.get("success"):
                return _text_response(result)

            # Enable relay_mode + init offset (matches create_relay_group)
            relay_r = await desktop_relay.set_relay_mode(str(chat_id), True)
            result["relay_mode"] = relay_r.get("success", False)
            await desktop_relay._update_synced_offset(str(chat_id), 0)

            # Auto-set account_label to bot's default OAuth label if not provided.
            # Without this, daemon spawns without a token and Claude CLI says "not logged in".
            acct_label = (args.get("account_label") or "").strip()
            if not acct_label:
                from config_overrides import get_default_oauth_label
                acct_label = get_default_oauth_label()
            acct_r = await desktop_relay.set_chat_account(str(chat_id), acct_label)
            result["account_label"] = acct_label
            result["account_set"] = acct_r.get("success", False)

            # Pin JSONL: explicit uuid, prefix match, or auto-newest
            pinned_uuid_arg = (args.get("pinned_uuid") or "").strip()
            auto_pin_note = ""
            chosen_uuid = ""
            try:
                jsonls = await desktop_relay.list_project_jsonls(
                    project_path, limit=20)
            except Exception as _list_err:
                jsonls = []
                auto_pin_note = f"list_jsonls failed: {_list_err}"
            if pinned_uuid_arg:
                matches = [j for j in jsonls
                           if j["uuid"].startswith(pinned_uuid_arg)]
                if len(matches) == 1:
                    chosen_uuid = matches[0]["uuid"]
                elif len(matches) > 1:
                    auto_pin_note = f"pinned_uuid ambiguous ({len(matches)} matches)"
                else:
                    auto_pin_note = f"pinned_uuid not found: {pinned_uuid_arg}"
            elif jsonls:
                chosen_uuid = jsonls[0]["uuid"]
                auto_pin_note = "auto-pinned newest"
            else:
                auto_pin_note = ("no JSONLs found in project (create one in "
                                 "Desktop Claude.app first, then /session pin)")

            actually_pinned = False
            if chosen_uuid:
                try:
                    pin_res = await desktop_relay.update_pinned_jsonl(
                        str(chat_id), chosen_uuid)
                    if pin_res.get("success"):
                        result["pinned"] = pin_res.get("new_path")
                        actually_pinned = True
                    else:
                        auto_pin_note = f"pin failed: {pin_res.get('error')}"
                except Exception as _pin_err:
                    auto_pin_note = f"pin exception: {_pin_err}"
            result["pin_note"] = auto_pin_note

            # Eager-spawn daemon so catchup fires immediately
            # Only spawn if pin actually succeeded (matches create_relay_group)
            if actually_pinned and chosen_uuid:
                cid_str = str(chat_id)
                spawn_uuid = chosen_uuid
                spawn_cwd = project_path

                async def _eager_link_spawn() -> None:
                    try:
                        from bot.relay.pool import get_pool
                        client = await get_pool().acquire(
                            cid_str, spawn_uuid, spawn_cwd)
                        await client.ensure_ready()
                        log.info("Link eager-spawn ok chat=%s uuid=%s",
                                 cid_str, spawn_uuid[:8])
                    except Exception as e:
                        log.warning("Link eager-spawn failed chat=%s: %s",
                                    cid_str, e)

                asyncio.create_task(_eager_link_spawn())
                result["daemon_spawn"] = "started"
            else:
                result["daemon_spawn"] = "skipped (no pinned uuid)"

            return _text_response(result)

        elif action == "unlink":
            chat_id = args.get("chat_id", "")
            if not chat_id:
                return _text_response({"error": "chat_id is required"})
            result = await desktop_relay.unlink_session(chat_id)
            return _text_response(result)

        elif action == "list_links":
            links = await desktop_relay.get_all_links()
            return _text_response({"links": links, "count": len(links)})

        elif action == "get_link":
            chat_id = args.get("chat_id", "")
            if not chat_id:
                return _text_response({"error": "chat_id is required"})
            entry = await desktop_relay.get_linked_session(chat_id)
            if entry:
                return _text_response({"linked": True, **entry})
            return _text_response({"linked": False, "chat_id": chat_id})

        elif action == "set_relay_mode":
            chat_id = args.get("chat_id", "")
            if not chat_id:
                return _text_response({"error": "chat_id is required"})
            enabled = _to_bool(args.get("enabled"), True)
            if not enabled:
                # Cancel any active relay polling task for this chat
                _relay_task = desktop_relay._relay_tasks.pop(chat_id, None)
                if _relay_task and not _relay_task.done():
                    _relay_task.cancel()
                    log.info("Relay task cancelled for chat=%s (set_relay_mode disabled)", chat_id)
            result = await desktop_relay.set_relay_mode(chat_id, enabled)
            return _text_response(result)

        elif action == "create_relay_group":
            # Phase 5A: create TG group, register as external chat,
            # set active_plus + relay_mode, link to Mac session
            session_name = args.get("session_name", "")
            project_path = args.get("project_path", "")
            group_title = args.get("group_title", "")
            if not session_name or not project_path:
                return _text_response({"error": "session_name and project_path are required"})
            if not group_title:
                # Derive from project path: ~/dev/tao-diet → "🖥 tao-diet"
                group_title = f"\U0001f5a5 {project_path.rstrip('/').split('/')[-1]}"

            # 1. Create TG group with requesting user only
            from integrations.telegram import create_group
            raw_ids = str(args.get("user_ids", "")).strip()
            if raw_ids:
                member_ids = [_safe_int(uid.strip(), 0) for uid in raw_ids.split(",") if uid.strip()]
                member_ids = [uid for uid in member_ids if uid != 0]
            else:
                from config import TG_ADMIN_CHAT_IDS
                member_ids = TG_ADMIN_CHAT_IDS[:1]
            result = await create_group(group_title, member_ids)
            if not result.get("success"):
                return _text_response({"error": f"Failed to create group: {result.get('error')}"})
            new_chat_id = result["chat_id"]

            # 2. Register in unified chat registry
            import bot.chat_registry as _cr
            _relay_chat_key = f"telegram:{new_chat_id}"
            if not _cr.is_registered(_relay_chat_key):
                _cr.register(_relay_chat_key, title=group_title, mode="active_plus",
                             relay_project=project_path)
            else:
                log.warning("Relay create_group: chat %s already registered, skipping", new_chat_id)

            # 3. Auto-pin latest JSONL for the project (newest by mtime) so
            # the first TG query has a target. User can override via
            # /session pin <N|uuid> later. Optional pinned_uuid arg accepts
            # an explicit uuid or uuid-prefix (>=4 hex chars, must match
            # uniquely) to override auto-selection.
            pinned_uuid_arg = (args.get("pinned_uuid") or "").strip()
            auto_pinned: str | None = None
            auto_pin_note = ""
            try:
                jsonls = await desktop_relay.list_project_jsonls(
                    project_path, limit=20)
            except Exception as _list_err:
                jsonls = []
                auto_pin_note = f"list_jsonls failed: {_list_err}"
            chosen_uuid = ""
            if pinned_uuid_arg:
                matches = [j for j in jsonls
                           if j["uuid"].startswith(pinned_uuid_arg)]
                if len(matches) == 1:
                    chosen_uuid = matches[0]["uuid"]
                elif len(matches) > 1:
                    auto_pin_note = (f"pinned_uuid ambiguous "
                                     f"({len(matches)} matches)")
                else:
                    auto_pin_note = (f"pinned_uuid not found: "
                                     f"{pinned_uuid_arg}")
            elif jsonls:
                chosen_uuid = jsonls[0]["uuid"]
            else:
                auto_pin_note = ("no JSONLs found in project (create one in "
                                 "Desktop Claude.app first, then /session pin)")

            # 5. Register link + enable relay_mode. Pin happens after link
            # so the registry entry exists for update_pinned_jsonl to update.
            _create_source = _sk.split(":")[0] if ":" in _sk else ""
            if _create_source not in ("telegram", "teams"):
                from config import ROOT_ADMIN
                _create_source = ROOT_ADMIN[0] or "telegram"
            # CB-140: Pass user_id from session key for per-user workstation
            _create_uid = _sk.split(":", 1)[1] if ":" in _sk else ""
            _create_uid_key = f"{_create_source}:{_create_uid}" if _create_uid else ""
            link_result = await desktop_relay.link_session(
                str(new_chat_id), session_name, project_path,
                pinned_jsonl_path="",
                source=_create_source,
                user_id=_create_uid_key,
            )
            relay_result = await desktop_relay.set_relay_mode(
                str(new_chat_id), True,
            )
            await desktop_relay._update_synced_offset(str(new_chat_id), 0)

            if chosen_uuid:
                try:
                    pin_res = await desktop_relay.update_pinned_jsonl(
                        str(new_chat_id), chosen_uuid)
                    if pin_res.get("success"):
                        auto_pinned = pin_res.get("new_path")
                    else:
                        auto_pin_note = (f"pin failed: "
                                         f"{pin_res.get('error')}")
                except Exception as _pin_err:
                    auto_pin_note = f"pin exception: {_pin_err}"

            # 6. Eager-spawn SDK daemon so catchup digest fires NOW instead of
            # waiting for the first user message. Fire-and-forget: SSH +
            # daemon startup takes ~5-15s — we return the MCP response
            # immediately. On failure the next user message retries through
            # the normal lazy-spawn path.
            eager_spawn_note = "skipped (no pinned uuid)"
            if auto_pinned and chosen_uuid:
                chat_id_str = str(new_chat_id)
                pinned_uuid = chosen_uuid
                cwd_for_spawn = project_path

                async def _eager_spawn() -> None:
                    try:
                        from bot.relay.pool import get_pool
                        client = await get_pool().acquire(
                            chat_id_str, pinned_uuid, cwd_for_spawn)
                        await client.ensure_ready()
                        log.info(
                            "Relay eager-spawn ok chat=%s uuid=%s",
                            chat_id_str, pinned_uuid[:8])
                    except Exception as e:
                        log.warning(
                            "Relay eager-spawn failed chat=%s: %s",
                            chat_id_str, e)

                asyncio.create_task(_eager_spawn())
                eager_spawn_note = ("spawning (catchup digest will arrive "
                                    "in ~5-15s)")

            return _text_response({
                "success": True,
                "chat_id": new_chat_id,
                "title": group_title,
                "session_name": session_name,
                "project_path": project_path,
                "relay_mode": True,
                "mode": "active_plus",
                "auto_pinned": auto_pinned,
                "auto_pin_note": auto_pin_note,
                "eager_spawn": eager_spawn_note,
                "link": link_result,
                "relay": relay_result,
            })

        elif action == "delete_relay_group":
            # Clean up a relay group: unlink, disable relay_mode, kill daemon,
            # remove from external chats, leave TG group.
            cid = args.get("chat_id", "")
            if not cid:
                return _text_response({"error": "chat_id is required"})

            results = {}
            # 0. Cancel active relay polling task (prevent zombie)
            _relay_task = desktop_relay._relay_tasks.pop(str(cid), None)
            if _relay_task and not _relay_task.done():
                _relay_task.cancel()
                log.info("Relay task cancelled for chat=%s (delete_relay_group)", cid)
            # 1. Disable relay mode + unlink from registry
            try:
                await desktop_relay.set_relay_mode(str(cid), False)
                results["relay_mode"] = "disabled"
            except Exception as e:
                results["relay_mode_error"] = str(e)
            try:
                unlink_r = await desktop_relay.unlink_session(str(cid))
                results["unlink"] = unlink_r
            except Exception as e:
                results["unlink_error"] = str(e)

            # 2. Remove from chat registry
            import bot.chat_registry as _cr
            _del_key = f"telegram:{cid}"
            if _cr.unregister(_del_key):
                results["chat_registry"] = "removed"
            else:
                results["chat_registry"] = "not found"
            int_cid = int(cid) if str(cid).lstrip("-").isdigit() else cid

            # 2b. Release bot-side pool client (prevent stale /relays entry)
            try:
                from bot.relay.pool import get_pool
                await get_pool().release(str(cid))
                results["pool_released"] = True
            except Exception as _pe:
                log.debug("delete_relay_group: pool.release chat=%s: %s", cid, _pe)

            # 2c. Clear desktop_turn dedup state for this chat
            from bot.relay.ux import clear_desktop_turn_dedup
            clear_desktop_turn_dedup(str(cid))

            # 3. Leave the TG group
            try:
                from integrations.telegram import send_message as tg_send
                await tg_send(
                    "\U0001f44b Relay group deactivated. This chat is no longer linked to a Mac session.",
                    chat_id=int_cid, parse_mode="html",
                )
                from integrations.telegram import get_client
                client = await get_client()
                await client.leave_chat(int_cid)
                results["tg_group"] = "left"
            except Exception as e:
                results["tg_group_error"] = str(e)

            return _text_response({"success": True, "chat_id": cid, **results})

        elif action == "daemons":
            # Merge Mac-side daemon list with bot-side pool health.
            mac = await desktop_relay.list_daemons()
            try:
                from bot.relay.pool import get_pool
                pool_health = get_pool().health_all()
            except Exception as e:
                pool_health = [{"error": f"pool_unavailable:{e}"}]
            return _text_response({
                "success": mac.get("ok", False),
                "mac_daemons": mac.get("daemons", []),
                "mac_raw": mac.get("raw", ""),
                "pool_clients": pool_health,
            })

        elif action in ("kill_daemon", "reset_daemon"):
            cid = args.get("chat_id", "").strip()
            if not cid:
                return _text_response({"error": "chat_id is required"})
            mac_res = await desktop_relay.kill_daemon(cid)
            # Release bot-side pool client
            pool_released = False
            try:
                from bot.relay.pool import get_pool
                await get_pool().release(cid)
                pool_released = True
            except Exception as e:
                log.warning("pool.release failed chat=%s: %s", cid, e)
            # Eagerly respawn daemon after kill
            respawned = False
            if mac_res.get("ok"):
                try:
                    from bot.relay.pool import get_pool as _gp
                    pool = _gp()
                    entry = desktop_relay.get_relay_mode(cid)
                    if entry:
                        uuid = entry.get("pinned_jsonl_path", "").replace(".jsonl", "").split("/")[-1]
                        cwd = entry.get("project_path", "/tmp")
                        client = await pool.acquire(cid, uuid, cwd)
                        await client.ensure_ready()
                        respawned = True
                except Exception as e:
                    log.warning("daemon respawn failed chat=%s: %s", cid, e)
            return _text_response({
                "success": mac_res.get("ok", False),
                "chat_id": cid,
                "mac": mac_res,
                "pool_released": pool_released,
                "respawned": respawned,
            })

        elif action == "install_skills":
            res = await desktop_relay.install_mac_skills()
            if res.get("pending_approval"):
                return _text_response({
                    "success": False,
                    "pending_approval": True,
                    "type": "skills",
                    "sha256": res.get("sha256", ""),
                    "message": "Skills staged on Mac — requires Mac-side approval. "
                               "Run ~/.relay/approve-install.sh on Mac terminal to review and approve. "
                               "Use install_status action to check status after approval.",
                })
            # Legacy direct-install response (old guard.sh without approval gate)
            return _text_response({
                "success": res.get("ok", False),
                "output": res.get("output", ""),
                "stderr": res.get("stderr", ""),
                "error": res.get("error"),
                "rc": res.get("rc"),
            })

        elif action == "install_daemon":
            res = await desktop_relay.install_mac_daemon()
            if res.get("pending_approval"):
                return _text_response({
                    "success": False,
                    "pending_approval": True,
                    "type": "daemon",
                    "sha256": res.get("sha256", ""),
                    "size": res.get("size"),
                    "message": "Daemon staged on Mac — requires Mac-side approval. "
                               "Run ~/.relay/approve-install.sh on Mac terminal to review and approve. "
                               "After approval, use install_status action to trigger daemon respawn.",
                })
            # Legacy direct-install (old guard.sh) — auto-respawn daemons
            daemon_results = []
            if res.get("ok"):
                daemon_results = await _respawn_all_relay_daemons()
            return _text_response({
                "success": res.get("ok", False),
                "output": res.get("output", ""),
                "size": res.get("size"),
                "sha256": res.get("sha256"),
                "error": res.get("error"),
                "rc": res.get("rc"),
                "daemons_restarted": daemon_results,
            })

        elif action == "install_status":
            # Check pending installs on Mac + auto-respawn daemons if approved.
            res = await desktop_relay.check_install_status()
            if not res.get("ok"):
                return _text_response({
                    "success": False,
                    "error": res.get("error", "unknown"),
                    "raw": res.get("raw", ""),
                    "message": "relay-install-status not supported — guard.sh may need updating.",
                })
            pending = res.get("pending", [])
            # If no pending daemon install, check if daemons are dead and respawn
            daemon_pending = any(p["type"] == "daemon" for p in pending)
            respawn_results = []
            if not daemon_pending:
                # No pending daemon = either approved or never pushed.
                # Check for dead daemons and respawn them.
                respawn_results = await _respawn_dead_relay_daemons()
            return _text_response({
                "success": True,
                "pending": pending,
                "raw": res.get("raw", ""),
                "daemons_respawned": respawn_results,
            })

        elif action == "reap_orphans":
            res = await desktop_relay.reap_orphans()
            return _text_response({
                "success": res.get("ok", False),
                "reaped_count": res.get("reaped_count", 0),
                "raw": res.get("raw", ""),
            })

        elif action == "promote_account_token":
            # Multi-account OAuth: rename the freshly-written default token
            # (~/.relay/oauth_token) into a labeled slot (~/.relay/oauth_token.<label>).
            # Typically invoked after a successful /mac-reauth flow.
            label = (args.get("account_label") or "").strip()
            if not label:
                return _text_response({"error": "account_label is required"})
            result = await desktop_relay.promote_oauth_token(label)
            return _text_response(result)

        elif action == "list_accounts":
            # Enumerate account labels present on Mac (filenames only, no
            # token values ever returned).
            result = await desktop_relay.list_mac_accounts()
            return _text_response(result)

        elif action == "set_chat_account":
            # Bind a relay chat to a named OAuth account (stored in
            # relay_sessions.json). Next daemon spawn for this chat uses
            # oauth_token.<label> via relay-sdk-persistent's 4th arg.
            chat_id = (args.get("chat_id") or "").strip()
            label = (args.get("account_label") or "").strip()
            if not chat_id or not label:
                return _text_response({"error": "chat_id and account_label required"})
            result = await desktop_relay.set_chat_account(chat_id, label)
            return _text_response(result)

        else:
            return _text_response({"error": f"Unknown action: {action}"})

    except Exception as e:
        log.error("desktop_relay_tool error: %s", e, exc_info=True)
        return _text_response({"error": str(e)})


async def _respawn_all_relay_daemons() -> list[dict]:
    """Kill + respawn all relay daemons. Used after a direct (legacy) install."""
    import bot.relay.core as desktop_relay  # noqa: PLC0415
    from bot.relay.registry import _load_registry  # noqa: PLC0415
    from bot.relay.sessions import kill_daemon as _kill_daemon  # noqa: PLC0415
    from bot.relay.pool import get_pool as _dp_pool  # noqa: PLC0415

    pool = _dp_pool()
    registry = _load_registry()
    results: list[dict] = []
    _RESPAWN_STAGGER_S = 3.0
    count = 0
    for cid, entry in registry.items():
        if not entry.get("relay_mode"):
            continue
        if count > 0:
            await asyncio.sleep(_RESPAWN_STAGGER_S)
        try:
            kill_r = await _kill_daemon(cid)
            await pool.release(cid)
            respawn_r = await desktop_relay.eager_respawn_daemon(cid)
            results.append({
                "chat_id": cid,
                "killed": kill_r.get("ok", False),
                "respawned": respawn_r.get("ok", False),
            })
        except Exception as e:
            results.append({"chat_id": cid, "error": str(e)})
        count += 1
    return results


async def _respawn_dead_relay_daemons() -> list[dict]:
    """Check all relay daemons; respawn any that are DEAD.

    Called from install_status after approval — ensures daemons come back
    immediately (always-live, not lazy).
    """
    import bot.relay.core as desktop_relay  # noqa: PLC0415
    from bot.relay.registry import _load_registry  # noqa: PLC0415
    from bot.relay.ssh import execute as _ssh_exec  # noqa: PLC0415
    from bot.relay.pool import get_pool as _dp_pool  # noqa: PLC0415

    pool = _dp_pool()
    registry = _load_registry()
    results: list[dict] = []
    _RESPAWN_STAGGER_S = 3.0
    count = 0
    for cid, entry in registry.items():
        if not entry.get("relay_mode"):
            continue
        # Check daemon health via SSH relay-ping-daemon
        try:
            _ping_ok, ping_out = await _ssh_exec(f"relay-ping-daemon {cid}")
            ping_out = ping_out.strip()
            if ping_out.startswith("ALIVE"):
                status = "ALIVE"
            elif ping_out.startswith("STALE"):
                status = "STALE"
            elif ping_out.startswith("DEAD"):
                status = "DEAD"
            elif ping_out.startswith("MISSING"):
                status = "MISSING"
            else:
                status = "unknown"
        except Exception:
            status = "unknown"
        if status in ("DEAD", "MISSING", "unknown"):
            if count > 0:
                await asyncio.sleep(_RESPAWN_STAGGER_S)
            try:
                await pool.release(cid)
                respawn_r = await desktop_relay.eager_respawn_daemon(cid)
                results.append({
                    "chat_id": cid,
                    "was_status": status,
                    "respawned": respawn_r.get("ok", False),
                })
            except Exception as e:
                results.append({"chat_id": cid, "error": str(e)})
            count += 1
    return results


__all__ = ["desktop_relay_tool"]
