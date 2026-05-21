# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Custom MCP tool servers for Claude Agent SDK.

Backwards-compatibility shim — all tools and factories have moved to bot.mcp package.
This module re-exports everything so existing imports continue to work:
  - from bot.mcp_tools import get_custom_mcp_servers
  - from bot.mcp_tools import create_messaging_server
  - from bot.mcp_tools import desktop_relay_tool
  - import bot.mcp_tools as tools
"""

from bot.mcp import *  # noqa: F401, F403
