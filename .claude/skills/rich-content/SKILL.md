---
description: "Rich content delivery: TG-native HTML, hosted reports via save_report, document attachments, platform-specific formatting."
---

# Rich Content Delivery — Telegram + Teams

Default: short, fact-dense inline messages. Rich content is for specific use cases only.
Inline messages up to ~4-6K chars are fine — don't reach for rich content unless the
content genuinely benefits from visual formatting.

3 delivery methods (both platforms):

## 1. Text Message (default)
Short replies, coding discussions, status updates, confirmations.
- **Telegram**: HTML parse_mode (`<b>`, `<i>`, `<code>`, `<pre>`, `<a>`, `<blockquote>`)
- **Teams**: Markdown (`**bold**`, `*italic*`, `` `code` ``, `[link](url)`)
- `<blockquote expandable>` for collapsible detail (TG only)

## 2. Screenshot PNG (inline visual)
Charts, tables, formulas — displayed inline in chat, no click needed.
- Write HTML to `/app/data/tmp/chart-{unique}.html` (Chart.js/KaTeX CDN + Tailwind)
- `browser_navigate` → `browser_take_screenshot` → send as photo/image
- **Telegram**: `telegram_send_photo`
- **Teams**: `teams_send_message` with image attachment (HTTPS URL from report server)
- Great for: bar/line/pie charts, data tables, comparison grids, LaTeX formulas

**LaTeX/math — 3 levels:**
- **Simple inline** (E=mc², x², α, ∑): Unicode characters in text
- **Single formula**: HTML + KaTeX CDN → screenshot → send as photo
- **Math article**: HTML + KaTeX → Playwright `page.pdf()` → send as document

## 3. Hosted HTML Link (full report)
Travel itineraries, research, dashboards — visual content that needs CSS/tables/images.
- `save_report(html_content, title)` → returns UUID URL + file path
- **Telegram**: send file via `telegram_send_document` + URL as `<a href>` link
- **Teams**: send URL as markdown link `[View Report](url)` in message
- Reports use UUID-based URLs (122-bit entropy, unguessable)
- Reports served via HTTPS when behind a reverse proxy (Cloudflare Tunnel recommended)

## WHEN TO USE EACH

- **Quick answer, code, status?** → Text message
- **Chart, graph, table, formula?** → Screenshot PNG (inline)
- **Travel plan, research, dashboard, long analysis?** → Hosted HTML report link
- **Coding, planning, admin chat?** → Always text (no rich content)

## Combining Formats

For complex deliveries:
- Chat summary + hosted report link (travel, research)
- Chat summary + inline chart photo (data analysis)
- Chat summary + report link + chart photos (full analysis with visuals)
