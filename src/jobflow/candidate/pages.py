"""The HTML page skeleton the interview briefing and the skill-gaps report share."""
from __future__ import annotations

import html

BASE_STYLE = ("body{font:%s/1.55 system-ui,sans-serif;max-width:%s;margin:2rem auto;"
              "padding:0 1rem;color:#17202a;background:#f7f8fa}.meta{color:#52606d}"
              "li{margin:.4rem 0}a{color:#125ca1}")


def html_page(title: str, body: str, style: str = "", *, width: str = "900px",
              font_size: str = "16px") -> str:
    """A whole page: `title` is escaped here; `body` and `style` must already be safe."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
{BASE_STYLE % (font_size, width)}
{style}
</style></head><body>
{body}
</body></html>"""
