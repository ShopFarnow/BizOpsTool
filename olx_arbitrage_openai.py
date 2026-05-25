"""
OLX Arbitrage Scraper — Mumbai & Mira Road
==========================================
Runs daily via GitHub Actions.
Scrapes OLX listings → GPT-4o scores arbitrage potential → Telegram alert.

Secrets required in GitHub repo (Settings → Secrets → Actions):
  OPENAI_API_KEY       — OpenAI API key (platform.openai.com → API keys)
  TELEGRAM_BOT_TOKEN   — from @BotFather
  TELEGRAM_CHAT_ID     — your personal chat ID

How to get Telegram credentials:
  1. Message @BotFather → /newbot → copy token
  2. Message your bot once
  3. Visit https://api.telegram.org/bot<TOKEN>/getUpdates → find "id" in "chat"
"""

import asyncio
import json
import os
import re
import random
import urllib.request
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright
from openai import OpenAI

# ──────────────────────────────────────────────────────────────
# CONFIGURATION — edit this section to customise
# ──────────────────────────────────────────────────────────────

CATEGORIES = [
    # Mumbai
    {"name": "Mumbai Mobiles",      "url": "https://www.olx.in/en-in/mumbai_g4058833/q-mobile"},
    {"name": "Mumbai Laptops",      "url": "https://www.olx.in/en-in/mumbai_g4058833/q-laptop"},
    {"name": "Mumbai Electronics",  "url": "https://www.olx.in/en-in/mumbai_g4058833/q-electronics"},
    {"name": "Mumbai Bikes",        "url": "https://www.olx.in/en-in/mumbai_g4058833/q-bike"},
    {"name": "Mumbai AC",           "url": "https://www.olx.in/en-in/mumbai_g4058833/q-air-conditioner"},
    # Mira Road
    {"name": "MiraRoad Mobiles",    "url": "https://www.olx.in/en-in/mira-road_g4058832/q-mobile"},
    {"name": "MiraRoad Electronics","url": "https://www.olx.in/en-in/mira-road_g4058832/q-electronics"},
    {"name": "MiraRoad Laptops",    "url": "https://www.olx.in/en-in/mira-road_g4058832/q-laptop"},
]

MAX_LISTINGS_PER_CATEGORY = 20   # how many cards to read per category
MIN_ALERT_SCORE           = 6    # only Telegram-alert deals with score >= this
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# ──────────────────────────────────────────────────────────────
# STAGE 1 — SCRAPING
# ──────────────────────────────────────────────────────────────
# HOW IT WORKS:
#   OLX renders listing cards with stable data-aut-id attributes.
#   Playwright launches a headless Chromium, loads each category URL,
#   scrolls the page 3× to trigger lazy-loading, then reads the DOM.
#
#   Key selectors used:
#     [data-aut-id="itemBox"]    → each listing card container
#     [data-aut-id="itemTitle"]  → listing title text
#     [data-aut-id="itemPrice"]  → price text e.g. "₹ 12,500"
#     [data-aut-id="item-location"] → area name
#     [data-aut-id="item-date"]  → "Today", "Yesterday", "2 days ago" etc.
#     <a href>                   → link to the full listing
#
#   Anti-detection measures:
#     - Real Chrome user-agent header
#     - Random 2-4 s delay after page load (human-like)
#     - Random 3-6 s delay between categories
#     - Images / fonts blocked to speed things up (OLX doesn't gate on these)
#     - Headless flag keeps memory low on GitHub Actions runners
# ──────────────────────────────────────────────────────────────

async def scrape_category(page, category: dict) -> list[dict]:
    listings = []
    print(f"  ↳ {category['name']}")
    try:
        await page.goto(category["url"], wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(random.uniform(2, 4))

        # Scroll to trigger lazy-loaded cards
        for _ in range(3):
            await page.evaluate("window.scrollBy(0, 900)")
            await asyncio.sleep(1)

        # Primary selector; fallback to generic li[data-aut-id]
        cards = await page.query_selector_all('[data-aut-id="itemBox"]')
        if not cards:
            cards = await page.query_selector_all('li[data-aut-id]')

        for card in cards[:MAX_LISTINGS_PER_CATEGORY]:
            item = await _parse_card(card, category["name"])
            if item:
                listings.append(item)

    except Exception as e:
        print(f"    ⚠ scrape error: {e}")

    return listings


async def _parse_card(card, category: str) -> dict | None:
    """Extract fields from one OLX listing card."""
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

        # Strip ₹ / commas → integer
        price_int = int(re.sub(r"[^\d]", "", price_raw) or 0)

        # Skip cards with no usable data
        if not title or price_int == 0:
            return None

        return {
            "title":        title,
            "price":        price_int,
            "price_display": price_raw,
            "location":     location,
            "posted":       posted,
            "category":     category,
            "url":          f"https://www.olx.in{href}" if href.startswith("/") else href,
            "scraped_at":   datetime.now().isoformat(),
        }
    except Exception:
        return None


async def run_scraper() -> list[dict]:
    all_listings = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()

        # Block heavy assets — speeds up by ~60%
        await page.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf}",
            lambda r: r.abort()
        )

        for cat in CATEGORIES:
            items = await scrape_category(page, cat)
            all_listings.extend(items)
            print(f"    → {len(items)} listings")
            await asyncio.sleep(random.uniform(3, 6))

        await browser.close()

    print(f"\n  Total scraped: {len(all_listings)}\n")
    return all_listings


# ──────────────────────────────────────────────────────────────
# STAGE 2 — AI ARBITRAGE ANALYSIS
# ──────────────────────────────────────────────────────────────
# HOW IT WORKS:
#   All listings are sent to GPT-4o in a single API call as JSON.
#   The prompt instructs GPT to act as an Indian second-hand market
#   expert and evaluate every listing on 6 dimensions:
#
#   1. arbitrage_score (1–10)
#      Built-in GPT-4o knowledge of typical OLX/Cashify/Amazon
#      refurbished prices for Indian market. Score reflects how far
#      below market the asking price is AND how liquid (fast-sellable)
#      the item type is.
#        10 = extreme underpricing (e.g. iPhone 14 for ₹5k)
#        7–9 = clear margin, flip within a week
#        4–6 = fair market price, thin or no margin
#        1–3 = overpriced or hard-to-move item
#
#   2. estimated_market_price — typical current OLX sale price
#   3. estimated_resale_price — realistic 7-day flip price
#   4. profit_estimate        — resale minus asking price
#   5. reasoning              — 1-2 sentence justification
#   6. action                 — BUY IMMEDIATELY / NEGOTIATE HARD /
#                               WATCH / SKIP
#
#   response_format={"type":"json_object"} forces GPT-4o to return
#   pure JSON — no markdown fences, no preamble, never fails to parse.
# ──────────────────────────────────────────────────────────────

def analyse_listings(listings: list[dict]) -> list[dict]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("⚠ OPENAI_API_KEY missing — skipping AI analysis")
        return listings

    client = OpenAI(api_key=api_key)

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

    print("  🤖 GPT-4o analysing deals...")
    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=4096,
            response_format={"type": "json_object"},   # guaranteed JSON output
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user",   "content": user_msg},
            ]
        )
        raw     = resp.choices[0].message.content.strip()
        parsed  = json.loads(raw)
        scores  = {item["id"]: item for item in parsed.get("results", [])}

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
# HOW IT WORKS:
#   Filters to deals >= MIN_ALERT_SCORE, sorts by score descending,
#   sends a header message then one card per top-5 deal.
#   Uses only stdlib (urllib) — no extra dependency.
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
# STAGE 4 — REPORT + PERSIST
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
    report_path = DATA_DIR / f"report_{datetime.now().strftime('%Y%m%d')}.txt"
    report_path.write_text(report_txt)
    print("\n" + report_txt)
    print(f"  📄 Report → {report_path}")


# ──────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────

async def main():
    print("🚀 OLX Arbitrage Scraper")
    print(f"   {len(CATEGORIES)} categories  |  min alert score {MIN_ALERT_SCORE}/10\n")

    print("── Stage 1: Scraping ──────────────────────────────────")
    listings = await run_scraper()

    print("── Stage 2: AI Analysis ───────────────────────────────")
    listings = analyse_listings(listings)

    print("── Stage 3: Telegram Alerts ───────────────────────────")
    send_telegram_alerts(listings)

    print("── Stage 4: Save & Report ─────────────────────────────")
    save_and_report(listings)

    print("\n✅ Done.")


if __name__ == "__main__":
    asyncio.run(main())
