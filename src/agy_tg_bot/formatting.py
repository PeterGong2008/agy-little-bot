"""Markdown-to-Telegram-HTML rendering, fence-aware chunking, and progress formatting."""

from __future__ import annotations

import html
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone

from .models import ToolTrace, TurnProgress, TurnResult

TELEGRAM_MAX_MESSAGE_LEN = 4096
SAFE_RAW_CHUNK_LEN = 3400

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_MODEL_VARIANT_RE = re.compile(r"^(.*?)\s*\(([^()]+)\)\s*$")
_EFFORT_ORDER: dict[str, int] = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "xhigh": 4,
    "max": 5,
    "argon": 1,
    "carbon": 2,
    "neon": 3,
}
_USAGE_WINDOW_LABELS: dict[str, str] = {
    "five hour limit remaining": "5 小时配额",
    "weekly limit remaining": "每周配额",
    "daily limit remaining": "每日配额",
    "monthly limit remaining": "每月配额",
}


def strip_ansi(text: str) -> str:
    """Remove ANSI terminal escape sequences from CLI output."""
    if not text:
        return ""
    return _ANSI_ESCAPE_RE.sub("", text)


@dataclass(frozen=True, slots=True)
class ModelVariant:
    slug: str
    label: str
    variant: str
    index: int


@dataclass(frozen=True, slots=True)
class ModelFamily:
    name: str
    variants: tuple[ModelVariant, ...]


def group_models_by_family(models: list[tuple[str, str]]) -> list[ModelFamily]:
    """Group `(slug, label)` entries like `Gemini 4 Hydrogen (XHigh)` by model family."""
    grouped: OrderedDict[str, list[ModelVariant]] = OrderedDict()
    for idx, (slug, label) in enumerate(models):
        m = _MODEL_VARIANT_RE.match(label.strip())
        if m:
            family_name = m.group(1).strip()
            variant_name = m.group(2).strip()
        else:
            family_name = label.strip() or slug
            variant_name = "默认"
        grouped.setdefault(family_name, []).append(
            ModelVariant(slug=slug, label=label, variant=variant_name, index=idx)
        )

    families: list[ModelFamily] = []
    for family_name, variants in grouped.items():
        # Sort known effort/tier variants in intuitive low -> high order
        if len(variants) > 1 and all(v.variant.lower() in _EFFORT_ORDER for v in variants):
            variants = sorted(variants, key=lambda v: _EFFORT_ORDER[v.variant.lower()])
        families.append(ModelFamily(name=family_name, variants=tuple(variants)))
    return families


def _render_progress_bar(pct: int, *, width: int = 10) -> str:
    clamped = max(0, min(100, pct))
    filled = round(clamped / 100 * width)
    bar = "█" * filled + "░" * (width - filled)
    if clamped >= 50:
        dot = "🟢"
    elif clamped >= 20:
        dot = "🟡"
    else:
        dot = "🔴"
    return f"{dot} <code>{bar}</code> <b>{clamped}%</b>"


def _format_reset_time(iso_str: str, *, now: datetime | None = None) -> str:
    raw = iso_str.strip()
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return html.escape(raw, quote=False)

    ref = now or datetime.now(timezone.utc)
    delta_secs = int((dt - ref).total_seconds())
    utc_label = dt.astimezone(timezone.utc).strftime("%m-%d %H:%M UTC")
    if delta_secs <= 0:
        return f"即将重置（{utc_label}）"

    days, rem = divmod(delta_secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    parts: list[str] = []
    if days > 0:
        parts.append(f"{days}天")
    if hours > 0:
        parts.append(f"{hours}小时")
    if days == 0 and mins > 0:
        parts.append(f"{mins}分")
    rel = "".join(parts) or "不足1分"
    return f"约 {rel}后重置（{utc_label}）"


def render_usage_report_html(raw_report: str, *, now: datetime | None = None) -> str:
    """Format `agy -p /usage` TSV output as clean mobile-friendly Telegram HTML cards."""
    cleaned = strip_ansi(raw_report).strip()
    if not cleaned:
        return "📊 <b>AGY 配额与用量</b>\n<i>（暂无返回数据）</i>"

    groups: OrderedDict[str, list[tuple[str, int | None, str, str]]] = OrderedDict()
    parsed_any = False

    for line in cleaned.splitlines():
        if "\t" not in line:
            continue
        cols = [c.strip() for c in line.split("\t")]
        if len(cols) < 3:
            continue
        group_name = cols[0]
        window_raw = cols[1]
        pct_raw = cols[2]
        reset_raw = cols[3] if len(cols) >= 4 else ""

        pct_match = re.search(r"(\d+)\s*%", pct_raw)
        pct_val = int(pct_match.group(1)) if pct_match else None
        window_label = _USAGE_WINDOW_LABELS.get(window_raw.lower(), window_raw)
        groups.setdefault(group_name, []).append((window_label, pct_val, pct_raw, reset_raw))
        parsed_any = True

    if not parsed_any:
        # Graceful fallback for non-TSV output (avoid ugly <pre> line wrapping)
        pretty_lines = [
            f"• {html.escape(ln.strip(), quote=False)}"
            for ln in cleaned.splitlines()
            if ln.strip()
        ]
        body = "\n".join(pretty_lines)[:3600]
        return f"📊 <b>AGY 配额与用量</b>\n\n{body}"

    sections: list[str] = ["📊 <b>AGY 配额与用量</b>"]
    for group_name, rows in groups.items():
        # Show shorter window (5-hour / daily) before weekly for quicker scanning
        rows_sorted = sorted(
            rows,
            key=lambda r: (0 if ("5" in r[0] or "小时" in r[0] or "日" in r[0]) else 1),
        )
        block = [f"<b>{html.escape(group_name, quote=False)}</b>"]
        for window_label, pct_val, pct_raw, reset_raw in rows_sorted:
            if pct_val is not None:
                bar_str = _render_progress_bar(pct_val)
            else:
                bar_str = f"<b>{html.escape(pct_raw, quote=False)}</b>"
            line_str = f"• {html.escape(window_label, quote=False)}：{bar_str}"
            if reset_raw:
                reset_str = _format_reset_time(reset_raw, now=now)
                line_str += f"\n  <i>⏱ {reset_str}</i>"
            block.append(line_str)
        sections.append("\n".join(block))

    return "\n\n".join(sections)

_FENCE_OPEN_RE = re.compile(r"^```([A-Za-z0-9_+\-.#]*)\s*$")
_FENCE_CLOSE_RE = re.compile(r"^```\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_BLOCKQUOTE_RE = re.compile(r"^>\s?(.*)$")

_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+|tg://[^\s)]+)\)")
_BOLD_ASTERISK_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD_UNDERSCORE_RE = re.compile(r"(?<!\w)__(.+?)__(?!\w)", re.DOTALL)
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_ITALIC_ASTERISK_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)")


def format_token_count(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.2f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    mins, secs = divmod(int(seconds), 60)
    if mins < 60:
        return f"{mins}m{secs:02d}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h{mins:02d}m"


def _format_inline_markdown(text: str) -> str:
    """Convert inline Markdown in a non-code block segment into Telegram HTML."""
    placeholders: list[str] = []

    def _stash(rendered_html: str) -> str:
        idx = len(placeholders)
        placeholders.append(rendered_html)
        return f"\x00PH{idx}\x00"

    # 1. Extract inline code first so its contents are never touched by regexes
    def _replace_code(match: re.Match[str]) -> str:
        return _stash(f"<code>{html.escape(match.group(1), quote=False)}</code>")

    working = _INLINE_CODE_RE.sub(_replace_code, text)

    # 2. Extract Markdown links [label](url)
    def _replace_link(match: re.Match[str]) -> str:
        label = html.escape(match.group(1), quote=False)
        url = html.escape(match.group(2), quote=True)
        return _stash(f'<a href="{url}">{label}</a>')

    working = _LINK_RE.sub(_replace_link, working)

    # 3. Escape remaining raw HTML characters
    working = html.escape(working, quote=False)

    # 4. Inline emphasis
    working = _BOLD_ASTERISK_RE.sub(r"<b>\1</b>", working)
    working = _BOLD_UNDERSCORE_RE.sub(r"<b>\1</b>", working)
    working = _STRIKE_RE.sub(r"<s>\1</s>", working)
    working = _ITALIC_ASTERISK_RE.sub(r"<i>\1</i>", working)

    # 5. Restore placeholders
    for idx, value in enumerate(placeholders):
        working = working.replace(f"\x00PH{idx}\x00", value)

    return working


def markdown_to_telegram_html(text: str) -> str:
    """Convert standard Markdown output from AGY into Telegram-safe HTML."""
    if not text:
        return ""

    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    in_code = False
    code_lang = ""
    code_lines: list[str] = []
    quote_lines: list[str] = []

    def _flush_quote() -> None:
        if quote_lines:
            joined = "\n".join(_format_inline_markdown(q) for q in quote_lines)
            out.append(f"<blockquote>{joined}</blockquote>")
            quote_lines.clear()

    def _flush_code() -> None:
        nonlocal in_code, code_lang
        escaped = html.escape("\n".join(code_lines), quote=False)
        if code_lang:
            safe_lang = html.escape(code_lang, quote=True)
            out.append(f'<pre><code class="language-{safe_lang}">{escaped}</code></pre>')
        else:
            out.append(f"<pre>{escaped}</pre>")
        code_lines.clear()
        in_code = False
        code_lang = ""

    for line in lines:
        if in_code:
            if _FENCE_CLOSE_RE.match(line):
                _flush_code()
            else:
                code_lines.append(line)
            continue

        fence_match = _FENCE_OPEN_RE.match(line)
        if fence_match:
            _flush_quote()
            in_code = True
            code_lang = fence_match.group(1).strip()
            code_lines.clear()
            continue

        quote_match = _BLOCKQUOTE_RE.match(line)
        if quote_match:
            quote_lines.append(quote_match.group(1))
            continue

        _flush_quote()

        heading_match = _HEADING_RE.match(line)
        if heading_match:
            title = _format_inline_markdown(heading_match.group(2).strip())
            out.append(f"<b>{title}</b>")
            continue

        out.append(_format_inline_markdown(line))

    _flush_quote()
    if in_code:
        _flush_code()

    return "\n".join(out)


def split_markdown_chunks(text: str, max_chars: int = SAFE_RAW_CHUNK_LEN) -> list[str]:
    """Split Markdown into chunks <= max_chars while cleanly closing/reopening code fences."""
    normalized = text.replace("\r\n", "\n")
    if len(normalized) <= max_chars:
        return [normalized] if normalized else []

    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0
    in_code = False
    code_lang = ""

    def _commit_chunk() -> None:
        nonlocal current_lines, current_len
        if not current_lines:
            return
        body = "\n".join(current_lines)
        if in_code:
            body += "\n```"
        chunks.append(body)
        current_lines = [f"```{code_lang}"] if in_code else []
        current_len = len(current_lines[0]) if current_lines else 0

    for line in normalized.split("\n"):
        # Hard-wrap single ultra-long lines
        segments = (
            [line[i : i + max_chars - 32] for i in range(0, len(line), max_chars - 32)]
            if len(line) > max_chars - 32
            else [line]
        )
        for seg in segments:
            reserve = 6 if in_code else 0
            add_len = len(seg) + (1 if current_lines else 0)
            if current_lines and (current_len + add_len + reserve > max_chars):
                _commit_chunk()

            current_lines.append(seg)
            current_len += len(seg) + (1 if len(current_lines) > 1 else 0)

            # Update fence state based on original line markers
            if in_code:
                if _FENCE_CLOSE_RE.match(seg):
                    in_code = False
                    code_lang = ""
            else:
                m = _FENCE_OPEN_RE.match(seg)
                if m:
                    in_code = True
                    code_lang = m.group(1).strip()

    if current_lines:
        body = "\n".join(current_lines)
        if in_code:
            body += "\n```"
        chunks.append(body)

    return chunks


def render_telegram_html_chunks(
    markdown_text: str,
    *,
    max_html_len: int = TELEGRAM_MAX_MESSAGE_LEN - 120,
) -> list[tuple[str, str]]:
    """Return [(html_chunk, raw_markdown_chunk), ...] guaranteed to fit Telegram's 4096 limit."""
    raw_chunks = split_markdown_chunks(markdown_text, max_chars=SAFE_RAW_CHUNK_LEN)
    result: list[tuple[str, str]] = []

    for raw in raw_chunks:
        rendered = markdown_to_telegram_html(raw)
        if len(rendered) <= max_html_len:
            result.append((rendered, raw))
            continue

        # HTML entity expansion pushed this chunk over limit; re-split smaller
        sub_limit = max(600, int(len(raw) * (max_html_len / max(len(rendered), 1)) * 0.8))
        for sub_raw in split_markdown_chunks(raw, max_chars=sub_limit):
            sub_html = markdown_to_telegram_html(sub_raw)
            if len(sub_html) <= TELEGRAM_MAX_MESSAGE_LEN:
                result.append((sub_html, sub_raw))
            else:
                # Final safety fallback: plain escaped slices
                step = TELEGRAM_MAX_MESSAGE_LEN - 200
                for idx in range(0, len(sub_raw), step):
                    piece = sub_raw[idx : idx + step]
                    result.append((html.escape(piece, quote=False), piece))

    return result


def render_tool_trace_lines(tools: list[ToolTrace], *, max_items: int = 8) -> str:
    """Format compact tool execution traces as Telegram HTML."""
    if not tools:
        return ""
    hidden = max(0, len(tools) - max_items)
    visible = tools[-max_items:]
    lines: list[str] = []
    if hidden:
        lines.append(f"<i>… 以及前面 {hidden} 个工具调用</i>")

    for t in visible:
        if t.error_message:
            icon = "⚠️"
        elif t.state == "DONE":
            icon = "✅"
        else:
            icon = "🔧" if not t.is_subagent else "🤖"

        name = html.escape(t.name, quote=False)
        dur = f" ({t.duration_seconds:.1f}s)" if t.duration_seconds is not None else ""
        summary = f": <code>{html.escape(t.summary, quote=False)}</code>" if t.summary else ""
        err = (
            f" — <i>{html.escape(t.error_message[:120], quote=False)}</i>"
            if t.error_message
            else ""
        )
        lines.append(f"{icon} <b>{name}</b>{dur}{summary}{err}")
    return "\n".join(lines)


def render_streaming_preview(progress: TurnProgress, *, verbose: bool = True) -> str:
    """Render an in-progress Telegram HTML status/preview message (<= 4096 chars)."""
    sections: list[str] = []

    total_tools = len(progress.tools)
    active_tools = [t for t in progress.tools if t.state != "DONE"]
    if active_tools:
        cur_tool = active_tools[-1]
        step_note = f"第 {total_tools} 步 · " if total_tools > 1 else ""
        status_header = (
            f"⚙️ <b>AGY 执行中</b>（{step_note}{html.escape(cur_tool.name)}…）"
        )
    elif progress.text_preview:
        status_header = "✍️ <b>AGY 输出中…</b>"
    elif total_tools > 0:
        status_header = f"⏳ <b>AGY 思考中…</b>（已完成 {total_tools} 个工具调用）"
    else:
        status_header = "⏳ <b>AGY 思考中…</b>"
    sections.append(status_header)

    if verbose and progress.tools:
        sections.append(render_tool_trace_lines(progress.tools, max_items=6))

    if progress.stderr_notices:
        notice_text = html.escape(progress.stderr_notices[-1][:180], quote=False)
        sections.append(f"🔐 <i>权限提示：{notice_text}</i>")

    header_block = "\n\n".join(sections)

    if progress.text_preview.strip():
        budget = max(400, TELEGRAM_MAX_MESSAGE_LEN - len(header_block) - 240)
        preview = progress.text_preview.strip()
        if len(preview) > budget:
            preview = "…\n" + preview[-budget:]
        # Render as escaped plain preview during streaming to avoid flickering partial tags
        sections.append(html.escape(preview, quote=False))

    combined = "\n\n".join(sections)
    if len(combined) > TELEGRAM_MAX_MESSAGE_LEN:
        combined = combined[: TELEGRAM_MAX_MESSAGE_LEN - 20] + "\n…"
    return combined


def render_turn_footer(result: TurnResult, *, verbose: bool = True) -> str:
    """Render compact completion metadata footer (HTML) appended to the final response."""
    parts: list[str] = []

    if result.permission_denials:
        sample = html.escape(result.permission_denials[0][:220], quote=False)
        parts.append(
            "🔐 <b>部分工具操作因 headless 权限策略未自动执行</b>\n"
            f"<code>{sample}</code>\n"
            "<i>提示：可通过 /perm 切换权限模式，或在 ~/.gemini/antigravity-cli/settings.json "
            "配置 permissions.allow 规则。</i>"
        )

    if verbose and result.tools:
        parts.append("<b>工具执行记录</b>\n" + render_tool_trace_lines(result.tools, max_items=8))

    if verbose:
        dur = format_duration(result.turn_duration_seconds or result.duration_seconds)
        meta_bits = [f"⏱ {dur}"]
        usage = result.turn_usage if result.turn_usage.total_tokens > 0 else result.usage
        if usage.total_tokens > 0:
            tok_detail = (
                f"in {format_token_count(usage.input_tokens)} / "
                f"out {format_token_count(usage.output_tokens)}"
            )
            if usage.thinking_tokens > 0:
                tok_detail += f" / think {format_token_count(usage.thinking_tokens)}"
            meta_bits.append(f"🔢 {format_token_count(usage.total_tokens)} tok ({tok_detail})")
        if result.tools:
            meta_bits.append(f"🛠 {len(result.tools)} tools")
        if result.conversation_id:
            meta_bits.append(f"💬 <code>{html.escape(result.conversation_id[:8])}</code>")
        parts.append("<i>" + " · ".join(meta_bits) + "</i>")

    return "\n\n".join(parts)
