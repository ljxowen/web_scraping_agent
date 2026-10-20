"""Spike: does the category listing render in Playwright, and what network calls feed it?"""
import asyncio, json, sys
from pathlib import Path
from playwright.async_api import async_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.safcodental.com/catalog/gloves"
OUT = Path("spike_out"); OUT.mkdir(exist_ok=True)

async def main():
    captured = []
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        page = await b.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36")
        async def on_resp(r):
            u = r.url
            if "/static/" not in u and ("algolia" in u.lower() or "graphql" in u.lower() or "/rest/" in u or "search" in u.lower()):
                try:
                    body = await r.text()
                except Exception:
                    body = ""
                captured.append({"url": u, "status": r.status, "method": r.request.method, "post": r.request.post_data, "body": body})
        page.on("response", on_resp)
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(8000)
        for _ in range(5):
            await page.mouse.wheel(0, 3000); await page.wait_for_timeout(1500)
        links = await page.eval_on_selector_all("a[href*='/product/']", "els => [...new Set(els.map(e => e.href))]")
        html = await page.content()
        (OUT / "listing_rendered.html").write_text(html, encoding="utf-8")
        await b.close()
    print("product links:", len(links)); print("\n".join(links[:10]))
    print("captured calls:", len(captured))
    for c in captured:
        print(c["method"], c["status"], c["url"][:150], "| post:", (c["post"] or "")[:300], "| body:", c["body"][:300].replace("\n"," "))
    (OUT / "captured.json").write_text(json.dumps(captured, indent=1), encoding="utf-8")

asyncio.run(main())
