# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Markdown → Telegram HTML converter.

Converts a subset of Markdown to Telegram's HTML parse mode whitelist so LLM
output (Claude native markdown) renders correctly in TG while still reading
naturally in other surfaces (Desktop.app, plain text, etc.).

TG HTML whitelist (supported tags ONLY):
  <b> <i> <u> <s> <code> <pre> <pre><code class="language-xxx"> <a href> <tg-spoiler>
  <blockquote> <blockquote expandable>

Disallowed: <h1..h6> <div> <ul> <li> <br> <table> <p> etc. — render as literal
characters. Lists rendered as plain unicode bullets.

Design principles:
- One-shot conversion: callers MUST pass raw markdown, NEVER pre-escaped HTML.
  (Idempotent conversion would require inverse-escape which is unsafe.)
- Escape-inside-rebuild: plain text and code bodies are HTML-escaped during
  emission; tags are injected fresh. No way to double-escape.
- Tolerant of malformed input: unclosed fences are closed at EOF, unmatched
  emphasis is left as literal characters.
- URL scheme allowlist for <a href>: http, https, tg, mailto. Other schemes
  (javascript:, data:, file:) fall back to plain-text rendering.
- Pure stdlib, zero new deps.

Supported markdown:
  **bold** __bold__         → <b>
  *italic* _italic_         → <i>
  ~~strike~~                → <s>
  `inline code`             → <code>
  ```lang\\ncode\\n```      → <pre><code class="language-lang">
  [text](url)               → <a href="url">text</a>  (scheme-checked)
  # H1 / ## H2 / ### H3     → <b>…</b> on its own line
  - item / * item           → • item  (plain text bullet)
  > quote                   → <blockquote>quote</blockquote>
"""
from __future__ import annotations

import html as _html
import re

_SAFE_URL_SCHEMES = ("http://", "https://", "tg://", "mailto:")

# GFM <details><summary>x</summary>body</details> → TG expandable blockquote.
# Non-greedy body, DOTALL so multi-line content is captured. Runs early so
# inner markdown in body still gets converted.
_DETAILS_RE = re.compile(
    r"<details>\s*(?:<summary>(.*?)</summary>)?\s*(.*?)\s*</details>",
    re.DOTALL | re.IGNORECASE)

# Bare URL auto-linkify. Matches http(s)://... and ws(s)://... up to whitespace
# or common trailing punctuation. Scheme-allowlisted — data:/file:/javascript:
# won't match. Applied AFTER explicit markdown links are protected so we don't
# double-wrap already-linked text.
_BARE_URL_RE = re.compile(
    r"(?<![\w>])(https?://|wss?://)"
    r"([^\s<>\"`')\]]+?)"
    r"(?=[.,;:!?)\]]*(?:\s|$))")


def _is_in_url_context(text: str, pos: int) -> bool:
    """Return True if *pos* is inside a bare URL (http/https/ws/wss).

    Walks backwards from *pos* through non-whitespace characters looking for
    ``://``.  If found, the match is part of a URL and autocode should skip it
    to avoid wrapping path segments in ``<code>`` tags (which breaks the URL
    before bare-URL auto-linking at step 9b).
    """
    i = pos - 1
    while i >= 0 and text[i] not in " \t\n\r":
        # Placeholders contain \x00 — stop scanning through them
        if text[i] == "\x00":
            return False
        if i >= 2 and text[i - 2 : i + 1] == "://":
            return True
        i -= 1
    return False


# Auto-code for common "identifier-looking" tokens in plain text so LLM output
# that forgets backticks still renders snake_case / hash refs / file paths as
# monospace. Conservative patterns — require a structural marker (underscore,
# hash, or known extension) so regular English prose isn't wrapped.
# (1) snake_case with 1+ underscore, letter-start, no leading digit.
_AUTOCODE_SNAKE_RE = re.compile(
    r"(?<![\w`])([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)(?![\w`])")
# (2) hash refs — e.g. PR#2562, GH#123, issue#42.
_AUTOCODE_HASH_RE = re.compile(
    r"(?<![\w`])([A-Za-z][A-Za-z0-9]*#\d+)(?![\w`])")
# (3) file names / paths with common source extensions. Includes optional
# directory prefix (`dir/sub/file.py`). Conservative ext allowlist.
_AUTOCODE_FILE_RE = re.compile(
    r"(?<![\w`/])"
    r"([A-Za-z0-9_][A-Za-z0-9_./-]*"
    r"\.(?:py|js|ts|tsx|jsx|md|json|yaml|yml|toml|sh|go|rs|rb|java|cpp|c|h|hpp|"
    r"txt|pdf|html|css|sql|ini|cfg|env|log|xml|csv)"
    r")(?![\w`/])")

# TG HTML whitelist — tags we PRESERVE when input is detected as already HTML.
_TG_TAG_NAMES = (
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "code", "pre", "blockquote", "a", "tg-spoiler", "span",
    "tg-emoji",
)
# Opening-tag regex for passthrough detection. Matches `<b>`, `<a href="...">`,
# `<blockquote expandable>`, etc. Lenient on attributes (captures until `>`).
_ANY_TG_TAG_RE = re.compile(
    r"<(/?)(" + "|".join(_TG_TAG_NAMES) + r")(\s[^>]*)?>", re.IGNORECASE)

# Markdown table detector — two+ consecutive lines starting with `|`, the
# second being a `|---|---|` separator row. Captures the whole table block.
_TABLE_RE = re.compile(
    r"(^\|[^\n]+\|[ \t]*\n"
    r"\|[\s\-:|]+\|[ \t]*\n"
    r"(?:\|[^\n]*\|[ \t]*\n?)+)",
    re.MULTILINE,
)

# Fenced code block: opening ``` optionally followed by a language hint, then
# body up to closing ``` (or EOF — handled by caller). Multiline + non-greedy.
_FENCE_RE = re.compile(
    r"```([A-Za-z0-9_+\-]*)\n(.*?)(?:\n```|\Z)", re.DOTALL)
# Inline code (single backtick pair, no nesting). Non-greedy.
_INLINE_CODE_RE = re.compile(r"`([^`\n]+?)`")
# Markdown link [text](url) — text doesn't contain ] or newline; url doesn't
# contain ) or whitespace. Simple, handles 99% of LLM output.
_LINK_RE = re.compile(r"\[([^\]\n]+?)\]\(([^)\s]+?)\)")
# Bold: ** or __ surrounding content. Non-greedy. DOTALL allows the span
# to cross newlines (real LLM output often has multi-sentence bold blocks).
# Cap at ~1000 chars to avoid catastrophic backtracking on malformed input.
_BOLD_RE = re.compile(
    r"\*\*([^*][\s\S]{0,1000}?)\*\*|__([^_][\s\S]{0,1000}?)__")
# Italic: * or _ surrounding content. Must be at word boundaries so
# snake_case (`BE_mtm`, `foo_bar_baz`) and arithmetic (`2*3=6`) don't
# accidentally become italic. Run AFTER bold substitution.
_ITALIC_RE = re.compile(
    r"(?<![\w*])\*(?!\s)([^\n*]+?)(?<!\s)\*(?![\w*])|"
    r"(?<![\w_])_(?!\s)([^\n_]+?)(?<!\s)_(?![\w_])")
# Strikethrough.
_STRIKE_RE = re.compile(r"~~([^\n~]+?)~~")
# Header (line-anchored): #/##/### followed by space + content.
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
# Task-list checkboxes (GFM + extended): `- [ ]` unchecked, `- [x]` checked,
# `- [-]` cancelled/crossed, `- [!]` important, `- [?]` unclear.
# Applied BEFORE generic bullet conversion so the state token is transformed
# first. Rendered as unicode icons so TG shows a visible mark, not `[x]`.
_CHECKBOX_RE = re.compile(
    r"^([ \t]*)[-*+]\s+\[([ xX\-!?])\]\s+(.+)$", re.MULTILINE)

# Horizontal rule: `---` / `***` / `___` (3+ repeats) on own line, with
# optional surrounding whitespace. Rendered as a unicode rule line so TG
# shows visual separation instead of literal dashes.
_HR_RE = re.compile(
    r"^[ \t]*([-*_])\1{2,}[ \t]*$", re.MULTILINE)

# Markdown images `![alt](url)`. TG messages can't embed images inline from
# markdown, so render as a 🖼 + clickable link. Text-only alt with no URL
# embed is acceptable fallback.
_IMAGE_RE = re.compile(r"!\[([^\]\n]*?)\]\(([^)\s]+?)\)")

# Backslash-escaped markdown chars. `\*`, `\_`, `` \` ``, `\[`, `\]`,
# `\(`, `\)`, `\#`, `\~`, `\|`, `\!`. Pre-process: replace with placeholders
# that hold the literal char so emphasis/link regexes won't match them.
_ESCAPED_CHAR_RE = re.compile(r"\\([*_`\[\]()#~|!\\])")

# Bullet list line (at line start): "- " or "* " or "+ " followed by content.
_BULLET_RE = re.compile(r"^([ \t]*)[-*+]\s+(.+)$", re.MULTILINE)
# Blockquote line.
_QUOTE_RE = re.compile(r"^>\s?(.*)$", re.MULTILINE)
# Placeholder pattern — used in restore step 10 and nested-placeholder step 11.
_PLACEHOLDER_RE = re.compile(r"\x00\x01(\d+)\x02")


def _looks_like_tg_html(text: str) -> bool:
    """Heuristic: does this text already look like TG-HTML output?

    Strategy: count legitimate TG tag occurrences. If the text has >=2 tags
    from the TG whitelist AND tag open-count ≈ close-count (within ±1) AND
    every `<` in the text belongs to a TG tag (no bare comparison operators
    or unknown tags), treat as pre-formatted HTML.

    The third condition prevents false positives like "handles <b>bold</b>
    text, but x<10" — which would otherwise passthrough and cause TG to return
    400 Bad Request when parsing the raw `<10` as an unknown tag.

    Returns True if passthrough is likely safer than markdown conversion.
    """
    if not text or "<" not in text:
        return False
    matches = _ANY_TG_TAG_RE.findall(text)
    if len(matches) < 2:
        return False
    # Balance check: opens vs closes, allowing self-close tolerance.
    opens = sum(1 for (closing, *_rest) in matches if not closing)
    closes = sum(1 for (closing, *_rest) in matches if closing)
    if abs(opens - closes) > 1:
        return False
    # Orphan-`<` check: strip all known TG tags and verify no raw `<` remains.
    # A raw `<` (comparison, partial tag, unknown element) means TG's HTML parser
    # would choke — safer to run the markdown converter which will escape it.
    stripped = _ANY_TG_TAG_RE.sub("", text)
    return "<" not in stripped


def _strip_inline_md(text: str) -> str:
    """Strip inline markdown markers from text (for contexts where HTML tags
    can't nest, e.g. inside `<pre>` table cells). `**x**` → `x`, `*x*` → `x`,
    `~~x~~` → `x`, `` `x` `` → `x`. Non-destructive — returns plain text.
    """
    s = re.sub(r"\*\*([^\n*]+?)\*\*", r"\1", text)
    s = re.sub(r"__([^\n_]+?)__", r"\1", s)
    s = re.sub(r"(?<!\*)\*([^\n*]+?)\*(?!\*)", r"\1", s)
    s = re.sub(r"(?<!_)_([^\n_]+?)_(?!_)", r"\1", s)
    s = re.sub(r"~~([^\n~]+?)~~", r"\1", s)
    s = re.sub(r"`([^`\n]+?)`", r"\1", s)
    return s


_TABLE_ALIGNED_MAX_WIDTH = 40   # mobile TG portrait fits ~40 chars clean
_TABLE_COMPACT_MAX_WIDTH = 50   # compact comma-row still fits ~50 chars


def _render_markdown_table(block: str) -> str:
    """Render a markdown table block as a TG `<pre>`, mobile-viewport aware.

    Three rendering tiers based on estimated width:
      - aligned (≤40 chars)  → classic ` | ` padded columns, dash underline
      - compact (41-50)      → header row + comma-separated value rows
      - record blocks (>50)  → one "Header: value" block per data row

    TG has no native table tag. `<pre>` strips nested tags, so inline markdown
    markers inside cells are flattened to plain text via `_strip_inline_md`
    (otherwise `**0.01**` would render with literal asterisks).
    """
    lines = [ln.rstrip() for ln in block.strip().split("\n") if ln.strip()]
    if len(lines) < 2:
        return block  # malformed — return as-is
    # Parse cells: strip leading/trailing `|`, split on `|`, strip markdown.
    rows: list[list[str]] = []
    for i, ln in enumerate(lines):
        if i == 1:  # separator row — skip
            continue
        cells = [_strip_inline_md(c.strip())
                 for c in ln.strip("|").split("|")]
        rows.append(cells)
    if not rows:
        return block

    n_cols = max(len(r) for r in rows)
    # Pad ragged rows for consistent access.
    for r in rows:
        while len(r) < n_cols:
            r.append("")

    # Width estimation: aligned width = sum(col widths) + ` | ` separators.
    widths = [max(len(r[i]) for r in rows) for i in range(n_cols)]
    aligned_width = sum(widths) + 3 * (n_cols - 1)

    # Tier 1: aligned <pre> with padded columns (narrow tables).
    if aligned_width <= _TABLE_ALIGNED_MAX_WIDTH:
        out_lines: list[str] = []
        for idx, r in enumerate(rows):
            padded = [r[i].ljust(widths[i]) for i in range(n_cols)]
            out_lines.append(" | ".join(padded).rstrip())
            if idx == 0:
                out_lines.append("-+-".join("-" * w for w in widths))
        return "<pre>" + _escape_text("\n".join(out_lines)) + "</pre>"

    header, data_rows = rows[0], rows[1:]

    # Tier 2: compact — header row declared once, then comma-joined values.
    # Compact row width = sum(cell) + 2*(n-1) for `, ` separators.
    compact_width = max(
        (sum(len(c) for c in r) + 2 * (n_cols - 1)) for r in rows)
    if compact_width <= _TABLE_COMPACT_MAX_WIDTH:
        out_lines = [" | ".join(header)]
        out_lines.append("-" * min(compact_width, _TABLE_COMPACT_MAX_WIDTH))
        for r in data_rows:
            out_lines.append(", ".join(r))
        return "<pre>" + _escape_text("\n".join(out_lines)) + "</pre>"

    # Tier 3: record blocks — "Header: value" per cell, one block per row.
    # Readable on mobile regardless of table width.
    out_lines = []
    for idx, r in enumerate(data_rows, 1):
        out_lines.append(f"── {idx} ──")
        for col, val in zip(header, r):
            label = col if col else f"col{data_rows.index(r) + 1}"
            out_lines.append(f"{label}: {val}")
    return "<pre>" + _escape_text("\n".join(out_lines)) + "</pre>"


def _is_safe_url(url: str) -> bool:
    """Check URL scheme against allowlist. Relative URLs and fragments rejected."""
    low = url.strip().lower()
    return any(low.startswith(s) for s in _SAFE_URL_SCHEMES)


def _escape_attr(value: str) -> str:
    """Escape for use inside an HTML attribute value (quote-safe)."""
    return _html.escape(value, quote=True)


def _escape_text(value: str) -> str:
    """Escape for use in HTML text content (preserves quotes)."""
    return _html.escape(value, quote=False)


def _make_placeholder(seq: int) -> str:
    """Unique unlikely-to-collide placeholder for protected substitutions.

    Uses only non-printable control characters (NUL, SOH, STX) with no
    readable text. If a placeholder somehow leaks into TG output, control
    chars are silently stripped rather than displaying readable garbage like
    "MDTG 9" (the old format that used a literal "MDTG" prefix).
    """
    return f"\x00\x01{seq}\x02"


def md_to_tg_html(text: str) -> str:
    """Convert markdown to Telegram HTML parse-mode.

    Input: raw markdown (NEVER pre-escaped HTML). Output: TG-HTML-safe string.
    """
    if not text:
        return ""

    # Strategy: protect raw-content blocks (fenced code, inline code, links,
    # headers, quotes, bullets) by replacing with placeholders. Run emphasis
    # transforms on the remaining text. Escape the remaining text. Restore
    # placeholders with their final HTML.
    protected: list[str] = []

    def _protect(final_html: str) -> str:
        seq = len(protected)
        protected.append(final_html)
        return _make_placeholder(seq)

    # -2. GFM <details><summary>x</summary>body</details> → TG expandable
    # blockquote. Runs BEFORE other passes so summary + body text still gets
    # inner markdown conversion via recursive call. Protected as placeholder.
    def _details_sub(m: "re.Match[str]") -> str:
        summary = (m.group(1) or "").strip()
        body = (m.group(2) or "").strip()
        # Recurse for inner markdown in both parts.
        summary_html = md_to_tg_html(summary) if summary else ""
        body_html = md_to_tg_html(body) if body else ""
        header = (f"<b>{summary_html}</b>\n" if summary_html else "")
        return _protect(
            f"<blockquote expandable>{header}{body_html}</blockquote>")
    text = _DETAILS_RE.sub(_details_sub, text)

    # -1. Backslash-escaped markdown chars: protect as literal chars BEFORE
    # any regex runs. `\*foo\*` should show `*foo*` literally, not become
    # bold. We wrap each escaped char in a protected placeholder that holds
    # the single literal char.
    def _escaped_char_sub(m: "re.Match[str]") -> str:
        return _protect(_escape_text(m.group(1)))
    text = _ESCAPED_CHAR_RE.sub(_escaped_char_sub, text)

    # 0. Markdown tables (highest priority — consume whole block, emit <pre>).
    def _table_sub(m: "re.Match[str]") -> str:
        return _protect(_render_markdown_table(m.group(1)))
    text = _TABLE_RE.sub(_table_sub, text)

    # 1. Fenced code blocks (highest priority — body is verbatim).
    def _fence_sub(m: "re.Match[str]") -> str:
        lang = m.group(1).strip().lower()
        body = m.group(2)
        escaped = _escape_text(body)
        if lang:
            lang_attr = re.sub(r"[^a-z0-9_+\-]", "", lang)[:20]
            return _protect(
                f'<pre><code class="language-{lang_attr}">{escaped}</code></pre>')
        return _protect(f"<pre>{escaped}</pre>")

    text = _FENCE_RE.sub(_fence_sub, text)
    # Close any unclosed fence (odd number of ``` remaining). Rare but handled.
    if text.count("```") % 2 == 1:
        # Treat remaining ``` as literal — escape it in text fragment later.
        pass

    # 2. Inline code.
    def _inline_code_sub(m: "re.Match[str]") -> str:
        return _protect(f"<code>{_escape_text(m.group(1))}</code>")
    text = _INLINE_CODE_RE.sub(_inline_code_sub, text)

    # 2a. Images `![alt](url)` — TG can't embed, render as `🖼 <a>alt</a>`.
    # Run BEFORE regular link regex (next step) so `![alt](url)` isn't
    # matched as `!` + regular `[alt](url)` link.
    def _image_sub(m: "re.Match[str]") -> str:
        alt, url = m.group(1).strip(), m.group(2)
        label = alt if alt else "image"
        if _is_safe_url(url):
            return _protect(
                f'🖼 <a href="{_escape_attr(url)}">{_escape_text(label)}</a>')
        # Unsafe scheme: drop URL, show alt only.
        return _protect(f"🖼 {_escape_text(label)}")
    text = _IMAGE_RE.sub(_image_sub, text)

    # 3. Links (URL scheme checked; unsafe → plain text fallback).
    def _link_sub(m: "re.Match[str]") -> str:
        label, url = m.group(1), m.group(2)
        if _is_safe_url(url):
            return _protect(
                f'<a href="{_escape_attr(url)}">{_escape_text(label)}</a>')
        # Unsafe scheme: render label as plain text, drop the URL.
        return _protect(_escape_text(label))
    text = _LINK_RE.sub(_link_sub, text)

    # 3b. Bare URL auto-linking moved to step 7b (after emphasis) so that
    # `**https://url/**` is processed as bold first, not as a bare URL with
    # trailing `**` eaten into the URL path.

    # 3c. Auto-code identifier-ish tokens in plain text (snake_case, hash refs,
    # file names with common extensions). Wraps each match in `<code>` so LLM
    # output that forgets backticks still reads as monospace. Conservative
    # patterns — require structural marker (underscore, hash, known ext) so
    # regular prose isn't wrapped. Order: file > hash > snake (file paths may
    # contain snake_case, so match them first).
    # URL-aware: skip matches inside bare URLs (e.g. path segments like
    # `ssp_benchmark_q1_2026.html`) to avoid breaking URL auto-linking.
    def _autocode_sub(m: "re.Match[str]") -> str:
        if _is_in_url_context(m.string, m.start()):
            return m.group(0)  # leave as-is inside URLs
        return _protect(f"<code>{_escape_text(m.group(1))}</code>")
    text = _AUTOCODE_FILE_RE.sub(_autocode_sub, text)
    text = _AUTOCODE_HASH_RE.sub(_autocode_sub, text)
    text = _AUTOCODE_SNAKE_RE.sub(_autocode_sub, text)

    # 4. Headers (line-anchored) → bold on own line.
    def _header_sub(m: "re.Match[str]") -> str:
        content = m.group(2)
        # Do NOT call md_to_tg_html(content) recursively — at this point
        # content already contains outer-call placeholders from earlier steps
        # (inline code, links, autocode). A fresh recursive call has an empty
        # protected[] and silently drops those references.
        # Instead, emit open/close <b> tag placeholders flanking content so
        # the outer pipeline (steps 7-9) processes bold/italic inside headers,
        # and step 10 escapes plain text + restores all placeholders.
        return _protect("<b>") + content + _protect("</b>")
    text = _HEADER_RE.sub(_header_sub, text)

    # 5. Blockquote lines (collapse consecutive > lines into one blockquote).
    # Each `> line` becomes its own <blockquote> (TG merges adjacent ones visually).
    def _quote_sub(m: "re.Match[str]") -> str:
        body = m.group(1)
        # Use open/close tag placeholders flanking body so:
        # (a) bold/italic/strike inside blockquotes is processed by steps 7-9,
        # (b) existing outer-call placeholders in body are restored in step 10,
        # (c) plain-text chars (including `<`, `>`, `&`) are escaped in step 10.
        return _protect("<blockquote>") + body + _protect("</blockquote>")
    text = _QUOTE_RE.sub(_quote_sub, text)

    # 5b. Horizontal rules (`---`, `***`, `___`) on own line → unicode separator.
    # TG has no native <hr> tag; a box-drawing line communicates visual
    # section separation cleanly in both TG and Desktop.app.
    text = _HR_RE.sub("──────────", text)

    # 6a. Task-list checkboxes (GFM + extended) → unicode icons. Applied
    # BEFORE the generic bullet pass so the state token becomes a visible
    # icon rather than being stripped as bullet content.
    #   [ ] → ☐   (todo)          [x] → ☑   (done)
    #   [-] → ❌  (cancelled)     [!] → ⚠️  (important)
    #   [?] → ❓  (unclear)
    _CHECKBOX_ICONS = {
        " ": "☐", "x": "☑", "X": "☑",
        "-": "❌", "!": "⚠️", "?": "❓",
    }
    def _checkbox_sub(m: "re.Match[str]") -> str:
        indent, mark, body = m.group(1), m.group(2), m.group(3)
        icon = _CHECKBOX_ICONS.get(mark, "☐")
        return f"{indent}{icon} {body}"
    text = _CHECKBOX_RE.sub(_checkbox_sub, text)

    # 6b. Bullet list markers → unicode bullet.
    text = _BULLET_RE.sub(r"\1• \2", text)

    # Helper: wrap inner emphasis content. If the content is a bare URL,
    # emit tag+<a href> so the link stays clickable (FB-53). LLMs commonly
    # write `**https://example.com/**` which must render as a bold link,
    # not as bold escaped text that TG cannot click.
    def _emphasis_inner(inner: str, tag: str) -> str:
        stripped = inner.strip()
        if stripped and _is_safe_url(stripped) and " " not in stripped:
            return _protect(
                f"<{tag}><a href=\"{_escape_attr(stripped)}\">"
                f"{_escape_text(stripped)}</a></{tag}>")
        return _protect(f"<{tag}>{_escape_text(inner)}</{tag}>")

    # 7. Bold (run before italic so ** isn't eaten as two italic markers).
    def _bold_sub(m: "re.Match[str]") -> str:
        inner = m.group(1) or m.group(2) or ""
        return _emphasis_inner(inner, "b")
    text = _BOLD_RE.sub(_bold_sub, text)

    # 8. Italic.
    def _italic_sub(m: "re.Match[str]") -> str:
        inner = m.group(1) or m.group(2) or ""
        return _emphasis_inner(inner, "i")
    text = _ITALIC_RE.sub(_italic_sub, text)

    # 9. Strikethrough.
    def _strike_sub(m: "re.Match[str]") -> str:
        return _emphasis_inner(m.group(1), "s")
    text = _STRIKE_RE.sub(_strike_sub, text)

    # 9b. Auto-link bare URLs (moved from old step 3b to AFTER emphasis).
    # `**https://url/**` is now handled by bold-first (step 7), so the bare
    # URL regex only sees un-emphasized URLs. Explicit [text](url) links are
    # already protected as placeholders from step 3, so no double-wrapping.
    def _bare_url_sub(m: "re.Match[str]") -> str:
        full_url = m.group(1) + m.group(2)
        scheme = m.group(1).rstrip(":/").lower()
        if scheme not in ("http", "https", "ws", "wss"):
            return m.group(0)
        return _protect(
            f'<a href="{_escape_attr(full_url)}">{_escape_text(full_url)}</a>')
    text = _BARE_URL_RE.sub(_bare_url_sub, text)

    # 10. Escape remaining plain text (which now contains only placeholders
    # and literal characters).
    out_parts: list[str] = []
    last = 0
    for m in _PLACEHOLDER_RE.finditer(text):
        # Plain text before this placeholder — escape.
        if m.start() > last:
            out_parts.append(_escape_text(text[last:m.start()]))
        # Placeholder → restore protected HTML.
        idx = int(m.group(1))
        if 0 <= idx < len(protected):
            out_parts.append(protected[idx])
        last = m.end()
    # Trailing plain text.
    if last < len(text):
        out_parts.append(_escape_text(text[last:]))

    result = "".join(out_parts)

    # 11. Second-pass restore: resolve nested placeholders that ended up inside
    # a protected[] string (e.g. _AUTOCODE_SNAKE_RE wraps `burn_flow` → P_N,
    # then _BOLD_RE wraps `**P_N only**` → P_M with protected[M] containing
    # the un-resolved P_N). Up to 5 iterations handles arbitrary nesting depth.
    for _ in range(5):
        if "\x00" not in result:
            break
        new_result = _PLACEHOLDER_RE.sub(
            lambda m: protected[int(m.group(1))] if 0 <= int(m.group(1)) < len(protected) else "",
            result,
        )
        if new_result == result:
            break
        result = new_result

    return result


def escape_text_for_tg(text: str) -> str:
    """Escape plain text for safe inclusion in a TG HTML message.

    Use when you have non-markdown text that must be wrapped with HTML tags
    OUTSIDE this module (e.g. role labels, timestamps, tool names).
    """
    return _escape_text(text)


def convert_for_tg(text: str) -> str:
    """Smart dispatcher: detect HTML vs markdown and convert appropriately.

    - If input looks like TG HTML already (contains ≥2 whitelist tags with
      balanced open/close), passes through unchanged — avoids double-escaping
      legitimate HTML produced by old Mac-Claude prompts.
    - Otherwise runs markdown → TG HTML conversion.

    This is the PRIMARY entry point for all relay / catchup rendering.
    The plain `md_to_tg_html` is kept for callers that know the input is
    always markdown (e.g. unit tests).
    """
    if not text:
        return ""
    if _looks_like_tg_html(text):
        return text  # passthrough — input is already TG-HTML
    return md_to_tg_html(text)


_TOOL_EMOJI: dict[str, str] = {
    "bash": "🐚", "read": "📖", "write": "✍️", "edit": "✏️",
    "grep": "🔎", "glob": "📁", "ls": "📋",
    "webfetch": "🌐", "websearch": "🔍", "fetch": "🌐",
    "todowrite": "✅", "task": "🤖", "agent": "🤖",
    "notebookedit": "📓", "skill": "🛠",
}


def tool_emoji(name: str) -> str:
    """Map a tool name to a per-tool emoji icon. Fallback: generic wrench.

    Case-insensitive lookup. Strips common prefixes (`mcp__`, `Tool`).
    Used for compact tool-chain display in relay catchup / streaming UX.
    """
    if not name:
        return "🔧"
    key = name.lower()
    # Strip MCP prefix: `mcp__server__tool` → `tool`.
    if key.startswith("mcp__"):
        parts = key.split("__")
        key = parts[-1] if parts else key
    return _TOOL_EMOJI.get(key, "🔧")


__all__ = [
    "md_to_tg_html",
    "convert_for_tg",
    "escape_text_for_tg",
    "tool_emoji",
]
