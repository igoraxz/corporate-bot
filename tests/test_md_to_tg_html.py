# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Tests for bot/md_to_tg_html.py — the markdown→TG HTML converter."""
from __future__ import annotations

import pytest

from bot.md_to_tg_html import (
    convert_for_tg,
    escape_text_for_tg,
    md_to_tg_html,
)


class TestBasicInline:
    def test_bold_asterisks(self):
        assert md_to_tg_html("**hi**") == "<b>hi</b>"

    def test_bold_underscores(self):
        assert md_to_tg_html("__hi__") == "<b>hi</b>"

    def test_italic_asterisk(self):
        assert md_to_tg_html("*hi*") == "<i>hi</i>"

    def test_italic_underscore(self):
        assert md_to_tg_html("_hi_") == "<i>hi</i>"

    def test_strike(self):
        assert md_to_tg_html("~~gone~~") == "<s>gone</s>"

    def test_inline_code(self):
        assert md_to_tg_html("`x`") == "<code>x</code>"

    def test_bold_and_italic_together(self):
        # Bold runs first; italic afterwards on remaining text.
        out = md_to_tg_html("**bold** and *italic*")
        assert "<b>bold</b>" in out
        assert "<i>italic</i>" in out


class TestCodeBlocks:
    def test_fenced_no_lang(self):
        assert md_to_tg_html("```\nfoo\n```") == "<pre>foo</pre>"

    def test_fenced_python(self):
        out = md_to_tg_html("```python\nprint(1)\n```")
        assert out == '<pre><code class="language-python">print(1)</code></pre>'

    def test_fenced_escapes_html_inside(self):
        # <b> inside code block must be escaped.
        out = md_to_tg_html("```\n<b>hi</b>\n```")
        assert "&lt;b&gt;hi&lt;/b&gt;" in out
        assert "<pre>" in out

    def test_unclosed_fence(self):
        # No closing ``` — converter still emits something sane.
        out = md_to_tg_html("```py\nx = 1\n")
        # Body preserved, wrapping allowed either way.
        assert "x = 1" in out


class TestCodeBlocksAdvanced:
    """Tricky code-block cases — verbatim preservation is CRITICAL."""

    def test_markdown_inside_fence_not_converted(self):
        # **bold** inside a code block MUST stay literal, not become <b>.
        md = "```\n**not bold** and *not italic*\n```"
        out = md_to_tg_html(md)
        assert "**not bold**" in out
        assert "<b>" not in out
        assert "<i>" not in out
        assert out.startswith("<pre>")

    def test_fence_with_many_langs(self):
        for lang in ["bash", "python", "js", "javascript", "ts",
                     "typescript", "yaml", "json", "diff", "go", "rust", "sh"]:
            md = f"```{lang}\necho hi\n```"
            out = md_to_tg_html(md)
            assert f'class="language-{lang}"' in out, f"lang={lang}"
            assert "echo hi" in out

    def test_multiple_fenced_blocks(self):
        md = "```\nfirst\n```\nbetween\n```\nsecond\n```"
        out = md_to_tg_html(md)
        assert out.count("<pre>") == 2
        assert "first" in out
        assert "between" in out
        assert "second" in out

    def test_fence_with_leading_whitespace(self):
        # Lines before/after closing fence with trailing spaces.
        md = "```\nline1\nline2\n```"
        out = md_to_tg_html(md)
        assert "line1\nline2" in out

    def test_fence_inside_list_like_context(self):
        md = "Example:\n```py\nx = 1\n```\nThat's it."
        out = md_to_tg_html(md)
        assert "x = 1" in out
        assert "That&#x27;s it" in out or "That's it" in out

    def test_inline_code_with_special_chars(self):
        # Inline code containing HTML-significant chars must be escaped but
        # preserved within <code>.
        md = "Run `grep -r '<script>' .` now"
        out = md_to_tg_html(md)
        assert "<code>grep -r &#x27;&lt;script&gt;&#x27; .</code>" in out or \
               "<code>grep -r '&lt;script&gt;' .</code>" in out

    def test_inline_code_with_backslash(self):
        md = r"Use `\n` for newline"
        out = md_to_tg_html(md)
        assert r"<code>\n</code>" in out

    def test_fence_preserves_indentation(self):
        md = "```python\ndef f():\n    return 1\n```"
        out = md_to_tg_html(md)
        # Preserve 4-space indent inside code.
        assert "    return 1" in out

    def test_fence_with_long_code(self):
        lines = ["line_" + str(i) for i in range(50)]
        md = "```\n" + "\n".join(lines) + "\n```"
        out = md_to_tg_html(md)
        assert "line_0" in out
        assert "line_49" in out

    def test_empty_code_block(self):
        md = "```\n\n```"
        out = md_to_tg_html(md)
        assert "<pre>" in out

    def test_code_block_with_url(self):
        # URL inside code block must NOT become a link.
        md = "```\nfetch('https://example.com')\n```"
        out = md_to_tg_html(md)
        assert "<a href" not in out
        assert "https://example.com" in out or "example.com" in out


class TestLinks:
    def test_safe_https(self):
        out = md_to_tg_html("[gh](https://github.com)")
        assert out == '<a href="https://github.com">gh</a>'

    def test_safe_mailto(self):
        out = md_to_tg_html("[mail](mailto:x@y.com)")
        assert 'href="mailto:x@y.com"' in out

    def test_javascript_url_stripped_to_plain(self):
        out = md_to_tg_html("[click](javascript:alert(1))")
        # URL gone, label remains
        assert "javascript" not in out
        assert "click" in out

    def test_data_url_stripped(self):
        out = md_to_tg_html("[x](data:text/html,<h1>)")
        assert "data:" not in out


class TestHtmlEscape:
    def test_basic_escape(self):
        assert md_to_tg_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"

    def test_no_double_escape_in_code(self):
        out = md_to_tg_html("`<b>&</b>`")
        # Escapes applied once inside the <code> block.
        assert "&lt;b&gt;&amp;&lt;/b&gt;" in out
        # Not double-escaped.
        assert "&amp;lt;" not in out


class TestHeaders:
    def test_h1(self):
        assert md_to_tg_html("# Heading") == "<b>Heading</b>"

    def test_h2_inline(self):
        out = md_to_tg_html("## Sub")
        assert out == "<b>Sub</b>"

    def test_header_with_inline_code(self):
        # Bug fix: recursive md_to_tg_html() dropped outer protected[] placeholders.
        # ## Use `config.py` must keep <code>config.py</code> inside the <b>.
        out = md_to_tg_html("## Use `config.py`")
        assert "<code>config.py</code>" in out
        assert "<b>" in out

    def test_header_with_link(self):
        # Same fix: link placeholder must survive header wrapping.
        out = md_to_tg_html("## See [docs](https://example.com)")
        assert '<a href="https://example.com">docs</a>' in out
        assert "<b>" in out

    def test_header_with_bold_inside(self):
        # **bold** inside header: outer <b> + inner <b> nesting is harmless in TG.
        out = md_to_tg_html("## **Bold header**")
        assert "<b>" in out
        assert "Bold header" in out
        assert "**" not in out

    def test_header_with_autocode(self):
        # snake_case tokens inside headers must be wrapped in <code>.
        out = md_to_tg_html("## The config_file setting")
        assert "<code>config_file</code>" in out
        assert "<b>" in out


class TestLists:
    def test_dash_bullet(self):
        assert md_to_tg_html("- one\n- two") == "• one\n• two"

    def test_star_bullet(self):
        assert md_to_tg_html("* a\n* b") == "• a\n• b"

    def test_indented_bullet(self):
        out = md_to_tg_html("  - nested")
        assert "• nested" in out


class TestBlockquotes:
    def test_simple_blockquote(self):
        out = md_to_tg_html("> hello")
        assert "<blockquote>hello</blockquote>" in out

    def test_blockquote_bold_inside(self):
        # Bug fix: **bold** inside blockquote was escaped before bold ran.
        out = md_to_tg_html("> **important**")
        assert "<blockquote>" in out
        assert "<b>important</b>" in out
        assert "**" not in out

    def test_blockquote_italic_inside(self):
        out = md_to_tg_html("> *note*")
        assert "<blockquote>" in out
        assert "<i>note</i>" in out

    def test_blockquote_with_inline_code(self):
        # Inline code placeholder must survive blockquote wrapping.
        out = md_to_tg_html("> Use `config.py` here")
        assert "<blockquote>" in out
        assert "<code>config.py</code>" in out

    def test_blockquote_escapes_special_chars(self):
        out = md_to_tg_html("> a < b & c")
        assert "<blockquote>" in out
        assert "&lt;" in out
        assert "&amp;" in out

    def test_multiple_blockquote_lines(self):
        out = md_to_tg_html("> line one\n> line two")
        assert out.count("<blockquote>") == 2


class TestTables:
    def test_simple_table(self):
        md = "| name | age |\n|------|-----|\n| Alex | 8   |\n| Sam  | 5   |\n"
        out = md_to_tg_html(md)
        assert out.startswith("<pre>")
        assert out.endswith("</pre>")
        # Header separator dashes present.
        assert "-+-" in out
        # Data rows present.
        assert "Alex" in out
        assert "Sam" in out

    def test_table_with_special_chars_escaped(self):
        # Table content with < should be escaped in the pre block.
        md = "| code | meaning |\n|------|---------|\n| <br> | line    |\n"
        out = md_to_tg_html(md)
        assert "&lt;br&gt;" in out

    def test_table_columns_aligned(self):
        md = "| A | Beta | Gamma |\n|---|------|-------|\n| x | y    | z     |\n"
        out = md_to_tg_html(md)
        # Header row should show padded names.
        assert "A | Beta | Gamma" in out


class TestPassthrough:
    def test_already_html_skips_converter(self):
        html = "<b>Bold</b> and <i>italic</i> text here."
        out = convert_for_tg(html)
        # Should NOT double-escape — tags preserved.
        assert "<b>Bold</b>" in out
        assert "&lt;b&gt;" not in out

    def test_html_with_blockquote_passthrough(self):
        html = "<blockquote>Quote</blockquote><b>After</b>"
        out = convert_for_tg(html)
        assert "<blockquote>Quote</blockquote>" in out

    def test_markdown_not_mistaken_for_html(self):
        # Markdown with < > that aren't real tags — should NOT passthrough.
        md = "Compare a < b and c > d and a**b**"
        out = convert_for_tg(md)
        # < > escaped (markdown path taken).
        assert "&lt;" in out
        assert "&gt;" in out

    def test_html_with_comparison_operator_not_passthrough(self):
        # Bug fix: <b>bold</b> + bare `x<10` must NOT trigger passthrough.
        # TG rejects messages with raw `<10` in HTML parse mode (400 Bad Request).
        html = "<b>bold</b> and <i>italic</i> but x<10 or y>20"
        out = convert_for_tg(html)
        # Must have gone through the markdown converter (escapes the raw `<`).
        assert "&lt;10" in out or "&lt;" in out

    def test_pure_markdown_runs_through_converter(self):
        out = convert_for_tg("**hi**")
        assert out == "<b>hi</b>"

    def test_empty_input(self):
        assert convert_for_tg("") == ""
        assert md_to_tg_html("") == ""


class TestRealWorldLlmOutput:
    """Patterns LLMs (Claude) typically emit."""

    def test_mixed_para_with_bold_code_link(self):
        md = "Run `git status` and then **commit** your changes. See [docs](https://git-scm.com)."
        out = md_to_tg_html(md)
        assert "<code>git status</code>" in out
        assert "<b>commit</b>" in out
        assert '<a href="https://git-scm.com">docs</a>' in out

    def test_numbered_list_with_bold(self):
        # Numbered lists aren't markdown-processed (left as-is).
        md = "1. First **item**\n2. Second *one*"
        out = md_to_tg_html(md)
        assert "<b>item</b>" in out
        assert "<i>one</i>" in out

    def test_code_block_then_prose(self):
        md = "Here is code:\n```python\nx = 1\n```\nThat's **it**."
        out = md_to_tg_html(md)
        assert "x = 1" in out
        assert "<b>it</b>" in out

    def test_arithmetic_not_bold(self):
        # "2*3=6" should NOT be parsed as italic bc no matching closer.
        out = md_to_tg_html("2*3=6 no italic")
        # Literal * preserved.
        assert "2*3=6" in out or "2&lt;" not in out

    def test_snake_case_not_italic(self):
        # `BE_mtm` must NOT render `mtm` as italic (real bug from tao-diet).
        out = md_to_tg_html("BE_mtm collapses and BE_liq drops")
        assert "<i>" not in out
        assert "BE_mtm" in out
        assert "BE_liq" in out

    def test_snake_case_longer_chain(self):
        out = md_to_tg_html("foo_bar_baz_qux works fine")
        assert "<i>" not in out

    def test_bold_across_newline(self):
        # Multi-sentence bold span (real bug from tao-diet).
        md = ("**Critical: first sentence.\n"
              "Second sentence still bold**.")
        out = md_to_tg_html(md)
        assert "<b>" in out
        assert "Critical" in out
        assert "still bold" in out
        # Literal `**` should be gone.
        assert "**Critical" not in out

    def test_italic_word_boundary(self):
        # `_x_` at word boundary still italic.
        out = md_to_tg_html("hello _world_ bye")
        assert "<i>world</i>" in out

    def test_italic_not_in_url(self):
        # `x_y_z.com` shouldn't become italic even though underscores present.
        out = md_to_tg_html("See docs_v2_final.pdf")
        assert "<i>" not in out


class TestAutoCodeAndLink:
    """Fixes D (bare URL linkify) + E (identifier auto-code)."""

    def test_bare_https_url_linkified(self):
        out = md_to_tg_html("See https://example.com/foo for docs")
        assert '<a href="https://example.com/foo">https://example.com/foo</a>' in out

    def test_bare_wss_url_linkified(self):
        out = md_to_tg_html("Connect to wss://archive.chain.opentensor.ai:443 now")
        assert '<a href="wss://archive.chain.opentensor.ai:443">' in out

    def test_bare_url_trailing_punct_stripped(self):
        # Trailing comma/period should not be part of the URL.
        out = md_to_tg_html("Visit https://example.com, then return.")
        assert '<a href="https://example.com">' in out
        # Punct stays in plain text.
        assert "," in out

    def test_snake_case_autocoded(self):
        out = md_to_tg_html("The BE_mtm metric and BE_dereg show burn.")
        assert "<code>BE_mtm</code>" in out
        assert "<code>BE_dereg</code>" in out

    def test_hash_ref_autocoded(self):
        out = md_to_tg_html("Fixed in PR#2562 last week")
        assert "<code>PR#2562</code>" in out

    def test_file_path_autocoded(self):
        out = md_to_tg_html("See experiments/fetch_all_subnet_prices.py for details")
        assert "<code>experiments/fetch_all_subnet_prices.py</code>" in out

    def test_md_extension_autocoded(self):
        out = md_to_tg_html("Update CLAUDE.md and README.md")
        assert "<code>CLAUDE.md</code>" in out
        assert "<code>README.md</code>" in out

    def test_inside_existing_code_not_double_wrapped(self):
        # `BE_mtm` already inside backticks should not get re-wrapped.
        out = md_to_tg_html("`BE_mtm` is a metric")
        # Exactly one <code> open tag for BE_mtm.
        assert out.count("<code>BE_mtm</code>") == 1
        assert "<code><code>" not in out

    def test_inside_markdown_link_not_wrapped(self):
        out = md_to_tg_html("[see repo](https://github.com/foo/bar.py)")
        # File path inside the URL should not become a nested <code>.
        assert "<code>" not in out
        assert '<a href="https://github.com/foo/bar.py">' in out

    def test_prose_with_underscore_not_wrapped(self):
        # No structural marker (single letter + underscore + word is regex-matched,
        # but "snake_case" requires letter start + underscore + alphanum). Pure
        # English prose without identifiers should stay untouched.
        out = md_to_tg_html("I really like it.")
        assert "<code>" not in out


class TestTableWidthTiers:
    """Fix B — width-aware table rendering."""

    def test_narrow_table_aligned(self):
        md = ("| A | B |\n"
              "|---|---|\n"
              "| 1 | 2 |\n"
              "| 3 | 4 |\n")
        out = md_to_tg_html(md)
        # Aligned tier uses `A | B` header + `-+-` separator.
        assert "A | B" in out
        assert "-+-" in out

    def test_medium_table_compact(self):
        # Aligned width > 40 (col widths 12,8,10,6 → sum 36 + 3*3=45)
        # but compact width ≤ 50 (sum + 2*3 = 42) → compact tier.
        md = ("| FirstColName | Second12 | ThirdLabel | Last00 |\n"
              "|--------------|----------|------------|--------|\n"
              "| abcdefghijkl | 87654321 | qrstuvwxyz | abcdef |\n"
              "| 1234567890ab | 99999999 | ZZZZZZZZZZ | 000000 |\n")
        out = md_to_tg_html(md)
        # Compact tier uses comma-separated data rows.
        assert ", " in out
        # Not aligned (no `-+-` separator).
        assert "-+-" not in out

    def test_wide_table_record_blocks(self):
        # 5-col, wide — should fall into record blocks.
        md = ("| Target | Metric   | Before (burn) | After (PR#2562) | Delta |\n"
              "|--------|----------|---------------|-----------------|-------|\n"
              "| 0.01   | BE_mtm   | D38           | D21             | -17d  |\n"
              "| 0.01   | BE_dereg | D66           | D55             | -11d  |\n")
        out = md_to_tg_html(md)
        # Record block markers.
        assert "── 1 ──" in out
        assert "Target: 0.01" in out
        assert "Metric: BE_mtm" in out
        assert "Delta: -17d" in out


class TestCheckboxes:
    """GFM task-list checkbox rendering."""

    def test_unchecked_box(self):
        out = md_to_tg_html("- [ ] Do the thing")
        assert "☐" in out
        assert "Do the thing" in out
        assert "[ ]" not in out

    def test_checked_box(self):
        out = md_to_tg_html("- [x] Done it")
        assert "☑" in out
        assert "Done it" in out
        assert "[x]" not in out

    def test_uppercase_X_also_checked(self):
        out = md_to_tg_html("- [X] All caps tick")
        assert "☑" in out

    def test_indented_checkbox(self):
        out = md_to_tg_html("  - [x] Nested task")
        assert "☑ Nested task" in out

    def test_mixed_checkbox_list(self):
        md = ("- [x] Item A\n"
              "- [ ] Item B\n"
              "- [x] Item C\n")
        out = md_to_tg_html(md)
        assert out.count("☑") == 2
        assert out.count("☐") == 1


class TestParityPolishV2:
    """Items 1-6 from the parity gap bundle."""

    def test_horizontal_rule_dashes(self):
        from bot.md_to_tg_html import md_to_tg_html
        out = md_to_tg_html("before\n---\nafter")
        assert "──────────" in out
        assert "---" not in out

    def test_horizontal_rule_asterisks(self):
        out = md_to_tg_html("before\n***\nafter")
        assert "──────────" in out

    def test_image_markdown_rendered(self):
        out = md_to_tg_html("![cute cat](https://example.com/cat.jpg)")
        assert "🖼" in out
        assert '<a href="https://example.com/cat.jpg">cute cat</a>' in out

    def test_image_unsafe_url_dropped(self):
        out = md_to_tg_html("![x](javascript:alert(1))")
        assert "🖼" in out
        assert "javascript" not in out

    def test_escaped_asterisk_literal(self):
        out = md_to_tg_html(r"Use \*literal\* asterisks")
        assert "*literal*" in out
        assert "<b>" not in out
        assert "<i>" not in out

    def test_escaped_underscore_literal(self):
        out = md_to_tg_html(r"Show \_underscore\_")
        assert "_underscore_" in out
        assert "<i>" not in out

    def test_extended_checkbox_cancelled(self):
        out = md_to_tg_html("- [-] Cancelled task")
        assert "❌" in out
        assert "Cancelled task" in out

    def test_extended_checkbox_important(self):
        out = md_to_tg_html("- [!] Important")
        assert "⚠️" in out

    def test_extended_checkbox_unclear(self):
        out = md_to_tg_html("- [?] Unclear")
        assert "❓" in out

    def test_details_expandable_blockquote(self):
        md = "<details><summary>Click to expand</summary>\nHidden body\n</details>"
        out = md_to_tg_html(md)
        assert "<blockquote expandable>" in out
        assert "Click to expand" in out
        assert "Hidden body" in out

    def test_tool_emoji_mapping(self):
        from bot.md_to_tg_html import tool_emoji
        assert tool_emoji("bash") == "🐚"
        assert tool_emoji("Read") == "📖"
        assert tool_emoji("Edit") == "✏️"
        assert tool_emoji("grep") == "🔎"
        assert tool_emoji("WebFetch") == "🌐"
        assert tool_emoji("unknowntool") == "🔧"
        assert tool_emoji("mcp__server__read") == "📖"
