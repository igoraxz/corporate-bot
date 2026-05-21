# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""bot.auth — Authentication and token management for corporate bot.

Modules:
- token_vault: Per-user encrypted OAuth token storage + OBO flow
- credentials: Universal credential store (typed, scoped, encrypted)
- oauth_flow: OAuth authentication flows for Claude/M365/Gmail (B5a, B5c)
- schema: OAuth token table DDL (B5a)
- in_chat_auth: In-chat auth MCP tools (B5e)
"""
