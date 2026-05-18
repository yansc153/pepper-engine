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


async def main() -> None:
    results = []
    print("=== Probing eastmoney guba (no cookie needed) ===")
    results.append(await probe_one("eastmoney_guba", GUBA_URL, None))
    print(json.dumps(results[-1], ensure_ascii=False, indent=2))

    print("\n=== Probing seekingalpha (with cookie) ===")
    results.append(await probe_one("seekingalpha", SA_URL, SA_COOKIE))
    print(json.dumps(results[-1], ensure_ascii=False, indent=2))

    print("\n=== VERDICT ===")
    for r in results:
        ok = (
            r.get("http_status") in (200, 304)
            and not r.get("challenge_hit", False)
            and r.get("body_chars", 0) > 1000
        )
        print(f"  {r['name']}: {'PASS' if ok else 'FAIL'} "
              f"(status={r.get('http_status')}, "
              f"body={r.get('body_chars')} chars, "
              f"challenge={r.get('challenge_hit')})")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
