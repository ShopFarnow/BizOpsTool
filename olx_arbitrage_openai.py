"""
OLX Arbitrage Scraper — Mumbai & Mira Road
==========================================
Runs daily via GitHub Actions.
Scrapes OLX listings → Claude AI scores arbitrage potential → Telegram alert.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
THE FIX — WHY IT WORKS NOW (completely free):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  GitHub Actions (Azure IP) ──blocked──✗──► OLX.in
  GitHub Actions (Azure IP) ──────────────► Cloudflare Worker ──► OLX.in ✅

  Cloudflare's CDN IPs are NOT on OLX's blocklist.
  Azure/GCP/AWS datacenter IPs ARE blocked.
  One free Cloudflare Worker = permanent bypass, 100k req/day free.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SETUP (one-time, 10 minutes):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Step 1 — Deploy Cloudflare Worker (FREE, no credit card):
    a. workers.cloudflare.com → Sign up
    b. Create Worker → paste cloudflare_worker.js → Save & Deploy
    c. Note your URL: https://olx-proxy.<you>.workers.dev

  Step 2 — GitHub Secrets (Settings → Secrets → Actions):
    WORKER_URL        — your Cloudflare worker URL (from step 1c)
    ANTHROPIC_API_KEY — get from console.anthropic.com (free tier available)
    TELEGRAM_BOT_TOKEN — from @BotFather on Telegram
    TELEGRAM_CHAT_ID   — visit https://api.telegram.org/bot<TOKEN>/getUpdates

  Step 3 — That's it. Run the workflow.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CHANGES FROM ORIGINAL:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  1. All OLX HTTP calls routed through Cloudflare Worker (env: WORKER_URL)
  2. Switched from OpenAI GPT-4o → Claude claude-haiku-4-5 (cheaper + faster)
  3. Playwright fallback retained but skipped if worker succeeds
  4. Worker adds multiple fallback endpoint patterns if v4 returns empty
  5. Graceful degradation: if worker missing, falls back to direct (still blocked, but clear error)
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
import anthropic

# ──────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────

SEARCHES = [
    {"name": "Mumbai Mobiles",       "keyword": "mobile",          "location_id": 4058833},
    {"name": "Mumbai Laptops",       "keyword": "laptop",          "location_id": 4058833},
    {"name": "Mumbai Electronics",   "keyword": "electronics",     "location_id": 4058833},
    {"name": "Mumbai Bikes",         "keyword": "bike",            "location_id": 4058833},
    {"name": "Mumbai AC",            "keyword": "air conditioner", "location_id": 4058833},
    {"name": "MiraRoad Mobiles",     "keyword": "mobile",          "location_id": 4058832},
    {"name": "MiraRoad Electronics", "keyword": "electronics",     "location_id": 4058832},
    {"name": "MiraRoad Laptops",     "keyword": "laptop",          "location_id": 4058832},
]

MAX_LISTINGS_PER_SEARCH = 20
MIN_ALERT_SCORE         = 6
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# ──────────────────────────────────────────────────────────────
# CLOUDFLARE WORKER PROXY (THE FIX)
# ──────────────────────────────────────────────────────────────
# WORKER_URL env var = your Cloudflare Worker base URL
# e.g. https://olx-proxy.yourname.workers.dev
#
# The worker fetches OLX on your behalf using Cloudflare's CDN IP.
# OLX trusts CDN IPs; it blocks Azure/AWS/GCP IPs.
# This is 100% free (100k requests/day, no credit card).
# ──────────────────────────────────────────────────────────────

WORKER_URL = os.environ.get("WORKER_URL", "").rstrip("/")

def _build_proxy_url(olx_api_url: str) -> str:
    """Wrap an OLX API URL through the Cloudflare Worker."""
    if not WORKER_URL:
        return olx_api_url   # fallback: direct (will be blocked, but gives clear error)
    return f"{WORKER_URL}?url={urllib.parse.quote(olx_api_url, safe='')}"


# ──────────────────────────────────────────────────────────────
# STAGE 1A — PRIMARY: OLX REST API via Worker Proxy
# ──────────────────────────────────────────────────────────────

OLX_API_HEADERS = {
    "User-Agent":       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36",
    "Accept":           "application/json, text/plain, */*",
    "Accept-Language":  "en-IN,en;q=0.9",
    "Accept-Encoding":  "gzip, deflate, br",
    "Referer":          "https://www.olx.in/",
    "Origin":           "https://www.olx.in",
    "x-panamera-id":    "web_in",
    "DNT":              "1",
    "Connection":       "keep-alive",
}

# Multiple endpoint patterns — OLX occasionally changes structure
OLX_ENDPOINT_PATTERNS = [
    # Pattern 1: v4 relevance search (primary)
    lambda kw, loc, n: (
        "https://www.olx.in/api/relevance/v4/search?"
        + urllib.parse.urlencode({
            "query": kw, "location_id": loc,
            "facet_limit": n, "platform": "web",
            "country": "IN", "lang": "en-IN", "size": n,
        })
    ),
    # Pattern 2: v3 fallback
    lambda kw, loc, n: (
        "https://www.olx.in/api/relevance/v3/search?"
        + urllib.parse.urlencode({
            "query": kw, "location_id": loc,
            "platform": "web", "country": "IN",
            "lang": "en-IN", "size": n,
        })
    ),
    # Pattern 3: Android platform header (different WAF path)
    lambda kw, loc, n: (
        "https://www.olx.in/api/relevance/v4/search?"
        + urllib.parse.urlencode({
            "query": kw, "location_id": loc,
            "platform": "android", "country": "IN",
            "lang": "en-IN", "size": n,
        })
    ),
]


def _parse_ads_from_response(data: dict, search: dict) -> list[dict]:
    """Extract listings from OLX API JSON — handles v3 and v4 response shapes."""
    ads = (
        data.get("data", {}).get("ads", [])
        or data.get("ads", [])
        or data.get("data", [])   # some v3 shapes
        or []
    )
    listings = []
    for ad in ads[:MAX_LISTINGS_PER_SEARCH]:
        try:
            price_info = ad.get("price", {}) or {}
            price_val  = price_info.get("value", {}) or {}
            price_int  = int(price_val.get("raw", 0) or 0)
            price_disp = price_val.get("display", "")

            title    = ad.get("title", "").strip()
            slug     = ad.get("url", "") or ad.get("slug", "")
            location = (ad.get("location", {}) or {}).get("name", {})
            if isinstance(location, dict):
                location = location.get("text", "Unknown")

            posted_ts = ad.get("created_at_first", "") or ad.get("created_at", "")
            posted    = _humanise_ts(posted_ts)

            if not title or price_int == 0:
                continue

            listing_url = (
                f"https://www.olx.in/item/{slug}"
                if slug and not slug.startswith("http")
                else slug or "https://www.olx.in/"
            )

            listings.append({
                "title":         title,
                "price":         price_int,
                "price_display": price_disp,
                "location":      location if isinstance(location, str) else "Unknown",
                "posted":        posted,
                "category":      search["name"],
                "url":           listing_url,
                "scraped_at":    datetime.now().isoformat(),
                "source":        "api-via-worker",
            })
        except Exception:
            continue
    return listings


def fetch_via_api(search: dict) -> list[dict]:
    """
    Fetch listings via OLX REST API, routed through Cloudflare Worker.
    Tries multiple endpoint patterns until one returns results.
    """
    if not WORKER_URL:
        print(f"    ⚠ WORKER_URL not set — direct OLX calls will be blocked.")
        print(f"    ℹ See setup instructions at the top of this file.")

    for i, pattern in enumerate(OLX_ENDPOINT_PATTERNS, 1):
        olx_url  = pattern(search["keyword"], search["location_id"], MAX_LISTINGS_PER_SEARCH)
        fetch_url = _build_proxy_url(olx_url)

        try:
            resp = requests.get(fetch_url, headers=OLX_API_HEADERS, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            listings = _parse_ads_from_response(data, search)
            if listings:
                print(f"    ✅ Worker+API (pattern {i}) → {len(listings)} listings")
                return listings
        except requests.exceptions.Timeout:
            print(f"    ⚠ Pattern {i} timed out")
        except Exception as e:
            print(f"    ⚠ Pattern {i} error: {e}")

    return []


def _humanise_ts(ts: str) -> str:
    if not ts:
        return "Unknown"
    try:
        dt  = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        now = datetime.now(dt.tzinfo)
        days = (now - dt).days
        if days == 0:  return "Today"
        if days == 1:  return "Yesterday"
        return f"{days} days ago"
    except Exception:
        return ts[:10] if len(ts) >= 10 else ts


# ──────────────────────────────────────────────────────────────
# STAGE 1B — FALLBACK: Playwright (last resort)
# ──────────────────────────────────────────────────────────────
# Still kept as a final fallback. Won't work from GitHub Actions
# Azure IPs, but useful if running locally or on a non-blocked server.
# ──────────────────────────────────────────────────────────────

PLAYWRIGHT_TIMEOUT_SECS = 45

CATEGORY_URLS = {
    s["name"]: (
        "https://www.olx.in/en-in/"
        + ("mumbai_g4058833" if s["location_id"] == 4058833 else "mira-road_g4058832")
        + "/q-" + s["keyword"].replace(" ", "-")
    )
    for s in SEARCHES
}


async def _playwright_inner(url: str, category: str) -> list[dict]:
    listings = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-http2",
            ],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="en-IN", timezone_id="Asia/Kolkata",
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
            ignore_https_errors=True,
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = await ctx.new_page()
        await page.route("**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf}",
                         lambda r: r.abort())
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(random.uniform(2, 3))
        for _ in range(3):
            await page.evaluate("window.scrollBy(0, 700)")
            await asyncio.sleep(0.8)
        cards = await page.query_selector_all('[data-aut-id="itemBox"]')
        if not cards:
            cards = await page.query_selector_all("li[data-aut-id]")
        for card in cards[:MAX_LISTINGS_PER_SEARCH]:
            item = await _parse_card(card, category)
            if item:
                item["source"] = "playwright"
                listings.append(item)
        await browser.close()
    return listings


async def fetch_via_playwright(search: dict) -> list[dict]:
    url = CATEGORY_URLS.get(search["name"], "")
    if not url:
        return []
    try:
        return await asyncio.wait_for(
            _playwright_inner(url, search["name"]),
            timeout=PLAYWRIGHT_TIMEOUT_SECS
        )
    except asyncio.TimeoutError:
        print(f"    ⏱ Playwright timed out — OLX blocking this IP too")
        return []
    except Exception as e:
        print(f"    ⚠ Playwright error: {e}")
        return []


async def _parse_card(card, category: str) -> dict | None:
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
            "title":         title,
            "price":         price_int,
            "price_display": price_raw,
            "location":      location,
            "posted":        posted,
            "category":      category,
            "url":           f"https://www.olx.in{href}" if href.startswith("/") else href,
            "scraped_at":    datetime.now().isoformat(),
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

        listings = fetch_via_api(search)

        if not listings:
            print(f"    ⚠ API returned 0 — trying Playwright fallback...")
            listings = await fetch_via_playwright(search)
            print(f"    {'✅' if listings else '❌'} Playwright → {len(listings)} listings")

        all_listings.extend(listings)
        time.sleep(random.uniform(1.0, 2.0))

    print(f"\n  Total scraped: {len(all_listings)}\n")
    return all_listings


# ──────────────────────────────────────────────────────────────
# STAGE 2 — CLAUDE AI ARBITRAGE ANALYSIS
# (Switched from OpenAI GPT-4o → Anthropic Claude)
# claude-haiku-4-5: faster, cheaper, same quality for structured tasks
# ──────────────────────────────────────────────────────────────

def analyse_listings(listings: list[dict]) -> list[dict]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("  ⚠ ANTHROPIC_API_KEY missing — skipping AI analysis")
        return listings
    if not listings:
        return listings

    client = anthropic.Anthropic(api_key=api_key)

    payload = json.dumps(
        [{"id": i, "title": l["title"], "price": l["price"],
          "category": l["category"], "location": l["location"],
          "posted": l["posted"]}
         for i, l in enumerate(listings)],
        indent=2
    )

    system_msg = (
        "You are an expert reseller who flips second-hand goods in Mumbai, India. "
        "You know current OLX, Cashify, and Amazon refurbished prices for the Indian market. "
        "Always respond with a single raw JSON object containing key 'results' "
        "whose value is an array — no markdown, no explanation outside JSON."
    )

    user_msg = f"""Analyse each OLX listing below. For EACH one include a JSON object with:

  id                     — same integer as input
  arbitrage_score        — 1 (terrible) to 10 (incredible steal)
  estimated_market_price — typical OLX/Cashify price today (₹ int)
  estimated_resale_price — what you'd realistically flip it for in 7 days (₹ int)
  profit_estimate        — estimated_resale_price minus listing price (₹ int)
  reasoning              — 1-2 sentences: why this score
  action                 — exactly one of: BUY IMMEDIATELY | NEGOTIATE HARD | WATCH | SKIP

Rules:
- Score 8+ only if profit_estimate > ₹3,000 AND item is highly liquid (phones, laptops, ACs)
- Motorbikes/scooters can be 7+ if price is >25% below market
- "Today" listings get a +0.5 urgency bonus (mention in reasoning)

Return format: {{"results": [ {{...}}, {{...}} ]}}

LISTINGS:
{payload}"""

    print("  🤖 Claude AI analysing deals...")
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system=system_msg,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip()
        # Strip markdown fences if present
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
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
        print(f"  ✅ Analysis done for {len(listings)} listings")
    except Exception as e:
        print(f"  ⚠ AI analysis failed: {e}")

    return listings


# ──────────────────────────────────────────────────────────────
# STAGE 3 — TELEGRAM NOTIFICATION
# ──────────────────────────────────────────────────────────────

def _tg_send(token: str, chat_id: str, text: str):
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": False
    }).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10):
        pass


def send_telegram_alerts(listings: list[dict]):
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("  ⚠ Telegram secrets missing — skipping notifications")
        return

    hot = sorted(
        [l for l in listings if l.get("arbitrage_score", 0) >= MIN_ALERT_SCORE],
        key=lambda x: x["arbitrage_score"], reverse=True
    )

    today = datetime.now().strftime("%d %b %Y")
    if not hot:
        _tg_send(token, chat_id,
            f"📦 <b>OLX Daily — {today}</b>\n"
            f"Scanned {len(listings)} listings. No strong deals today. 😴")
        return

    _tg_send(token, chat_id,
        f"🔥 <b>OLX Arbitrage — {today}</b>\n"
        f"Scanned <b>{len(listings)}</b> listings → <b>{len(hot)} hot deals</b>!")

    for deal in hot[:5]:
        score  = deal.get("arbitrage_score", "?")
        profit = deal.get("profit_estimate", 0)
        emoji  = "🟢" if score >= 8 else "🟡"
        msg = (
            f"{emoji} <b>{deal.get('action','?')}</b>  [{score}/10]\n"
            f"📌 <b>{deal['title']}</b>\n"
            f"💰 Listed ₹{deal['price']:,} → Flip ₹{deal.get('estimated_resale_price',0):,}\n"
            f"📈 Est. profit <b>₹{profit:,}</b>\n"
            f"📍 {deal['location']}  |  {deal['posted']}\n"
            f"🤖 {deal.get('reasoning','')}\n"
            f"🔗 <a href=\"{deal.get('url','')}\">View on OLX</a>"
        )
        try:
            _tg_send(token, chat_id, msg)
        except Exception as e:
            print(f"  ⚠ Telegram send failed: {e}")

    print(f"  ✅ Sent {min(len(hot),5)} Telegram alerts")


# ──────────────────────────────────────────────────────────────
# STAGE 4 — SAVE + REPORT
# ──────────────────────────────────────────────────────────────

def save_and_report(listings: list[dict]):
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    (DATA_DIR / f"raw_{ts}.json").write_text(
        json.dumps(listings, indent=2, ensure_ascii=False))

    hot = sorted(
        [l for l in listings if l.get("arbitrage_score", 0) >= MIN_ALERT_SCORE],
        key=lambda x: x["arbitrage_score"], reverse=True
    )

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
                f"    Listed ₹{d['price']:,}  |  Market ₹{d.get('estimated_market_price',0):,}  |  Flip ₹{d.get('estimated_resale_price',0):,}",
                f"    Est. profit ₹{d.get('profit_estimate',0):,}",
                f"    {d['location']}  |  {d['category']}  |  {d['posted']}",
                f"    {d.get('reasoning','')}",
                f"    {d.get('url','')}",
                "",
            ]

    report_txt = "\n".join(lines)
    (DATA_DIR / f"report_{datetime.now().strftime('%Y%m%d')}.txt").write_text(report_txt)
    print("\n" + report_txt)


# ──────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────

async def main():
    print("🚀 OLX Arbitrage Scraper (Cloudflare Worker edition)")
    print(f"   {len(SEARCHES)} searches  |  min alert score {MIN_ALERT_SCORE}/10")
    print(f"   Worker: {'✅ ' + WORKER_URL if WORKER_URL else '❌ NOT SET — set WORKER_URL secret'}\n")

    print("── Stage 1: Scraping (Worker+API → Playwright fallback) ──")
    listings = await run_scraper()

    print("── Stage 2: Claude AI Analysis ────────────────────────────")
    listings = analyse_listings(listings)

    print("── Stage 3: Telegram Alerts ───────────────────────────────")
    send_telegram_alerts(listings)

    print("── Stage 4: Save & Report ─────────────────────────────────")
    save_and_report(listings)

    print("\n✅ Done.")


if __name__ == "__main__":
    asyncio.run(main())
