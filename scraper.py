#!/usr/bin/env python3
"""
GitHub Trend Intelligence Engine v3.1 (Mini Edition)
─────────────────────────────────────────────────────
Institutional-grade GitHub trend bot that:
  1. Discovers trending repositories with multi-signal scoring
  2. Synthesises cross-repo patterns into a novel startup/product idea
  3. Renders a minimal ASCII flowchart (A→B→C→D) in the Telegram digest
  4. Uses OpenAI GPT-4o-mini for all generation tasks (cost-optimised)
  5. SQLite cache for README + engagement metrics (TTL-based)
  6. Tenacity-powered retry with exponential back-off
  7. Full MarkdownV2 escaping on all dynamic content
  8. --test dry-run mode, deduplication, startup config log

Required environment variables
───────────────────────────────
  GITHUB_TOKEN        – GitHub personal access token (read-only scopes)
  OPENAI_API_KEY      – OpenAI API key
  TELEGRAM_BOT_TOKEN  – Telegram bot token
  TELEGRAM_CHAT_ID    – Target chat / channel ID

Optional
────────
  LANGUAGES           – comma-separated   (e.g. "Python,TypeScript")
  TOPICS              – comma-separated   (e.g. "llm,agents")
  TOP_N               – repos in digest   (default 8)
  DAYS_BACK           – recency window    (default 7)
  MIN_STARS           – minimum stars     (default 50)
  MAX_PAGES           – search pages      (default 2)
  SKIP_CI_CHECK       – skip CI check     (default true)
  README_FETCH_CHARS  – chars to fetch    (default 5000)
  README_PROMPT_CHARS – chars for prompt  (default 2500)
  CACHE_DB            – SQLite path       (default .cache.db)
  README_TTL_DAYS     – README cache TTL  (default 7)
  METRIC_TTL_HOURS    – metric cache TTL  (default 24)
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import logging
import os
import re
import sqlite3
import time
import atexit
import requests
import httpx
from openai import OpenAI
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Environment validation ────────────────────────────────────────────────────
_REQUIRED = ["GITHUB_TOKEN", "OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
_missing = [k for k in _REQUIRED if not os.getenv(k)]
if _missing:
    log.error("Missing required environment variables: %s", ", ".join(_missing))
    raise SystemExit(1)

GITHUB_TOKEN       = os.environ["GITHUB_TOKEN"]
OPENAI_API_KEY     = os.environ["OPENAI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT      = os.environ["TELEGRAM_CHAT_ID"]

LANGUAGES = [l.strip() for l in os.getenv("LANGUAGES", "").split(",") if l.strip()]
TOPICS    = [t.strip() for t in os.getenv("TOPICS",    "").split(",") if t.strip()]

TOP_N      = int(os.getenv("TOP_N",      "8"))
DAYS_BACK  = int(os.getenv("DAYS_BACK",  "7"))
MIN_STARS  = int(os.getenv("MIN_STARS",  "50"))
MAX_PAGES  = int(os.getenv("MAX_PAGES",  "2"))

SKIP_CI_CHECK       = os.getenv("SKIP_CI_CHECK", "true").lower() == "true"
README_FETCH_CHARS  = int(os.getenv("README_FETCH_CHARS",  "5000"))
README_PROMPT_CHARS = int(os.getenv("README_PROMPT_CHARS", "2500"))
CACHE_DB            = os.getenv("CACHE_DB", ".cache.db")
README_TTL_DAYS     = int(os.getenv("README_TTL_DAYS",  "7"))
METRIC_TTL_HOURS    = int(os.getenv("METRIC_TTL_HOURS", "24"))

# ── GitHub client ─────────────────────────────────────────────────────────────
GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# ── OpenAI client ─────────────────────────────────────────────────────────────
_http = httpx.Client()
ai = OpenAI(api_key=OPENAI_API_KEY, http_client=_http)


# ── Corrected UTC time helper (no recursion) ─────────────────────────────────
def _utcnow() -> datetime.datetime:
    """Return current UTC time as a naive datetime (no timezone info)."""
    # Using datetime.now(timezone.utc) is available in Python 3.2+
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


# ─────────────────────────────────────────────────────────────────────────────
# Startup config log
# ─────────────────────────────────────────────────────────────────────────────

def log_config(test_mode: bool = False) -> None:
    log.info(
        "Configuration: languages=%s, topics=%s, top_n=%d, min_stars=%d, "
        "days_back=%d, max_pages=%d, skip_ci=%s, readme_fetch=%d, "
        "readme_prompt=%d, readme_ttl=%dd, metric_ttl=%dh, test_mode=%s",
        LANGUAGES or "any", TOPICS or "any", TOP_N, MIN_STARS,
        DAYS_BACK, MAX_PAGES, SKIP_CI_CHECK,
        README_FETCH_CHARS, README_PROMPT_CHARS,
        README_TTL_DAYS, METRIC_TTL_HOURS, test_mode,
    )


# ─────────────────────────────────────────────────────────────────────────────
# SQLite cache — single persistent connection, closed via atexit
# ─────────────────────────────────────────────────────────────────────────────

_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    """Return the module-level SQLite connection, creating it on first call."""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(CACHE_DB, check_same_thread=False)
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                key       TEXT PRIMARY KEY,
                value     TEXT NOT NULL,
                stored_at REAL NOT NULL
            )
        """)
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS ci_cache (
                repo      TEXT PRIMARY KEY,
                has_ci    INTEGER NOT NULL,
                stored_at REAL NOT NULL
            )
        """)
        _conn.commit()
        atexit.register(_conn.close)
    return _conn


def cache_get(key: str, ttl_seconds: float) -> str | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT value, stored_at FROM cache WHERE key = ?", (key,)
    ).fetchone()
    if row and (time.time() - row[1]) < ttl_seconds:
        return row[0]
    return None


def cache_set(key: str, value: str) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO cache(key, value, stored_at) VALUES(?,?,?)",
        (key, value, time.time()),
    )
    conn.commit()


def ci_cache_get(repo_key: str) -> bool | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT has_ci FROM ci_cache WHERE repo = ?", (repo_key,)
    ).fetchone()
    return bool(row[0]) if row else None


def ci_cache_set(repo_key: str, has_ci: bool) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO ci_cache(repo, has_ci, stored_at) VALUES(?,?,?)",
        (repo_key, int(has_ci), time.time()),
    )
    conn.commit()


def _purge_stale_ci_cache(max_age_days: int = 30) -> None:
    """Delete CI cache entries older than max_age_days."""
    cutoff = time.time() - max_age_days * 86400
    conn = _get_conn()
    deleted = conn.execute(
        "DELETE FROM ci_cache WHERE stored_at < ?", (cutoff,)
    ).rowcount
    conn.commit()
    if deleted:
        log.info("Purged %d stale CI cache entries (>%dd old)", deleted, max_age_days)


# ─────────────────────────────────────────────────────────────────────────────
# Tenacity-powered GitHub HTTP helper
# ─────────────────────────────────────────────────────────────────────────────

def _is_rate_limit(exc: Exception) -> bool:
    resp = getattr(exc, "response", None)
    if resp is None:
        return False
    if resp.status_code in (403, 429):
        reset = resp.headers.get("X-RateLimit-Reset")
        if reset:
            wait = max(int(reset) - time.time(), 1)
            log.warning("Primary rate-limit. Sleeping %.0fs …", wait)
            time.sleep(wait)
        return True
    return False


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    retry=retry_if_exception(_is_rate_limit),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)
def gh_get(url: str, params: dict | None = None) -> dict | list:
    resp = requests.get(url, headers=GH_HEADERS, params=params, timeout=20)
    if resp.status_code == 422:
        log.warning("422 Unprocessable for %s — skipping", url)
        return {}
    resp.raise_for_status()
    return resp.json()


# ─────────────────────────────────────────────────────────────────────────────
# GitHub helpers
# ─────────────────────────────────────────────────────────────────────────────

def since_date(days: int = DAYS_BACK) -> str:
    return (_utcnow() - datetime.timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def search_repos(page: int = 1) -> list[dict]:
    cutoff = (_utcnow() - datetime.timedelta(days=DAYS_BACK * 4)).strftime("%Y-%m-%d")
    parts = [f"created:>{cutoff}", f"stars:>{MIN_STARS}"]
    if LANGUAGES:
        parts.append(" ".join(f"language:{l}" for l in LANGUAGES))
    if TOPICS:
        parts.append(" ".join(f"topic:{t}" for t in TOPICS))
    query = " ".join(parts)
    log.info("GitHub search (page %d): %s", page, query)
    try:
        data = gh_get(
            "https://api.github.com/search/repositories",
            params={"q": query, "sort": "stars", "order": "desc", "per_page": 50, "page": page},
        )
    except Exception as exc:
        log.error("search_repos page %d failed: %s", page, exc)
        return []
    return data.get("items", []) if isinstance(data, dict) else []


def get_comment_count(owner: str, repo: str) -> int:
    cache_key = f"comments:{owner}/{repo}:{since_date()}"
    cached = cache_get(cache_key, METRIC_TTL_HOURS * 3600)
    if cached is not None:
        return int(cached)
    since = since_date()
    try:
        ic = gh_get(f"https://api.github.com/repos/{owner}/{repo}/issues/comments",
                    {"since": since, "per_page": 100})
        pc = gh_get(f"https://api.github.com/repos/{owner}/{repo}/pulls/comments",
                    {"since": since, "per_page": 100})
    except Exception as exc:
        log.warning("comment_count failed for %s/%s: %s", owner, repo, exc)
        return 0
    human = lambda lst: sum(1 for c in lst if c.get("user", {}).get("type") != "Bot")
    count = human(ic if isinstance(ic, list) else []) + human(pc if isinstance(pc, list) else [])
    cache_set(cache_key, str(count))
    return count


def get_commit_count(owner: str, repo: str) -> int:
    cache_key = f"commits:{owner}/{repo}:{since_date()}"
    cached = cache_get(cache_key, METRIC_TTL_HOURS * 3600)
    if cached is not None:
        return int(cached)
    try:
        data = gh_get(
            f"https://api.github.com/repos/{owner}/{repo}/commits",
            {"since": since_date(), "per_page": 100},
        )
    except Exception as exc:
        log.warning("commit_count failed for %s/%s: %s", owner, repo, exc)
        return 0
    count = len(data) if isinstance(data, list) else 0
    cache_set(cache_key, str(count))
    return count


def has_ci_workflow(owner: str, repo: str) -> bool:
    if SKIP_CI_CHECK:
        return True
    key = f"{owner}/{repo}"
    cached = ci_cache_get(key)
    if cached is not None:
        return cached
    try:
        data = gh_get(
            "https://api.github.com/search/code",
            {"q": f"path:.github/workflows repo:{key}", "per_page": 1},
        )
    except Exception as exc:
        log.warning("ci_check failed for %s: %s", key, exc)
        return False
    result = isinstance(data, dict) and data.get("total_count", 0) > 0
    ci_cache_set(key, result)
    return result


def stars_gained(repo: dict) -> int:
    created = datetime.datetime.strptime(repo["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    age = max((_utcnow() - created).days, 1)
    return (
        repo["stargazers_count"]
        if age <= DAYS_BACK
        else int(repo["stargazers_count"] * DAYS_BACK / age)
    )


def forks_gained(repo: dict, age_days: int) -> int:
    fc = repo.get("forks_count", 0)
    return fc if age_days <= DAYS_BACK else int(fc * DAYS_BACK / age_days)


def get_readme_snippet(owner: str, repo: str) -> str:
    cache_key = f"readme:{owner}/{repo}"
    cached = cache_get(cache_key, README_TTL_DAYS * 86400)
    if cached is not None:
        log.info("README cache hit for %s/%s", owner, repo)
        return cached
    try:
        data = gh_get(f"https://api.github.com/repos/{owner}/{repo}/readme")
    except Exception as exc:
        log.warning("README fetch failed for %s/%s: %s", owner, repo, exc)
        return "README unreadable."
    if isinstance(data, dict) and "content" in data:
        try:
            raw = base64.b64decode(data["content"]).decode("utf-8")
            clean = re.sub(r"\n{2,}", "\n", raw)
            snippet = clean[:README_FETCH_CHARS] + ("…" if len(clean) > README_FETCH_CHARS else "")
            cache_set(cache_key, snippet)
            return snippet
        except Exception as exc:
            log.warning("README decode failed for %s/%s: %s", owner, repo, exc)
            return "README unreadable."
    return "No README found."


def compute_score(
    stars_7d: int, forks_7d: int, comments: int, commits: int, ci: bool
) -> float:
    return (
        stars_7d * 0.5
        + forks_7d * 2.0
        + comments * 1.5
        + commits * 1.0
        + (10.0 if ci else 0.0)
    )


# ─────────────────────────────────────────────────────────────────────────────
# MarkdownV2 escaping
# ─────────────────────────────────────────────────────────────────────────────

_MDV2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def escape_mdv2(text: str) -> str:
    """Escape all Telegram MarkdownV2 special characters."""
    return _MDV2_SPECIAL.sub(r"\\\1", text)


# ─────────────────────────────────────────────────────────────────────────────
# Idea synthesis
# ─────────────────────────────────────────────────────────────────────────────

def synthesise_idea(repos: list[dict]) -> dict:
    summaries = [
        f"• {r['full_name']} — {r.get('description','no desc')} "
        f"[lang:{r.get('language','?')} stars:{r['stargazers_count']:,} "
        f"topics:{','.join(r.get('topics',[])[:4]) or 'none'}]"
        for r in repos
    ]
    prompt = f"""You are a world-class venture technologist and product strategist.

Below are the top trending GitHub repositories this week:

{chr(10).join(summaries)}

Your task:
1. Identify the SINGLE most novel and actionable product or startup idea that EMERGES from the combination of these trends. It must be non-obvious — not just "build an AI chatbot".
2. Return ONLY a JSON object (no markdown fences, no preamble) with these exact keys:

{{
  "title": "Short punchy product name (≤6 words)",
  "tagline": "One-line value proposition (≤12 words)",
  "problem": "Crisp description of the painful problem being solved (≤2 sentences)",
  "solution": "What the product does, using the tech patterns observed (≤2 sentences)",
  "audience": "Precise target user / buyer (≤1 sentence)",
  "moat": "Why this would be defensible 18 months from now (≤1 sentence)",
  "flowchart_steps": ["Step A (≤4 words)", "Step B (≤4 words)", "Step C (≤4 words)", "Step D (≤4 words)"]
}}

flowchart_steps must represent the core USER JOURNEY or PRODUCT LOOP in exactly 4 short verb phrases.
"""
    log.info("Calling GPT-4o-mini for idea synthesis …")
    try:
        resp = ai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=600,
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content.strip())
    except (json.JSONDecodeError, Exception) as exc:
        log.warning("Idea synthesis failed (%s) — using fallback.", exc)
        return {
            "title": "Trend-Derived Idea",
            "tagline": "Synthesised from this week's signals",
            "problem": "See digest for context.",
            "solution": "Cross-pollinate the top repos above.",
            "audience": "Developers and indie builders",
            "moat": "First-mover advantage plus data flywheel",
            "flowchart_steps": ["Discover signal", "Validate problem", "Build MVP", "Grow community"],
        }


def render_flowchart(steps: list[str]) -> str:
    return " → ".join(steps)


# ─────────────────────────────────────────────────────────────────────────────
# Digest builder
# ─────────────────────────────────────────────────────────────────────────────

def build_repo_prompt(repos: list[dict], today: str) -> str:
    window = f"last {DAYS_BACK}d"
    blocks = []
    for i, r in enumerate(repos, 1):
        topics = ", ".join(r.get("topics", [])[:5]) or "none"
        readme = r.get("readme_snippet", "")[:README_PROMPT_CHARS]
        blocks.append(
            f"#{i} {r['full_name']} — {r.get('description','no desc')}\n"
            f"  lang:{r.get('language','?')} | stars:{r['stargazers_count']:,} "
            f"| score:{r['trend_score']:.0f} | stars({window}):~{r['stars_7d']} "
            f"| forks({window}):~{r['forks_7d']} | comments({window}):{r['comments_7d']} "
            f"| commits({window}):{r['commits_7d']} | ci:{'yes' if r['has_ci'] else 'no'}\n"
            f"  topics: {topics}\n"
            f"  README: {readme}\n"
            f"  url: {r['html_url']}"
        )

    return f"""You are a senior developer and institutional research analyst writing a weekly GitHub intelligence digest for a technical audience.
Today: {today}

TOP {len(repos)} TRENDING REPOSITORIES
{'='*60}
{chr(10).join(blocks)}
{'='*60}

Write a Telegram digest using EXACTLY this structure and Telegram MarkdownV2 formatting rules:

FORMATTING RULES:
- Bold: *text*
- Italic: _text_
- Monospace: `text`
- CRITICAL: Escape ALL special characters that appear outside of formatting tags: _ * [ ] ( ) ~ ` > # + - = | {{ }} . !
  For example: "gpt-4o" becomes "gpt\\-4o", "repo.name" becomes "repo\\.name", "v1.2" becomes "v1\\.2"
- No HTML. No markdown link syntax. Bare URLs only (URLs are exempt from escaping).

OUTPUT (copy structure exactly):

📊 *GitHub Intelligence Digest — {today}*
_Institutional\\-grade signal on what builders are actually shipping_

For EACH repo output this block:
---
*\\#{i} · repo\\-name*
🌐 Language ｜ ⭐ X,XXX ｜ 📈 Score: XXX
💡 _One sentence: unique technical insight or novel architectural decision, not hype\\._
🏷 `tag1` `tag2`
🔗 https://github.com/owner/repo

After all repo blocks, add ONE blank line then output:
📐 *Weekly Signal Summary*
_Two sentences on dominant technical themes or architectural shifts this week\\._

Output ONLY the digest. No preamble. No code fences. Ensure ALL special chars are escaped."""


def gpt_digest(repos: list[dict]) -> str:
    today = _utcnow().strftime("%d %b %Y")
    prompt = build_repo_prompt(repos, today)
    log.info("Calling GPT-4o-mini for repo digest …")
    resp = ai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.35,
        max_tokens=2800,
    )
    return resp.choices[0].message.content.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Idea block — all dynamic fields escaped before insertion
# ─────────────────────────────────────────────────────────────────────────────

def build_idea_block(idea: dict) -> str:
    e = escape_mdv2  # shorthand

    title    = e(idea.get("title",    "Unnamed Idea"))
    tagline  = e(idea.get("tagline",  ""))
    problem  = e(idea.get("problem",  ""))
    solution = e(idea.get("solution", ""))
    audience = e(idea.get("audience", ""))
    moat     = e(idea.get("moat",     ""))
    flow_raw = render_flowchart(idea.get("flowchart_steps", ["A", "B", "C", "D"]))
    flow_esc = e(flow_raw)

    return (
        "\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 *IDEA ENGINE — This Week's Signal*\n\n"
        f"🚀 *{title}*\n"
        f"_{tagline}_\n\n"
        f"🔴 *Problem:* {problem}\n"
        f"🟢 *Solution:* {solution}\n"
        f"👥 *Audience:* {audience}\n"
        f"🛡 *Moat:* {moat}\n\n"
        f"🗺 *Product Flow:*\n"
        f"`{flow_esc}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(text: str, parse_mode: str = "MarkdownV2") -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chunk in [text[i : i + 4096] for i in range(0, len(text), 4096)]:
        payload = {
            "chat_id": TELEGRAM_CHAT,
            "text": chunk,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        resp = requests.post(url, json=payload, timeout=20)
        if not resp.ok:
            log.warning(
                "Telegram send failed (%s): %s — %s",
                parse_mode, resp.status_code, resp.text[:300],
            )
            if parse_mode:
                log.info("Retrying as plain text …")
                send_telegram(chunk, parse_mode="")
        else:
            log.info("Telegram chunk sent (%d chars)", len(chunk))
        time.sleep(0.5)


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests
# ─────────────────────────────────────────────────────────────────────────────

def run_unit_tests() -> None:
    failures: list[str] = []

    def check(name: str, got, expected) -> None:
        if got != expected:
            failures.append(f"{name}: expected {expected!r}, got {got!r}")

    def check_true(name: str, condition: bool) -> None:
        if not condition:
            failures.append(f"{name}: condition was False")

    # compute_score
    check("score_baseline",    compute_score(0, 0, 0, 0, False), 0.0)
    check("score_ci_bonus",    compute_score(0, 0, 0, 0, True),  10.0)
    check("score_all",         compute_score(100, 50, 20, 10, True),
          100*0.5 + 50*2.0 + 20*1.5 + 10*1.0 + 10.0)

    # stars_gained
    old_repo = {
        "created_at": "2020-01-01T00:00:00Z",
        "stargazers_count": 3650,
    }
    gained = stars_gained(old_repo)
    age = max((_utcnow() - datetime.datetime(2020, 1, 1)).days, 1)
    check("stars_gained_old", gained, int(3650 * DAYS_BACK / age))

    new_repo = {
        "created_at": _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stargazers_count": 500,
    }
    check("stars_gained_new", stars_gained(new_repo), 500)

    # forks_gained
    check("forks_gained_new",  forks_gained({"forks_count": 100}, 3),   100)
    check("forks_gained_old",  forks_gained({"forks_count": 100}, 70),
          int(100 * DAYS_BACK / 70))

    # README truncation
    long_text = "x" * (README_FETCH_CHARS + 100)
    truncated = long_text[:README_FETCH_CHARS] + "…"
    check_true("readme_truncation", len(truncated) == README_FETCH_CHARS + 1)

    # JSON fallback
    bad_json = "not json {"
    try:
        json.loads(bad_json)
        failures.append("json_fallback: should have raised JSONDecodeError")
    except json.JSONDecodeError:
        pass  # expected

    # escape_mdv2
    check("escape_dots",    escape_mdv2("v1.2.3"),    r"v1\.2\.3")
    check("escape_dashes",  escape_mdv2("gpt-4o"),    r"gpt\-4o")
    check("escape_parens",  escape_mdv2("(test)"),    r"\(test\)")
    check("escape_bang",    escape_mdv2("hello!"),    r"hello\!")
    check("escape_mixed",   escape_mdv2("a.b-c_d"),   r"a\.b\-c\_d")
    check("escape_clean",   escape_mdv2("hello"),     "hello")

    # render_flowchart
    steps = ["Discover", "Validate", "Build", "Grow"]
    check("flowchart_render", render_flowchart(steps), "Discover → Validate → Build → Grow")

    # Deduplication (dict-by-id pattern)
    dupes = [{"id": 1, "name": "a"}, {"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    deduped = list({r["id"]: r for r in dupes}.values())
    check("dedup_count", len(deduped), 2)

    if failures:
        log.error("Unit test FAILURES:\n  %s", "\n  ".join(failures))
        raise SystemExit(1)
    log.info("All unit tests passed ✓")


# ─────────────────────────────────────────────────────────────────────────────
# Main orchestration
# ─────────────────────────────────────────────────────────────────────────────

def run(test_mode: bool = False) -> None:
    log.info("=== GitHub Trend Intelligence Engine v3.1 starting ===")
    log_config(test_mode)
    _purge_stale_ci_cache()

    effective_pages = 1 if test_mode else MAX_PAGES
    effective_top_n = 3 if test_mode else TOP_N

    # 1 — Collect candidates, deduplicated by repo id
    seen: dict[int, dict] = {}
    for page in range(1, effective_pages + 1):
        results = search_repos(page=page)
        for repo in results:
            seen.setdefault(repo["id"], repo)
        if len(results) < 50:
            break
    raw_repos = list(seen.values())
    log.info(
        "Fetched %d unique candidate repos over %d page(s)%s",
        len(raw_repos), effective_pages, " [TEST MODE]" if test_mode else "",
    )

    if not raw_repos:
        log.warning("No repos found — check search filters.")
        send_telegram("📭 No trending repos found today\\. Check LANGUAGES, TOPICS, MIN\\_STARS\\.")
        return

    # 2 — Enrich with cached engagement signals
    enriched: list[dict] = []
    for repo in raw_repos:
        owner = repo["owner"]["login"]
        name  = repo["name"]
        log.info("Enriching %s/%s …", owner, name)
        created = datetime.datetime.strptime(repo["created_at"], "%Y-%m-%dT%H:%M:%SZ")
        age     = max((_utcnow() - created).days, 1)
        s7d  = stars_gained(repo)
        f7d  = forks_gained(repo, age)
        cmts = get_comment_count(owner, name)
        coms = get_commit_count(owner, name)
        ci   = has_ci_workflow(owner, name)
        enriched.append({
            **repo,
            "stars_7d":    s7d,
            "forks_7d":    f7d,
            "comments_7d": cmts,
            "commits_7d":  coms,
            "has_ci":      ci,
            "trend_score": compute_score(s7d, f7d, cmts, coms, ci),
        })
        time.sleep(0.1 if test_mode else 0.3)

    # 3 — Select top-N by composite score
    top: list[dict] = sorted(
        enriched, key=lambda r: r["trend_score"], reverse=True
    )[:effective_top_n]
    log.info("Top %d selected:", len(top))
    for r in top:
        log.info("  score=%.0f  %s", r["trend_score"], r["full_name"])

    # 4 — Fetch READMEs (cached)
    for r in top:
        log.info("Fetching README for %s …", r["full_name"])
        r["readme_snippet"] = get_readme_snippet(r["owner"]["login"], r["name"])
        time.sleep(0.1 if test_mode else 0.3)

    # 5 — Generate AI outputs
    digest     = gpt_digest(top)
    idea       = synthesise_idea(top)
    idea_block = build_idea_block(idea)

    # 6 — Compose and send (or print in test mode)
    full_message = digest + idea_block
    if test_mode:
        log.info("=== TEST MODE — printing output, not sending to Telegram ===")
        print("\n" + "=" * 60)
        print(full_message)
        print("=" * 60 + "\n")
    else:
        send_telegram(full_message)

    log.info("=== Done ===")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GitHub Trend Intelligence Engine v3.1")
    parser.add_argument(
        "--test", action="store_true",
        help="Dry-run: 1 page, top 3 repos, print output instead of sending to Telegram",
    )
    parser.add_argument(
        "--unit-tests", action="store_true",
        help="Run unit tests and exit",
    )
    args = parser.parse_args()

    if args.unit_tests:
        run_unit_tests()
    else:
        run(test_mode=args.test)
