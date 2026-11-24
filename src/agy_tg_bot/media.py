"""Image path extraction and workspace artifact discovery for automatic Telegram delivery."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote

from .config import BotConfig

IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
)
PHOTO_API_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp"})

# Telegram Bot API limits
TELEGRAM_MAX_PHOTO_BYTES = 10 * 1024 * 1024
TELEGRAM_MAX_DOC_BYTES = 50 * 1024 * 1024

_SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".tg_inbox",
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
    }
)

_EXT_PATTERN = r"(?:png|jpe?g|webp|gif|bmp)"

# 1. Markdown image or link: ![alt](target) or [label](target)
_MD_TARGET_RE = re.compile(r"!?\[[^\]]*\]\(([^)\r\n]+)\)")

# 2. Inline code span containing a path ending with an image extension
_INLINE_CODE_IMG_RE = re.compile(
    rf"`([^`\r\n]+\.{_EXT_PATTERN})`",
    re.IGNORECASE,
)

# 3. file:// URI anywhere in text
_FILE_URI_IMG_RE = re.compile(
    rf"file://(/[^\s\"'<>)\]]+\.{_EXT_PATTERN})",
    re.IGNORECASE,
)

_URL_STRIP_RE = re.compile(r"https?://[^\s)\]>\"'`]+", re.IGNORECASE)

# 4. Bare Unix/Windows/relative paths ending in an image extension
_BARE_PATH_IMG_RE = re.compile(
    rf"(?<![A-Za-z0-9_/:.\\])("
    rf"(?:~?/|(?:\./|\.\./))(?:[A-Za-z0-9_.\-\u4e00-\u9fff]+/)*[A-Za-z0-9_.\-\u4e00-\u9fff]+\.{_EXT_PATTERN}"
    rf"|(?:[A-Za-z0-9_.\-\u4e00-\u9fff]+/)+[A-Za-z0-9_.\-\u4e00-\u9fff]+\.{_EXT_PATTERN}"
    rf"|[A-Za-z]:\\(?:[^\\\r\n\"'<>|]+\\)*[^\\\r\n\"'<>|]+\.{_EXT_PATTERN}"
    rf")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

# 5. Simple filename in backticks or quotes like "chart.png" or 'plot_1.jpg'
_QUOTED_FILE_IMG_RE = re.compile(
    rf"[\"']([^\"'\r\n]+\.{_EXT_PATTERN})[\"']",
    re.IGNORECASE,
)


def _clean_candidate(raw: str) -> str | None:
    s = raw.strip().strip("<>").strip()
    if not s:
        return None
    # Strip optional Markdown title: `path.png "title"`
    if " " in s and not Path(s).exists():
        first_tok = s.split()[0]
        if first_tok.lower().endswith(tuple(IMAGE_EXTENSIONS)):
            s = first_tok
    low = s.lower()
    if low.startswith(("http://", "https://", "data:", "tg://", "mailto:")):
        return None
    if low.startswith("file://"):
        s = unquote(s[7:])
        # On Windows file:///C:/... -> C:/...
        if os.name == "nt" and len(s) >= 3 and s[0] == "/" and s[2] == ":":
            s = s[1:]
    # Strip optional fragment/query like #L1 or ?v=1
    s = s.split("#", 1)[0].split("?", 1)[0].strip()
    if not s:
        return None
    if Path(s).suffix.lower() not in IMAGE_EXTENSIONS:
        return None
    return s


def extract_image_paths_from_text(text: str) -> list[str]:
    """Extract local image path candidates from Markdown, tool outputs, or prose."""
    if not text:
        return []

    found: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        cleaned = _clean_candidate(raw)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            found.append(cleaned)

    for pattern in (
        _MD_TARGET_RE,
        _FILE_URI_IMG_RE,
        _INLINE_CODE_IMG_RE,
        _QUOTED_FILE_IMG_RE,
    ):
        for match in pattern.finditer(text):
            _add(match.group(1))

    # Strip HTTP(S) URLs before matching bare local paths to avoid partial URL matches
    text_no_urls = _URL_STRIP_RE.sub(" ", text)
    for match in _BARE_PATH_IMG_RE.finditer(text_no_urls):
        _add(match.group(1))

    return found


def _is_inside_tg_inbox(path: Path) -> bool:
    return ".tg_inbox" in path.parts


def _is_agy_artifact_path(path: Path) -> bool:
    """Allow AGY-generated image artifacts stored under ~/.gemini/antigravity/brain/."""
    try:
        brain_root = (Path.home() / ".gemini" / "antigravity" / "brain").resolve()
        resolved = path.resolve()
        return resolved == brain_root or brain_root in resolved.parents
    except OSError:
        return False


def _is_image_allowed(path: Path, config: BotConfig) -> bool:
    if _is_inside_tg_inbox(path):
        return False
    if config.is_path_allowed(path):
        return True
    # Always allow AGY's own brain artifact images when generated during a turn
    if _is_agy_artifact_path(path):
        return True
    return False


def scan_recent_workspace_images(
    workspace: Path,
    *,
    since_timestamp: float,
    max_depth: int = 2,
    max_results: int = 8,
) -> list[Path]:
    """Find image files created/modified inside `workspace` during the current turn."""
    if not workspace.is_dir():
        return []

    matches: list[tuple[float, Path]] = []
    root_depth = len(workspace.resolve().parts)

    for dirpath, dirnames, filenames in os.walk(workspace):
        cur_path = Path(dirpath).resolve()
        depth = len(cur_path.parts) - root_depth

        # Prune skipped directories and stop descending beyond max_depth
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _SKIP_DIR_NAMES and not d.startswith(".") and depth < max_depth
        ]

        for fname in filenames:
            if Path(fname).suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            fpath = cur_path / fname
            try:
                st = fpath.stat()
            except OSError:
                continue
            if st.st_size <= 0 or st.st_size > TELEGRAM_MAX_DOC_BYTES:
                continue
            if st.st_mtime >= since_timestamp - 1.0:
                matches.append((st.st_mtime, fpath))

    matches.sort(key=lambda item: item[0])
    return [p for _, p in matches[:max_results]]


def collect_turn_images(
    *,
    response_text: str,
    tool_candidates: Iterable[str],
    workspace: str,
    config: BotConfig,
    turn_started_at: float,
) -> list[Path]:
    """Resolve, deduplicate, and authorize all local images produced/referenced in a turn."""
    ws_path = Path(workspace).expanduser().resolve()
    ordered: list[Path] = []
    seen: set[Path] = set()

    def _try_add(raw_or_path: str | Path) -> None:
        if len(ordered) >= config.max_auto_images:
            return
        p = Path(raw_or_path).expanduser() if isinstance(raw_or_path, str) else raw_or_path
        if not p.is_absolute():
            p = ws_path / p
        try:
            resolved = p.resolve()
        except OSError:
            return
        if resolved in seen:
            return
        if not resolved.is_file():
            return
        if resolved.suffix.lower() not in IMAGE_EXTENSIONS:
            return
        if not _is_image_allowed(resolved, config):
            return
        try:
            size = resolved.stat().st_size
        except OSError:
            return
        if size <= 0 or size > TELEGRAM_MAX_DOC_BYTES:
            return
        seen.add(resolved)
        ordered.append(resolved)

    # 1. Explicit paths in the final response text (highest relevance)
    for raw in extract_image_paths_from_text(response_text):
        _try_add(raw)

    # 2. Image paths reported in tool call outputs/parameters during this turn
    for raw in tool_candidates:
        _try_add(raw)

    # 3. Newly created/updated image files in workspace during this turn
    for fpath in scan_recent_workspace_images(
        ws_path,
        since_timestamp=turn_started_at,
        max_results=config.max_auto_images,
    ):
        _try_add(fpath)

    return ordered


def should_send_as_photo(path: Path) -> bool:
    """Return True if the image is suitable for Telegram send_photo (else send_document/animation)."""
    if path.suffix.lower() not in PHOTO_API_EXTENSIONS:
        return False
    try:
        return 0 < path.stat().st_size <= TELEGRAM_MAX_PHOTO_BYTES
    except OSError:
        return False
