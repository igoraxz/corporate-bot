---
description: "Sending emails and downloading Gmail attachments. TRIGGER when: user asks to send/write/draft/compose an email, email someone (school, doctor, business), forward an email, reply to an email, download/get/send an email attachment, or mentions send_gmail_message, download_gmail_attachment tools. Also trigger when showing email drafts for approval."
---

# Email Sending (REQUIRES APPROVAL)

You can send emails on behalf of the configured primary email user using the `send_gmail_message` tool.
This is an OUTBOUND ACTION — it ALWAYS requires explicit approval before sending.

## Workflow
1. User asks to send an email (e.g. "email the school about an absence tomorrow")
2. Draft the email and show a PREVIEW on the SAME platform the request came from:
   📧 **Email Draft**
   **To:** recipient@example.com
   **Subject:** Your subject here
   **Body:**
   Your email body here...

   Reply "send it" to send, "edit" to change, or "cancel" to discard.
3. WAIT for the user to reply with approval in the NEXT message
4. On approval ("send it", "go ahead", "yes", "confirm") — use `send_gmail_message` tool
5. On rejection ("cancel", "no") — discard
6. On edit request ("edit", "change") — revise and show new preview
7. After sending, confirm: "✅ Email sent to recipient@example.com"

## Important Rules
- NEVER send an email without showing a preview and getting explicit approval first
- Only one Gmail account is configured — if asked to send from another, explain this
- Include proper greeting and sign-off appropriate to the recipient
- Match the language of the email to the context

---

# Gmail Attachments

Use `download_gmail_attachment` to download and save an email attachment to disk.
This is the PREFERRED method — works in all chat tiers (no Bash needed).

Parameters:
  - message_id: Gmail message ID (from search_gmail_messages)
  - attachment_id: Attachment ID (from get_gmail_message_content)
  - filename: desired filename (e.g. "schedule.pdf")
  - user_google_email: (optional) which Google account to use (default: primary)

Returns: `{"file_path": "/app/data/tmp/schedule.pdf", "size_bytes": 12345}`

## Workflow
1. `search_gmail_messages` → find the email
2. `get_gmail_message_content` → get message details including attachment IDs
3. `download_gmail_attachment` → download attachment to disk (returns file_path)
4. Send to user via `telegram_send_document` (see file-sending skill)

NOTE: The older `get_gmail_attachment_content` tool (from workspace-mcp) also works but
returns raw base64 data requiring Bash to decode — prefer `download_gmail_attachment`.
