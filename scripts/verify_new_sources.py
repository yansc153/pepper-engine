"""Probe whether eastmoney guba + seekingalpha are reachable from the VPS.

Runs INSIDE the pepperbot container (has Playwright). Saves a small report so
we know whether to invest 1+ hr writing full adapters.

Usage in docker:
    docker compose exec pepperbot python /app/scripts/verify_new_sources.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

SA_COOKIE = Path("/app/secrets/seekingalpha_cookies.json")
SA_URL = "https://seekingalpha.com/stock-ideas/ai-tech-stocks"
GUBA_URL = "https://guba.eastmoney.com/list,zssh000001,99.html"
WSB_URL = "https://www.reddit.com/r/wallstreetbets/hot.json?limit=10"
STOCKS_SUB_URL = "https://www.reddit.com/r/stocks/hot.json?limit=10"
STOCKTWITS_URL = "https://api.stocktwits.com/api/2/streams/symbol/AAPL.json"
# New candidates (likely VPS-friendly: no Cloudflare/PerimeterX)
SUBSTACK_URLS = [
    ("doomberg",   "https://doomberg.substack.com/feed"),         # geopolitics+energy+macro
    ("themacrotourist", "https://themacrotourist.substack.com/feed"),  # macro/markets
    ("netinterest", "https://netinterest.substack.com/feed"),     # finance industry deep dives
]
HN_TOP_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
# US-stock candidates — old-school financial news + community sites
US_STOCK_URLS = [
    ("yahoo_finance",  "https://finance.yahoo.com/quote/AAPL/community"),
    ("marketwatch",    "https://www.marketwatch.com/investing/stock/aapl"),
    ("cnbc_quote",     "https://www.cnbc.com/quotes/AAPL"),
    ("investing_com",  "https://www.investing.com/equities/apple-computer-inc"),
    ("finviz",         "https://finviz.com/quote.ashx?t=AAPL"),
    ("benzinga",       "https://www.benzinga.com/quote/AAPL"),
    ("tipranks",       "https://www.tipranks.com/stocks/aapl/forecast"),
]

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def probe_one(name: str, url: str, cookie_file: Path | None) -> dict:
    from playwright.async_api import async_playwright

    report: dict = {"name": name, "url": url}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            ctx = await browser.new_context(user_agent=UA)
            if cookie_file and cookie_file.exists():
                cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
                await ctx.add_cookies(cookies)
                report["cookies_loaded"] = len(cookies)
            page = await ctx.new_page()
            try:
                resp = await page.goto(url, timeout=25000, wait_until="domcontentloaded")
                report["http_status"] = resp.status if resp else None
            except Exception as exc:  # noqa: BLE001
                report["error"] = f"goto: {exc}"
                return report

            try:
                await page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:  # noqa: BLE001 — many sites keep XHRs pinging forever
                pass
            await page.wait_for_timeout(1500)  # let initial render settle
            report["title"] = await page.title()
            body_text = await page.locator("body").inner_text()
            report["body_chars"] = len(body_text)
            report["body_snippet"] = body_text[:200].replace("\n", " | ")
            # Sample 20 candidate class names of text-bearing elements
            probe = await page.evaluate(
                """() => {
                    const cls = new Set();
                    for (const el of document.querySelectorAll('div, article, section, li')) {
                        if (el.children.length >= 1 && el.innerText && el.innerText.length > 30) {
                            if (el.className && typeof el.className === 'string') {
                                cls.add(el.className.split(' ')[0]);
                            }
                            if (cls.size >= 20) break;
                        }
                    }
                    return Array.from(cls).slice(0, 20);
                }"""
            )
            report["candidate_classes"] = probe
            # Anti-bot smell test
            haystack = body_text.lower()
            report["challenge_hit"] = any(
                kw in haystack for kw in [
                    "verification", "verify you are human", "press & hold",
                    "captcha", "are you a robot", "checking your browser",
                ]
            )
        finally:
            await browser.close()
    return report


async def probe_reddit_json(name: str, url: str) -> dict:
    """Reddit .json endpoint — no Playwright needed, just httpx with realistic UA."""
    import httpx
    report: dict = {"name": name, "url": url}
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": UA})
            report["http_status"] = resp.status_code
            if resp.status_code != 200:
                report["body_snippet"] = resp.text[:200]
                return report
            data = resp.json()
            children = data.get("data", {}).get("children", [])
            report["post_count"] = len(children)
            if children:
                first = children[0]["data"]
                report["first_title"] = first.get("title", "")[:80]
                report["first_score"] = first.get("score", 0)
                report["first_url"] = first.get("url", "")
                report["first_has_image"] = bool(
                    first.get("preview") or first.get("post_hint") == "image"
                )
                report["first_selftext_chars"] = len(first.get("selftext", ""))
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)
    return report


async def probe_html(name: str, url: str) -> dict:
    """Generic HTML GET probe with realistic UA. Detect Cloudflare/anti-bot challenge."""
    import httpx
    report: dict = {"name": name, "url": url}
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            report["http_status"] = resp.status_code
            body = resp.text
            report["body_chars"] = len(body)
            report["body_snippet"] = body[:150]
            haystack = body.lower()
            report["challenge_hit"] = any(
                kw in haystack for kw in [
                    "just a moment", "access denied", "captcha",
                    "checking your browser", "px-captcha",
                ]
            )
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)
    return report


async def probe_substack(name: str, feed_url: str) -> dict:
    """Substack RSS feed — public, no anti-bot."""
    import httpx
    report: dict = {"name": f"substack:{name}", "url": feed_url}
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(feed_url, headers={"User-Agent": UA})
            report["http_status"] = resp.status_code
            if resp.status_code != 200:
                report["body_snippet"] = resp.text[:200]
                return report
            body = resp.text
            report["body_chars"] = len(body)
            # RSS-style: count <item> tags (each post)
            import re
            items = re.findall(r"<item>", body)
            report["post_count"] = len(items)
            # First title via simple regex
            title_match = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>", body)
            report["first_title"] = (title_match.group(1) if title_match else "")[:80]
            # Detect image presence
            report["has_images_in_feed"] = "<img" in body or "enclosure" in body
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)
    return report


async def probe_hn_top() -> dict:
    """Hacker News Firebase API — 100% public, no auth, no rate limits."""
    import httpx
    report: dict = {"name": "hackernews", "url": HN_TOP_URL}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(HN_TOP_URL, headers={"User-Agent": UA})
            report["http_status"] = resp.status_code
            if resp.status_code != 200:
                return report
            ids = resp.json()
            report["post_count"] = len(ids)
            # Sample top item for shape check
            if ids:
                item = await client.get(
                    f"https://hacker-news.firebaseio.com/v0/item/{ids[0]}.json",
                    headers={"User-Agent": UA},
                )
                if item.status_code == 200:
                    d = item.json()
                    report["first_title"] = (d.get("title") or "")[:80]
                    report["first_score"] = d.get("score", 0)
                    report["first_descendants"] = d.get("descendants", 0)
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)
    return report


async def probe_stocktwits() -> dict:
    """StockTwits public JSON — no cookie/key needed for symbol streams."""
    import httpx
    report: dict = {"name": "stocktwits", "url": STOCKTWITS_URL}
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(STOCKTWITS_URL, headers={"User-Agent": UA})
            report["http_status"] = resp.status_code
            if resp.status_code != 200:
                report["body_snippet"] = resp.text[:200]
                return report
            data = resp.json()
            messages = data.get("messages", [])
            report["post_count"] = len(messages)
            if messages:
                first = messages[0]
                report["first_user"] = first.get("user", {}).get("username", "")
                report["first_body_chars"] = len(first.get("body", ""))
                report["first_has_image"] = bool(first.get("entities", {}).get("chart"))
                report["first_likes"] = first.get("likes", {}).get("total", 0)
                report["first_body_snip"] = (first.get("body") or "")[:100]
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)
    return report


async def main() -> None:
    results = []
    print("=== Probing eastmoney guba (no cookie needed) ===")
    results.append(await probe_one("eastmoney_guba", GUBA_URL, None))
    print(json.dumps(results[-1], ensure_ascii=False, indent=2))

    print("\n=== Probing seekingalpha (with cookie) ===")
    results.append(await probe_one("seekingalpha", SA_URL, SA_COOKIE))
    print(json.dumps(results[-1], ensure_ascii=False, indent=2))

    print("\n=== Probing reddit /r/wallstreetbets via .json ===")
    wsb = await probe_reddit_json("wallstreetbets", WSB_URL)
    results.append(wsb)
    print(json.dumps(wsb, ensure_ascii=False, indent=2))

    print("\n=== Probing reddit /r/stocks via .json (serious investor sub) ===")
    rs = await probe_reddit_json("stocks_sub", STOCKS_SUB_URL)
    results.append(rs)
    print(json.dumps(rs, ensure_ascii=False, indent=2))

    print("\n=== Probing stocktwits /streams/symbol/AAPL.json (US stock chatter) ===")
    st = await probe_stocktwits()
    results.append(st)
    print(json.dumps(st, ensure_ascii=False, indent=2))

    print("\n=== Probing Substack financial newsletters ===")
    for name, url in SUBSTACK_URLS:
        sub = await probe_substack(name, url)
        results.append(sub)
        print(json.dumps(sub, ensure_ascii=False, indent=2))

    print("\n=== Probing Hacker News top stories ===")
    hn = await probe_hn_top()
    results.append(hn)
    print(json.dumps(hn, ensure_ascii=False, indent=2))

    print("\n=== Probing US-stock candidates (Yahoo/MW/CNBC/Investing/Finviz/Benzinga/TipRanks) ===")
    for name, url in US_STOCK_URLS:
        r = await probe_html(name, url)
        results.append(r)
        # short line per result, not full json
        print(f"  {name}: status={r.get('http_status')} body={r.get('body_chars')} "
              f"challenge={r.get('challenge_hit')} snip='{r.get('body_snippet','')[:80]}'")

    print("\n=== VERDICT ===")
    us_names = {n for n, _ in US_STOCK_URLS}
    for r in results:
        name = r["name"]
        if name in ("wallstreetbets", "stocks_sub", "stocktwits", "hackernews") or name.startswith("substack:"):
            ok = r.get("http_status") == 200 and r.get("post_count", 0) > 0
        elif name in us_names:
            ok = (
                r.get("http_status") in (200, 304)
                and not r.get("challenge_hit", False)
                and r.get("body_chars", 0) > 5000  # real page, not redirect
            )
        else:
            ok = (
                r.get("http_status") in (200, 304)
                and not r.get("challenge_hit", False)
                and r.get("body_chars", 0) > 1000
            )
        print(f"  {r['name']}: {'PASS' if ok else 'FAIL'} "
              f"(status={r.get('http_status')}, "
              f"body/posts={r.get('body_chars') or r.get('post_count')}, "
              f"challenge={r.get('challenge_hit', 'n/a')})")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
