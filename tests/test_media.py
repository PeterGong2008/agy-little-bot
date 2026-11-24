"""Tests for image path extraction, workspace image scanning, and security filtering."""

from __future__ import annotations

import time
from pathlib import Path

from agy_tg_bot.config import BotConfig
from agy_tg_bot.media import (
    collect_turn_images,
    extract_image_paths_from_text,
    should_send_as_photo,
)


def _make_config(
    ws: Path,
    *,
    allowed_roots: tuple[str, ...] = (),
    max_auto_images: int = 8,
) -> BotConfig:
    return BotConfig(
        telegram_bot_token="999:test",
        allowed_user_ids=frozenset({1}),
        allowed_chat_ids=frozenset(),
        telegram_proxy_url=None,
        agy_bin="agy",
        default_workspace=str(ws),
        allowed_workspace_roots=allowed_roots,
        extra_dirs=(),
        runner_mode="persistent",
        default_permission_mode="default",
        allow_dangerous_mode=True,
        default_sandbox=False,
        default_model=None,
        default_effort=None,
        default_agent=None,
        disable_slash_commands=False,
        disable_auto_update=True,
        turn_timeout_seconds=60.0,
        session_idle_timeout_seconds=300.0,
        max_concurrent_runs=2,
        stream_edit_interval_seconds=1.0,
        default_verbose=True,
        long_output_file_threshold=12000,
        max_upload_mb=20,
        db_path=str(ws / "bot.db"),
        log_level="INFO",
        auto_send_images=True,
        max_auto_images=max_auto_images,
    )


def test_extract_image_paths_various_formats() -> None:
    text = (
        "Here is your chart: ![CPU Usage](plots/cpu.png)\n"
        "Also saved at [mem.jpg](file:///tmp/mem.jpg) and `out/disk.webp`.\n"
        "Ignore web URL: ![web](https://example.com/logo.png)\n"
        'Script wrote "summary.gif" and /var/tmp/report.bmp.\n'
    )
    paths = extract_image_paths_from_text(text)
    assert "plots/cpu.png" in paths
    assert "/tmp/mem.jpg" in paths
    assert "out/disk.webp" in paths
    assert "summary.gif" in paths
    assert "/var/tmp/report.bmp" in paths
    assert not any("logo.png" in p for p in paths)


def test_collect_turn_images_finds_referenced_and_new_and_skips_inbox(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    # Pre-existing old image (should NOT be auto-picked by mtime scan unless referenced)
    old_img = ws / "old.png"
    old_img.write_bytes(b"\x89PNG\r\n\x1a\nold")
    # Set mtime 1 hour in the past
    past = time.time() - 3600
    import os

    os.utime(old_img, (past, past))

    turn_start = time.time()

    # 1. Image referenced in response text
    chart = ws / "chart.png"
    chart.write_bytes(b"\x89PNG\r\n\x1a\nchart")

    # 2. Image referenced only in tool output
    sub = ws / "out"
    sub.mkdir()
    tool_img = sub / "diagram.jpg"
    tool_img.write_bytes(b"\xff\xd8\xff\xe0diagram")

    # 3. Newly created image in workspace NOT mentioned anywhere in text
    silent_img = ws / "silent_plot.webp"
    silent_img.write_bytes(b"RIFF....WEBP")

    # 4. User's own upload inside .tg_inbox (MUST be excluded even if mentioned!)
    inbox = ws / ".tg_inbox"
    inbox.mkdir()
    uploaded = inbox / "20261124_photo.jpg"
    uploaded.write_bytes(b"\xff\xd8\xff\xe0user_upload")

    # 5. Image outside ALLOWED_WORKSPACE_ROOTS (MUST be blocked!)
    secret_img = outside / "secret.png"
    secret_img.write_bytes(b"\x89PNG\r\n\x1a\nsecret")

    cfg = _make_config(ws, allowed_roots=(str(ws),))
    found = collect_turn_images(
        response_text=(
            f"Generated ![chart](chart.png), saw `{uploaded}`, "
            f"and checked `{secret_img}`."
        ),
        tool_candidates=[str(tool_img), str(chart)],  # duplicate chart should dedup
        workspace=str(ws),
        config=cfg,
        turn_started_at=turn_start,
    )

    assert found == [chart.resolve(), tool_img.resolve(), silent_img.resolve()]
    assert should_send_as_photo(chart) is True
    gif_file = ws / "anim.gif"
    gif_file.write_bytes(b"GIF89a")
    assert should_send_as_photo(gif_file) is False
