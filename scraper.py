"""
GitHub Trend Bot (Enhanced)
----------------------------
Fetches trending GitHub repos, scores them, summarises with GPT-4o-mini,
and sends a structured digest to Telegram.
Features:
- CI status caching to avoid repeated code searches
- Configurable skip of CI detection
- Exponential backoff with jitter for rate limits
- No httpx issues — works on GitHub Actions
"""

import os
import re
import json
import time
import random
import logging
import datetime
import requests
import httpx
from openai import OpenAI

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─── Validate required environment variables ─────────────────────────────────
_REQUIRED_ENV = ["GITHUB_TOKEN", "OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
_missing = [k for k in _REQUIRED_ENV if not os.getenv(k)]
if _missing:
    log.error("Missing required environment variables: %s", ", ".join(_missing))
    raise SystemExit(1)

GITHUB_TOKEN        = os.environ["GITHUB_TOKEN"]
OPENAI_API_KEY      = os.environ["OPENAI_API_KEY"]
TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT       = os.environ["TELEGRAM_CHAT_ID"]

# Optional filters
LANGUAGES = [l.strip() for l in os.getenv("LANGUAGES", "").split(",") if l.strip()]
TOPICS    = [t.strip() for t in os.getenv("TOPICS",    "").split(",") if t.strip()]

TOP_N      = int(os.getenv("TOP_N",      "8"))
DAYS_BACK  = int(os.getenv("DAYS_BACK",  "7"))
MIN_STARS  = int(os.getenv("MIN_STARS",  "20"))
MAX_PAGES  = int(os.getenv("MAX_PAGES",  "2"))

# If True, assume all repos have CI (skip API call)
SKIP_CI_CHECK = os.getenv("SKIP_CI_CHECK", "false").lower() == "true"

# Cache file for CI status (avoids repeated searches)
CI_CACHE_FILE = ".ci_cache.json"

GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# ─── Fix for httpx (custom client, no proxy arg) ────────────────────────────
custom_http_client = httpx.Client()
openai_client = OpenAI(api_key=OPENAI_API_KEY, http_client=custom_http_client)

# ─── Helper: load/save CI cache ─────────────────────────────────────────────
def load_ci_cache() -> dict:
    if os.path.exists(CI_CACHE_FILE):
        try:
            with open(CI_CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_ci_cache(cache: dict):
    try:
        with open(CI_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        log.warning(f"Failed to save CI cache: {e}")

# ─── GitHub helpers with improved rate limit handling ───────────────────────
def gh_get(url: str, params: dict = None, retries: int = 5) -> dict | list:
    base_delay = 1
    for attempt in range(retries):
        resp = requests.get(url, headers=GH_HEADERS, params=params, timeout=15)

        # Primary rate limit (has reset header)
        if resp.status_code == 403 and "X-RateLimit-Reset" in resp.headers:
            reset = int(resp.headers["X-RateLimit-Reset"])
            wait = max(reset - time.time(), 1)
            log.warning("Primary rate limit. Sleeping %.0fs …", wait)
            time.sleep(wait)
            continue

        # Secondary / abuse rate limit (no reset header) → exponential backoff + jitter
        if resp.status_code in (403, 429):
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
            log.warning("Secondary rate limit (attempt %d). Backing off %.1fs …", attempt + 1, delay)
            time.sleep(delay)
            continue

        if resp.status_code == 422:
            log.warning("Unprocessable entity for %s — skipping", url)
            return {}

        resp.raise_for_status()
        return resp.json()

    log.error("All %d retries exhausted for %s", retries, url)
    return {}

# ─── Core functions (unchanged except for CI caching) ───────────────────────
def since_date(days: int = DAYS_BACK) -> str:
    return (datetime.datetime.utcnow() - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

def search_repos(page: int = 1) -> list[dict]:
    date_cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=DAYS_BACK * 4)).strftime("%Y-%m-%d")
    query_parts = [f"created:>{date_cutoff}", f"stars:>{MIN_STARS}"]
    if LANGUAGES:
        query_parts.append(" ".join(f"language:{l}" for l in LANGUAGES))
    if TOPICS:
        query_parts.append(" ".join(f"topic:{t}" for t in TOPICS))

    query = " ".join(query_parts)
    log.info("Search query (page %d): %s", page, query)

    data = gh_get(
        "https://api.github.com/search/repositories",
        params={"q": query, "sort": "stars", "order": "desc", "per_page": 50, "page": page},
    )
    return data.get("items", []) if isinstance(data, dict) else []

def get_comment_count(owner: str, repo: str) -> int:
    since = since_date()
    params = {"since": since, "per_page": 100}
    issue_data = gh_get(f"https://api.github.com/repos/{owner}/{repo}/issues/comments", params)
    pr_data    = gh_get(f"https://api.github.com/repos/{owner}/{repo}/pulls/comments",  params)
    issue_comments = issue_data if isinstance(issue_data, list) else []
    pr_comments    = pr_data    if isinstance(pr_data,    list) else []
    return sum(1 for c in (issue_comments + pr_comments) if c.get("user", {}).get("type") != "Bot")

def get_commit_count(owner: str, repo: str) -> int:
    data = gh_get(f"https://api.github.com/repos/{owner}/{repo}/commits", {"since": since_date(), "per_page": 100})
    return len(data) if isinstance(data, list) else 0

def has_ci_workflow(owner: str, repo: str, cache: dict) -> bool:
    """Check CI with caching. If SKIP_CI_CHECK is True, return True immediately."""
    if SKIP_CI_CHECK:
        return True
    key = f"{owner}/{repo}"
    if key in cache:
        return cache[key]
    data = gh_get("https://api.github.com/search/code", {"q": f"path:.github/workflows repo:{key}", "per_page": 1})
    result = isinstance(data, dict) and data.get("total_count", 0) > 0
    cache[key] = result
    return result

def stars_gained(repo: dict) -> int:
    created = datetime.datetime.strptime(repo["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    age_days = max((datetime.datetime.utcnow() - created).days, 1)
    if age_days <= DAYS_BACK:
        return repo["stargazers_count"]
    return int(repo["stargazers_count"] * (DAYS_BACK / age_days))

def forks_gained(repo: dict, age_days: int) -> int:
    if age_days <= DAYS_BACK:
        return repo.get("forks_count", 0)
    return int(repo.get("forks_count", 0) * (DAYS_BACK / age_days))

def compute_score(stars_7d: int, forks_7d: int, comments: int, commits: int, ci: bool) -> float:
    return stars_7d * 2.0 + forks_7d * 1.5 + comments * 0.5 + commits * 0.3 + (5.0 if ci else 0.0)

# ─── GPT prompt (unchanged) ─────────────────────────────────────────────────
def build_prompt(repos: list[dict], today: str) -> str:
    window_label = f"last {DAYS_BACK} days"
    repo_lines = []
    for i, r in enumerate(repos):
        topics = ", ".join(r.get("topics", [])) or "none"
        lang = r.get("language") or "unknown"
        desc = r.get("description") or "No description"
        line = (
            f"#{i+1} {r['full_name']} (stars: {r['stargazers_count']:,})\n"
            f"Description: {desc}\n"
            f"Topics: {topics}\n"
            f"Language: {lang}\n"
            f"Score: {r['trend_score']:.0f} | "
            f"Stars ({window_label}): ~{r['stars_7d']} | "
            f"Forks ({window_label}): ~{r['forks_7d']} | "
            f"Comments ({window_label}): {r['comments_7d']} | "
            f"Commits ({window_label}): {r['commits_7d']} | "
            f"CI: {'yes' if r['has_ci'] else 'no'}\n"
            f"URL: {r['html_url']}"
        )
        repo_lines.append(line)

    repo_block = "\n\n".join(repo_lines)
    n = len(repos)

    header = (
        "You are a senior developer writing a daily GitHub trend digest for a Telegram channel.\n"
        "Today is " + today + ".\n\n"
        "Here are the top " + str(n) + " trending repositories with their engagement metrics:\n\n"
        + repo_block + "\n\n"
        "Write a Telegram message using this EXACT structure.\n\n"
        "FORMATTING RULES (Telegram MarkdownV2):\n"
        "  - Bold:        *text*\n"
        "  - Italic:      _text_\n"
        "  - Code/inline: `text`\n"
        "  - Escape ALL special chars outside formatting: _ * [ ] ( ) ~ ` > # + - = | { } . !\n"
        "  - URLs go bare — no markdown link syntax.\n"
        "  - NO HTML tags.\n\n"
        "OUTPUT STRUCTURE:\n\n"
        "=== START OF MESSAGE ===\n"
        "📊 *GitHub Trend Digest \\— " + today + "*\n"
        "_Top " + str(n) + " repos gaining traction this week_\n\n"
    )

    repo_template = (
        "For EACH repo, output exactly this block (replace N, values, and text):\n"
        "---\n"
        "*#N · repo-name*\n"
        "🌐 Language | ⭐ X,XXX stars | 📈 Score: XXX\n"
        "💬 One sentence describing what it does and why it is gaining traction now.\n"
        "🏷 Topics: tag1, tag2\n"
        "🔗 https://github.com/owner/repo\n\n"
    )

    footer = (
        "After all repo blocks, append:\n"
        "🧠 *Trend Insight*\n"
        "_Two sentences summarising the dominant themes or technologies this week._\n"
        "=== END OF MESSAGE ===\n\n"
        "RULES:\n"
        "- One sentence per repo description. Direct and informative, not hype-y.\n"
        "- Do NOT add extra blank lines inside a repo block.\n"
        "- Do NOT wrap the output in code fences.\n"
        "- Output ONLY the Telegram message, nothing else.\n"
    )

    return header + repo_template + footer

def gpt_summarize(repos: list[dict]) -> str:
    today = datetime.datetime.utcnow().strftime("%d %b %Y")
    prompt = build_prompt(repos, today)
    log.info("Calling GPT-4o-mini for digest …")
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
        max_tokens=2000,
    )
    return response.choices[0].message.content.strip()

# ─── Telegram sender with fallback ──────────────────────────────────────────
def send_telegram(text: str, parse_mode: str = "MarkdownV2") -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]

    for chunk in chunks:
        payload = {
            "chat_id": TELEGRAM_CHAT,
            "text": chunk,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        resp = requests.post(url, json=payload, timeout=15)

        if not resp.ok:
            log.warning("Telegram send failed (%s): %s — %s", parse_mode, resp.status_code, resp.text)
            if parse_mode != "":
                log.info("Retrying as plain text …")
                send_telegram(chunk, parse_mode="")
        else:
            log.info("Telegram message sent (%d chars)", len(chunk))
        time.sleep(0.5)

# ─── Main pipeline with CI cache ────────────────────────────────────────────
def run() -> None:
    log.info("=== GitHub Trend Bot (Enhanced) starting ===")

    # Load cached CI statuses
    ci_cache = load_ci_cache()
    log.info(f"Loaded {len(ci_cache)} cached CI statuses")

    # 1. Fetch candidate repos
    repos = []
    for page in range(1, MAX_PAGES + 1):
        page_results = search_repos(page=page)
        repos.extend(page_results)
        if len(page_results) < 50:
            break
    log.info("Fetched %d candidate repos across %d page(s)", len(repos), MAX_PAGES)

    if not repos:
        log.warning("No repos found — check your search filters.")
        send_telegram("📭 *No trending repos found today.* Check your search filters (LANGUAGES, TOPICS, MIN_STARS).")
        return

    # 2. Enrich each repo (with caching)
    enriched = []
    for repo in repos:
        owner = repo["owner"]["login"]
        name = repo["name"]
        full_name = f"{owner}/{name}"
        log.info("Enriching %s …", full_name)

        created_date = datetime.datetime.strptime(repo["created_at"], "%Y-%m-%dT%H:%M:%SZ")
        age_days = max((datetime.datetime.utcnow() - created_date).days, 1)

        s7d = stars_gained(repo)
        f7d = forks_gained(repo, age_days)
        cmts = get_comment_count(owner, name)
        coms = get_commit_count(owner, name)
        ci = has_ci_workflow(owner, name, ci_cache)

        enriched.append({
            **repo,
            "stars_7d": s7d,
            "forks_7d": f7d,
            "comments_7d": cmts,
            "commits_7d": coms,
            "has_ci": ci,
            "trend_score": compute_score(s7d, f7d, cmts, coms, ci),
        })
        time.sleep(0.3)  # gentle pacing

    # Save updated CI cache
    save_ci_cache(ci_cache)
    log.info(f"Saved {len(ci_cache)} CI statuses to cache")

    # 3. Sort and take top N
    top = sorted(enriched, key=lambda r: r["trend_score"], reverse=True)[:TOP_N]
    log.info("Top %d repos selected:", len(top))
    for r in top:
        log.info("  score=%.0f  %s", r["trend_score"], r["full_name"])

    # 4. GPT digest
    digest = gpt_summarize(top)

    # 5. Send to Telegram
    send_telegram(digest)
    log.info("=== Done ===")

if __name__ == "__main__":
    run()
