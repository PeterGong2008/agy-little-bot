---
name: fast-image-analysis
description: Fast, pragmatic visual analysis workflow for user-uploaded photos (location guessing, sign/text reading, screenshot debugging, object identification) in Telegram chats. Prevents slow blind web-search loops and uses pre-installed Pillow/exiftool cleanly.
---

# Fast Image Analysis Workflow

When the user uploads a photo (e.g. under `.tg_inbox/`) and asks to identify a location, read text, or analyze the scene:

1. **Inspect directly first**: Call `read_file` on the image path.
2. **Zoom/crop in at most 1 step if small text is unreadable**:
   - `python3` with `PIL` (`Pillow`) and `exiftool` are available on this VPS.
   - If you need to read small street signs, building labels, or EXIF metadata, write a single short command (`PIL.Image.open(...).crop(...).save("/tmp/crop.jpg")`) and inspect the crop once with `read_file`.
   - Never spend multiple commands hunting for alternative image libraries.
3. **Keep web verification tightly bounded**:
   - Run at most **2 to 4 targeted `search_web` queries** to verify visual clues.
   - Do **NOT** run dozens of speculative web searches in a loop.
   - Once you have a strong hypothesis (or the top 1–2 candidates), stop searching and give a clear, concise answer explaining the visual clues.
4. **Clean up temporary crops** (keep them in `/tmp/` rather than the workspace root so temporary crops are not auto-sent back to the user as final images!).
