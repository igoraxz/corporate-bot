# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Adaptive Cards for Teams UX — structured rich UI components.

Teams Adaptive Cards (schema v1.6) provide forms, buttons, dropdowns,
and structured layouts that replace TG's text-only responses.

Card types:
- Welcome: first-time user onboarding
- Auth prompt: SSO consent request
- Error: structured error with action suggestions
- Search results: domain knowledge results with source attribution
- Domain list: available domains with health status
- Status: bot health and session info

Usage:
    from bot.teams_cards import build_welcome_card, build_error_card
    card = build_welcome_card(user_name="Admin", domains=["eng", "hr"])
    await teams_client.send_activity(conv_id, {
        "type": "message",
        "attachments": [card_attachment(card)],
    })
"""



def card_attachment(card_body: dict) -> dict:
    """Wrap an Adaptive Card body into a Teams attachment object."""
    return {
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.6",
            **card_body,
        },
    }


# ============================================================
# Welcome / Onboarding Card (CB-24)
# ============================================================

def build_welcome_card(
    user_name: str,
    available_domains: list[str] | None = None,
    has_sso_token: bool = False,
) -> dict:
    """Build welcome card for first-time users.

    Shows: greeting, capabilities, available domains, SSO status.
    """
    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": f"Welcome, {user_name}! \U0001f44b",
            "size": "Large",
            "weight": "Bolder",
        },
        {
            "type": "TextBlock",
            "text": "I'm your AI assistant. I can help with:",
            "wrap": True,
        },
        {
            "type": "FactSet",
            "facts": [
                {"title": "\U0001f4da", "value": "Search knowledge bases (policies, runbooks, docs)"},
                {"title": "\U0001f4bb", "value": "Execute code in a sandboxed environment"},
                {"title": "\U0001f4c5", "value": "Check your calendar and email (after SSO)"},
                {"title": "\U0001f50d", "value": "Browse websites and research topics"},
                {"title": "\u23f0", "value": "Set reminders and scheduled tasks"},
            ],
        },
    ]

    # Domain info
    if available_domains:
        body.append({
            "type": "TextBlock",
            "text": f"**Knowledge domains available:** {', '.join(available_domains)}",
            "wrap": True,
            "spacing": "Medium",
        })

    # SSO status
    if not has_sso_token:
        body.append({
            "type": "TextBlock",
            "text": "\u26a0\ufe0f **Personal access not connected.** "
                    "I can still search knowledge bases, but to access your "
                    "email and calendar, please authorize below.",
            "wrap": True,
            "spacing": "Medium",
            "color": "Warning",
        })
        # Note: actual SSO trigger is via Teams webApplicationInfo in manifest
        # This is just informational
    else:
        body.append({
            "type": "TextBlock",
            "text": "\u2705 Personal access connected — I can read your email and calendar.",
            "wrap": True,
            "spacing": "Medium",
            "color": "Good",
        })

    # Quick start commands
    body.append({
        "type": "TextBlock",
        "text": "**Quick commands:**",
        "spacing": "Large",
        "weight": "Bolder",
    })
    body.append({
        "type": "ColumnSet",
        "columns": [
            {"type": "Column", "width": "auto", "items": [
                {"type": "TextBlock", "text": "`/help`"},
                {"type": "TextBlock", "text": "`/domains`"},
                {"type": "TextBlock", "text": "`/status`"},
            ]},
            {"type": "Column", "width": "stretch", "items": [
                {"type": "TextBlock", "text": "Show all commands"},
                {"type": "TextBlock", "text": "List available knowledge domains"},
                {"type": "TextBlock", "text": "Check bot status"},
            ]},
        ],
    })

    return {"body": body}


# ============================================================
# Error Card
# ============================================================

def build_error_card(
    title: str,
    message: str,
    suggestion: str = "",
) -> dict:
    """Build an error card with structured info."""
    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": f"\u274c {title}",
            "size": "Medium",
            "weight": "Bolder",
            "color": "Attention",
        },
        {
            "type": "TextBlock",
            "text": message,
            "wrap": True,
        },
    ]
    if suggestion:
        body.append({
            "type": "TextBlock",
            "text": f"\U0001f4a1 **Suggestion:** {suggestion}",
            "wrap": True,
            "spacing": "Medium",
        })

    return {"body": body}


# ============================================================
# Auth Prompt Card (CB-24)
# ============================================================

def build_auth_prompt_card() -> dict:
    """Build OAuth consent prompt card.

    Note: actual SSO is handled by Teams infrastructure via manifest
    webApplicationInfo. This card is shown when SSO fails or needs
    explicit user action.
    """
    return {
        "body": [
            {
                "type": "TextBlock",
                "text": "\U0001f510 Authorization Required",
                "size": "Medium",
                "weight": "Bolder",
            },
            {
                "type": "TextBlock",
                "text": "To access your personal email and calendar, "
                        "I need your permission. This is a one-time setup.",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": "**What I'll be able to do:**\n"
                        "- Read your email (Mail.Read)\n"
                        "- View and create calendar events (Calendars.ReadWrite)\n"
                        "- See your profile info (User.Read)",
                "wrap": True,
                "spacing": "Medium",
            },
            {
                "type": "TextBlock",
                "text": "\U0001f512 Your data stays private — only accessible in this DM, "
                        "never shared with other users or channels.",
                "wrap": True,
                "spacing": "Medium",
                "isSubtle": True,
            },
        ],
        "actions": [
            {
                "type": "Action.Submit",
                "title": "Authorize Access",
                "data": {"action": "authorize_sso"},
            },
            {
                "type": "Action.Submit",
                "title": "Skip for Now",
                "data": {"action": "skip_sso"},
            },
        ],
    }


# ============================================================
# Search Results Card
# ============================================================

def build_search_results_card(
    query: str,
    results: list[dict],
    domains_searched: list[str],
) -> dict:
    """Build a card showing knowledge search results with source attribution."""
    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": f"\U0001f50d Search: \"{query}\"",
            "weight": "Bolder",
            "size": "Medium",
        },
        {
            "type": "TextBlock",
            "text": f"Searched domains: {', '.join(domains_searched)}",
            "isSubtle": True,
            "spacing": "None",
        },
    ]

    if not results:
        body.append({
            "type": "TextBlock",
            "text": "No relevant results found.",
            "spacing": "Medium",
        })
    else:
        for i, r in enumerate(results[:5], 1):
            body.append({
                "type": "Container",
                "spacing": "Medium",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": f"**{i}. [{r.get('domain', '?')}]** `{r.get('source', '?')}`"
                                f" (relevance: {r.get('relevance', 0):.0%})",
                        "wrap": True,
                    },
                    {
                        "type": "TextBlock",
                        "text": r.get("text", "")[:300],
                        "wrap": True,
                        "isSubtle": True,
                    },
                ],
            })

    return {"body": body}


# ============================================================
# Domain Health Card (CB-30)
# ============================================================

def build_domain_health_card(domains: list[dict]) -> dict:
    """Build a card showing all domains with health status."""
    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": "\U0001f4da Knowledge Domains",
            "weight": "Bolder",
            "size": "Medium",
        },
    ]

    if not domains:
        body.append({"type": "TextBlock", "text": "No domains registered."})
    else:
        facts = []
        for d in domains:
            status_icon = "\u2705" if d.get("status") == "active" else "\u26a0\ufe0f"
            facts.append({
                "title": f"{status_icon} {d.get('id', '?')}",
                "value": f"{d.get('chunks', 0)} chunks | Last sync: {d.get('last_sync', 'never')}",
            })
        body.append({"type": "FactSet", "facts": facts})

    return {"body": body}


# ============================================================
# Status Card
# ============================================================

def build_status_card(
    version: str = "0.1.0",
    domains_count: int = 0,
    active_sessions: int = 0,
    users_with_tokens: int = 0,
) -> dict:
    """Build bot status card."""
    return {
        "body": [
            {
                "type": "TextBlock",
                "text": "\U0001f916 Bot Status",
                "weight": "Bolder",
                "size": "Medium",
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Version", "value": version},
                    {"title": "Domains", "value": str(domains_count)},
                    {"title": "Active sessions", "value": str(active_sessions)},
                    {"title": "Users with SSO", "value": str(users_with_tokens)},
                    {"title": "Status", "value": "\u2705 Operational"},
                ],
            },
        ],
    }
