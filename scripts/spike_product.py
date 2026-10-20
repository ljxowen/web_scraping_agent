"""Spike: render a product page; capture client-side calls (recommendations, inventory)."""
import asyncio, json, re, sys
from pathlib import Path
from playwright.async_api import async_playwright

URL = sys.argv[1]
OUT = Path("spike_out"); OUT.mkdir(exist_ok=True)

async def main():
    calls = []
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        page = await b.new_page()
        async def on_resp(r):
            u = r.url
            if "/static/" in u or r.request.resource_type not in ("xhr", "fetch"): return
            try: body = await r.text()
            except Exception: body = ""
            try: post = r.request.post_data
            except Exception: post = None
            calls.append({"url": u, "post": post, "status": r.status, "body": body})
        page.on("response", on_resp)
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(6000)
        for _ in range(6):
            await page.mouse.wheel(0, 2500); await page.wait_for_timeout(1200)
        txt = await page.inner_text("main")
        (OUT / "product_rendered.html").write_text(await page.content(), encoding="utf-8")
        await b.close()
    (OUT / "product_calls.json").write_text(json.dumps(calls), encoding="utf-8")
    print(re.sub(r"\n\s*\n+", "\n", txt)[:4000])
    for c in calls:
        if any(x in c["url"] for x in ("google", "clarity", "fullstory", "bing", "pinterest", "shop.pe", "contentsquare", "facebook", "doubleclick")): continue
        print("CALL", c["status"], c["url"][:160], "| POST", (c["post"] or "")[:300], "| BODY", c["body"][:500].replace("\n", " "))
asyncio.run(main())
