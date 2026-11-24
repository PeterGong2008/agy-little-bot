"""Tests for web image search parsing, magic-byte validation, and /img handler."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agy_tg_bot.img_search import (
    DownloadedImage,
    ImageCandidate,
    detect_image_extension,
    download_verified_image,
    parse_bing_image_results,
)


def test_detect_image_extension_magic_bytes() -> None:
    assert detect_image_extension(b"\x89PNG\r\n\x1a\n" + b"0" * 20) == ".png"
    assert detect_image_extension(b"\xff\xd8\xff\xe0" + b"0" * 20) == ".jpg"
    assert detect_image_extension(b"GIF89a" + b"0" * 20) == ".gif"
    assert detect_image_extension(b"RIFF1234WEBP" + b"0" * 20) == ".webp"
    assert detect_image_extension(b"<html>not an image</html>") is None


def test_parse_bing_image_results() -> None:
    sample_html = (
        '<a m="{&quot;murl&quot;:&quot;https://img.example.com/a.jpg&quot;,'
        '&quot;t&quot;:&quot;Huanggang Port&quot;}"></a>'
        '<a m="{&quot;murl&quot;:&quot;https://img.example.com/b.png&quot;,'
        '&quot;desc&quot;:&quot;Rendering&quot;}"></a>'
    )
    # Match the escaped m=&quot;{...}&quot; attribute format Bing emits
    bing_html = (
        'm=&quot;{&quot;murl&quot;:&quot;https://img.example.com/a.jpg&quot;,'
        '&quot;t&quot;:&quot;Huanggang Port&quot;}&quot; '
        'm=&quot;{&quot;murl&quot;:&quot;https://img.example.com/b.png&quot;,'
        '&quot;desc&quot;:&quot;Rendering&quot;}&quot;'
    )
    items = parse_bing_image_results(bing_html)
    assert len(items) == 2
    assert items[0].url == "https://img.example.com/a.jpg"
    assert items[0].title == "Huanggang Port"
    assert items[1].url == "https://img.example.com/b.png"
    assert items[1].title == "Rendering"

    # Also verify fallback murl-only extraction
    fallback = parse_bing_image_results(sample_html)
    assert len(fallback) == 2


def test_download_verified_image_fixes_wrong_extension_and_rejects_html(tmp_path: Path) -> None:
    fake_jpg = b"\xff\xd8\xff\xe0" + (b"\x00" * 6000)

    class _FakeResp:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self, n: int = -1) -> bytes:
            return self._payload[:n] if n > 0 else self._payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    # Even if URL ends with `.jpg.2`, magic bytes save it cleanly as `.jpg`
    with patch("urllib.request.urlopen", return_value=_FakeResp(fake_jpg)):
        res = download_verified_image(
            ImageCandidate(url="https://news.example.com/pic/123.jpg.2", title="News", source="bing"),
            tmp_path,
            prefix="test_1",
        )
    assert isinstance(res, DownloadedImage)
    assert res.extension == ".jpg"
    assert Path(res.local_path).is_file()
    assert Path(res.local_path).suffix == ".jpg"

    # Rejects HTML error page pretending to be an image
    with patch("urllib.request.urlopen", return_value=_FakeResp(b"<html>403 Forbidden</html>" * 500)):
        bad = download_verified_image(
            ImageCandidate(url="https://news.example.com/blocked.jpg", source="bing"),
            tmp_path,
            prefix="test_2",
        )
    assert bad is None
