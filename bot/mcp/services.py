# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Services MCP tools — phone, image gen, flights, model/effort/oauth, scheduling, harness, external chats.

Extracted from bot/mcp_tools.py (Phase 1B refactor). Pure extraction — no behavior changes.
"""

import asyncio
import json
import logging
import os
import re
from typing import Any

from claude_agent_sdk import tool

from bot.mcp import _safe_int, _to_bool, _text_response, _run_with_timeout

log = logging.getLogger(__name__)


# ============================================================
# IMAGE GENERATION (Gemini Imagen 4.0)
# ============================================================

@tool("generate_image", "Generate a NEW image from a text prompt using Imagen 4.0. Returns a file path. Cannot include real people.", {
    "prompt": str, "filename": str,
})
async def generate_image(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.gemini import generate_image as _gen
        result = await _gen(
            args["prompt"],
            filename=args.get("filename", "generated.png"),
        )
        if "error" in result:
            return result
        return {"success": True, "file_path": result["file_path"]}
    return await _run_with_timeout(_do(), "generate_image")


@tool("edit_image", "Edit an EXISTING image using Gemini. Send an image + text instructions describing the edit. Use for: style changes, adding/removing objects, background swap, color correction, etc.", {
    "image_path": str, "prompt": str, "filename": str, "use_pro": bool,
})
async def edit_image(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.gemini import edit_image as _edit
        result = await _edit(
            image_path=args["image_path"],
            prompt=args["prompt"],
            filename=args.get("filename", "edited.png"),
            use_pro=args.get("use_pro", False),
        )
        if "error" in result:
            return result
        return {"success": True, "file_path": result["file_path"]}
    return await _run_with_timeout(_do(), "edit_image")


# ============================================================
# GMAIL ATTACHMENT DOWNLOAD (Google API — direct integration)
# ============================================================

@tool("download_gmail_attachment", "Download a Gmail attachment and save to local disk. Returns the file path for sending via telegram_send_document. No Bash needed.", {
    "message_id": str, "attachment_id": str, "filename": str, "user_google_email": str,
})
async def download_gmail_attachment(args: dict[str, Any]) -> dict:
    _MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024  # 50 MB

    async def _do():
        import base64
        import time as _time
        from pathlib import Path

        args.pop("_session_key", None)

        message_id = args.get("message_id", "").strip()
        attachment_id = args.get("attachment_id", "").strip()
        filename = args.get("filename", "attachment").strip()
        email = args.get("user_google_email", "").strip()

        if not message_id or not attachment_id:
            return {"error": "message_id and attachment_id are required"}

        # Resolve credentials directory
        creds_dir = os.environ.get(
            "WORKSPACE_MCP_CREDENTIALS_DIR",
            "/app/data/google-workspace-creds",
        )
        creds_path = Path(creds_dir).resolve()

        # Find the right credential file
        if email:
            cred_file = (creds_path / f"{email}.json").resolve()
            # H2 fix: path traversal protection
            if not cred_file.is_relative_to(creds_path):
                return {"error": f"Invalid email: {email}"}
        else:
            # Default to first available credential
            cred_files = sorted(creds_path.glob("*.json"))
            if not cred_files:
                return {"error": "No Google credentials configured"}
            cred_file = cred_files[0]
            email = cred_file.stem

        if not cred_file.exists():
            return {"error": f"No credentials for {email}"}

        # M1 fix: offload blocking Google API calls to a thread
        def _sync_download() -> tuple[bytes, str]:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            cred_data = json.loads(cred_file.read_text())
            creds = Credentials(
                token=cred_data.get("token"),
                refresh_token=cred_data.get("refresh_token"),
                token_uri=cred_data.get("token_uri", "https://oauth2.googleapis.com/token"),
                client_id=cred_data.get("client_id"),
                client_secret=cred_data.get("client_secret"),
                scopes=cred_data.get("scopes"),
            )
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())

            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            att = service.users().messages().attachments().get(
                userId="me", messageId=message_id, id=attachment_id,
            ).execute()

            data_b64 = att.get("data", "")
            if not data_b64:
                raise ValueError("Attachment data is empty")

            raw = base64.urlsafe_b64decode(data_b64)
            return raw, email

        raw_bytes, email = await asyncio.to_thread(_sync_download)

        # M2 fix: size limit
        if len(raw_bytes) > _MAX_ATTACHMENT_BYTES:
            return {"error": f"Attachment too large ({len(raw_bytes)} bytes, max {_MAX_ATTACHMENT_BYTES})"}

        # Sanitize filename and add timestamp prefix to avoid collisions (L1 fix)
        safe_name = re.sub(r'[^\w\-.]', '_', filename)
        if not safe_name or safe_name.startswith("."):
            safe_name = "attachment"

        from config import TMP_DIR
        stem = Path(safe_name).stem
        suffix = Path(safe_name).suffix or ""
        ts = str(int(_time.time()))
        out_path = TMP_DIR / f"{stem}_{ts}{suffix}"

        out_path.write_bytes(raw_bytes)
        size = len(raw_bytes)
        log.info("download_gmail_attachment: saved %s (%d bytes) from %s", out_path, size, email)

        return {
            "success": True,
            "file_path": str(out_path),
            "filename": safe_name,
            "size_bytes": size,
            "email_account": email,
        }

    return await _run_with_timeout(_do(), "download_gmail_attachment", timeout=60)


# ============================================================
# GOOGLE FLIGHTS (fli library — direct integration)
# ============================================================
# The fli MCP server (fli-mcp) is broken with fastmcp>=3.0 (_tool_manager removed).
# These tools call fli's search engine directly, bypassing the broken MCP layer.
# Provides: google_flights_search (specific date) + google_flights_search_dates (cheapest dates).
# Concurrency cap: Google rate-limits at ~9 concurrent requests; semaphore caps at 8.
_GFLIGHTS_SEM = asyncio.Semaphore(8)
# M8 fix: serialize currency env var mutation across concurrent searches
import threading as _threading
_GFLIGHTS_CURRENCY_LOCK = _threading.Lock()

@tool("google_flights_search", "Search Google Flights for a specific route and date. Returns flights with prices, airlines, durations. Use IN PARALLEL with Kiwi search-flight for every flight search.", {
    "type": "object",
    "properties": {
        "origin": {"type": "string", "description": "IATA airport code (e.g. LHR)"},
        "destination": {"type": "string", "description": "IATA airport code (e.g. SAN)"},
        "departure_date": {"type": "string", "description": "YYYY-MM-DD"},
        "return_date": {"type": "string", "description": "YYYY-MM-DD for round-trip, omit for one-way"},
        "cabin_class": {"type": "string", "description": "ECONOMY, PREMIUM_ECONOMY, BUSINESS, FIRST"},
        "max_stops": {"type": "integer", "description": "0=Any, 1=Nonstop, 2=1-stop, 3=2-stops"},
        "sort_by": {"type": "string", "description": "CHEAPEST, TOP_FLIGHTS, DEPARTURE_TIME, DURATION"},
        "adults": {"type": "integer", "description": "Number of adults (default 1)"},
        "children": {"type": "integer", "description": "Number of children (default 0)"},
        "max_results": {"type": "integer", "description": "Max flights to return (default 10)"},
        "currency": {"type": "string", "description": "GBP, EUR, USD etc. (default GBP)"},
    },
    "required": ["origin", "destination", "departure_date"],
})
async def google_flights_search(args: dict[str, Any]) -> dict:
    async def _do():
        async with _GFLIGHTS_SEM:
            return await _do_search()

    async def _do_search():
        from fli.search import SearchFlights
        from fli.models import FlightSearchFilters, PassengerInfo, TripType
        from fli.models.google_flights.base import FlightSegment
        from fli.core import (
            resolve_airport, parse_cabin_class, parse_max_stops, parse_sort_by,
        )

        origin = resolve_airport(args["origin"])
        dest = resolve_airport(args["destination"])
        dep_date = args["departure_date"]  # YYYY-MM-DD
        ret_date = args.get("return_date")

        cabin = parse_cabin_class(args.get("cabin_class", "ECONOMY"))
        stops = parse_max_stops(_safe_int(args.get("max_stops"), 0))  # 0=Any, 1=Nonstop, 2=1-stop, 3=2-stops
        sort = parse_sort_by(args.get("sort_by", "CHEAPEST"))
        adults = _safe_int(args.get("adults"), 1)
        children = _safe_int(args.get("children"), 0)
        max_results = _safe_int(args.get("max_results"), 10)
        currency = args.get("currency", "GBP")

        # Build segments
        segments = [FlightSegment(
            departure_airport=[[origin, 0]],
            arrival_airport=[[dest, 0]],
            travel_date=dep_date,
        )]
        trip_type = TripType.ONE_WAY
        if ret_date:
            trip_type = TripType.ROUND_TRIP
            segments.append(FlightSegment(
                departure_airport=[[dest, 0]],
                arrival_airport=[[origin, 0]],
                travel_date=ret_date,
            ))

        passengers = PassengerInfo(adults=adults, children=children)
        filters = FlightSearchFilters(
            trip_type=trip_type,
            passenger_info=passengers,
            flight_segments=segments,
            stops=stops,
            seat_type=cabin,
            sort_by=sort,
        )

        # fli reads currency from env at module level; serialize via lock
        # to prevent races when concurrent searches use different currencies.
        with _GFLIGHTS_CURRENCY_LOCK:
            os.environ["FLI_MCP_DEFAULT_CURRENCY"] = currency
            searcher = SearchFlights()
            results = searcher.search(filters, top_n=max_results)

        if not results:
            return {"flights": [], "message": "No flights found for this route/date."}

        flights = []
        for item in results:
            # Handle both one-way (single FlightResult) and round-trip (tuple)
            if isinstance(item, tuple):
                outbound, inbound = item
                flight_data = _format_flight_result(outbound)
                flight_data["return"] = _format_flight_result(inbound)
            else:
                flight_data = _format_flight_result(item)
            flights.append(flight_data)

        return {
            "flights": flights,
            "count": len(flights),
            "route": f"{args['origin']}\u2192{args['destination']}",
            "date": dep_date,
            "currency": currency,
            "source": "Google Flights (fli)",
        }

    return await _run_with_timeout(_do(), "google_flights_search", timeout=30)


@tool("google_flights_search_dates", "Find cheapest flight dates across a date range on Google Flights. Great for flexible travel planning.", {
    "type": "object",
    "properties": {
        "origin": {"type": "string", "description": "IATA airport code"},
        "destination": {"type": "string", "description": "IATA airport code"},
        "start_date": {"type": "string", "description": "Start of date range (YYYY-MM-DD)"},
        "end_date": {"type": "string", "description": "End of date range (YYYY-MM-DD)"},
        "trip_duration": {"type": "integer", "description": "Trip duration in days (for round-trips)"},
        "is_round_trip": {"type": "boolean", "description": "True for round-trip search"},
        "cabin_class": {"type": "string", "description": "ECONOMY, PREMIUM_ECONOMY, BUSINESS, FIRST"},
        "max_stops": {"type": "integer", "description": "0=Any, 1=Nonstop, 2=1-stop, 3=2-stops"},
        "adults": {"type": "integer", "description": "Number of adults (default 1)"},
        "children": {"type": "integer", "description": "Number of children (default 0)"},
        "currency": {"type": "string", "description": "GBP, EUR, USD etc. (default GBP)"},
    },
    "required": ["origin", "destination", "start_date", "end_date"],
})
async def google_flights_search_dates(args: dict[str, Any]) -> dict:
    async def _do():
        async with _GFLIGHTS_SEM:
            return await _do_search()

    async def _do_search():
        from fli.search import SearchDates
        from fli.models import DateSearchFilters, PassengerInfo, TripType
        from fli.models.google_flights.base import FlightSegment
        from fli.core import (
            resolve_airport, parse_cabin_class, parse_max_stops,
        )

        origin = resolve_airport(args["origin"])
        dest = resolve_airport(args["destination"])
        start_date = args["start_date"]  # YYYY-MM-DD
        end_date = args["end_date"]      # YYYY-MM-DD
        duration = _safe_int(args.get("trip_duration"), 0) or None
        is_rt = _to_bool(args.get("is_round_trip"), False)
        cabin = parse_cabin_class(args.get("cabin_class", "ECONOMY"))
        stops = parse_max_stops(_safe_int(args.get("max_stops"), 0))
        adults = _safe_int(args.get("adults"), 1)
        children = _safe_int(args.get("children"), 0)
        currency = args.get("currency", "GBP")

        segments = [FlightSegment(
            departure_airport=[[origin, 0]],
            arrival_airport=[[dest, 0]],
            travel_date=start_date,
        )]
        trip_type = TripType.ROUND_TRIP if is_rt else TripType.ONE_WAY

        passengers = PassengerInfo(adults=adults, children=children)
        filters = DateSearchFilters(
            trip_type=trip_type,
            passenger_info=passengers,
            flight_segments=segments,
            stops=stops,
            seat_type=cabin,
            from_date=start_date,
            to_date=end_date,
            duration=duration,
        )

        with _GFLIGHTS_CURRENCY_LOCK:
            os.environ["FLI_MCP_DEFAULT_CURRENCY"] = currency
            searcher = SearchDates()
            results = searcher.search(filters)

        if not results:
            return {"dates": [], "message": "No date pricing found for this route."}

        dates = []
        for dp in results:
            dates.append({
                "departure_date": str(dp.departure_date),
                "return_date": str(dp.return_date) if hasattr(dp, "return_date") and dp.return_date else None,
                "price": getattr(dp, "price", 0),
            })

        # Sort by price
        dates.sort(key=lambda x: x["price"])

        return {
            "dates": dates,
            "count": len(dates),
            "route": f"{args['origin']}\u2192{args['destination']}",
            "currency": currency,
            "source": "Google Flights (fli)",
        }

    return await _run_with_timeout(_do(), "google_flights_search_dates", timeout=30)


def _format_flight_result(result) -> dict:
    """Format a fli FlightResult into a clean dict."""
    legs_data = []
    for leg in (result.legs or []):
        legs_data.append({
            "airline": leg.airline.name if hasattr(leg.airline, "name") else str(leg.airline),
            "flight_number": getattr(leg, "flight_number", ""),
            "from": leg.departure_airport.name if hasattr(leg.departure_airport, "name") else str(leg.departure_airport),
            "to": leg.arrival_airport.name if hasattr(leg.arrival_airport, "name") else str(leg.arrival_airport),
            "departure": str(leg.departure_datetime),
            "arrival": str(leg.arrival_datetime),
            "duration_min": getattr(leg, "duration", 0),
        })
    return {
        "price": getattr(result, "price", 0),
        "total_duration_min": getattr(result, "duration", 0),
        "stops": getattr(result, "stops", 0),
        "legs": legs_data,
    }


# ============================================================
# PHONE CALL TOOLS (Vapi-specific)
# ============================================================

@tool("phone_call", "Make an outbound phone call via Vapi AI voice agent. ALWAYS show a call plan and get approval first!", {
    "to_number": str, "objective": str, "first_message": str,
    "authorized_info": str, "voice": str, "language": str,
})
async def phone_call(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.phone import make_call
        return await make_call(
            args["to_number"], args["objective"], args["first_message"],
            authorized_info=args.get("authorized_info", ""),
            voice=args.get("voice", "male"),
            language=args.get("language", ""),
        )
    return await _run_with_timeout(_do(), "phone_call")


@tool("phone_get_transcript", "Get the transcript and outcome of a completed phone call", {
    "call_id": str,
})
async def phone_get_transcript(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.phone import get_call_transcript
        return await get_call_transcript(args["call_id"])
    return await _run_with_timeout(_do(), "phone_get_transcript")


# ============================================================
# MODEL / EFFORT / OAUTH / BUDGET / USAGE
# ============================================================

async def _send_model_result_direct(main_key: str, html_msg: str) -> None:
    """Send model-switch confirmation directly to the originating chat.

    Bypasses LLM paraphrase so the user sees the exact harness/model info.
    Handles TG (HTML), WA (plain text), and staging (buffered).
    """
    try:
        from config import IS_STAGING
        plain = re.sub(r"<[^>]+>", "", html_msg)
        if IS_STAGING:
            from main import staging_buffer_response
            staging_buffer_response(main_key, "switch_model", {"text": plain, "parse_mode": "HTML"})
            return
        if main_key.startswith("telegram:"):
            _parts = main_key.split(":")
            chat_id = int(_parts[1])
            _tid: int | None = None
            if len(_parts) >= 3:
                try:
                    _tid = int(_parts[2])
                except (ValueError, TypeError):
                    pass
            from integrations.telegram import send_message
            _sm_kwargs: dict = {"chat_id": chat_id, "parse_mode": "HTML"}
            if _tid is not None:
                _sm_kwargs["message_thread_id"] = _tid
            await send_message(html_msg, **_sm_kwargs)
        else:
            log.debug("_send_model_result_direct: unknown platform in %r, skipping", main_key)
    except Exception:
        log.warning("switch_model: direct send failed for %s", main_key, exc_info=True)


@tool("switch_model", "Switch the Claude model for THIS chat session. Admin only. Valid: claude-sonnet-4-6, claude-opus-4-6, claude-sonnet-4-6[1m], claude-opus-4-6[1m], claude-opus-4-7, claude-opus-4-7[1m]. The [1m] suffix enables 1M context (default 200k). Use 'default' to clear per-chat override. This tool sends a direct confirmation to the chat with exact harness/model info — do NOT echo or summarize the result after calling it. Session resets after delivery.", {
    "model": {"type": "string", "description": "Model name (e.g. 'claude-opus-4-6[1m]') or 'default' to clear override"},
})
async def switch_model(args: dict[str, Any]) -> dict:
    """Switch model for the current chat (per-chat override, not global)."""
    from config import (get_model_primary, get_model_for_chat, set_model_for_chat,
                        clear_model_for_chat, get_chat_model_overrides, ALLOWED_MODELS)
    from bot.agent import schedule_session_reset
    model = args.get("model", "").strip()
    active_key = args.pop("_session_key", "")
    main_key = active_key.split(":task-")[0] if ":task-" in active_key else active_key
    if not model:
        overrides = get_chat_model_overrides()
        if main_key in overrides:
            current = overrides[main_key]
            source_label = "override"
        else:
            try:
                from bot.harness import get_harness_for_chat, resolve_field
                _harness = get_harness_for_chat(main_key)
                _harness_model = resolve_field(_harness, "model", main_key)
                if _harness_model:
                    current = _harness_model
                    source_label = f"harness ({_harness.label})"
                else:
                    current = get_model_primary()
                    source_label = "global default"
            except Exception:
                current = get_model_primary()
                source_label = "global default"
        default = get_model_primary()
        info = f"This chat: {current} ({source_label})"
        info += f"\nGlobal default: {default}"
        info += f"\nAllowed: {', '.join(sorted(ALLOWED_MODELS))}"
        if overrides:
            info += f"\nPer-chat overrides: {overrides}"
        return _text_response(info)
    try:
        old = get_model_for_chat(main_key)
        if model.lower() == "default":
            clear_model_for_chat(main_key)
            # Effective model: harness model (if set) else global default
            try:
                from bot.harness import get_harness_for_chat, resolve_field
                _harness = get_harness_for_chat(main_key)
                _harness_model = resolve_field(_harness, "model", main_key)
                new = _harness_model or get_model_primary()
                label = f"harness ({_harness.label})" if _harness_model else "global default"
            except Exception:
                log.warning("Failed to resolve harness model for %s, using global default", main_key, exc_info=True)
                new = get_model_primary()
                label = "global default"
            log.info(f"Model override cleared for {main_key}: was {old}, now {label} {new}")
            schedule_session_reset(main_key)
            html = (
                f"Model override cleared.\n"
                f"Now using <b>{label}</b>: <code>{new}</code>\n"
                f"Session will reset."
            )
            await _send_model_result_direct(main_key, html)
            return _text_response("(confirmation sent directly to chat)")
        new = set_model_for_chat(main_key, model)
        log.info(f"Model switched for {main_key}: {old} \u2192 {new}")
        schedule_session_reset(main_key)
        html = (
            f"Model switched: <code>{old}</code> \u2192 <code>{new}</code>\n"
            f"Session will reset. Next message uses <b>{new}</b>."
        )
        await _send_model_result_direct(main_key, html)
        return _text_response("(confirmation sent directly to chat)")
    except ValueError as e:
        return _text_response(f"Error: {e}")


@tool("switch_effort", "Switch the effort level at runtime. Admin only. Controls thinking depth and overall token spend. Valid levels: low, medium, high, max (max is Opus 4.6 only). No session reset needed — takes effect on next query.", {
    "level": {"type": "string", "description": "Effort level: low, medium, high, or max"},
})
async def switch_effort(args: dict[str, Any]) -> dict:
    """Switch the effort level at runtime."""
    from config import get_effort_level, set_effort_level, ALLOWED_EFFORTS
    level = args.get("level", "").strip()
    if not level:
        current = get_effort_level()
        return _text_response(f"Current effort: {current}\nAllowed: {', '.join(sorted(ALLOWED_EFFORTS))}")
    try:
        old = get_effort_level()
        new = set_effort_level(level)
        log.info(f"Effort switched: {old} \u2192 {new}")
        return _text_response(f"Effort switched: {old} \u2192 {new}")
    except ValueError as e:
        return _text_response(f"Error: {e}")


@tool("switch_oauth", "Switch the OAuth profile for a chat session. Admin only. Profiles are unified — same token works for bot SDK and Mac relay. Use 'default' to revert to the default profile. Use manage_oauth to list/add/remove profiles or set-default to change which is default. Optionally target a DIFFERENT chat via target_chat_id (e.g. a relay chat ID). Caller's session is NOT reset when targeting another chat.", {
    "label": {"type": "string", "description": "OAuth profile label (e.g. 'personal', 'fiducia', 'work') or 'default'. Omit to show current."},
    "target_chat_id": {"type": "string", "description": "Optional: switch OAuth for a DIFFERENT chat (e.g. relay chat '-5238886766'). Auto-prefixes 'telegram:' if no prefix. When set, only the TARGET chat's session resets — caller stays live."},
})
async def switch_oauth(args: dict[str, Any]) -> dict:
    """Switch OAuth profile for the current chat or a target chat (per-chat override)."""
    from config import (get_oauth_for_chat, set_oauth_for_chat, clear_oauth_for_chat,
                        get_chat_oauth_overrides, list_oauth_profiles, is_default_oauth,
                        get_default_oauth_label)
    from bot.agent import schedule_session_reset
    label = args.get("label", "").strip()
    active_key = args.pop("_session_key", "")
    caller_key = active_key.split(":task-")[0] if ":task-" in active_key else active_key

    # Determine target: explicit target_chat_id or caller's own chat
    target_chat_id_raw = args.get("target_chat_id", "").strip()
    if target_chat_id_raw:
        # Auto-prefix telegram: if no source prefix present
        if ":" not in target_chat_id_raw:
            main_key = f"telegram:{target_chat_id_raw}"
        else:
            main_key = target_chat_id_raw
        is_cross_chat = True
        # Warn if target_chat_id doesn't match any known chat (typo protection)
        _target_cid = main_key.split(":", 1)[-1] if ":" in main_key else main_key
        _known = False
        try:
            import bot.chat_registry as _cr
            _cr_key = f"telegram:{_target_cid}" if _target_cid.lstrip("-").isdigit() else _target_cid
            if _cr.is_registered(_cr_key):
                _known = True
            if not _known:
                from bot.desktop_relay import get_relay_mode
                if get_relay_mode(_target_cid):
                    _known = True
        except Exception:
            pass
        if not _known:
            log.warning("switch_oauth: target_chat_id %s not found in any registry", _target_cid)
    else:
        main_key = caller_key
        is_cross_chat = False
        _known = True  # own chat is always "known"

    if not label:
        current = get_oauth_for_chat(main_key)
        overrides = get_chat_oauth_overrides()
        profiles = list_oauth_profiles()
        profile_names = [p["label"] for p in profiles]
        target_label = "Target chat" if is_cross_chat else "This chat"
        info = f"{target_label} ({main_key}): {current}"
        if main_key in overrides:
            info += " (override)"
        else:
            info += f" (default={get_default_oauth_label()})"
        info += f"\nAvailable profiles: {', '.join(profile_names)}"
        if overrides:
            info += f"\nPer-chat overrides: {overrides}"
        return _text_response(info)
    try:
        old = get_oauth_for_chat(main_key)
        effective_label = get_default_oauth_label() if is_default_oauth(label.lower()) else label

        # RBAC credential check: non-admin must have the RESOLVED profile in allowed_credentials.
        # Check AFTER label resolution so "default" → actual label is matched correctly.
        if active_key:
            from bot.core.session_state import _get_or_create_state as _gocs_oauth
            _oauth_state = _gocs_oauth(active_key)
            _oauth_is_admin = _oauth_state.get("rbac_execution", {}).get("filesystem") == "full"
            if not _oauth_is_admin:
                _eff_creds = _oauth_state.get("effective_allowed_credentials") or []
                if not _eff_creds:
                    from bot.harness import get_harness_for_chat as _get_h_oauth, resolve_field as _rf_oauth
                    _h_oauth = _get_h_oauth(active_key)
                    _eff_creds = _rf_oauth(_h_oauth, "allowed_credentials", active_key) or []
                from bot.harness import is_credential_unrestricted as _is_unr_svc
                if not _is_unr_svc(_eff_creds):
                    _cred_id = f"claude:{effective_label}"
                    if _cred_id not in _eff_creds and effective_label not in _eff_creds:
                        return _text_response(f"Access denied: profile '{effective_label}' not in your allowed credentials")
        if is_default_oauth(effective_label):
            clear_oauth_for_chat(main_key)
            log.info(f"OAuth override cleared for {main_key}: was {old}")
        else:
            set_oauth_for_chat(main_key, effective_label)
            log.info(f"OAuth switched for {main_key}: {old} \u2192 {effective_label}")
        # Audit log (CB-50)
        from bot.audit import log_admin_action
        await log_admin_action("oauth.switch", caller_key, main_key, "success",
                               {"old": old, "new": effective_label})
        # Reset only the target chat's session (not caller when cross-chat)
        schedule_session_reset(main_key)
        # Auto-respawn relay daemon if target is a relay chat
        relay_note = ""
        chat_id = main_key.split(":", 1)[-1] if ":" in main_key else ""
        if chat_id.lstrip("-").isdigit():
            from bot.desktop_relay import get_relay_mode, set_chat_account, kill_daemon
            entry = get_relay_mode(chat_id)
            if entry:
                # Update relay registry account_label
                await set_chat_account(chat_id, effective_label)
                # Kill daemon and eagerly respawn with new token
                kill_result = await kill_daemon(chat_id)
                try:
                    from bot.relay.pool import get_pool
                    pool = get_pool()
                    if pool:
                        await pool.release(chat_id)
                except Exception as e:
                    log.debug(f"Pool release on OAuth switch: {e}")
                # Eager respawn so daemon doesn't wait for next message
                respawned = False
                if kill_result.get("ok"):
                    try:
                        from bot.desktop_relay import eager_respawn_daemon
                        resp = await eager_respawn_daemon(chat_id)
                        respawned = resp.get("ok", False)
                    except Exception as e:
                        log.debug(f"Eager respawn on OAuth switch: {e}")
                if kill_result.get("ok"):
                    relay_note = f"\nRelay daemon killed and {'respawned' if respawned else 'will respawn on next message'}."
                else:
                    relay_note = "\nRelay daemon kill failed — may need manual /relay-kill."
        target_desc = f"chat {main_key}" if is_cross_chat else "this chat"
        unknown_warn = "\n\u26a0\ufe0f Warning: target chat not found in any known registry — check chat ID." if (is_cross_chat and not _known) else ""
        reset_note = "" if is_cross_chat else "\nCaller session will reset after this response."
        if is_default_oauth(effective_label):
            return _text_response(
                f"OAuth override cleared for {target_desc}.\n"
                f"Now using {get_default_oauth_label()} credentials.{reset_note}{relay_note}{unknown_warn}"
            )
        return _text_response(
            f"OAuth switched for {target_desc}: {old} \u2192 {effective_label}\n"
            f"Next message uses {effective_label} profile.{reset_note}{relay_note}{unknown_warn}"
        )
    except ValueError as e:
        return _text_response(f"Error: {e}")


@tool("manage_oauth", "Manage unified OAuth profiles (bot SDK + Mac relay). Admin only. Actions: status (full OAuth state: default, profiles, overrides, relay mappings — ONE call), list (profiles only), add (store new token), remove (delete profile), set-default (change default). Tokens from 'claude setup-token' command.", {
    "action": {"type": "string", "description": "Action: status (recommended — full state dump), list, add, remove, or set-default"},
    "label": {"type": "string", "description": "Profile label (for add/remove/set-default). Must be lowercase alphanumeric + hyphens/underscores, max 32 chars."},
    "token": {"type": "string", "description": "OAuth token starting with sk-ant- (for add action only)"},
    "sync_to_mac": {"type": "boolean", "description": "Also sync token to Mac relay (default true for add)"},
})
async def manage_oauth(args: dict[str, Any]) -> dict:
    """Manage OAuth profiles."""
    from config import list_oauth_profiles, add_oauth_profile, remove_oauth_profile
    action = args.get("action", "list")
    if action == "status":
        # One-stop-shop: full OAuth state for admin decision-making
        from config import get_chat_oauth_overrides, get_default_oauth_label
        profiles = list_oauth_profiles()
        default_label = get_default_oauth_label()
        overrides = get_chat_oauth_overrides()
        # Gather relay chats with their OAuth assignments
        relay_chats = []
        try:
            from bot.relay.registry import _load_registry
            registry = _load_registry()
            for cid, entry in registry.items():
                if entry.get("relay_mode"):
                    # Get display name from external chats or session_name
                    import bot.chat_registry as _cr
                    _cr_entry = _cr.get(f"telegram:{cid}") if cid.lstrip("-").isdigit() else None
                    name = (_cr_entry or {}).get("title", "")
                    if not name:
                        sname = entry.get("session_name", "")
                        name = sname[6:] if sname.startswith("relay_") else sname
                    acct = entry.get("account_label", default_label)
                    relay_chats.append({
                        "chat_id": cid,
                        "name": name or cid,
                        "oauth": acct,
                        "project": entry.get("project_path", ""),
                    })
        except Exception as e:
            log.debug(f"Failed to load relay registry for OAuth status: {e}")
        return _text_response({
            "default_profile": default_label,
            "profiles": [p["label"] for p in profiles],
            "per_chat_overrides": overrides if overrides else "none",
            "relay_chats": relay_chats if relay_chats else "none",
        })
    if action == "list":
        profiles = list_oauth_profiles()
        return _text_response({"profiles": profiles})
    elif action == "add":
        label = args.get("label", "").strip()
        token = args.get("token", "").strip()
        if not label:
            return _text_response({"error": "label required"})
        if not token:
            return _text_response({"error": "token required (from 'claude setup-token')"})
        try:
            result = add_oauth_profile(label, token)
            # Optionally sync to Mac relay (only when desktop relay is enabled)
            from bot.relay.core import DESKTOP_ENABLED
            sync = _to_bool(args.get("sync_to_mac"), True) and DESKTOP_ENABLED
            mac_synced = False
            if sync:
                try:
                    from bot.desktop_relay import push_oauth_to_mac
                    mac_result = await push_oauth_to_mac(label, token)
                    mac_synced = mac_result.get("success", False)
                except Exception as e:
                    log.warning("Failed to sync OAuth to Mac: %s", e)
            result["mac_synced"] = mac_synced
            # Audit log (CB-50)
            from bot.audit import log_admin_action
            _sk = args.get("_session_key", "")
            await log_admin_action("oauth.add", _sk, label, "success",
                                   {"mac_synced": mac_synced})
            return _text_response(result)
        except ValueError as e:
            return _text_response({"error": str(e)})
    elif action == "remove":
        label = args.get("label", "").strip()
        if not label:
            return _text_response({"error": "label required"})
        try:
            removed = remove_oauth_profile(label)
            # Audit log (CB-50)
            from bot.audit import log_admin_action
            _sk = args.get("_session_key", "")
            await log_admin_action("oauth.remove", _sk, label, "success")
            return _text_response({"removed": removed, "label": label})
        except ValueError as e:
            return _text_response({"error": str(e)})
    elif action == "set-default":
        label = args.get("label", "").strip()
        if not label:
            return _text_response({"error": "label required"})
        try:
            from config import set_default_oauth_label, get_default_oauth_label
            old_default = get_default_oauth_label()
            new_default = set_default_oauth_label(label)
            # Audit log (CB-50)
            from bot.audit import log_admin_action
            _sk = args.get("_session_key", "")
            await log_admin_action("oauth.set_default", _sk, label, "success",
                                   {"old_default": old_default})
            return _text_response({
                "success": True,
                "old_default": old_default,
                "new_default": new_default,
                "note": "All chats without per-chat overrides now use this profile. "
                        "IMPORTANT: Already-running SDK sessions still use the OLD token "
                        "until reset. Suggest /reset for the current chat and any other "
                        "active sessions that should pick up the new default.",
            })
        except ValueError as e:
            return _text_response({"error": str(e)})
    return _text_response({"error": f"Unknown action: {action}. Use: status, list, add, remove, set-default"})


@tool("extend_budget", "Raise the CURRENT session's USD budget cap by delta_usd WITHOUT losing conversation context. Admin only. Gated by a mandatory 'extend budget' keyword in the user's message — NEVER auto-invoke. Hard caps: per-extension MAX_EXTEND_DELTA_USD ($100 default), total session MAX_TOTAL_BUDGET_CAP ($500 default). Disconnect is deferred to end-of-query so the tool's own reply reaches the user; next query spawns a fresh CLI subprocess with --resume <session_id> + the new cap.", {
    "delta_usd": {"type": "number", "description": "Positive USD amount to ADD to the current cap (e.g. 20 raises a $50 cap to $70). Must be finite and > 0; rejected if it would push total cap over the global ceiling."},
})
async def extend_budget(args: dict[str, Any]) -> dict:
    """Extend the current session's budget cap (admin + keyword gated).

    Hard-caps per-extension and per-session totals; rejects non-finite
    values. Disconnect is deferred to end-of-query so the tool's reply
    is delivered before the client is torn down.
    """
    from bot.agent import client_pool
    raw = args.get("delta_usd")
    if raw is None:
        return _text_response({"error": "delta_usd required (positive finite number)"})
    active_key = args.pop("_session_key", "")
    main_key = active_key.split(":task-")[0] if ":task-" in active_key else active_key
    if not main_key:
        return _text_response({"error": "no active session"})
    try:
        new_cap = await client_pool.extend_budget(main_key, raw)
    except ValueError as e:
        # ValueError from extend_budget covers: non-finite, non-positive,
        # per-extension cap exceeded, total cap exceeded. Surface verbatim.
        return _text_response({"error": str(e)})
    except Exception as e:
        log.error("extend_budget failed for %s: %s", main_key, e, exc_info=True)
        return _text_response({"error": f"extend failed: {e}"})
    try:
        delta_f = float(raw)
    except (TypeError, ValueError):
        delta_f = 0.0
    return _text_response(
        f"Budget extended by ${delta_f:.2f} — new cap ${new_cap:.2f}. "
        f"Context preserved via --resume; fresh CLI subprocess spawns on next query "
        f"(disconnect deferred to end of this query so this reply reaches you)."
    )


@tool("usage_report", "View usage statistics and cost breakdown. Admin only. Actions: summary (24h + 5h stats), top (most expensive queries), rate_limits (current rate limit state), by_profile (cost per OAuth profile).", {
    "action": {"type": "string", "description": "Action: summary, top, rate_limits, or by_profile"},
    "hours": {"type": "integer", "description": "Time window in hours (default 24, for summary/by_profile)"},
})
async def usage_report(args: dict[str, Any]) -> dict:
    """Query usage_log for cost and token statistics.

    Chat isolation: only admin in private DMs gets global usage.
    Admin in group/topic sees only their own chat's usage.
    """
    # Resolve chat scope: admin DM = global, admin group = chat-scoped
    active_key = args.pop("_session_key", "")
    _sk_prefix = ""  # default: global scope
    if active_key:
        parts = active_key.split(":")
        _chat_id = parts[1] if len(parts) >= 2 else ""
        try:
            _is_private = int(_chat_id) > 0
        except (ValueError, TypeError):
            _is_private = False
        if not _is_private:
            # Admin in group/topic: scope to this chat's usage
            _source = parts[0] if parts else ""
            _sk_prefix = f"{_source}:{_chat_id}" if _source and _chat_id else ""

    action = args.get("action", "summary")
    if action == "summary":
        hours = args.get("hours", 24)
        from bot.storage.memory import get_usage_summary
        summary = await get_usage_summary(hours)
        summary_5h = await get_usage_summary(5)
        return _text_response({"window_hours": hours, **summary, "last_5h": summary_5h})
    elif action == "top":
        from bot.storage.memory import get_top_queries
        top = await get_top_queries(10)
        return _text_response({"top_queries": top})
    elif action == "rate_limits":
        from bot.agent import get_rate_limit_state
        return _text_response({"rate_limits": get_rate_limit_state()})
    elif action == "by_profile":
        hours = args.get("hours", 24)
        from bot.storage.memory import get_usage_by_profile
        by_profile = await get_usage_by_profile(hours, session_key_prefix=_sk_prefix)
        total = sum(p["cost_usd"] for p in by_profile)
        total_queries = sum(p["queries"] for p in by_profile)
        return _text_response({
            "hours": hours, "by_profile": by_profile,
            "total_cost_usd": round(total, 4), "total_queries": total_queries,
        })
    return _text_response({"error": f"Unknown action: {action}. Use: summary, top, rate_limits, by_profile"})


@tool("reset_session", "Force-reset an SDK session for any chat. Admin only. \u26a0\ufe0f WARNING: Resetting YOUR OWN session (the chat you're currently in) will TERMINATE this conversation mid-response — the user will see no reply. Only reset OTHER chats' sessions, never your own. Use for: clearing corrupt sessions, forcing fresh context in other chats, recovering stuck sessions.", {
    "session_key": {"type": "string", "description": "Session key to reset (e.g. 'telegram:12345', 'whatsapp:jid...'). If omitted, returns current session info WITHOUT resetting (safe)."},
})
async def reset_session_tool(args: dict[str, Any]) -> dict:
    """Reset SDK session for a chat. Admin only."""
    from bot.agent import client_pool

    active_key = args.pop("_session_key", "")
    main_key = active_key.split(":task-")[0] if ":task-" in active_key else active_key
    target = args.get("session_key", "").strip()

    if not target:
        # Info mode — show current session without resetting
        pool_status = client_pool.get_status()
        return _text_response({
            "current_session": main_key,
            "active_sessions": list(pool_status.keys()),
            "hint": "Pass session_key to reset a specific session.",
        })

    # Safety check: warn if targeting own session
    target_main = target.split(":task-")[0] if ":task-" in target else target
    if target_main == main_key:
        return _text_response(
            "\u26a0\ufe0f BLOCKED: You are trying to reset YOUR OWN session. "
            "This would kill the current conversation and the user would see no reply. "
            "Use the lightweight 'reset' command in chat instead (works without SDK), "
            "or target a DIFFERENT chat's session_key."
        )

    # Reset the target session
    pool_status = client_pool.get_status()
    existed = target in pool_status
    saved_sid = await client_pool.get_session_id(target)
    await client_pool.reset_session(target)

    result = {"reset": target, "existed_in_pool": existed, "had_saved_session": bool(saved_sid)}
    if saved_sid:
        result["cleared_session_id"] = saved_sid[:8] + "..."
    return _text_response(result)


def _reset_sessions_for_harness(label: str) -> list[str]:
    """Reset all SDK sessions using a given harness. Returns list of reset chat_keys."""
    try:
        from bot.harness import get_chats_by_harness
        from bot.agent import client_pool
        chats = get_chats_by_harness(label)
        if client_pool and chats:
            for ck in chats:
                client_pool.schedule_session_reset(ck)
        return chats
    except Exception:
        log.debug("_reset_sessions_for_harness failed", exc_info=True)
        return []


@tool("manage_harness", "Manage harness profiles (per-chat configuration). Actions: list (show all harnesses), get (show one), create (new harness), update (modify fields), delete (remove harness), assign (set chat harness), unassign (revert chat to default).", {
    "action": {"type": "string", "description": "Action: list, get, create, update, delete, assign, unassign"},
    "label": {"type": "string", "description": "Harness label (for get/create/update/delete/assign)"},
    "chat_key": {"type": "string", "description": "Chat key e.g. 'telegram:999999999' (for assign/unassign)"},
    "fields": {"type": "string", "description": "JSON object of fields to set (for create/update). E.g. {\"model\":\"claude-sonnet-4-6\",\"default_role\":\"teamwork\",\"domains\":[\"teamwork\"],\"write_domain\":\"teamwork\"}"},
})
async def manage_harness(args: dict[str, Any]) -> dict:
    """Manage harness profiles — per-chat configuration."""
    from bot.harness import (
        list_harnesses, get_harness, create_harness, update_harness,
        delete_harness, clear_chat_harness,
        get_chat_assignments, format_harness,
    )
    action = args.get("action", "list")
    label = args.get("label", "")
    chat_key = args.get("chat_key", "")

    if action == "list":
        harnesses = list_harnesses()
        assignments = get_chat_assignments()
        lines = []
        for lbl, h in sorted(harnesses.items()):
            count = sum(1 for k, v in assignments.items()
                        if v == lbl and not k.startswith("system:"))
            lines.append(f"{lbl} ({count} chats): {format_harness(h, compact=True)}")
        return _text_response("\n".join(lines) if lines else "No harnesses defined.")

    if action == "get":
        if not label:
            return _text_response("Error: 'label' required for get action.")
        h = get_harness(label)
        if not h:
            return _text_response(f"Harness '{label}' not found.")
        return _text_response(format_harness(h))

    if action == "create":
        if not label:
            return _text_response("Error: 'label' required for create action.")
        fields_str = args.get("fields", "{}")
        try:
            fields = json.loads(fields_str) if isinstance(fields_str, str) else fields_str
        except json.JSONDecodeError as e:
            return _text_response(f"Error: invalid JSON in fields: {e}")
        copy_from = fields.pop("copy_from", None) or args.get("copy_from")
        try:
            h = create_harness(label, copy_from=copy_from, **fields)
            src_note = f" (copied from '{copy_from}')" if copy_from else ""
            # Audit log (CB-50)
            from bot.audit import log_admin_action
            _sk = args.get("_session_key", "")
            await log_admin_action("harness.create", _sk, label, "success",
                                   {"copy_from": copy_from or None})
            return _text_response(f"Created harness '{label}'{src_note}: {format_harness(h, compact=True)}")
        except ValueError as e:
            return _text_response(f"Error: {e}")

    if action == "update":
        if not label:
            return _text_response("Error: 'label' required for update action.")
        fields_str = args.get("fields", "{}")
        try:
            fields = json.loads(fields_str) if isinstance(fields_str, str) else fields_str
        except json.JSONDecodeError as e:
            return _text_response(f"Error: invalid JSON in fields: {e}")
        try:
            h = update_harness(label, **fields)
            # Any harness change may affect SDK behavior (model, tools, skills,
            # effort, custom_instructions, etc.) — always reset affected sessions
            _reset_chats = _reset_sessions_for_harness(label)
            reset_note = f" Session reset: {len(_reset_chats)} chat(s)." if _reset_chats else ""
            # Audit log (CB-50)
            from bot.audit import log_admin_action
            _sk = args.get("_session_key", "")
            await log_admin_action("harness.update", _sk, label, "success",
                                   {"fields": list(fields.keys()), "resets": len(_reset_chats)})
            return _text_response(f"Updated harness '{label}': {format_harness(h, compact=True)}{reset_note}")
        except ValueError as e:
            return _text_response(f"Error: {e}")

    if action == "delete":
        if not label:
            return _text_response("Error: 'label' required for delete action.")
        try:
            _reset_chats = _reset_sessions_for_harness(label)
            delete_harness(label)
            reset_note = f" Session reset: {len(_reset_chats)} chat(s)." if _reset_chats else ""
            # Audit log (CB-50)
            from bot.audit import log_admin_action
            _sk = args.get("_session_key", "")
            await log_admin_action("harness.delete", _sk, label, "success",
                                   {"resets": len(_reset_chats)})
            return _text_response(f"Deleted harness '{label}'. Affected chats reverted to default.{reset_note}")
        except ValueError as e:
            return _text_response(f"Error: {e}")

    if action == "assign":
        if not label or not chat_key:
            return _text_response("Error: both 'label' and 'chat_key' required for assign action.")
        try:
            # CB-5: Use assign_chat_harness for unified harness + RBAC assignment
            from bot.harness import assign_chat_harness
            h = await assign_chat_harness(chat_key, label)
            # Schedule session reset so new harness takes effect immediately
            try:
                from bot.agent import client_pool
                if client_pool:
                    client_pool.schedule_session_reset(chat_key)
            except Exception:
                pass
            # Audit log (CB-50)
            from bot.audit import log_admin_action
            _sk = args.get("_session_key", "")
            await log_admin_action("harness.assign", _sk, label, "success",
                                   {"chat_key": chat_key, "default_role": h.default_role})
            result = f"Chat '{chat_key}' assigned to harness '{label}'. Session reset scheduled."
            if h.default_role:
                result += f"\nRBAC role '{h.default_role}' auto-assigned."
            return _text_response(result)
        except ValueError as e:
            return _text_response(f"Error: {e}")

    if action == "unassign":
        if not chat_key:
            return _text_response("Error: 'chat_key' required for unassign action.")
        clear_chat_harness(chat_key)
        try:
            from bot.agent import client_pool
            if client_pool:
                client_pool.schedule_session_reset(chat_key)
        except Exception:
            pass
        return _text_response(f"Chat '{chat_key}' reverted to default harness. Session reset scheduled.")

    return _text_response(f"Unknown action: {action}. Use: list, get, create, update, delete, assign, unassign")


@tool("manage_external_chat", "Manage external chats (Telegram). Actions: list (show all), set_mode (change mode), add (register chat), remove (forget chat), configure (set custom_instructions), tool_overrides (manage security tool access overrides). Custom instructions are appended as chat-specific guidance. Skills are configured via harness claude_md_modules.", {
    "action": {"type": "string", "description": "Action: list, set_mode, add, remove/delete, configure, tool_overrides"},
    "platform": {"type": "string", "description": "Platform: telegram (default: telegram)"},
    "chat_id": {"type": "string", "description": "Chat ID — TG integer (required for set_mode, add, remove, configure)"},
    "mode": {"type": "string", "description": "New mode: silent, active, or active_plus (required for set_mode and add, defaults to 'active' for add)"},
    "title": {"type": "string", "description": "Chat title (optional, for add action)"},
    "instructions": {"type": "string", "description": "Custom instructions text for the chat (for configure). Empty string clears."},
    "tool_prefixes": {"type": "array", "items": {"type": "string"}, "description": "Tool prefixes to allow in this chat (for tool_overrides). E.g. ['mcp__google-workspace__']. Empty list clears."},
    "allowed_emails": {"type": "array", "items": {"type": "string"}, "description": "Google account emails permitted in this chat (for tool_overrides). Empty list clears."},
})
async def manage_external_chat_tool(args: dict[str, Any]) -> dict:
    """Manage external chats (TG or WA). Uses unified chat_registry."""
    import bot.chat_registry as _cr

    # Note: manage_external_chat currently only supports Telegram groups.
    # Default to "telegram" intentionally (not ROOT_ADMIN) — this is a TG-specific feature.
    platform = args.get("platform", "telegram").lower()
    if platform in ("telegram", "tg"):
        label = "TG"
    else:
        return _text_response(f"Error: unknown platform '{platform}'. Use 'telegram'.")

    # Build a virtual 'chats' dict from chat_registry for backward-compat logic
    _all = _cr.all_entries()
    chats: dict[int, dict] = {}
    for _ck, _cv in _all.items():
        if _ck.startswith("telegram:"):
            _parts = _ck.split(":")
            if len(_parts) == 2:
                try:
                    chats[int(_parts[1])] = _cv
                except ValueError:
                    pass

    action = args.get("action", "list")
    if action == "list":
        if not chats:
            return _text_response(f"No external {label} chats yet.")
        lines = []
        for cid, info in chats.items():
            parts = [f"mode={info.get('mode', 'silent')}"]
            if info.get("custom_instructions"):
                preview = info["custom_instructions"][:60]
                parts.append(f"instructions=\"{preview}{'...' if len(info['custom_instructions']) > 60 else ''}\"")
            parts.append(f"added={info.get('added_at', '?')}")
            lines.append(f"  {cid}: {info.get('title', '?')} ({', '.join(parts)})")
        return _text_response(f"Discovered {label} chats:\n" + "\n".join(lines))

    raw_id = args.get("chat_id")
    if not raw_id and action != "tool_overrides":
        return _text_response("Error: chat_id is required for this action.")

    # TG uses int keys, WA uses string JIDs
    chat_id = _safe_int(raw_id, 0) if (raw_id and label == "TG") else (str(raw_id) if raw_id else None)
    if label == "TG" and chat_id == 0 and raw_id:
        return _text_response(f"Error: invalid TG chat_id '{raw_id}' — must be numeric.")

    if action == "set_mode":
        mode = args.get("mode", "silent")
        if mode not in ("silent", "active", "active_plus"):
            return _text_response("Error: mode must be 'silent', 'active', or 'active_plus'.")
        _chat_key = f"telegram:{chat_id}"
        if not _cr.is_registered(_chat_key):
            return _text_response(f"Error: chat {chat_id} not found in registry.")
        _cr.set_mode(_chat_key, mode)
        entry = _cr.get(_chat_key) or {}
        title = entry.get("title", "?")
        # Audit log (CB-50)
        from bot.audit import log_admin_action
        _sk = args.get("_session_key", "")
        await log_admin_action("chat.set_mode", _sk, str(chat_id), "success",
                               {"platform": label, "mode": mode, "title": title})
        return _text_response(f"{label} chat '{title}' ({chat_id}) mode set to '{mode}'.")

    elif action == "add":
        mode = args.get("mode", "active")
        if mode not in ("silent", "active", "active_plus"):
            return _text_response("Error: mode must be 'silent', 'active', or 'active_plus'.")
        _chat_key = f"telegram:{chat_id}"
        title = args.get("title", f"Chat {chat_id}")
        existing = _cr.get(_chat_key)
        if existing:
            # Update existing entry
            existing["mode"] = mode
            if title and title != f"Chat {chat_id}":
                existing["title"] = title
            _cr.register(_chat_key, **existing)
            # Audit log (CB-5 H2: was missing on update path)
            from bot.audit import log_admin_action
            _sk = args.get("_session_key", "")
            await log_admin_action("chat.update", _sk, str(chat_id), "success",
                                   {"platform": label, "mode": mode})
            return _text_response(f"{label} chat '{existing.get('title', '?')}' ({chat_id}) updated to mode '{mode}'.")
        _cr.register(_chat_key, title=title, mode=mode)
        # Audit log (CB-50)
        from bot.audit import log_admin_action
        _sk = args.get("_session_key", "")
        await log_admin_action("chat.add", _sk, str(chat_id), "success",
                               {"platform": label, "mode": mode, "title": title})
        return _text_response(f"{label} chat '{title}' ({chat_id}) added as external chat in '{mode}' mode.")

    elif action in ("remove", "delete"):
        _chat_key = f"telegram:{chat_id}"
        entry = _cr.get(_chat_key)
        if not entry:
            return _text_response(f"Error: chat {chat_id} not found in registry.")
        title = entry.get("title", "?")
        mode = entry.get("mode", "silent")
        # Safety: warn if chat has an active relay
        if mode == "active_plus" and label == "TG":
            try:
                from bot.desktop_relay import get_relay_mode, is_enabled
                if is_enabled() and get_relay_mode(str(chat_id)):
                    return _text_response(
                        f"Error: '{title}' has an active relay. "
                        f"Delete the relay first (desktop_relay action=delete_relay_group), "
                        f"then remove the chat."
                    )
            except Exception:
                pass
        _cr.unregister(_chat_key)
        log.info("Removed chat: cid=%s title=%s mode=%s", chat_id, title, mode)
        # Audit log (CB-50)
        from bot.audit import log_admin_action
        _sk = args.get("_session_key", "")
        await log_admin_action("chat.remove", _sk, str(chat_id), "success",
                               {"platform": label, "title": title})
        return _text_response(f"{label} chat '{title}' ({chat_id}) removed from registry.")

    elif action == "configure":
        _chat_key = f"telegram:{chat_id}"
        entry = _cr.get(_chat_key)
        if not entry:
            return _text_response(f"Error: chat {chat_id} not found in registry.")
        result_parts = []
        # Handle custom instructions
        new_instructions = args.get("instructions")
        if new_instructions is not None:
            entry["custom_instructions"] = new_instructions
            _cr.register(_chat_key, **entry)
            if new_instructions:
                result_parts.append(f"custom_instructions set ({len(new_instructions)} chars)")
            else:
                result_parts.append("custom_instructions cleared")
        if not result_parts:
            return _text_response("Error: configure requires 'instructions' parameter. "
                                  "Skills are now configured via harness claude_md_modules.")
        title = entry.get("title", "?")
        return _text_response(f"{label} chat '{title}' ({chat_id}) configured: {'; '.join(result_parts)}")

    elif action == "tool_overrides":
        from bot.hooks import (get_external_tool_overrides,
                               set_external_tool_override,
                               remove_external_tool_override)
        if not chat_id:
            # List all overrides
            overrides = get_external_tool_overrides()
            if not overrides:
                return _text_response("No tool overrides configured for any external chat.")
            lines = []
            for cid, ov in overrides.items():
                lines.append(f"  {cid}: prefixes={ov.get('prefixes', [])}, emails={ov.get('allowed_emails', [])}")
            return _text_response("Tool overrides:\n" + "\n".join(lines))
        # Set or clear overrides for specific chat
        prefixes = args.get("tool_prefixes")
        emails = args.get("allowed_emails")
        if prefixes is not None or emails is not None:
            # Parse JSON strings if SDK passed them that way
            import json as _json
            if isinstance(prefixes, str):
                try:
                    prefixes = _json.loads(prefixes)
                except (ValueError, TypeError):
                    prefixes = []
            if isinstance(emails, str):
                try:
                    emails = _json.loads(emails)
                except (ValueError, TypeError):
                    emails = []
            prefixes = prefixes or []
            emails = emails or []
            if not prefixes and not emails:
                removed = remove_external_tool_override(str(chat_id))
                if removed:
                    return _text_response(f"Removed tool overrides for chat {chat_id}.")
                return _text_response(f"No overrides existed for chat {chat_id}.")
            set_external_tool_override(str(chat_id), prefixes, emails)
            return _text_response(
                f"Tool overrides set for chat {chat_id}:\n"
                f"  Allowed prefixes: {prefixes}\n"
                f"  Allowed emails: {emails}"
            )
        # Show current overrides for this chat
        overrides = get_external_tool_overrides()
        ov = overrides.get(str(chat_id))
        if ov:
            return _text_response(
                f"Tool overrides for chat {chat_id}:\n"
                f"  Allowed prefixes: {ov.get('prefixes', [])}\n"
                f"  Allowed emails: {ov.get('allowed_emails', [])}"
            )
        return _text_response(f"No tool overrides for chat {chat_id}.")

    return _text_response(f"Unknown action: {action}")


# ============================================================
# SCHEDULING
# ============================================================

@tool("list_scheduled_tasks", "List all scheduled tasks with their status, times, and IDs.", {})
async def list_scheduled_tasks(args: dict[str, Any]) -> dict:
    """List scheduled tasks. Non-admins see only their own chat's tasks."""
    from bot.scheduler import list_tasks, format_task_list
    _sk = args.pop("_session_key", "")
    tasks = list_tasks()
    # Per-chat filtering: non-admins see only their own tasks
    _is_admin_tasks = False
    try:
        from bot.hooks import _is_rbac_admin
        from bot.core.session_state import _get_or_create_state
        _state = _get_or_create_state(_sk) if _sk else {}
        _is_admin_tasks = _is_rbac_admin(_state)
    except Exception:
        pass
    if not _is_admin_tasks and _sk:
        # Filter to tasks matching this session's chat_id (may include thread_id for topics)
        # session_key format: source:chat_id or source:chat_id:thread_id
        _sk_parts = _sk.split(":")
        # Extract chat_id (and thread_id if present) — everything after source prefix
        _my_chat_id = _sk_parts[1] if len(_sk_parts) > 1 else ""
        _my_chat_with_thread = ":".join(_sk_parts[1:]) if len(_sk_parts) > 1 else ""
        tasks = [t for t in tasks if t.get("chat_id") in (_my_chat_id, _my_chat_with_thread)]
    return _text_response({"tasks": tasks, "formatted": format_task_list(tasks)})


@tool("manage_scheduled_task", "Add, edit, delete, or toggle a scheduled task. Use action='add' to create, 'edit' to modify, 'delete' to remove, 'toggle' to enable/disable.", {
    "action": {"type": "string", "description": "Action: add, edit, delete, toggle"},
    "task_id": {"type": "string", "description": "Task ID (required for edit/delete/toggle)"},
    "name": {"type": "string", "description": "Task name (for add/edit)"},
    "hour": {"type": "integer", "description": "Hour 0-23 (for add/edit)"},
    "minute": {"type": "integer", "description": "Minute 0-59 (for add/edit)"},
    "days": {"type": "array", "items": {"type": "string"}, "description": "Days: ['daily'], ['weekdays'], ['weekends'], or specific days like ['mon','wed','fri']"},
    "prompt": {"type": "string", "description": "The prompt/instruction to execute when the task fires. Should include what to check and how to format the message."},
    "platform": {"type": "string", "description": "Where to send output: telegram or teams (default: telegram)"},
    "enabled": {"type": "boolean", "description": "Whether the task is enabled (default: true)"},
    "chat_id": {"type": "string", "description": "Origin chat ID — task inherits this chat's fact access. Auto-detected from current session if not provided."},
})
async def manage_scheduled_task(args: dict[str, Any]) -> dict:
    """Manage scheduled tasks (CRUD + toggle)."""
    from bot.scheduler import add_task, update_task, delete_task, toggle_task, get_task

    # M9 fix: always pop _session_key at top to prevent leakage
    _sk = args.pop("_session_key", "")
    action = args.get("action", "").lower()

    # Ownership detection — used by add/edit/delete/toggle for non-admin validation
    # Include thread_id for forum topics: source:chat_id:thread_id → chat_id or chat_id:thread_id
    _own_chat = ""
    _own_chat_with_thread = ""
    if _sk:
        _sk_parts = _sk.split(":")
        _own_chat = _sk_parts[1] if len(_sk_parts) >= 2 else ""
        _own_chat_with_thread = ":".join(_sk_parts[1:]) if len(_sk_parts) >= 2 else ""
    from bot.core.session_state import _get_or_create_state
    _st = _get_or_create_state(_sk) if _sk else {}
    # Use same admin check as list_scheduled_tasks for consistency
    try:
        from bot.hooks import _is_rbac_admin
        _is_admin_session = _is_rbac_admin(_st)
    except Exception:
        _is_admin_session = _st.get("rbac_execution", {}).get("filesystem") == "full"

    if action == "add":
        name = args.get("name")
        hour = args.get("hour")
        minute = args.get("minute", 0)
        prompt = args.get("prompt")
        if not name or hour is None or not prompt:
            return _text_response({"error": "Required fields for add: name, hour, prompt"})
        # Derive chat_id: explicit param > injected session_key > empty (admin)
        task_chat_id = args.get("chat_id", "")
        if not task_chat_id and _sk:
            parts = _sk.split(":")
            task_chat_id = parts[1] if len(parts) >= 2 else ""
        if not _is_admin_session and task_chat_id and task_chat_id not in (_own_chat, _own_chat_with_thread):
            return _text_response({
                "error": "Non-admin users can only create tasks for their own chat. "
                         f"Your chat: {_own_chat}, requested: {task_chat_id}"
            })
        task = add_task(
            name=name, hour=_safe_int(hour, 0), minute=_safe_int(minute, 0), prompt=prompt,
            days=args.get("days"), platform=args.get("platform", ""),  # CB-132: inherit from ROOT_ADMIN
            enabled=_to_bool(args.get("enabled"), True),
            chat_id=task_chat_id,
        )
        # Auto-mark onboarding schedule_tasks step done (if active)
        if task_chat_id and _sk:
            try:
                _sk_src = _sk.split(":")[0] if ":" in _sk else "telegram"
                from bot.commands import _mark_onboarding_step_done
                import asyncio
                asyncio.ensure_future(_mark_onboarding_step_done(_sk_src, task_chat_id, task_chat_id, "schedule_tasks"))
            except Exception:
                pass
        return _text_response({"status": "created", "task": task})

    elif action == "edit":
        task_id = args.get("task_id")
        if not task_id:
            return _text_response({"error": "task_id is required for edit"})
        # Ownership check: non-admin can only edit own tasks.
        # Tasks with empty chat_id are admin-created (system tasks) — block non-admin.
        if not _is_admin_session:
            existing = get_task(task_id)
            if not existing:
                return _text_response({"error": f"Task {task_id} not found"})
            if not existing.get("chat_id") or existing.get("chat_id", "") not in (_own_chat, _own_chat_with_thread):
                return _text_response({"error": "Non-admin users can only edit their own tasks"})
        updates = {}
        for key in ("name", "hour", "minute", "days", "prompt", "platform", "enabled"):
            if key in args and args[key] is not None:
                val = args[key]
                if key in ("hour", "minute"):
                    val = _safe_int(val, 0)
                elif key == "enabled":
                    val = bool(val)
                updates[key] = val
        # Non-admin cannot change chat_id on edit (prevents scope hijacking)
        if "chat_id" in args and args["chat_id"] is not None:
            if _is_admin_session:
                updates["chat_id"] = args["chat_id"]
        task = update_task(task_id, **updates)
        if task:
            return _text_response({"status": "updated", "task": task})
        return _text_response({"error": f"Task {task_id} not found"})

    elif action == "delete":
        task_id = args.get("task_id")
        if not task_id:
            return _text_response({"error": "task_id is required for delete"})
        # Ownership check: empty chat_id = admin system task, block non-admin
        if not _is_admin_session:
            existing = get_task(task_id)
            if not existing:
                return _text_response({"error": f"Task {task_id} not found"})
            if not existing.get("chat_id") or existing.get("chat_id", "") not in (_own_chat, _own_chat_with_thread):
                return _text_response({"error": "Non-admin users can only delete their own tasks"})
        if delete_task(task_id):
            return _text_response({"status": "deleted", "task_id": task_id})
        return _text_response({"error": f"Task {task_id} not found"})

    elif action == "toggle":
        task_id = args.get("task_id")
        if not task_id:
            return _text_response({"error": "task_id is required for toggle"})
        # Ownership check: empty chat_id = admin system task, block non-admin
        if not _is_admin_session:
            existing = get_task(task_id)
            if not existing:
                return _text_response({"error": f"Task {task_id} not found"})
            if not existing.get("chat_id") or existing.get("chat_id", "") not in (_own_chat, _own_chat_with_thread):
                return _text_response({"error": "Non-admin users can only toggle their own tasks"})
        task = toggle_task(task_id)
        if task:
            return _text_response({"status": "toggled", "task": task})
        return _text_response({"error": f"Task {task_id} not found"})

    else:
        return _text_response({"error": f"Unknown action: {action}. Use: add, edit, delete, toggle"})


@tool("manage_chat", "Register, update, or remove chats/topics in the unified chat registry. Admin only. "
      "Register with harness= for ONE-STEP setup (assigns harness + auto-RBAC role). "
      "Modes: active_plus (full UX, default), active (no streaming, 3P chats), silent (listen only).", {
    "action": {"type": "string", "description": "list, get, register, set_mode, unregister"},
    "chat_key": {"type": "string", "description": "Chat key: telegram:<chat_id> or telegram:<chat_id>:<thread_id>"},
    "title": {"type": "string", "description": "Display name (for register)"},
    "mode": {"type": "string", "description": "active_plus, active, or silent"},
    "parent": {"type": "string", "description": "Parent chat key for forum topics (optional)"},
    "harness": {"type": "string", "description": "Harness label to assign (for register). Auto-assigns RBAC role from harness.default_role."},
})
async def manage_chat_tool(args: dict[str, Any]) -> dict:
    """Manage the unified chat registry (CB-5: unified admin model)."""
    _sk = args.pop("_session_key", "")
    from bot.chat_registry import (
        register, unregister, set_mode, all_entries, get_entry, VALID_MODES, DEFAULT_MODE,
    )

    action = args.get("action", "list")

    if action == "list":
        entries = all_entries()
        if not entries:
            return _text_response("No registered chats.")
        from bot.harness import get_chat_harness_label
        lines = []
        for key, info in sorted(entries.items()):
            mode = info.get("mode", DEFAULT_MODE)
            title = info.get("title", "")
            parent = info.get("parent", "")
            hlabel = get_chat_harness_label(key)
            prefix = "  \u2514\u2500 " if parent else "  "
            harness_tag = f" harness={hlabel}" if hlabel else ""
            lines.append(f"{prefix}{key}: {title} [{mode}]{harness_tag}"
                         + (f" (parent: {parent})" if parent else ""))
        return _text_response(f"Registered chats ({len(entries)}):\n" + "\n".join(lines))

    chat_key = (args.get("chat_key") or "").strip()
    if not chat_key:
        return _text_response("Error: chat_key is required (e.g. telegram:-1001234567890)")

    if action == "get":
        entry = get_entry(chat_key)
        if not entry:
            return _text_response(f"Chat {chat_key} not registered.")
        from bot.harness import get_harness_for_chat, derive_reference_domains
        h = get_harness_for_chat(chat_key)
        refs = derive_reference_domains(h)
        info_lines = [
            f"<b>Chat:</b> {chat_key}",
            f"<b>Title:</b> {entry.get('title', '')}",
            f"<b>Mode:</b> {entry.get('mode', DEFAULT_MODE)}",
            f"<b>Harness:</b> {h.label}",
            f"<b>Model:</b> {h.model or 'default'}",
            f"<b>Default role:</b> {h.default_role or '(none)'}",
            f"<b>Domains:</b> {', '.join(h.domains or [])} (+ core implicit)",
            f"<b>Write domain:</b> {h.write_domain or '(chat-local)'}",
            f"<b>Reference domains:</b> {', '.join(refs) if refs else '(none)'}",
        ]
        if entry.get("parent"):
            info_lines.append(f"<b>Parent:</b> {entry['parent']}")
        return _text_response("\n".join(info_lines))

    if action == "register":
        title = args.get("title", "")
        mode = args.get("mode", DEFAULT_MODE)
        if mode not in VALID_MODES:
            return _text_response(f"Error: mode must be one of {VALID_MODES}")
        parent = args.get("parent", "")
        harness_label = (args.get("harness") or "").strip()

        # Validate harness before registering (fail fast)
        if harness_label:
            from bot.harness import list_harnesses
            if harness_label not in list_harnesses():
                return _text_response(f"Error: unknown harness '{harness_label}'. Use manage_harness list to see available.")

        register(chat_key, title=title, mode=mode, parent=parent)

        result_parts = [f"Registered: {chat_key} [{mode}] title={title!r}"]

        # CB-5: ONE-STEP — assign harness + auto-RBAC role
        if harness_label:
            from bot.harness import assign_chat_harness
            h = await assign_chat_harness(chat_key, harness_label)
            result_parts.append(f"Harness: {harness_label}")
            # Report actual RBAC assignment status (not just intent)
            _rbac_status = getattr(h, "_rbac_status", "unknown")
            if _rbac_status == "assigned":
                result_parts.append(f"RBAC role: {h.default_role} (auto-assigned \u2705)")
            elif _rbac_status == "skipped_admin":
                result_parts.append(f"RBAC role: kept admin-granted role (harness default: {h.default_role})")
            elif _rbac_status == "failed":
                result_parts.append(f"\u26a0\ufe0f RBAC role assignment FAILED — {h.default_role} not assigned. Check logs.")
            elif h.default_role:
                result_parts.append(f"RBAC role: {h.default_role} (status: {_rbac_status})")
            if h.write_domain:
                result_parts.append(f"Write domain: {h.write_domain}")

        # Audit log (CB-50)
        from bot.audit import log_admin_action
        await log_admin_action("chat.register", _sk, chat_key, "success",
                               {"harness": harness_label or "(none)", "mode": mode})
        return _text_response("\n".join(result_parts))

    elif action == "set_mode":
        mode = args.get("mode", "")
        if mode not in VALID_MODES:
            return _text_response(f"Error: mode must be one of {VALID_MODES}")
        try:
            set_mode(chat_key, mode)
            from bot.audit import log_admin_action
            await log_admin_action("chat.set_mode", _sk, chat_key, "success", {"mode": mode})
            return _text_response(f"Mode updated: {chat_key} \u2192 {mode}")
        except KeyError:
            return _text_response(f"Error: {chat_key} not registered")

    elif action == "unregister":
        if unregister(chat_key):
            from bot.audit import log_admin_action
            await log_admin_action("chat.unregister", _sk, chat_key, "success", {})
            return _text_response(f"Unregistered: {chat_key}")
        return _text_response(f"Error: {chat_key} not found")

    return _text_response(f"Unknown action: {action}. Use: list, get, register, set_mode, unregister")


# ============================================================
# save_report — HTML report hosting
# ============================================================

@tool("save_report", "Save an HTML report and get a hosted URL. The report is stored per-chat and served at a public URL with an unguessable UUID. Auto-cleaned after 365 days.", {
    "html_content": {"type": "string", "description": "Complete self-contained HTML string"},
    "title": {"type": "string", "description": "Optional title for logging"},
})
async def save_report_tool(args: dict[str, Any]) -> dict:
    sk = args.pop("_session_key", "")
    html_content = args.get("html_content", "")
    title = args.get("title", "")

    if not html_content or not html_content.strip():
        return _text_response({"error": "html_content is required"})

    # Size cap: 10 MB (prevents disk exhaustion on shared bot-data volume)
    _MAX_REPORT_BYTES = 10 * 1024 * 1024
    if len(html_content) > _MAX_REPORT_BYTES:
        return _text_response({"error": f"Report too large ({len(html_content) // 1024}KB, max {_MAX_REPORT_BYTES // 1024}KB)"})

    # Use session_key as chat_id for per-chat isolation (includes thread_id)
    chat_id = sk or "unknown"

    from bot.reports import save_report
    result = save_report(html_content=html_content, title=title, chat_id=chat_id)
    return _text_response(result)


# ============================================================
# ============================================================
# search_artifacts — cross-session coding artifact discovery (CB-113)
# ============================================================

@tool("search_artifacts", "Search coding artifacts (scripts, configs, generated files) across all sessions. Returns file paths, descriptions, and metadata.", {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query (FTS5 full-text)"},
        "domain_id": {"type": "string", "description": "Optional domain filter"},
    },
    "required": ["query"],
})
async def search_artifacts_tool(args: dict[str, Any]) -> dict:
    """Search coding artifacts for cross-session discovery."""
    args.pop("_session_key", "")
    query = args.get("query", "")
    domain_id = args.get("domain_id", "")
    if not query:
        return _text_response({"error": "query required"})

    async def _do():
        from bot.storage.artifacts import search_artifacts
        results = await search_artifacts(query, domain_id=domain_id)
        return {"results": results, "count": len(results)}

    return await _run_with_timeout(_do(), "search_artifacts")


# ============================================================
# Tool list for server registration
# ============================================================

SERVICE_TOOLS = [
    phone_call, phone_get_transcript,
    generate_image, edit_image, download_gmail_attachment,
    google_flights_search, google_flights_search_dates,
    switch_model, switch_effort, extend_budget, usage_report,
    switch_oauth, manage_oauth, reset_session_tool,
    manage_harness, manage_external_chat_tool, manage_chat_tool,
    list_scheduled_tasks, manage_scheduled_task,
    save_report_tool,
    search_artifacts_tool,
]


# ============================================================
# manage_skill — runtime skill editing (admin only)
# ============================================================

@tool("manage_skill", "View, edit, diff, or revert a skill's SKILL.md at runtime without deploying. Admin only. Actions: list (all skills), view <name>, edit <name> (provide content), diff <name> (show changes vs repo), revert <name> (restore from repo).", {
    "action": {"type": "string", "description": "list | view | edit | diff | revert"},
    "name": {"type": "string", "description": "Skill name (directory name in .claude/skills/)"},
    "content": {"type": "string", "description": "New SKILL.md content (for edit action only)"},
})
async def manage_skill_tool(args: dict[str, Any]) -> dict:
    sk = args.pop("_session_key", "")
    action = args.get("action", "list")
    name = args.get("name", "")
    content = args.get("content", "")

    from pathlib import Path
    import re
    skills_dir = Path("/app/.claude/skills")
    repo_skills_dir = Path("/host-repo/.claude/skills")

    if action == "list":
        if not skills_dir.exists():
            return _text_response({"skills": []})
        skill_names = sorted(d.name for d in skills_dir.iterdir()
                             if d.is_dir() and (d / "SKILL.md").exists())
        return _text_response({"skills": skill_names, "count": len(skill_names)})

    if not name or not re.match(r"^[a-z0-9][a-z0-9_-]*$", name):
        return _text_response({"error": "Invalid skill name (lowercase alphanumeric, hyphens, underscores)"})

    skill_path = skills_dir / name / "SKILL.md"
    repo_path = repo_skills_dir / name / "SKILL.md"

    if action == "view":
        if not skill_path.exists():
            return _text_response({"error": f"Skill '{name}' not found"})
        return _text_response({"name": name, "content": skill_path.read_text()[:50000],
                               "size": skill_path.stat().st_size})

    elif action == "edit":
        if not content:
            return _text_response({"error": "content is required for edit action"})
        _MAX_SKILL_BYTES = 500 * 1024  # 500 KB
        if len(content) > _MAX_SKILL_BYTES:
            return _text_response({"error": f"Content too large ({len(content)} bytes, max {_MAX_SKILL_BYTES})"})
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        # Auto-backup
        if skill_path.exists():
            backup = skill_path.with_suffix(".prev.md")
            backup.write_text(skill_path.read_text())
        skill_path.write_text(content)
        # Invalidate module cache
        from bot.harness import _module_cache, _module_mtime
        _module_cache.pop(name, None)
        _module_mtime.pop(name, None)
        log.info("Skill '%s' edited at runtime (%d bytes, session=%s)", name, len(content), sk)
        return _text_response({"success": True, "name": name, "size": len(content),
                               "message": "Skill updated. Cache invalidated. /reset sessions to pick up changes."})

    elif action == "diff":
        if not skill_path.exists():
            return _text_response({"error": f"Skill '{name}' not found in running copy"})
        if not repo_path.exists():
            return _text_response({"error": f"Skill '{name}' not found in repo"})
        running = skill_path.read_text()
        repo = repo_path.read_text()
        if running == repo:
            return _text_response({"name": name, "diff": "No differences", "identical": True})
        # Simple line diff
        import difflib
        diff = "\n".join(difflib.unified_diff(
            repo.splitlines(), running.splitlines(),
            fromfile=f"repo/{name}", tofile=f"running/{name}", lineterm=""))
        return _text_response({"name": name, "diff": diff[:10000], "identical": False})

    elif action == "revert":
        if not repo_path.exists():
            return _text_response({"error": f"Skill '{name}' not found in repo"})
        repo_content = repo_path.read_text()
        if skill_path.exists():
            backup = skill_path.with_suffix(".prev.md")
            backup.write_text(skill_path.read_text())
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(repo_content)
        from bot.harness import _module_cache, _module_mtime
        _module_cache.pop(name, None)
        _module_mtime.pop(name, None)
        log.info("Skill '%s' reverted to repo version (session=%s)", name, sk)
        return _text_response({"success": True, "name": name,
                               "message": "Skill reverted to repo version. Cache invalidated."})

    return _text_response({"error": f"Unknown action: {action}. Use: list, view, edit, diff, revert"})


# Append after definition (SERVICE_TOOLS defined above, before manage_skill_tool)
SERVICE_TOOLS.append(manage_skill_tool)

__all__ = [
    "_GFLIGHTS_SEM", "_format_flight_result",
    "phone_call", "phone_get_transcript",
    "generate_image", "edit_image", "download_gmail_attachment",
    "google_flights_search", "google_flights_search_dates",
    "switch_model", "switch_effort", "switch_oauth", "manage_oauth",
    "extend_budget", "usage_report", "reset_session_tool",
    "manage_harness", "manage_external_chat_tool",
    "list_scheduled_tasks", "manage_scheduled_task",
    "save_report_tool",
    "manage_skill_tool",
    "SERVICE_TOOLS",
]
