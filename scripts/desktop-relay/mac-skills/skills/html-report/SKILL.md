---
description: "Professional corporate HTML reports and visualizations with Chart.js + Tailwind CSS. TRIGGER when: user asks to create an HTML report, make a business dashboard, generate a corporate infographic, build a data visualization, create a formatted report, make a one-pager, or create any styled HTML output."
---

# Corporate HTML Report Creation -- Dual-Mode

Create professional corporate HTML reports, dashboards, and visualizations.
Two rendering modes based on where the report will be consumed.

## Mode Detection -- MANDATORY

Before writing any HTML, determine the mode:

| Mode | When | How detected |
|------|------|-------------|
| **tg-native** | Report sent as TG document attachment | Default for `source=telegram` chats |
| **teams-card** | Report delivered via Teams adaptive card or link | Default for Teams chats |
| **hosted** | Report viewed in browser via URL | When user says "host it" / "share link" / relay/coding chats |

**Decision flow:**
1. If user explicitly says "host" / "share link" / "URL" -> **hosted**
2. If source=telegram (check SESSION METADATA) -> **tg-native**
3. If source=teams -> **hosted** (Teams can render links; adaptive cards have size limits)
4. If relay/coding chat -> **hosted**
5. When in doubt -> generate BOTH: tg-native file + hosted URL

## Workflow

1. Determine report type (dashboard, infographic, timeline, one-pager, etc.)
2. Determine mode (tg-native vs hosted) per rules above
3. Plan sections and data visualizations
4. Write complete HTML file
5. **tg-native**: Write to `/app/data/tmp/report.html`, send via `telegram_send_document`
6. **hosted**: Call `save_report` MCP tool -> get `file_path` + `report_url` -> send both the document AND the URL link
7. **both**: Write tg-native to tmp, save hosted via `save_report`, send document + link

## Output Paths

- Admin/organization chats (TG): `/app/data/tmp/report.html`
- Hosted reports: use `save_report` tool (saves to `/app/data/reports/{chat_id}/{uuid}.html`)
- Sandbox/coding chats: `./report.html` (current directory)
- Relay chats (Mac): `/tmp/report.html`, use `[RELAY_FILE: /tmp/report.html]`

## Report Types

1. **Dashboard**: KPI cards + charts + tables (best for metrics/status)
2. **Infographic**: Scrollable visual story (best for presentations)
3. **Timeline**: Chronological events (best for project updates, history)
4. **One-pager**: Print-friendly summary (best for executive briefs)
5. **Data table**: Sortable, filterable data (best for detailed reports)
6. **Comparison**: Side-by-side analysis (best for options/decisions)

---

## MODE A: TG-Native

For reports sent as Telegram document attachments. TG's built-in HTML viewer
is limited -- NO JavaScript, NO external resources, NO CDN.

### TG-Native -- Mandatory Rules

- **NO `<script>` tags** -- TG viewer ignores ALL JavaScript
- **NO external URLs** -- no CDN links (Tailwind, Chart.js, Google Fonts, images)
- **ALL CSS in `<style>` block** in `<head>` -- TG viewer parses this correctly
- **Images as base64 data URIs** -- `data:image/jpeg;base64,...`
- **System fonts only** -- `-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif`
- **Mobile-first layout** -- TG opens in phone-width viewport
- **Simple block layout** -- no CSS Grid, no complex flexbox
- **No CSS custom properties** -- `var()` not supported
- **No @import** -- not supported

### TG-Native -- CSS Design System

Use hex colors directly, not Tailwind classes:
- Primary bg: #ffffff
- Surface/card: #f8fafc
- Text primary: #1e293b
- Text secondary: #64748b
- Accent (pick ONE): #2563eb (blue-600)
- Success: #16a34a / Warning: #d97706 / Danger: #dc2626

Card style: `background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 20px;`
Layout: `max-width: 600px; margin: 0 auto; padding: 16px;`

### TG-Native -- Charts Without JavaScript

Since Chart.js is unavailable, represent data visually using pure CSS:

**Horizontal bar chart (CSS-only):**
```html
<div style="margin-bottom: 8px;">
    <div style="display: flex; align-items: center; gap: 8px;">
        <span style="width: 80px; font-size: 13px; color: #64748b;">Label</span>
        <div style="flex: 1; background: #f1f5f9; border-radius: 6px; height: 24px;">
            <div style="width: 75%; background: #2563eb; border-radius: 6px; height: 100%;"></div>
        </div>
        <span style="font-size: 13px; font-weight: 600;">75%</span>
    </div>
</div>
```

### TG-Native Template Skeleton

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Report Title</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #ffffff; color: #1e293b; line-height: 1.6;
        }
        .container { max-width: 600px; margin: 0 auto; padding: 16px; }
        .header { background: #0f172a; color: #fff; padding: 32px 16px; text-align: center; }
        .header h1 { font-size: 24px; font-weight: 700; }
        .header p { color: #94a3b8; margin-top: 4px; font-size: 14px; }
        .card {
            background: #fff; border: 1px solid #e2e8f0; border-radius: 12px;
            padding: 20px; margin-bottom: 16px;
        }
        h2 { font-size: 20px; font-weight: 700; margin: 24px 0 12px; }
        .label { font-size: 12px; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; }
        .value { font-size: 28px; font-weight: 700; margin-top: 4px; }
        .positive { color: #16a34a; } .negative { color: #dc2626; }
        table { width: 100%; border-collapse: collapse; font-size: 14px; }
        th { background: #f8fafc; text-align: left; padding: 10px 12px; font-weight: 600; color: #64748b; }
        td { padding: 10px 12px; border-bottom: 1px solid #f1f5f9; }
        .footer { text-align: center; color: #94a3b8; font-size: 12px; padding: 24px 16px; }
        @media print { .no-print { display: none; } }
    </style>
</head>
<body>
    <div class="header">
        <h1>Report Title</h1>
        <p>Subtitle or date</p>
    </div>
    <div class="container">
        <!-- Content sections here -->
        <div class="footer">Generated on [DATE]</div>
    </div>
</body>
</html>
```

---

## MODE B: Hosted

For reports viewed in a browser via shareable URL. Full CDN access, JavaScript, interactivity.

### Hosted -- Workflow

1. Write HTML with Tailwind + Chart.js CDN
2. Call `save_report` MCP tool with the HTML content
3. Tool returns `{file_path, report_url, report_id}`
4. Send the `report_url` as a clickable link in chat
5. Optionally also send `file_path` as document attachment

### Hosted -- Mandatory Technical Requirements

- Complete HTML5 skeleton with proper DOCTYPE
- Tailwind CSS via CDN: `<script src="https://cdn.tailwindcss.com"></script>`
- Chart.js via CDN: `<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>`
- Self-contained: NO external dependencies beyond CDNs
- Responsive: works on mobile and desktop
- Print-friendly: include @media print styles

### Hosted -- Design System

Uses Tailwind utility classes. Typography: system-ui or Inter. Headings text-2xl to text-5xl.
Body text-base (16px min). Colors: bg-white/bg-slate-900 base. Accent: ONE of blue-600,
emerald-600, violet-600, amber-600. Layout: max-w-6xl mx-auto. Grid: grid-cols-1 md:grid-cols-2
lg:grid-cols-3. Cards: rounded-xl shadow-sm border p-6.

### Hosted Template Skeleton

```html
<!DOCTYPE html>
<html lang="en" class="scroll-smooth">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Report Title</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        @media print { .no-print { display: none; } .page-break { page-break-before: always; } }
    </style>
</head>
<body class="bg-white text-gray-900 antialiased">
    <header class="bg-slate-900 text-white py-16">
        <div class="max-w-6xl mx-auto px-6">
            <h1 class="text-4xl font-bold">Report Title</h1>
            <p class="text-slate-300 mt-2 text-lg">Subtitle</p>
        </div>
    </header>
    <section class="max-w-6xl mx-auto px-6 -mt-8">
        <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
            <div class="bg-white rounded-xl shadow-md border p-6">
                <p class="text-sm text-gray-500 uppercase tracking-wide">Metric</p>
                <p class="text-3xl font-bold mt-1">Value</p>
                <p class="text-green-600 text-sm mt-1">+Change</p>
            </div>
        </div>
    </section>
    <section class="max-w-6xl mx-auto px-6 py-12">
        <h2 class="text-2xl font-bold mb-6">Chart Section</h2>
        <div class="bg-white rounded-xl shadow-sm border p-6">
            <canvas id="chart1" style="max-height: 350px;"></canvas>
        </div>
    </section>
    <footer class="bg-gray-50 border-t py-8 mt-12">
        <div class="max-w-6xl mx-auto px-6 text-center text-gray-500 text-sm">
            Generated on <span id="date"></span>
        </div>
    </footer>
    <script>
        document.getElementById('date').textContent = new Date().toLocaleDateString();
        // Chart initialization here
    </script>
</body>
</html>
```

---

## save_report MCP Tool

```
save_report(html_content="<full HTML>", title="My Report")
-> {
    "file_path": "/app/data/reports/{chat_id}/a1b2c3d4-....html",
    "report_url": "http://host:8210/reports/a1b2c3d4-...",
    "report_id": "a1b2c3d4-..."
  }
```

The URL is shareable -- anyone with the link can view the report (UUID4 = unguessable).
Reports auto-expire after 365 days.

---

## Anti-Patterns

**Both modes:**
- No font sizes below 14px
- No walls of text without visual breaks
- No rainbow colors (one accent + gray scale)
- No missing responsive consideration

**TG-native specific:**
- No `<script>` tags of any kind
- No external URLs (CDN, images, fonts)
- No Tailwind classes (use inline/embedded CSS)
- No CSS Grid
- No CSS `var()` custom properties
- No `@import`

**Hosted specific:**
- No inline styles when Tailwind classes exist
- No JavaScript frameworks (vanilla JS + Chart.js only)

## Quality Checklist

- Mode matches destination (tg-native for TG, hosted for browser)
- Self-contained (no broken external references)
- Responsive layout (test at 375px width mentally for tg-native)
- Print-friendly (@media print styles)
- Consistent color scheme
- Typography hierarchy clear
- Footer with generation date
- Images embedded as base64 (tg-native) or have fallbacks (hosted)

## TG Viewer Capabilities Reference

| Feature | Supported |
|---------|-----------|
| `<style>` blocks in `<head>` | Yes |
| Inline `style=""` attributes | Yes |
| Base64 `data:image/jpeg;base64,...` | Yes |
| Basic elements: div, p, h1-h6, span, img, a, table, ul, ol, li | Yes |
| CSS: color, background, padding, margin, border, border-radius | Yes |
| CSS: font-size, font-weight, text-align, width, max-width | Yes |
| JavaScript (`<script>`) | No |
| External resources (CDN, images, fonts) | No |
| CSS Grid | No |
| Complex flexbox | No |
| CSS custom properties `var()` | No |
| `@import` CSS rules | No |
