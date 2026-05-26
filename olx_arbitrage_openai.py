"""
OLX Arbitrage Scraper — Mumbai & Mira Road
==========================================
GitHub Actions → Cloudflare Worker → OLX.in

Required GitHub Secrets:
  WORKER_URL         — https://dawn-forest-2777.shopfarnow.workers.dev (your CF worker)
  OPENAI_API_KEY     — from platform.openai.com
  TELEGRAM_BOT_TOKEN — from @BotFather
  TELEGRAM_CHAT_ID   — from https://api.telegram.org/bot<TOKEN>/getUpdates
"""

import asyncio
import json
import os
import re
import random
import time
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests
from playwright.async_api import async_playwright
from openai import OpenAI

# ──────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────

SEARCHES = [
    {"name": "Mumbai Mobiles",       "keyword": "mobile",          "location_id": 4058997},
    {"name": "Mumbai Laptops",       "keyword": "laptop",          "location_id": 4058997},
    {"name": "Mumbai Electronics",   "keyword": "electronics",     "location_id": 4058997},
    {"name": "Mumbai Bikes",         "keyword": "bike",            "location_id": 4058997},
    {"name": "Mumbai AC",            "keyword": "air conditioner", "location_id": 4058997},
    {"name": "MiraRoad Mobiles",     "keyword": "mobile",          "location_id": 5460046},
    {"name": "MiraRoad Electronics", "keyword": "electronics",     "location_id": 5460046},
    {"name": "MiraRoad Laptops",     "keyword": "laptop",          "location_id": 5460046},
]

MAX_LISTINGS_PER_SEARCH = 20
MIN_ALERT_SCORE         = 6
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

WORKER_URL = os.environ.get("WORKER_URL", "").rstrip("/")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# ──────────────────────────────────────────────────────────────
# STAGE 1A — OLX API via Cloudflare Worker
# ──────────────────────────────────────────────────────────────

# Location config
LOCATION_SLUGS = {
    4058997: "mumbai_g4058997",
    5460046: "mira-road_g5460046",
}


def fetch_via_nextjs(search: dict) -> list[dict]:
    """
    Fetch OLX listings via Next.js _next/data endpoint.
    This completely bypasses Akamai — no cookies needed.
    The worker auto-discovers OLX's current build ID and fetches
    the same JSON that Next.js uses for server-side rendering.
    """
    if not WORKER_URL:
        print("    ⚠ WORKER_URL not set")
        return []

    location_slug = LOCATION_SLUGS.get(search["location_id"], "mumbai_g4058997")
    keyword       = search["keyword"].replace(" ", "-")

    url = (f"{WORKER_URL}?"
           f"keyword={urllib.parse.quote(search['keyword'])}"
           f"&location_id={search['location_id']}"
           f"&location_slug={location_slug}")

    try:
        resp = requests.get(url, timeout=30)
        build_id = resp.headers.get("X-Build-ID", "unknown")
        next_url = resp.headers.get("X-Next-URL", "")

        if resp.status_code != 200:
            print(f"    ⚠ Worker returned HTTP {resp.status_code}")
            return []

        data = resp.json()

        # _next/data wraps everything in pageProps
        page_props = (
            data.get("pageProps") or
            data.get("props", {}).get("pageProps") or
            {}
        )

        # OLX stores listings in multiple possible keys within pageProps
        ads_raw = (
            page_props.get("listingProps", {}).get("ads") or
            page_props.get("ads") or
            page_props.get("data", {}).get("ads") or
            page_props.get("initialData", {}).get("ads") or
            []
        )

        if not ads_raw:
            # Debug: show pageProps keys
            print(f"    🔍 buildId={build_id}")
            print(f"    🔍 pageProps keys: {list(page_props.keys())[:8]}")
            if "listingProps" in page_props:
                print(f"    🔍 listingProps keys: {list(page_props['listingProps'].keys())[:8]}")
            return []

        listings = []
        for ad in ads_raw[:MAX_LISTINGS_PER_SEARCH]:
            try:
                price_val = (ad.get("price") or {}).get("value") or {}
                price_int = int(price_val.get("raw", 0) or 0)
                title     = (ad.get("title") or ad.get("subject") or "").strip()
                if not title or price_int == 0:
                    continue
                slug     = ad.get("url") or ad.get("slug") or str(ad.get("id",""))
                loc      = (ad.get("location") or {})
                loc_name = loc.get("name") or {}
                location = loc_name.get("text", "Unknown") if isinstance(loc_name, dict) else str(loc_name)
                ts       = ad.get("created_at_first") or ad.get("created_at") or ""
                url_full = (f"https://www.olx.in/item/{slug}"
                            if slug and not str(slug).startswith("http")
                            else str(slug) or "https://www.olx.in/")
                listings.append({
                    "title": title, "price": price_int,
                    "price_display": price_val.get("display", f"₹{price_int:,}"),
                    "location": location, "posted": _humanise_ts(str(ts)),
                    "category": search["name"], "url": url_full,
                    "scraped_at": datetime.now().isoformat(),
                    "source": "nextjs",
                })
            except Exception:
                continue

        print(f"    ✅ Next.js → {len(listings)} listings (buildId: {build_id})")
        return listings

    except Exception as e:
        print(f"    ⚠ Next.js fetch error: {e}")
        return []


def _parse_ads(data, search: dict) -> list[dict]:
    """Parse OLX API response — handles v3/v4 shapes and list/dict root.
    
    Confirmed OLX v4 shape (from debug output):
      {"version":..., "data": [...ads list...], "metadata":..., "empty":..., ...}
    So data["data"] is the ads list directly.
    """
    if isinstance(data, list):
        ads = data                                         # bare list at root
    elif isinstance(data.get("data"), list) and data["data"]:
        ads = data["data"]                                 # OLX v4: data key IS the list
    elif isinstance(data.get("data"), dict):
        ads = data["data"].get("ads") or []                # old nested shape
    else:
        ads = data.get("ads") or []

    # OLX sometimes returns 0 results in "data" but puts real results in "suggested_data"
    # This happens when the query is treated as a bot — use suggested as fallback
    if not ads:
        sg = data.get("suggested_data") or {}
        sg_inner = sg.get("data") or sg.get("ads") or []
        if isinstance(sg_inner, list) and sg_inner:
            ads = sg_inner
    listings = []
    for ad in ads[:MAX_LISTINGS_PER_SEARCH]:
        try:
            # Shape A: standard dict with nested price
            if isinstance(ad, dict):
                price_raw = (ad.get("price") or {}).get("value") or {}
                price_int = int(price_raw.get("raw", 0) or 0)

                # Shape B: flat dict with direct price field
                if price_int == 0:
                    price_int = int(ad.get("price_value", 0) or ad.get("priceValue", 0) or 0)

                title = (ad.get("title") or ad.get("subject") or "").strip()
                if not title or price_int == 0:
                    continue

                slug     = ad.get("url") or ad.get("slug") or ad.get("id") or ""
                loc      = ad.get("location") or {}
                loc_name = loc.get("name") or {}
                location = (loc_name.get("text", "Unknown")
                            if isinstance(loc_name, dict) else str(loc_name or "Unknown"))

                ts     = ad.get("created_at_first") or ad.get("created_at") or ad.get("date") or ""
                posted = _humanise_ts(str(ts))

                url = (f"https://www.olx.in/item/{slug}"
                       if slug and not str(slug).startswith("http") else
                       str(slug) or "https://www.olx.in/")

                listings.append({
                    "title": title, "price": price_int,
                    "price_display": price_raw.get("display", f"₹{price_int:,}"),
                    "location": location, "posted": posted,
                    "category": search["name"], "url": url,
                    "scraped_at": datetime.now().isoformat(),
                    "source": "api-via-worker",
                })
        except Exception:
            continue
    return listings


def _humanise_ts(ts: str) -> str:
    if not ts:
        return "Unknown"
    try:
        dt  = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        now = datetime.now(dt.tzinfo)
        d   = (now - dt).days
        return "Today" if d == 0 else "Yesterday" if d == 1 else f"{d} days ago"
    except Exception:
        return ts[:10] if len(ts) >= 10 else ts


def fetch_via_api(search: dict) -> list[dict]:
    if not WORKER_URL:
        print("    ⚠ WORKER_URL not set — skipping API fetch")
        return []

    for i, pattern in enumerate(OLX_ENDPOINT_PATTERNS, 1):
        olx_url   = pattern(search["keyword"], search["location_id"], MAX_LISTINGS_PER_SEARCH)
        fetch_url = _build_proxy_url(olx_url)
        try:
            if i == 1:
                print(f"    🔍 Calling: {fetch_url[:120]}")
            resp = requests.get(fetch_url, timeout=25)

            # Debug: show what OLX actually returned
            content_type = resp.headers.get("content-type", "")
            if resp.status_code != 200:
                print(f"    ⚠ Pattern {i}: HTTP {resp.status_code}")
                continue

            if "json" not in content_type and not resp.text.strip().startswith("{"):
                # OLX returned HTML (bot detection page) — log first 120 chars
                preview = resp.text.strip()[:120].replace("\n", " ")
                print(f"    ⚠ Pattern {i}: got HTML not JSON → '{preview}'")
                continue

            data     = resp.json()

            # Debug: show JSON shape on first pattern only
            if i == 1:
                if isinstance(data, list):
                    first_keys = list(data[0].keys())[:8] if data else []
                    print(f"    🔍 Root=list, {len(data)} items, first ad keys: {first_keys}")
                else:
                    top_keys = list(data.keys())[:6]
                    print(f"    🔍 Root=dict, top keys: {top_keys}")
                    inner = data.get("data")
                    print(f"    🔍 data[data] type={type(inner).__name__}, len={len(inner) if isinstance(inner, (list,dict)) else 'N/A'}")
                    print(f"    🔍 empty={data.get('empty')}, not_empty={data.get('not_empty')}")
                    if isinstance(inner, list) and inner:
                        print(f"    🔍 First ad keys: {list(inner[0].keys())[:10]}")
                        print(f"    🔍 First ad price: {inner[0].get('price')}")
                        print(f"    🔍 First ad title: {inner[0].get('title') or inner[0].get('subject')}")
                    elif isinstance(inner, list) and not inner:
                        print(f"    🔍 data[data] is EMPTY LIST — OLX returned 0 results")
                        print(f"    🔍 suggested_data keys: {list((data.get('suggested_data') or {}).keys())[:5]}")
                        # Check if results are in suggested_data instead
                        sg = data.get("suggested_data") or {}
                        sg_ads = sg.get("data") or sg.get("ads") or []
                        if sg_ads:
                            print(f"    🔍 suggested_data has {len(sg_ads)} items! First keys: {list(sg_ads[0].keys())[:8]}")

            listings = _parse_ads(data, search)
            if listings:
                print(f"    ✅ Pattern {i} → {len(listings)} listings")
                return listings
            else:
                print(f"    ⚠ Pattern {i}: 0 ads parsed from response")

        except requests.exceptions.Timeout:
            print(f"    ⚠ Pattern {i}: timeout")
        except Exception as e:
            print(f"    ⚠ Pattern {i}: {e}")

    return []


# ──────────────────────────────────────────────────────────────
# STAGE 1B — Playwright fallback (works locally, blocked on GH Actions)
# ──────────────────────────────────────────────────────────────

CATEGORY_URLS = {
    s["name"]: (
        "https://www.olx.in/en-in/"
        + ("mumbai_g4058997" if s["location_id"] == 4058997 else "mira-road_g5460046")
        + "/q-" + s["keyword"].replace(" ", "-")
    )
    for s in SEARCHES
}


async def fetch_via_playwright(search: dict) -> list[dict]:
    url = CATEGORY_URLS.get(search["name"], "")
    if not url:
        return []
    try:
        return await asyncio.wait_for(_playwright_inner(url, search["name"]), timeout=40)
    except asyncio.TimeoutError:
        print("    ⏱ Playwright timed out")
        return []
    except Exception as e:
        print(f"    ⚠ Playwright error: {e}")
        return []


async def _playwright_inner(url: str, category: str) -> list[dict]:
    listings = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled",
                  "--disable-http2"],   # force HTTP/1.1 — OLX blocks HTTP/2 from headless
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
            locale="en-IN", timezone_id="Asia/Kolkata",
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = await ctx.new_page()
        await page.route("**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2}",
                         lambda r: r.abort())
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(random.uniform(2, 3))
        for _ in range(3):
            await page.evaluate("window.scrollBy(0,600)")
            await asyncio.sleep(0.8)
        cards = await page.query_selector_all('[data-aut-id="itemBox"]')
        for card in cards[:MAX_LISTINGS_PER_SEARCH]:
            item = await _parse_pw_card(card, category)
            if item:
                listings.append(item)
        await browser.close()
    return listings


async def _parse_pw_card(card, category: str) -> dict | None:
    try:
        t = await card.query_selector('[data-aut-id="itemTitle"]')
        p = await card.query_selector('[data-aut-id="itemPrice"]')
        l = await card.query_selector('[data-aut-id="item-location"]')
        d = await card.query_selector('[data-aut-id="item-date"]')
        a = await card.query_selector("a")
        title     = (await t.inner_text()).strip() if t else ""
        price_raw = (await p.inner_text()).strip() if p else ""
        location  = (await l.inner_text()).strip() if l else "Unknown"
        posted    = (await d.inner_text()).strip() if d else ""
        href      = await a.get_attribute("href") if a else ""
        price_int = int(re.sub(r"[^\d]", "", price_raw) or 0)
        if not title or price_int == 0:
            return None
        return {
            "title": title, "price": price_int, "price_display": price_raw,
            "location": location, "posted": posted, "category": category,
            "url": f"https://www.olx.in{href}" if href.startswith("/") else href,
            "scraped_at": datetime.now().isoformat(), "source": "playwright",
        }
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────
# ORCHESTRATOR
# ──────────────────────────────────────────────────────────────

async def run_scraper() -> list[dict]:
    all_listings = []
    for search in SEARCHES:
        print(f"  ↳ {search['name']}")
        listings = fetch_via_nextjs(search)
        if not listings:
            print("    ⚠ Next.js returned 0 — trying Playwright fallback...")
            listings = await fetch_via_playwright(search)
            print(f"    {'✅' if listings else '❌'} Playwright → {len(listings)} listings")
        all_listings.extend(listings)
        time.sleep(random.uniform(1.0, 2.0))
    print(f"\n  Total scraped: {len(all_listings)}\n")
    return all_listings


# ──────────────────────────────────────────────────────────────
# STAGE 2 — OPENAI ANALYSIS
# ──────────────────────────────────────────────────────────────

def analyse_listings(listings: list[dict]) -> list[dict]:
    if not OPENAI_API_KEY:
        print("  ⚠ OPENAI_API_KEY missing — skipping AI analysis")
        return listings
    if not listings:
        return listings

    client = OpenAI(api_key=OPENAI_API_KEY)

    payload = json.dumps([
        {"id": i, "title": l["title"], "price": l["price"],
         "category": l["category"], "location": l["location"], "posted": l["posted"]}
        for i, l in enumerate(listings)
    ], indent=2)

    system_msg = (
        "You are an expert reseller who flips second-hand goods in Mumbai, India. "
        "You know current OLX, Cashify, and Amazon refurbished prices. "
        "Always respond with ONLY a raw JSON object with key 'results' — "
        "no markdown, no backticks, no explanation."
    )

    user_msg = f"""Score each OLX listing for arbitrage potential. For each return:
  id, arbitrage_score (1-10), estimated_market_price (₹ int),
  estimated_resale_price (₹ int), profit_estimate (₹ int),
  reasoning (1-2 sentences), action (BUY IMMEDIATELY | NEGOTIATE HARD | WATCH | SKIP)

Rules:
- Score 8+ only if profit > ₹3000 AND item is liquid (phones/laptops/ACs)
- Bikes 7+ if >25% below market
- "Today" listings get +0.5 urgency bonus

Return: {{"results": [...]}}

LISTINGS:
{payload}"""

    print("  🤖 GPT-4o-mini analysing deals...")
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=4096,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user",   "content": user_msg},
            ],
        )
        raw    = resp.choices[0].message.content.strip()
        raw    = re.sub(r"^```json\s*", "", raw)
        raw    = re.sub(r"\s*```$",    "", raw)
        parsed = json.loads(raw)
        scores = {item["id"]: item for item in parsed.get("results", [])}

        for i, listing in enumerate(listings):
            s = scores.get(i, {})
            listing.update({
                "arbitrage_score":        s.get("arbitrage_score", 0),
                "estimated_market_price": s.get("estimated_market_price", 0),
                "estimated_resale_price": s.get("estimated_resale_price", 0),
                "profit_estimate":        s.get("profit_estimate", 0),
                "reasoning":              s.get("reasoning", ""),
                "action":                 s.get("action", "SKIP"),
            })
        print(f"  ✅ Analysis done — {len(listings)} listings scored")
    except Exception as e:
        print(f"  ⚠ AI analysis failed: {e}")
    return listings


# ──────────────────────────────────────────────────────────────
# STAGE 3 — TELEGRAM
# ──────────────────────────────────────────────────────────────

def _tg(token: str, chat_id: str, text: str):
    data = json.dumps({"chat_id": chat_id, "text": text,
                       "parse_mode": "HTML", "disable_web_page_preview": False}).encode()
    req  = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10):
        pass


def send_telegram_alerts(listings: list[dict]):
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("  ⚠ Telegram secrets missing — skipping")
        return

    today = datetime.now().strftime("%d %b %Y")
    hot   = sorted([l for l in listings if l.get("arbitrage_score", 0) >= MIN_ALERT_SCORE],
                   key=lambda x: x["arbitrage_score"], reverse=True)

    if not hot:
        _tg(token, chat_id,
            f"📦 <b>OLX Daily — {today}</b>\n"
            f"Scanned {len(listings)} listings. No strong deals today. 😴")
        return

    _tg(token, chat_id,
        f"🔥 <b>OLX Arbitrage — {today}</b>\n"
        f"Scanned <b>{len(listings)}</b> listings → <b>{len(hot)} hot deals!</b>")

    for deal in hot[:5]:
        score  = deal.get("arbitrage_score", "?")
        profit = deal.get("profit_estimate", 0)
        emoji  = "🟢" if score >= 8 else "🟡"
        try:
            _tg(token, chat_id,
                f"{emoji} <b>{deal.get('action','?')}</b>  [{score}/10]\n"
                f"📌 <b>{deal['title']}</b>\n"
                f"💰 Listed ₹{deal['price']:,} → Flip ₹{deal.get('estimated_resale_price',0):,}\n"
                f"📈 Est. profit <b>₹{profit:,}</b>\n"
                f"📍 {deal['location']}  |  {deal['posted']}\n"
                f"🤖 {deal.get('reasoning','')}\n"
                f"🔗 <a href=\"{deal.get('url','')}\">View on OLX</a>")
        except Exception as e:
            print(f"  ⚠ Telegram send failed: {e}")
    print(f"  ✅ Sent {min(len(hot),5)} Telegram alerts")


# ──────────────────────────────────────────────────────────────
# STAGE 4 — SAVE & REPORT
# ──────────────────────────────────────────────────────────────

def save_and_report(listings: list[dict]):
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    (DATA_DIR / f"raw_{ts}.json").write_text(json.dumps(listings, indent=2, ensure_ascii=False))

    hot   = sorted([l for l in listings if l.get("arbitrage_score", 0) >= MIN_ALERT_SCORE],
                   key=lambda x: x["arbitrage_score"], reverse=True)
    lines = [
        "=" * 62,
        f"  OLX ARBITRAGE DIGEST — {datetime.now().strftime('%d %B %Y')}",
        "=" * 62,
        f"  Listings scanned : {len(listings)}",
        f"  Hot deals (≥{MIN_ALERT_SCORE}) : {len(hot)}",
        "=" * 62, "",
    ]
    if not hot:
        lines.append("No strong deals today.")
    else:
        lines.append(f"🔥 TOP {min(len(hot),10)} DEALS\n")
        for i, d in enumerate(hot[:10], 1):
            lines += [
                f"#{i}  [{d.get('action','?')}]  Score {d.get('arbitrage_score','?')}/10",
                f"    {d['title']}",
                f"    Listed ₹{d['price']:,}  |  Flip ₹{d.get('estimated_resale_price',0):,}",
                f"    Est. profit ₹{d.get('profit_estimate',0):,}",
                f"    {d['location']}  |  {d['category']}  |  {d['posted']}",
                f"    {d.get('reasoning','')}",
                f"    {d.get('url','')}",
                "",
            ]
    report = "\n".join(lines)
    (DATA_DIR / f"report_{datetime.now().strftime('%Y%m%d')}.txt").write_text(report)
    print("\n" + report)


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

async def main():
    print("🚀 OLX Arbitrage Scraper")
    print(f"   {len(SEARCHES)} searches  |  min alert score {MIN_ALERT_SCORE}/10")
    print(f"   Worker : {'✅ set' if WORKER_URL else '❌ WORKER_URL not set'}")
    print(f"   OpenAI : {'✅ set' if OPENAI_API_KEY else '❌ OPENAI_API_KEY not set'}\n")

    print("── Stage 1: Scraping ──────────────────────────────────────")
    listings = await run_scraper()

    print("── Stage 2: AI Analysis ───────────────────────────────────")
    listings = analyse_listings(listings)

    print("── Stage 3: Telegram Alerts ───────────────────────────────")
    send_telegram_alerts(listings)

    print("── Stage 4: Save & Report ─────────────────────────────────")
    save_and_report(listings)

    print("\n✅ Done.")


if __name__ == "__main__":
    asyncio.run(main())
