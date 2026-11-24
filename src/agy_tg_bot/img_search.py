"""Lightweight web image search & verified downloader (`agy-tg-imgsearch`)."""

from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
WIKI_USER_AGENT = "AgyLittleBot/0.1 (https://github.com/PeterGong2008/agy-little-bot)"

MIN_IMAGE_BYTES = 4 * 1024  # Skip tiny icons / tracking pixels
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # Telegram send_photo cap (10 MB)

_BING_MURL_RE = re.compile(r'murl&quot;:&quot;(https?://[^&]+?)&quot;')
_BING_JSON_ITEM_RE = re.compile(
    r'm=&quot;(\{[^"]*?murl[^"]*?\})&quot;',
    re.DOTALL,
)
_SAFE_SLUG_RE = re.compile(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+")


@dataclass(frozen=True, slots=True)
class ImageCandidate:
    url: str
    title: str = ""
    source: str = ""


@dataclass(frozen=True, slots=True)
class DownloadedImage:
    local_path: str
    source_url: str
    title: str
    source: str
    size_bytes: int
    extension: str


def detect_image_extension(data: bytes) -> str | None:
    """Identify image type strictly from binary magic bytes."""
    if len(data) < 12:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    return None


def parse_bing_image_results(page_html: str) -> list[ImageCandidate]:
    """Extract direct image URLs and titles from a Bing Images search result page."""
    results: list[ImageCandidate] = []
    seen: set[str] = set()

    for match in _BING_JSON_ITEM_RE.finditer(page_html):
        raw_json = html_lib.unescape(match.group(1))
        try:
            obj = json.loads(raw_json)
        except json.JSONDecodeError:
            continue
        url = str(obj.get("murl") or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        title = str(obj.get("t") or obj.get("desc") or "").strip()
        results.append(ImageCandidate(url=url, title=title[:120], source="bing"))

    if not results:
        for match in _BING_MURL_RE.finditer(page_html):
            url = html_lib.unescape(match.group(1)).strip()
            if not url.startswith(("http://", "https://")) or url in seen:
                continue
            seen.add(url)
            results.append(ImageCandidate(url=url, title="", source="bing"))

    return results


def search_bing_images(query: str, *, limit: int = 12, timeout: float = 10.0) -> list[ImageCandidate]:
    """Query Bing Images public search page."""
    params = urllib.parse.urlencode({"q": query, "form": "HDRSC3", "mkt": "zh-CN"})
    url = f"https://www.bing.com/images/search?{params}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
    return parse_bing_image_results(body)[:limit]


def search_wikimedia_images(
    query: str, *, limit: int = 8, timeout: float = 10.0
) -> list[ImageCandidate]:
    """Query Wikimedia Commons public API for high-res images."""
    params = urllib.parse.urlencode(
        {
            "action": "query",
            "generator": "search",
            "gsrnamespace": 6,
            "gsrsearch": query,
            "gsrlimit": limit,
            "prop": "imageinfo",
            "iiprop": "url|mime|size",
            "iiurlwidth": 1280,
            "format": "json",
        }
    )
    url = f"https://commons.wikimedia.org/w/api.php?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": WIKI_USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="ignore"))

    pages = (payload.get("query") or {}).get("pages") or {}
    results: list[ImageCandidate] = []
    for page in pages.values():
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        info = infos[0]
        mime = str(info.get("mime") or "").lower()
        if not mime.startswith("image/") or "svg" in mime or "tiff" in mime:
            continue
        img_url = str(info.get("thumburl") or info.get("url") or "").strip()
        if not img_url:
            continue
        title = str(page.get("title") or "").removeprefix("File:").strip()
        results.append(ImageCandidate(url=img_url, title=title[:120], source="wikimedia"))
    return results


def download_verified_image(
    candidate: ImageCandidate,
    out_dir: Path,
    *,
    prefix: str,
    timeout: float = 8.0,
) -> DownloadedImage | None:
    """Download an image URL, verify binary magic bytes and size, and save with true extension."""
    parsed = urllib.parse.urlparse(candidate.url)
    referer = f"{parsed.scheme}://{parsed.netloc}/" if parsed.scheme and parsed.netloc else ""
    headers = {
        "User-Agent": WIKI_USER_AGENT if candidate.source == "wikimedia" else USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    if referer and candidate.source != "wikimedia":
        headers["Referer"] = referer

    req = urllib.request.Request(candidate.url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(MAX_IMAGE_BYTES + 1)
    except Exception:
        return None

    if len(data) < MIN_IMAGE_BYTES or len(data) > MAX_IMAGE_BYTES:
        return None

    ext = detect_image_extension(data)
    if ext is None:
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{prefix}{ext}"
    dest.write_bytes(data)
    return DownloadedImage(
        local_path=str(dest.resolve()),
        source_url=candidate.url,
        title=candidate.title,
        source=candidate.source,
        size_bytes=len(data),
        extension=ext,
    )


def search_and_download_images(
    query: str,
    *,
    out_dir: Path,
    count: int = 3,
    source: str = "auto",
) -> list[DownloadedImage]:
    """Search web images and download up to `count` verified local files."""
    count = max(1, min(8, count))
    candidates: list[ImageCandidate] = []

    if source in {"auto", "bing"}:
        try:
            candidates.extend(search_bing_images(query, limit=max(12, count * 4)))
        except Exception:
            pass

    if source == "wikimedia" or (source == "auto" and len(candidates) < count * 2):
        try:
            candidates.extend(search_wikimedia_images(query, limit=max(6, count * 2)))
        except Exception:
            pass

    slug = _SAFE_SLUG_RE.sub("_", query.strip()).strip("_")[:28] or "img"
    ts = time.strftime("%Y%m%d_%H%M%S")
    downloaded: list[DownloadedImage] = []
    tried_urls: set[str] = set()

    for idx, cand in enumerate(candidates, start=1):
        if len(downloaded) >= count:
            break
        if cand.url in tried_urls:
            continue
        tried_urls.add(cand.url)
        item = download_verified_image(
            cand,
            out_dir,
            prefix=f"{ts}_{slug}_{idx}",
        )
        if item is not None:
            downloaded.append(item)

    return downloaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agy-tg-imgsearch",
        description="Search and download verified web images to local directory for AGY / Telegram",
    )
    parser.add_argument("query", nargs="?", help="Image search keywords")
    parser.add_argument(
        "--count", "-n", type=int, default=3, help="Number of images to download (1-8, default: 3)"
    )
    parser.add_argument(
        "--out",
        "-o",
        type=Path,
        default=Path("./web_images"),
        help="Output directory (default: ./web_images)",
    )
    parser.add_argument(
        "--source",
        choices=("auto", "bing", "wikimedia"),
        default="auto",
        help="Image search source (default: auto)",
    )
    parser.add_argument(
        "--download-url",
        default=None,
        help="Download a single direct image URL instead of searching",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args(argv)

    out_dir = args.out.expanduser().resolve()

    if args.download_url:
        ts = time.strftime("%Y%m%d_%H%M%S")
        item = download_verified_image(
            ImageCandidate(url=args.download_url, source="direct"),
            out_dir,
            prefix=f"{ts}_web",
        )
        items = [item] if item is not None else []
    elif args.query:
        items = search_and_download_images(
            args.query,
            out_dir=out_dir,
            count=args.count,
            source=args.source,
        )
    else:
        parser.error("Provide either a search query or --download-url")
        return 2

    if args.json:
        print(json.dumps([asdict(x) for x in items], ensure_ascii=False, indent=2))
    else:
        if not items:
            print("No valid images found or downloaded.", file=sys.stderr)
            return 1
        for i, item in enumerate(items, start=1):
            print(f"[{i}] {item.local_path} ({item.size_bytes} bytes, {item.source})")
            if item.title:
                print(f"    title: {item.title}")
            print(f"    source: {item.source_url}")
    return 0 if items else 1


if __name__ == "__main__":
    raise SystemExit(main())
