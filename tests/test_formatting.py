"""Tests for Markdown -> Telegram HTML conversion and fence-aware chunking."""

from __future__ import annotations

from datetime import datetime, timezone

from agy_tg_bot.formatting import (
    TELEGRAM_MAX_MESSAGE_LEN,
    group_models_by_family,
    markdown_to_telegram_html,
    render_streaming_preview,
    render_telegram_html_chunks,
    render_turn_footer,
    render_usage_report_html,
    split_markdown_chunks,
    strip_ansi,
)
from agy_tg_bot.models import TokenUsage, ToolTrace, TurnProgress, TurnResult


def test_markdown_to_html_basics_and_escaping() -> None:
    md = (
        "# Heading <script>\n"
        "Use `a < b && *not_italic*` and **bold** + *italic* + ~~old~~.\n"
        "Visit [Antigravity](https://antigravity.google/docs).\n"
        "> Quoted line\n\n"
        "```python\n"
        "x = 1 < 2\n"
        "```\n"
    )
    rendered = markdown_to_telegram_html(md)
    assert "<b>Heading &lt;script&gt;</b>" in rendered
    assert "<code>a &lt; b &amp;&amp; *not_italic*</code>" in rendered
    assert "<b>bold</b>" in rendered
    assert "<i>italic</i>" in rendered
    assert "<s>old</s>" in rendered
    assert '<a href="https://antigravity.google/docs">Antigravity</a>' in rendered
    assert "<blockquote>Quoted line</blockquote>" in rendered
    assert '<pre><code class="language-python">x = 1 &lt; 2</code></pre>' in rendered


def test_split_markdown_preserves_code_fences() -> None:
    code_lines = "\n".join(f"print({i})" for i in range(400))
    md = f"Intro text\n\n```python\n{code_lines}\n```\n\nDone!"
    chunks = split_markdown_chunks(md, max_chars=800)
    assert len(chunks) >= 3
    for chunk in chunks:
        # Every chunk must have balanced ``` fence markers
        fence_count = sum(1 for ln in chunk.splitlines() if ln.strip().startswith("```"))
        assert fence_count % 2 == 0
    # Continuation chunks inside code block should reopen with ```python
    assert chunks[1].startswith("```python\n")


def test_render_html_chunks_respects_telegram_limit_under_heavy_escaping() -> None:
    # `<` expands 4x into `&lt;` in HTML!
    raw = ("<" * 1500 + "\n") * 4
    chunks = render_telegram_html_chunks(raw)
    assert len(chunks) >= 4
    for html_chunk, _ in chunks:
        assert len(html_chunk) <= TELEGRAM_MAX_MESSAGE_LEN


def test_streaming_preview_and_footer() -> None:
    progress = TurnProgress(
        conversation_id="12345678-abcd",
        text_preview="Analyzing logs...",
        tools=[ToolTrace(step_index=1, name="run_command", state="ACTIVE", summary="journalctl -n 50")],
    )
    preview = render_streaming_preview(progress, verbose=True)
    assert "run_command" in preview
    assert "journalctl -n 50" in preview
    assert "Analyzing logs..." in preview

    result = TurnResult(
        conversation_id="12345678-abcd",
        status="SUCCESS",
        response="All clear",
        turn_duration_seconds=3.4,
        turn_usage=TokenUsage(input_tokens=1200, output_tokens=300, thinking_tokens=100, total_tokens=1500),
        tools=[ToolTrace(step_index=1, name="run_command", state="DONE", duration_seconds=0.2)],
        permission_denials=["tool run_command soft-denied"],
    )
    footer = render_turn_footer(result, verbose=True)
    assert "3.4s" in footer
    assert "1.5k tok" in footer
    assert "think 100" in footer
    assert "soft-denied" in footer


def test_group_models_by_family_and_usage_cards_and_ansi_strip() -> None:
    assert strip_ansi("\x1b[32mhello\x1b[0m") == "hello"

    models = [
        ("gemini-4-hydrogen-high", "Gemini 4 Hydrogen (High)"),
        ("gemini-4-hydrogen-xhigh", "Gemini 4 Hydrogen (XHigh)"),
        ("gemini-4-hydrogen-low", "Gemini 4 Hydrogen (Low)"),
        ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)"),
    ]
    families = group_models_by_family(models)
    assert len(families) == 2
    assert families[0].name == "Gemini 4 Hydrogen"
    # Sorted Low -> High -> XHigh
    assert [v.variant for v in families[0].variants] == ["Low", "High", "XHigh"]
    assert families[1].name == "GPT-OSS 120B"

    tsv = (
        "Gemini Models\tWeekly Limit Remaining\t90%\t2026-12-01T07:40:02Z\n"
        "Gemini Models\tFive Hour Limit Remaining\t42%\t2026-11-24T12:40:02Z\n"
        "Claude and GPT models\tFive Hour Limit Remaining\t15%\t2026-11-24T13:57:49Z\n"
    )
    now = datetime(2026, 11, 24, 9, 0, 0, tzinfo=timezone.utc)
    usage_html = render_usage_report_html(tsv, now=now)
    assert "<pre>" not in usage_html
    assert "<b>Gemini Models</b>" in usage_html
    assert "5 小时配额" in usage_html
    assert "每周配额" in usage_html
    assert "42%" in usage_html
    assert "🟡" in usage_html  # 42% yellow
    assert "🟢" in usage_html  # 90% green
    assert "🔴" in usage_html  # 15% red
    assert "3小时40分后重置" in usage_html
    # 5 小时配额 appears before 每周配额 inside Gemini Models
    assert usage_html.index("5 小时配额") < usage_html.index("每周配额")
