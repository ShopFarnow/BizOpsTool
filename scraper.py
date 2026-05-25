"""
GitHub Trend Bot
----------------
Fetches trending GitHub repos, scores them, summarizes with GPT-4o,
and sends a structured digest to Telegram.
"""

import os
import json
import time
import logging
import datetime
import requests
from openai import OpenAI

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─── Config from environment ─────────────────────────────────────────────────
GITHUB_TOKEN   = os.environ["GITHUB_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT  = os.environ["TELEGRAM_CHAT_ID"]

# Optional filters (comma-separated, e.g. "python,typescript")
LANGUAGES = [l.strip() for l in os.getenv("LANGUAGES", "").split(",") if l.strip()]
# Optional topics filter (comma-separated, e.g. "ai,llm,automation")
TOPICS    = [t.strip() for t in os.getenv("TOPICS", "").split(",") if t.strip()]

TOP_N          = int(os.getenv("TOP_N", "8"))          # repos per digest
DAYS_BACK      = int(os.getenv("DAYS_BACK", "7"))       # recency window
MIN_STARS      = int(os.getenv("MIN_STARS", "20"))      # exclude tiny repos

GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

openai_client = OpenAI(api_key=OPENAI_API_KEY)


# ─── GitHub helpers ───────────────────────────────────────────────────────────

def gh_get(url: str, params: dict = None, retries: int = 3) -> dict | list:
    """GET wrapper with retry + rate-limit handling."""
    for attempt in range(retries):
        resp = requests.get(url, headers=GH_HEADERS, params=params, timeout=15)
        if resp.status_code == 403:
            reset = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait  = max(reset - time.time(), 1)
            log.warning("Rate limited. Sleeping %.0fs …", wait)
            time.sleep(wait)
            continue
        if resp.status_code == 422:
            log.warning("Unprocessable entity for %s — skipping", url)
            return {}
        resp.raise_for_status()
        return resp.json()
    return {}


def since_date(days: int = DAYS_BACK) -> str:
    return (datetime.datetime.utcnow() - datetime.timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def search_repos(page: int = 1) -> list[dict]:
    """Search recently-created, high-star repos."""
    date_cutoff = (
        datetime.datetime.utcnow() - datetime.timedelta(days=DAYS_BACK * 4)
    ).strftime("%Y-%m-%d")

    query_parts = [f"created:>{date_cutoff}", f"stars:>{MIN_STARS}"]
    if LANGUAGES:
        lang_q = " ".join(f"language:{l}" for l in LANGUAGES)
        query_parts.append(lang_q)
    if TOPICS:
        topic_q = " ".join(f"topic:{t}" for t in TOPICS)
        query_parts.append(topic_q)

    query = " ".join(query_parts)
    log.info("Search query: %s", query)

    data = gh_get(
        "https://api.github.com/search/repositories",
        params={"q": query, "sort": "stars", "order": "desc",
                "per_page": 50, "page": page},
    )
    return data.get("items", [])


def get_comment_count(owner: str, repo: str) -> int:
    """Count issue comments + PR review comments in the past DAYS_BACK days."""
    since  = since_date()
    params = {"since": since, "per_page": 100}

    issue_data = gh_get(
        f"https://api.github.com/repos/{owner}/{repo}/issues/comments", params
    )
    pr_data = gh_get(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/comments", params
    )

    issue_comments = issue_data if isinstance(issue_data, list) else []
    pr_comments    = pr_data    if isinstance(pr_data,    list) else []

    # Filter out bot comments from both sources
    all_comments = issue_comments + pr_comments
    return sum(1 for c in all_comments if c.get("user", {}).get("type") != "Bot")


def get_commit_count(owner: str, repo: str) -> int:
    """Count commits pushed in the past DAYS_BACK days."""
    url    = f"https://api.github.com/repos/{owner}/{repo}/commits"
    params = {"since": since_date(), "per_page": 100}
    data   = gh_get(url, params)
    return len(data) if isinstance(data, list) else 0


def has_ci_workflow(owner: str, repo: str) -> bool:
    """Check if the repo contains a GitHub Actions workflow file."""
    data = gh_get(
        "https://api.github.com/search/code",
        params={"q": f"path:.github/workflows repo:{owner}/{repo}", "per_page": 1},
    )
    return data.get("total_count", 0) > 0


def stars_gained(repo: dict) -> int:
    """
    Approximate stars gained recently.
    GitHub doesn't expose star velocity directly, so we use total stars
    weighted by how recently the repo was created as a rough proxy.
    Repos created within DAYS_BACK get full star count; older ones get partial.
    """
    created = datetime.datetime.strptime(
        repo["created_at"], "%Y-%m-%dT%H:%M:%SZ"
    )
    age_days = (datetime.datetime.utcnow() - created).days or 1
    if age_days <= DAYS_BACK:
        return repo["stargazers_count"]
    # Decay: assume even distribution of stars over lifetime
    fraction = DAYS_BACK / age_days
    return int(repo["stargazers_count"] * fraction)


# ─── Scoring ──────────────────────────────────────────────────────────────────

def compute_score(
    stars_7d: int,
    forks_7d: int,
    comments: int,
    commits: int,
    ci: bool,
) -> float:
    return (
        stars_7d   * 2.0
        + forks_7d * 1.5
        + comments * 0.5
        + commits  * 0.3
        + (5.0 if ci else 0.0)
    )


# ─── OpenAI summarizer ────────────────────────────────────────────────────────

def gpt_summarize(repos: list[dict]) -> str:
    """
    Ask GPT-4o-mini to write a crisp, Telegram-friendly digest of the top repos.
    Returns a Markdown string ready to send.
    """
    repo_list = "\n\n".join(
        f"#{i+1} {r['full_name']} (⭐{r['stargazers_count']:,})\n"
        f"Description: {r.get('description') or 'No description'}\n"
        f"Topics: {', '.join(r.get('topics', [])) or 'none'}\n"
        f"Language: {r.get('language') or 'unknown'}\n"
        f"Score: {r['trend_score']:.0f} | "
        f"Stars≈{r['stars_7d']} | Forks≈{r['forks_7d']} | "
        f"Comments: {r['comments_30d']} | Commits: {r['commits_7d']} | "
        f"CI: {'yes' if r['has_ci'] else 'no'}\n"
        f"URL: {r['html_url']}"
        for i, r in enumerate(repos)
    )

    today = datetime.datetime.utcnow().strftime("%d %b %Y")

    # Telegram Markdown (parse_mode="Markdown"):
    #   **text** = bold   |   _text_ = italic   |   `text` = code
    prompt = f"""
You are a senior developer writing a daily GitHub trend digest for a Telegram channel.
Today is {today}.

Here are the top {len(repos)} trending repositories with their engagement metrics:

{repo_list}

Write a Telegram message in this EXACT format.
CRITICAL formatting rules — Telegram Markdown (NOT Discord/Reddit/GitHub Markdown):
  - Bold:   **text**   (double asterisks)
  - Italic: _text_     (single underscores)
  - Code:   `text`     (backticks)
  - NO HTML. NO single-asterisk bold like *text*.

Use this structure:

📊 **GitHub Trend Digest — {today}**
_Top {len(repos)} repos gaining traction this week_

For each repo write exactly this block:
---
**#{{}N · repo-name**
🌐 Language | ⭐ X,XXX stars | 📈 Score: XXX
💬 One sentence: what it does and why it is trending right now.
🏷 Topics: tag1, tag2
🔗 URL

---

After all repos, add:
🧠 **Trend Insight**
_Two sentences summarising the dominant themes or technologies this week._

Rules:
- Each repo description: ONE sentence, direct and informative, not hype-y.
- Do NOT add extra blank lines inside a repo block.
- Do NOT deviate from the format above.
""".strip()

    log.info("Calling GPT-4o-mini for digest …")
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
        max_tokens=2000,
    )
    return response.choices[0].message.content.strip()


# ─── Telegram sender ──────────────────────────────────────────────────────────

def send_telegram(text: str) -> None:
    """Send a message to Telegram, splitting if over the 4096-char limit."""
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    chunks  = [text[i:i+4000] for i in range(0, len(text), 4000)]

    for chunk in chunks:
        payload = {
            "chat_id":    TELEGRAM_CHAT,
            "text":       chunk,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        resp = requests.post(url, json=payload, timeout=15)
        if not resp.ok:
            log.error("Telegram error: %s — %s", resp.status_code, resp.text)
        else:
            log.info("Telegram message sent (%d chars)", len(chunk))
        time.sleep(0.5)


# ─── Main pipeline ────────────────────────────────────────────────────────────

def run() -> None:
    log.info("=== GitHub Trend Bot starting ===")

    # 1. Fetch candidate repos
    repos = search_repos(page=1) + search_repos(page=2)
    log.info("Fetched %d candidate repos", len(repos))

    if not repos:
        log.warning("No repos found — check your search filters.")
        send_telegram("📭 *No trending repos found today.* Check your search filters (LANGUAGES, TOPICS, MIN\\_STARS).")
        return

    # 2. Enrich each repo (rate-limit aware: ~3 API calls per repo)
    enriched = []
    for repo in repos:
        owner = repo["owner"]["login"]
        name  = repo["name"]
        full  = repo["full_name"]
        log.info("Enriching %s …", full)

        created_date = datetime.datetime.strptime(repo["created_at"], "%Y-%m-%dT%H:%M:%SZ")
        age_days = max((datetime.datetime.utcnow() - created_date).days, 1)

        s7d = stars_gained(repo)

        # Fix: never estimate more forks than the repo actually has
        if age_days <= DAYS_BACK:
            f7d = repo.get("forks_count", 0)
        else:
            f7d = int(repo.get("forks_count", 0) * (DAYS_BACK / age_days))

        cmts_30 = get_comment_count(owner, name)
        coms    = get_commit_count(owner, name)
        ci   = has_ci_workflow(owner, name)

        score = compute_score(s7d, f7d, cmts_30, coms, ci)

        enriched.append({
            **repo,
            "stars_7d":     s7d,
            "forks_7d":     f7d,
            "comments_30d": cmts_30,
            "commits_7d":   coms,
            "has_ci":       ci,
            "trend_score":  score,
        })

        time.sleep(0.3)  # gentle pacing

    # 3. Sort and take top N
    top = sorted(enriched, key=lambda r: r["trend_score"], reverse=True)[:TOP_N]
    log.info("Top %d repos selected", len(top))
    for r in top:
        log.info("  %.0f — %s", r["trend_score"], r["full_name"])

    # 4. GPT-4o-mini digest
    digest = gpt_summarize(top)

    # 5. Send to Telegram
    send_telegram(digest)
    log.info("=== Done ===")


if __name__ == "__main__":
    run()
