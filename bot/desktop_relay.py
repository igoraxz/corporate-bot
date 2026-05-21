# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Employee workstation control — backwards compatibility shim.

All functionality moved to bot.relay package (Phase 1A refactor).
This module re-exports everything for backwards compatibility.
The relay package provides employee Mac workstation management.
"""
from bot.relay import *  # noqa: F401,F403
from bot.relay.ssh import *  # noqa: F401,F403
from bot.relay.registry import *  # noqa: F401,F403
from bot.relay.oauth import *  # noqa: F401,F403
from bot.relay.sessions import *  # noqa: F401,F403
from bot.relay.watchdog import *  # noqa: F401,F403
from bot.relay.core import *  # noqa: F401,F403
