#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""One-time interactive setup for the Pyrogram MTProto userbot session.

Run inside the container:
    docker exec -it <container> python /host-repo/scripts/setup_tg_userbot.py

This will:
1. Ask for your phone number
2. Send a Telegram login code
3. Create a .session file in DATA_DIR/tg_session/

The session file persists in the Docker volume. You only need to run this once
unless the session is revoked (e.g., from Telegram settings > Active Sessions).

Prerequisites:
- TG_API_ID and TG_API_HASH must be set in .env
  (get them from https://my.telegram.org/apps)
"""

import asyncio
import os
import sys
from pathlib import Path

# Add parent dir to path so we can import config
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()


async def main():
    api_id = int(os.environ.get("TG_API_ID", "0"))
    api_hash = os.environ.get("TG_API_HASH", "")

    if not api_id or not api_hash:
        print("ERROR: TG_API_ID and TG_API_HASH must be set in .env")
        print("Get them from https://my.telegram.org/apps")
        sys.exit(1)

    data_dir = Path(os.environ.get("DATA_DIR", "/app/data"))
    session_dir = data_dir / "tg_session"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_name = os.environ.get("TG_SESSION_NAME", "corporate_bot")
    session_path = str(session_dir / session_name)

    print(f"Session will be saved to: {session_path}.session")
    print()

    try:
        from pyrogram import Client
    except ImportError:
        print("ERROR: pyrogram not installed. Run: pip install pyrotgfork")
        sys.exit(1)

    client = Client(
        name=session_path,
        api_id=api_id,
        api_hash=api_hash,
    )

    print("Starting Pyrogram authentication...")
    print("You will be asked for your phone number and a login code.")
    print()

    async with client:
        me = await client.get_me()
        print()
        print(f"✅ Authenticated as: {me.first_name} (ID: {me.id})")
        print(f"   Session saved to: {session_path}.session")
        print()
        print("You can now restart the bot — it will use this session automatically.")


if __name__ == "__main__":
    asyncio.run(main())
