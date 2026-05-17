#!/usr/bin/env python3
"""
Extract og:image URL from a web article.

Usage:
  python3 extract_ogimage.py "https://example.com/article"

Output:
  Prints the og:image URL to stdout, or "NO_IMAGE" if not found / fetch fails.

Also exposes ``extract_og_image_from_url`` for in-process callers (publisher).
"""

from __future__ import annotations

import re
import ssl
import sys
import urllib.request
from urllib.parse import urljoin, urlparse


_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def fetch_html(url: str, timeout: int = 15) -> str:
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as response:
        charset = "utf-8"
        content_type = response.headers.get("Content-Type", "")
        if "charset=" in content_type:
            charset = content_type.split("charset=")[-1].strip()
        return response.read().decode(charset, errors="replace")


def extract_og_image(html: str, base_url: str) -> str | None:
    patterns = [
        r'<meta\s[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta\s[^>]*content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']',
        r'<meta\s[^>]*name=["\']twitter:image["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta\s[^>]*content=["\']([^"\']+)["\'][^>]*name=["\']twitter:image["\']',
    ]
    match = None
    for pat in patterns:
        match = re.search(pat, html, re.IGNORECASE)
        if match:
            break
    if not match:
        return None

    img_url = match.group(1).strip()
    if img_url.startswith("//"):
        parsed = urlparse(base_url)
        img_url = f"{parsed.scheme}:{img_url}"
    elif img_url.startswith("/"):
        img_url = urljoin(base_url, img_url)
    return img_url


def extract_og_image_from_url(url: str, timeout: int = 15) -> str | None:
    """Convenience: fetch + extract. Returns None on any failure."""
    try:
        html = fetch_html(url, timeout=timeout)
        return extract_og_image(html, url)
    except Exception:
        return None


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 extract_ogimage.py <url>", file=sys.stderr)
        sys.exit(1)
    url = sys.argv[1].strip()
    img = extract_og_image_from_url(url)
    print(img if img else "NO_IMAGE")


if __name__ == "__main__":
    main()
