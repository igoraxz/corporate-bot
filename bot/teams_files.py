# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Teams file sharing — OneDrive/SharePoint integration.

Handles sending files to Teams conversations:
- 1:1 DMs: FileConsentCard → user approves → bot uploads to OneDrive
- Channels/groups: Upload to SharePoint via Graph API, share link

Also handles receiving files from Teams (attachments in activities).

Dependencies: MS Graph API access (via per-user token or app token).
"""

import logging
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_GRAPH_API = "https://graph.microsoft.com/v1.0"


# ============================================================
# Sending Files
# ============================================================

async def send_file_to_dm(
    user_token: str,
    conversation_id: str,
    file_path: str,
    file_name: str | None = None,
) -> dict:
    """Send a file to a Teams 1:1 DM via OneDrive upload.

    Flow:
    1. Upload file to user's OneDrive (app folder)
    2. Create sharing link
    3. Send message with file attachment reference

    Args:
        user_token: User's MS365 access token (from token vault)
        conversation_id: Teams conversation ID
        file_path: Local path to file
        file_name: Display filename (defaults to basename)

    Returns: {"status": "ok", "file_id": "...", "web_url": "..."}
    """
    if not file_name:
        file_name = Path(file_path).name
    # SECURITY: sanitize filename (prevent path traversal in OneDrive)
    file_name = Path(file_name).name  # strip directory components
    if ".." in file_name or "/" in file_name or "\\" in file_name:
        file_name = "file"

    file_content = Path(file_path).read_bytes()
    file_size = len(file_content)

    async with httpx.AsyncClient(timeout=60) as http:
        # Upload to OneDrive app folder
        upload_url = f"{_GRAPH_API}/me/drive/root:/BotFiles/{file_name}:/content"
        resp = await http.put(
            upload_url,
            content=file_content,
            headers={
                "Authorization": f"Bearer {user_token}",
                "Content-Type": "application/octet-stream",
            },
        )
        resp.raise_for_status()
        item = resp.json()

        file_id = item.get("id", "")
        web_url = item.get("webUrl", "")

        log.info("File uploaded to OneDrive: %s (%d bytes) → %s",
                 file_name, file_size, web_url[:50])

        return {
            "status": "ok",
            "file_id": file_id,
            "web_url": web_url,
            "file_name": file_name,
            "size_bytes": file_size,
        }


async def send_file_link_to_channel(
    app_token: str,
    site_id: str,
    drive_id: str,
    file_path: str,
    file_name: str | None = None,
) -> dict:
    """Upload file to SharePoint and return sharing link for channel message.

    For channels, we can't use FileConsentCard. Instead:
    1. Upload to team's SharePoint document library
    2. Create a sharing link
    3. Include link in the bot's text response

    Args:
        app_token: App/delegated token with Files.ReadWrite.All
        site_id: SharePoint site ID for the team
        drive_id: Document library drive ID
        file_path: Local path to file
        file_name: Display filename

    Returns: {"status": "ok", "share_url": "...", "file_name": "..."}
    """
    if not file_name:
        file_name = Path(file_path).name
    # SECURITY: sanitize filename (prevent path traversal in SharePoint)
    file_name = Path(file_name).name  # strip directory components (also removes .., /, \)

    file_content = Path(file_path).read_bytes()

    async with httpx.AsyncClient(timeout=60) as http:
        upload_url = f"{_GRAPH_API}/drives/{drive_id}/root:/BotShared/{file_name}:/content"
        resp = await http.put(
            upload_url,
            content=file_content,
            headers={
                "Authorization": f"Bearer {app_token}",
                "Content-Type": "application/octet-stream",
            },
        )
        resp.raise_for_status()
        item = resp.json()

        # Create sharing link
        item_id = item.get("id", "")
        share_url = item.get("webUrl", "")

        return {
            "status": "ok",
            "share_url": share_url,
            "file_name": file_name,
            "item_id": item_id,
        }


# ============================================================
# Receiving Files (from Teams attachments)
# ============================================================

async def download_teams_attachment(
    attachment: dict,
    service_url: str,
    bot_token: str,
    download_dir: str = "/app/data/tmp",
) -> str | None:
    """Download a file attachment from a Teams message.

    Teams file attachments have a downloadUrl that requires bot auth.

    Args:
        attachment: Attachment dict from Teams activity
        service_url: The activity's serviceUrl
        bot_token: Bot's access token
        download_dir: Local directory to save file

    Returns: Local file path, or None on failure.
    """
    content_url = attachment.get("contentUrl", "")
    raw_name = attachment.get("name", "attachment")
    _content_type = attachment.get("contentType", "")

    if not content_url:
        # For inline/content attachments (adaptive cards, etc.)
        return None

    # SECURITY: Validate contentUrl against Microsoft domains (prevent SSRF)
    from urllib.parse import urlparse
    parsed_url = urlparse(content_url)
    hostname = (parsed_url.hostname or "").lower()
    _ALLOWED_DOWNLOAD_DOMAINS = (
        ".sharepoint.com", ".sharepoint-df.com", ".blob.core.windows.net",
        ".microsoft.com", ".office.com", ".office365.com",
    )
    if not parsed_url.scheme == "https" or not any(hostname.endswith(d) for d in _ALLOWED_DOWNLOAD_DOMAINS):
        log.warning("Teams attachment URL rejected (SSRF protection): %s", content_url[:80])
        return None

    # SECURITY: Sanitize filename (prevent path traversal)
    import re
    file_name = Path(raw_name).name  # strip directory components
    file_name = re.sub(r'[/\\]', '_', file_name)  # extra safety
    if not file_name or file_name.startswith('.'):
        file_name = "attachment"

    try:
        async with httpx.AsyncClient(timeout=60) as http:
            resp = await http.get(
                content_url,
                headers={"Authorization": f"Bearer {bot_token}"},
                follow_redirects=False,  # SECURITY: no redirects (prevent SSRF bounce)
            )
            resp.raise_for_status()

            # Save to temp dir with sanitized filename
            download_path = Path(download_dir) / f"teams_{int(time.time())}_{file_name}"
            download_path.parent.mkdir(parents=True, exist_ok=True)
            # SECURITY: verify resolved path stays within download_dir
            resolved = str(download_path.resolve())
            if not resolved.startswith(str(Path(download_dir).resolve())):
                log.warning("Path traversal blocked: %s", resolved)
                return None
            download_path.write_bytes(resp.content)

            log.info("Downloaded Teams attachment: %s (%d bytes) → %s",
                     file_name, len(resp.content), download_path)
            return str(download_path)

    except Exception as e:
        log.error("Failed to download Teams attachment %s: %s", file_name, e)
        return None


# ============================================================
# FileConsentCard (for 1:1 file upload with user approval)
# ============================================================

def build_file_consent_card(file_name: str, file_size: int) -> dict:
    """Build a FileConsentCard that asks user permission to upload.

    Teams requires user consent before bot can upload to their OneDrive.
    This creates the consent card attachment.

    The user clicks "Accept" → Teams sends an invoke activity back to bot
    with the upload URL → bot uploads the file.
    """
    return {
        "contentType": "application/vnd.microsoft.teams.card.file.consent",
        "name": file_name,
        "content": {
            "description": f"I'd like to share a file: {file_name} ({file_size:,} bytes)",
            "sizeInBytes": file_size,
            "acceptContext": {
                "file_name": file_name,
            },
            "declineContext": {
                "file_name": file_name,
            },
        },
    }
