---
description: "Sending files, photos, and documents to users on Telegram. TRIGGER when: sending any file/photo/document/image to chat, delivering downloaded attachments, sharing screenshots, sending generated images, or using telegram_send_photo, telegram_send_document tools. Also trigger when user asks to send/share/forward a file or document."
---

# Sending Files

## For Telegram
- Photos/images → use `telegram_send_photo` tool (photo_path, optional caption)
- Documents (PDF, etc.) → use `telegram_send_document` tool (document_path, optional caption)
- Files must be saved to a local path first (e.g. from get_gmail_attachment_content).

## Workflow
1. Obtain the file (e.g. download email attachment via `get_gmail_attachment_content`)
2. Send via the appropriate tool for the originating platform
3. Files are stored temporarily in the data/tmp/ directory

## NO BASH FOR MEDIA — GLOBAL RULE
Do NOT use Bash for ANY media/file operations. Specifically:
- Do NOT use Bash to ls, cp, mv, rename, or verify media files
- Do NOT use Bash to check if a file exists — just try to send it; the tool will error if missing
- Do NOT use Bash to convert, resize, or process images
- Use ONLY the dedicated tools: telegram_send_photo, telegram_send_document,
  get_document, search_media, get_gmail_attachment_content, generate_image, edit_image
- The only acceptable Bash use for files: downloading from a URL with curl when no other tool works
