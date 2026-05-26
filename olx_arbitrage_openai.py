"""
OLX Arbitrage Scraper
GitHub Actions → Cloudflare Worker → OLX HTML → parse window.__APP

Secrets needed:
  WORKER_URL         — https://your-worker.workers.dev
  OPENAI_API_KEY     — from platform.openai.com
  TELEGRAM_BOT_TOKEN — from @BotFather
  TELEGRAM_CHAT_ID   — from api.telegram.org/bot<TOKEN>/getUpdates
"""

import asyncio, json, os, re, random, time, urllib.request, urllib.parse
from datetime import datetime
from pathlib import Path

import requests
from openai import OpenAI

# ── Config ────────────────────────────────────────────────────────
SEARCHES = [
    {"name": "Mumbai Mobiles",       "keyword": "mobile",          "location_slug": "mumbai_g4058997"},
    {"name": "Mumbai Laptops",       "keyword": "laptop",          "location_slug": "mumbai_g4058997"},
    {"name": "Mumbai Electronics",   "keyword": "electronics",     "location_slug": "mumbai_g4058997"},
    {"name": "Mumbai Bikes",         "keyword": "bike",            "location_slug": "mumbai_g4058997"},
    {"name": "Mumbai AC",            "keyword": "air conditioner", "location_slug": "mumbai_g4058997"},
    {"name": "MiraRoad Mobiles",     "keyword": "mobile",          "location_slug": "mira-road_g5460046"},
    {"name": "MiraRoad Electronics", "keyword": "electronics",     "location_slug": "mira-road_g5460046"},
    {"name": "MiraRoad Laptops",     "keyword": "laptop",          "location_slug": "mira-road_g5460046"},
]
MAX_LISTINGS  = 20
MIN_SCORE     = 6
DATA_DIR      = Path("data"); DATA_DIR.mkdir(exist_ok=True)
WORKER_URL    = os.environ.get("WORKER_URL", "").rstrip("/")
OPENAI_KEY    = os.environ.get("OPENAI_API_KEY", "")

# ── Stage 1: Scrape via Cloudflare Worker ─────────────────────────

def fetch_search(search: dict) -> list[dict]:
    if not WORKER_URL:
        print("    ⚠ WORKER_URL not set"); return []

    url = (f"{WORKER_URL}?"
           f"keyword={urllib.parse.quote(search['keyword'])}"
           f"&location_slug={search['location_slug']}")
    try:
        resp = requests.get(url, timeout=30)
        data = resp.json()

        if "error" in data:
            print(f"    ⚠ Worker error: {data['error']}")
            if "props_keys" in data:
                print(f"    🔍 props_keys: {data['props_keys']}")
            return []

        ads = data.get("ads", [])
        if not ads:
            print(f"    🔍 props_keys: {data.get('props_keys', [])}")
            print(f"    🔍 total={data.get('total', 0)}")
            return []

        listings = []
        for ad in ads[:MAX_LISTINGS]:
            try:
                price_raw = (ad.get("price") or {}).get("value") or {}
                price_int = int(price_raw.get("raw", 0) or 0)
                title     = (ad.get("title") or ad.get("subject") or "").strip()
                if not title or price_int == 0:
                    continue
                slug     = ad.get("url") or ad.get("slug") or str(ad.get("id",""))
                loc      = ad.get("location") or {}
                loc_name = loc.get("name") or {}
                location = loc_name.get("text","Unknown") if isinstance(loc_name,dict) else str(loc_name)
                ts       = ad.get("created_at_first") or ad.get("created_at") or ""
                ad_url   = (f"https://www.olx.in/item/{slug}"
                            if slug and not str(slug).startswith("http")
                            else str(slug) or "https://www.olx.in/")
                listings.append({
                    "title": title, "price": price_int,
                    "price_display": price_raw.get("display", f"₹{price_int:,}"),
                    "location": location, "posted": _humanise(str(ts)),
                    "category": search["name"], "url": ad_url,
                    "scraped_at": datetime.now().isoformat(), "source": "worker",
                })
            except Exception:
                continue

        print(f"    ✅ {len(listings)} listings")
        return listings

    except Exception as e:
        print(f"    ⚠ Error: {e}"); return []


def _humanise(ts):
    try:
        dt  = datetime.fromisoformat(ts.replace("Z","+00:00"))
        now = datetime.now(dt.tzinfo)
        d   = (now-dt).days
        return "Today" if d==0 else "Yesterday" if d==1 else f"{d} days ago"
    except:
        return ts[:10] if len(ts)>=10 else ts


def run_scraper() -> list[dict]:
    all_listings = []
    for s in SEARCHES:
        print(f"  ↳ {s['name']}")
        listings = fetch_search(s)
        all_listings.extend(listings)
        time.sleep(random.uniform(1, 2))
    print(f"\n  Total: {len(all_listings)}\n")
    return all_listings

# ── Stage 2: OpenAI Analysis ──────────────────────────────────────

def analyse(listings: list[dict]) -> list[dict]:
    if not OPENAI_KEY:
        print("  ⚠ OPENAI_API_KEY missing"); return listings
    if not listings: return listings

    client  = OpenAI(api_key=OPENAI_KEY)
    payload = json.dumps([{"id":i,"title":l["title"],"price":l["price"],
                           "category":l["category"],"posted":l["posted"]}
                          for i,l in enumerate(listings)], indent=2)

    print("  🤖 Analysing...")
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini", max_tokens=4096,
            messages=[
                {"role":"system","content":
                 "Expert Mumbai reseller. Respond ONLY with raw JSON {results:[...]}. "
                 "No markdown, no backticks."},
                {"role":"user","content":
                 f"Score each listing 1-10 for arbitrage. Return: id, arbitrage_score, "
                 f"estimated_market_price, estimated_resale_price, profit_estimate, "
                 f"reasoning (1 sentence), action (BUY IMMEDIATELY|NEGOTIATE HARD|WATCH|SKIP)\n"
                 f"Score 8+ only if profit>₹3000 and liquid item.\n\n{payload}"},
            ],
        )
        raw    = resp.choices[0].message.content.strip()
        raw    = re.sub(r"^```json\s*","",raw); raw = re.sub(r"\s*```$","",raw)
        scores = {s["id"]:s for s in json.loads(raw).get("results",[])}
        for i,l in enumerate(listings):
            s = scores.get(i,{})
            l.update({"arbitrage_score":s.get("arbitrage_score",0),
                      "estimated_resale_price":s.get("estimated_resale_price",0),
                      "profit_estimate":s.get("profit_estimate",0),
                      "reasoning":s.get("reasoning",""),
                      "action":s.get("action","SKIP")})
        print(f"  ✅ Scored {len(listings)} listings")
    except Exception as e:
        print(f"  ⚠ Analysis failed: {e}")
    return listings

# ── Stage 3: Telegram ─────────────────────────────────────────────

def _tg(token, chat_id, text):
    data = json.dumps({"chat_id":chat_id,"text":text,
                       "parse_mode":"HTML","disable_web_page_preview":False}).encode()
    req  = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data, headers={"Content-Type":"application/json"})
    urllib.request.urlopen(req, timeout=10)

def send_alerts(listings):
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("  ⚠ Telegram not configured"); return

    today = datetime.now().strftime("%d %b %Y")
    hot   = sorted([l for l in listings if l.get("arbitrage_score",0)>=MIN_SCORE],
                   key=lambda x: x["arbitrage_score"], reverse=True)
    if not hot:
        _tg(token, chat_id, f"📦 <b>OLX Daily — {today}</b>\nScanned {len(listings)} listings. No strong deals. 😴")
        return
    _tg(token, chat_id, f"🔥 <b>OLX Arbitrage — {today}</b>\n<b>{len(hot)} hot deals</b> from {len(listings)} listings!")
    for deal in hot[:5]:
        score = deal.get("arbitrage_score","?")
        try:
            _tg(token, chat_id,
                f"{'🟢' if score>=8 else '🟡'} <b>{deal.get('action','?')}</b> [{score}/10]\n"
                f"📌 <b>{deal['title']}</b>\n"
                f"💰 ₹{deal['price']:,} → flip ₹{deal.get('estimated_resale_price',0):,}\n"
                f"📈 Profit ₹{deal.get('profit_estimate',0):,}\n"
                f"📍 {deal['location']} | {deal['posted']}\n"
                f"🤖 {deal.get('reasoning','')}\n"
                f"🔗 <a href=\"{deal['url']}\">View on OLX</a>")
        except Exception as e:
            print(f"  ⚠ {e}")
    print(f"  ✅ {min(len(hot),5)} alerts sent")

# ── Stage 4: Save & Report ────────────────────────────────────────

def save_report(listings):
    ts  = datetime.now().strftime("%Y%m%d_%H%M")
    hot = sorted([l for l in listings if l.get("arbitrage_score",0)>=MIN_SCORE],
                 key=lambda x: x["arbitrage_score"], reverse=True)
    (DATA_DIR/f"raw_{ts}.json").write_text(json.dumps(listings,indent=2,ensure_ascii=False))
    lines = ["="*62,
             f"  OLX ARBITRAGE DIGEST — {datetime.now().strftime('%d %B %Y')}",
             "="*62,
             f"  Listings scanned : {len(listings)}",
             f"  Hot deals (≥{MIN_SCORE})  : {len(hot)}",
             "="*62,""]
    if not hot:
        lines.append("No strong deals today.")
    else:
        for i,d in enumerate(hot[:10],1):
            lines += [f"#{i} [{d.get('action','?')}] Score {d.get('arbitrage_score','?')}/10",
                      f"   {d['title']}",
                      f"   ₹{d['price']:,} → flip ₹{d.get('estimated_resale_price',0):,}  profit ₹{d.get('profit_estimate',0):,}",
                      f"   {d['location']} | {d['category']} | {d['posted']}",
                      f"   {d.get('reasoning','')}",
                      f"   {d['url']}",""]
    report = "\n".join(lines)
    (DATA_DIR/f"report_{datetime.now().strftime('%Y%m%d')}.txt").write_text(report)
    print("\n"+report)

# ── Main ──────────────────────────────────────────────────────────

def main():
    print("🚀 OLX Arbitrage Scraper")
    print(f"   {len(SEARCHES)} searches | min score {MIN_SCORE}/10")
    print(f"   Worker : {'✅ set' if WORKER_URL else '❌ NOT SET'}")
    print(f"   OpenAI : {'✅ set' if OPENAI_KEY else '❌ NOT SET'}\n")

    print("── Stage 1: Scraping ──────────────────────────────────────")
    listings = run_scraper()

    print("── Stage 2: AI Analysis ───────────────────────────────────")
    listings = analyse(listings)

    print("── Stage 3: Telegram Alerts ───────────────────────────────")
    send_alerts(listings)

    print("── Stage 4: Save & Report ─────────────────────────────────")
    save_report(listings)
    print("\n✅ Done.")

if __name__ == "__main__":
    main()
