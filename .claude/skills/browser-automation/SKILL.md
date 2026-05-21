---
description: "Headless browser automation via Playwright MCP (default) and Camoufox MCP (anti-detect). TRIGGER when: user asks to open/visit/check a website, take a screenshot of a page, fill out an online form, book something on a website, extract data from a webpage, log into a site, check a web portal (school portal, booking site, etc.), or browse the web. Also trigger for any browser_*/camofox_* tool usage (browser_navigate, browser_snapshot, browser_take_screenshot, browser_click, browser_type, browser_fill_form, camofox_navigate, camofox_snapshot, camofox_click)."
---

# Browser Automation — Two Browsers Available

## Browser Selection Guide

| Scenario | Use | Why |
|----------|-----|-----|
| General browsing, portals, forms | **Playwright** (`browser_*`) | Fast, lightweight, sufficient |
| Airline sites, flight aggregators | **Camoufox** (`camofox_*`) | Anti-bot bypass (Cloudflare, Akamai) |
| Marriott.com, Akamai WAF sites | **Camoufox** (`camofox_*`) | Akamai blocks Chromium entirely |
| Any site that blocks Playwright | **Camoufox** (`camofox_*`) | Fallback with real fingerprints |
| Simple screenshots | **Playwright** (`browser_*`) | Faster, lower resources |

**DEFAULT**: Start with Playwright. Switch to Camoufox if blocked or for known anti-bot sites.

---

# TURN BUDGET — MANDATORY

Browser automation is expensive. Follow these hard limits:

- **Max 30 browser tool calls per task.** At 25, send partial results immediately.
- **Same selector fails 3x** -> abandon, try alternative (different selector, JS eval, or report failure).
- **Same action loop** (navigate->wait->fail->retry) -> stop after 3 iterations, report.
- **ALWAYS send a telegram_send_message BEFORE exhausting turns.** Silent failure = worst outcome.
- **Send partial results every 20 turns** if multi-step.

---

# PAGE READING STRATEGY — PRIORITY ORDER

## 1st: Scoped Extraction (PRIMARY — use for all pages)

**Playwright** — `browser_evaluate` with CSS selector:
```
browser_evaluate({ expression: "document.querySelector('#results')?.innerText || 'Not found'" })
```

**Playwright** — `browser_run_code` to batch complex multi-step extraction in ONE call:
```
browser_run_code({ code: "const p = await page.$$eval('.price', els => els.map(e => e.textContent)); return p;" })
```

**Camoufox** — `camofox_evaluate_js` for targeted extraction:
```
camofox_evaluate_js({ expression: "document.querySelector('.results')?.innerText" })
```

## 2nd: Snapshot (simple/small pages)

- **Playwright**: `browser_snapshot` — use `ref` param to scope to subtree
- **Camoufox**: `camofox_snapshot` — use `offset` for pagination (max ~2000 nodes)

**Snapshot ref coverage — know the limits:**
Refs (e1, e2...) cover 19 ARIA roles: button, link, textbox, checkbox, radio, menuitem,
tab, searchbox, slider, spinbutton, switch, combobox, listbox, option, select, dialog,
alertdialog, gridcell, treeitem.

Refs do NOT cover: generic divs, spans, custom dropdowns, autocomplete lists, non-standard
components. For these, use CSS selectors or JS evaluation — don't retry refs.

**Important ref rules:**
- Always `snapshot` BEFORE using refs
- After navigation or major DOM change, re-snapshot (refs invalidate)
- If element not in snapshot, don't guess — switch to CSS selector workflow
- Combobox/autocomplete controls are the most consistent ref gap across all sites

## 3rd: Screenshot (visual verification only)

- **Playwright**: `browser_take_screenshot` with `savePath="/app/data/tmp/screenshot.png"`
- **Camoufox**: `camofox_screenshot` — auto-saved to `data/tmp/camofox-<timestamp>.png`

Use for: visual verification before irreversible actions, when snapshots fail, proof of state.

## Safety Net: Size Guard

All browser MCP responses go through `mcp_size_guard.py`:
- Text >500KB truncated -> switch to scoped approach
- Images always saved to `data/tmp/` as PNG files (never inline base64)

---

# COMPOUND TOOLS — REDUCE CALLS

These batch multiple operations into ONE call. **Prefer them over sequential calls:**

| Instead of... | Use this | Saves |
|---------------|----------|-------|
| navigate -> snapshot | `navigate_and_snapshot` | 1 call |
| scroll -> snapshot | `scroll_and_snapshot` / `scroll_element_and_snapshot` | 1 call |
| type field1 -> type field2 -> click submit | **`fill_form`** | 3-5 calls |
| type -> press Enter | `type_and_submit` | 1 call |
| click elem1 -> click elem2 -> click elem3 | `batch_click` | 2 calls |
| fill + select + type across a form | **`browser_run_code`** (Playwright) | 5-10 calls |

**`fill_form`** (Camoufox) — populate multiple fields + optional submit in ONE call:
```
fill_form({ fields: [
  { selector: "#email", value: "user@example.com" },
  { selector: "#password", value: "secret123" },
  { selector: "#city", value: "London" }
], submit: true })
```

**`batch_click`** (Camoufox) — click multiple targets sequentially:
```
batch_click({ targets: ["#agree-checkbox", "#terms-checkbox", "#submit-btn"] })
```

**`browser_run_code`** (Playwright) — batch complex multi-step JS in ONE call:
```
browser_run_code({
  code: "await page.fill('#from', 'LHR'); await page.fill('#to', 'JFK'); await page.selectOption('#class', 'business'); await page.click('#search');"
})
```

---

# WEBSITE ARCHETYPE PATTERNS

Identify the site type FIRST, then follow the matching strategy:

## Static HTML (docs, blogs, simple pages)
- Ref coverage: LOW (~20%). Use CSS selectors for precision.
- `snapshot` -> `evaluate_js` or `query_selector` -> `click(selector)`

## Search Engines (Google, Bing)
- Ref coverage: HIGH for links/buttons (~74%), but search input often lacks refs (combobox gap)
- `snapshot` -> `type_text(ref or selector)` -> `press_key("Enter")` -> `snapshot`

## React/Vue SPAs
- Ref coverage: MODERATE (~50%), refs stale after rerender
- `snapshot` -> action by ref -> `wait_for_selector` after transition -> re-snapshot
- Re-snapshot after every client-side route change

## E-commerce (Amazon, booking sites)
- Ref coverage: GOOD on visible controls, large pages truncate
- `snapshot` -> `type_text(ref)` -> `press_key` -> `snapshot(offset)` or `scroll_and_snapshot`
- Paginate snapshots with `offset` for long result lists

## Airline/Travel Booking
- Ref coverage: VARIABLE, comboboxes/date pickers often lack refs
- **Use `fill_form` for input fields** (biggest win — avoids 10+ sequential type calls)
- `navigate` -> `fill_form` -> `wait_for_text` for results -> `snapshot` or `evaluate_js`
- Always set `timeout: 30000` — these sites are slow

## Auth Dashboards (portals, admin panels)
- `create_tab` -> `load_profile` or `import_cookies` -> `navigate` -> `snapshot`
- Session persistence via Camoufox profiles (`save_profile` / `load_profile`)

## Infinite Scroll (social media, news feeds)
- Refs change with each scroll load
- `snapshot` -> `scroll_and_snapshot` -> interact only on LATEST snapshot
- Re-snapshot after every scroll

---

# Playwright (browser_* tools — headless Chromium)

Persistent profile. Viewport: 1920x1080.

## Key Tools
- `browser_navigate` — go to URL
- `browser_snapshot` — accessibility tree (`ref` param scopes to subtree)
- `browser_take_screenshot` — ALWAYS pass `savePath`
- `browser_click` — click by CSS selector or text
- `browser_type` — type into input
- `browser_evaluate` — run JS in page (scoped extraction)
- `browser_run_code` — **batch complex multi-step Playwright code in ONE call**
- `browser_select_option` — dropdown
- `browser_fill_form` — fill multiple fields at once
- `browser_press_key` — keyboard input

## Selector Best Practices (from lackeyjb/playwright-skill)
```
# PREFERRED: data-testid (most stable)
[data-testid="submit-button"]

# GOOD: Role-based (accessible)
getByRole('button', { name: 'Submit' })
getByRole('textbox', { name: 'Email' })

# GOOD: Text content (unique text)
getByText('Sign in')

# OK: Semantic HTML
button[type="submit"]
input[name="email"]

# AVOID: Classes/IDs (change frequently)
.btn-primary    # fragile
#submit         # fragile
```

## Waiting Strategies (avoid fixed timeouts)
```
# Wait for element states
page.locator('button').waitFor({ state: 'visible' })
page.locator('.spinner').waitFor({ state: 'hidden' })

# Wait for navigation
page.waitForURL('**/success')

# Wait for network idle
page.waitForLoadState('networkidle')

# Wait for custom condition
page.waitForFunction(() => document.querySelector('.loaded'))

# Wait for API response
const resp = page.waitForResponse('**/api/data')
await page.click('#load'); await resp;
```

## Workflows

**Research**: navigate -> evaluate (scoped selector) -> extract -> summarize
**Form filling**: navigate -> `browser_run_code` (batch fills) -> screenshot BEFORE submit -> approval -> submit
**Login**: navigate -> check session -> if needed: type creds from facts -> submit

---

# Camoufox (camofox_* tools — anti-detect Firefox)

C++ level fingerprint manipulation. Undetectable by CreepJS, DataDome, Cloudflare, Akamai.
Per-chat profile isolation.

## Key Tools
**Navigation**: `navigate` / `navigate_and_snapshot` — `timeout: 30000` for heavy sites
**Reading**: `snapshot` (offset pagination), `query_selector`, `evaluate_js`, `get_page_html`
**Batch Interaction**: **`fill_form`**, **`batch_click`**, **`type_and_submit`**
**Single Interaction**: `click`, `type_text`, `press_key`, `hover`
**Scrolling**: `scroll_and_snapshot`, `scroll_element_and_snapshot`
**Waiting**: `wait_for_selector`, `wait_for`, `wait_for_text` — `timeout: 15000`
**Tabs**: `create_tab` (ALWAYS viewport 1920x1080 + light colorScheme), `close_tab`, `list_tabs`
**Sessions**: `import_cookies`, `save_profile`, `load_profile`, `list_profiles`
**Extraction**: `extract_resources`, `get_links`, `screenshot`

## Timeouts — CRITICAL
- `navigate`: `timeout: 30000` (30s) for heavy sites
- `wait_for_*`: `timeout: 15000` for dynamic content
- Size guard: 120s max

## Fallback: Playwright -> Camoufox
1. Playwright blocked -> "Switching to Camoufox..."
2. `camofox_navigate` to same URL
3. Continue with `camofox_*` tools

---

# Security Rules

- NEVER enter credit card numbers, CVV, or bank details
- NEVER complete purchases without explicit user approval
- ALWAYS screenshot before irreversible actions
- Use stored credentials from facts — NEVER ask for passwords in chat
- If tools fail, report error — NEVER ask user to paste credentials

---

# Known Gotchas

**Akamai WAF blocks Chromium** — marriott.com, airline sites. Skip Playwright, use Camoufox.

**React autocomplete ignores `.click()`** — Use `dispatchEvent(new MouseEvent('click', {bubbles:true}))` with mousedown/mouseup. Or Playwright's native `click()`.

**Camoufox dark mode + 1280x720** — Auto-fixed by size guard. Set in `create_tab` explicitly.

**Playwright shared profile** — External chats blocked from Playwright (Layer 1c). Use Camoufox with per-chat isolation.

**500KB response cap** — Size guard truncates. Use scoped eval or offset pagination.

**Retry loops waste turns** — If `evaluate_js` -> `wait_for_selector` fails 3x with same selector, the element doesn't exist or has different structure. Switch to: screenshot, `get_page_html`, or different selector. Do NOT loop.

**Combobox/autocomplete gap** — These consistently lack refs across ALL tested sites. Always use CSS selectors for autocomplete inputs, custom dropdowns, and date pickers.

---

# TAB & SESSION LIFECYCLE — MANDATORY

## Tab Reuse — ALWAYS check before creating
- **ALWAYS** call `list_tabs` BEFORE `create_tab` for any site you've visited before
- If target URL is already open in an existing tab -> reuse that tab (navigate or snapshot)
- Only create a new tab when no existing tab serves the purpose
- Close tabs explicitly with `close_tab` when done — this triggers Camoufox auto-save

## TAB_NOT_FOUND Recovery — dead tab pattern
- `TAB_NOT_FOUND` = tab is DEAD. Do NOT retry interaction. Create new tab immediately.
- `list_tabs` can show **zombie tabs** (server evicted but MCP map is stale). Interaction is the only reliable test.
- Recovery flow: `TAB_NOT_FOUND` -> `create_tab` -> `load_profile` (if saved) -> navigate -> verify auth

## Profile Save/Load — HARD RULES (Camoufox)
- After **ANY** successful login (especially with 2FA/OTP): **IMMEDIATELY** call `save_profile` with a descriptive name (e.g. `"amex_session"`, `"taap_session"`)
- Store a fact: `"{site}_camofox_profile"` with cookie count and date
- Before navigating to **ANY** auth-required site: search facts for saved profile -> `load_profile` FIRST -> then navigate -> verify login state via snapshot
- When user says "save cookies" / "save device": call `save_profile` (the MCP tool), not just the website's "remember device" button

## Tab Budget Awareness
- Bot enforces a global tab budget: **6 immune** (team/admin) + **6 LRU** (coding/external) per browser
- If you're in an external/coding chat, your tabs CAN be evicted by LRU when budget is full
- Minimize open tabs — close what you don't need
- Team/admin tabs are never evicted (immune)

## Session Timeout Awareness
- Camoufox: session timeout is DISABLED (tabs live indefinitely until closed or container restart)
- Playwright Tier 1 (team): tabs live as long as the SSE server runs (bot restart kills them)
- Playwright Tier 2/3: tabs live as long as the SDK session (session reset = browser restart, but cookies persist in `--user-data-dir`)
- After any session reset: re-check tab state with `list_tabs` before assuming tabs exist

