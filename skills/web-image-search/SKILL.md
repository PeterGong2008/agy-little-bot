---
name: web-image-search
description: Search and download real web images (landmarks, architecture renderings, news photos, products, diagrams) to local disk so they are automatically sent to the user in Telegram. Use whenever the user asks to find, search, fetch, or show real photos/images from the internet.
---

# Web Image Search (`agy-tg-imgsearch`)

Built-in `search_web` only returns text summaries and webpage links, not image files.
Whenever the user wants real photos or images from the web, use the pre-installed CLI helper **`agy-tg-imgsearch`**:

```bash
agy-tg-imgsearch "新皇岗口岸 效果图" --count 3 --out ./web_images
```

## Workflow

1. Run `agy-tg-imgsearch "<concise keywords>" --count 3 --out ./web_images` via `run_command`.
   - Use `--source wikimedia` if looking specifically for encyclopedic / Wikimedia Commons photography.
   - Use `--download-url "<url>" --out ./web_images` if you already have a direct image URL.
2. Optionally inspect the downloaded local image(s) with `read_file` to verify relevance and quality before answering.
3. Reference the local file paths (and source URLs) in your final reply. Any valid image saved in `./web_images/` is **automatically delivered as a photo in Telegram**—the user does not need to use `/get`.
4. Keep image search concise: 1 command is usually enough; do not loop endlessly.
