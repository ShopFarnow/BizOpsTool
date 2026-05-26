#!/usr/bin/env python3
"""
GitHub Trend Intelligence Engine v3.2 (BizOps Score + Beehiiv draft)
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
import math
from typing import Any
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
DOCS_DIR            = os.getenv("DOCS_DIR", "docs")            # GitHub Pages root
SITE_BASE_URL       = os.getenv("SITE_BASE_URL", "https://bizopstool.com")
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

# ── UTC time helper ──────────────────────────────────────────────────────────
def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

# ─────────────────────────────────────────────────────────────────────────────
# BizOps Score Engine (inline)
# ─────────────────────────────────────────────────────────────────────────────
def _minmax(values: list[float]) -> list[float]:
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]

def _recency_score(last_commit_days_ago: float) -> float:
    if last_commit_days_ago <= 0:
        return 1.0
    score = math.exp(-last_commit_days_ago / 30)
    return round(max(0.0, min(1.0, score)), 4)

def _issue_response_score(avg_hours: float) -> float:
    if avg_hours <= 0:
        return 1.0
    score = math.exp(-avg_hours / 48)
    return round(max(0.0, min(1.0, score)), 4)

def compute_bizops_batch(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not tools:
        return tools
    stars_raw   = [float(t.get("stars", 0)) for t in tools]
    forks_raw   = [float(t.get("forks_30d", 0)) for t in tools]
    contrib_raw = [float(t.get("contributor_count", 1)) for t in tools]

    stars_norm   = _minmax(stars_raw)
    forks_norm   = _minmax(forks_raw)
    contrib_norm = _minmax(contrib_raw)

    for i, tool in enumerate(tools):
        recency  = _recency_score(float(tool.get("last_commit_days", 30)))
        issue_r  = _issue_response_score(float(tool.get("avg_issue_hours", 48)))
        ci       = 1.0 if tool.get("ci_passing", False) else 0.3

        breakdown = {
            "stars":        round(stars_norm[i], 3),
            "fork_velocity":round(forks_norm[i], 3),
            "commit_recency":recency,
            "issue_response":issue_r,
            "ci_status":    ci,
            "contributors": round(contrib_norm[i], 3),
        }

        raw_score = (
            breakdown["stars"]          * 0.20 +
            breakdown["fork_velocity"]  * 0.20 +
            breakdown["commit_recency"] * 0.25 +
            breakdown["issue_response"] * 0.15 +
            breakdown["ci_status"]      * 0.10 +
            breakdown["contributors"]   * 0.10
        )
        score = round(raw_score * 100)

        prev = tool.get("prev_score")
        if prev is None:
            trend = "new"
        elif score > prev + 3:
            trend = "rising"
        elif score < prev - 3:
            trend = "falling"
        else:
            trend = "stable"

        tool["bizops_score"]     = score
        tool["score_breakdown"]  = breakdown
        tool["trend_direction"]  = trend

    return tools

# ─────────────────────────────────────────────────────────────────────────────
# SQLite cache – one connection, reused
# ─────────────────────────────────────────────────────────────────────────────
_conn: sqlite3.Connection | None = None

def _get_conn() -> sqlite3.Connection:
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
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS prev_scores (
                repo      TEXT PRIMARY KEY,
                score     INTEGER NOT NULL,
                stored_at REAL NOT NULL
            )
        """)
        _conn.commit()
        atexit.register(_conn.close)
    return _conn

def cache_get(key: str, ttl_seconds: float) -> str | None:
    conn = _get_conn()
    row = conn.execute("SELECT value, stored_at FROM cache WHERE key = ?", (key,)).fetchone()
    if row and (time.time() - row[1]) < ttl_seconds:
        return row[0]
    return None

def cache_set(key: str, value: str) -> None:
    conn = _get_conn()
    conn.execute("INSERT OR REPLACE INTO cache(key, value, stored_at) VALUES(?,?,?)", (key, value, time.time()))
    conn.commit()

def ci_cache_get(repo_key: str) -> bool | None:
    conn = _get_conn()
    row = conn.execute("SELECT has_ci FROM ci_cache WHERE repo = ?", (repo_key,)).fetchone()
    return bool(row[0]) if row else None

def ci_cache_set(repo_key: str, has_ci: bool) -> None:
    conn = _get_conn()
    conn.execute("INSERT OR REPLACE INTO ci_cache(repo, has_ci, stored_at) VALUES(?,?,?)", (repo_key, int(has_ci), time.time()))
    conn.commit()

def _purge_stale_ci_cache(max_age_days: int = 30) -> None:
    cutoff = time.time() - max_age_days * 86400
    conn = _get_conn()
    deleted = conn.execute("DELETE FROM ci_cache WHERE stored_at < ?", (cutoff,)).rowcount
    conn.commit()
    if deleted:
        log.info("Purged %d stale CI cache entries (>%dd old)", deleted, max_age_days)

def get_prev_score(repo_full_name: str) -> int | None:
    conn = _get_conn()
    row = conn.execute("SELECT score FROM prev_scores WHERE repo = ?", (repo_full_name,)).fetchone()
    return row[0] if row else None

def set_prev_score(repo_full_name: str, score: int) -> None:
    conn = _get_conn()
    conn.execute("INSERT OR REPLACE INTO prev_scores(repo, score, stored_at) VALUES(?,?,?)", (repo_full_name, score, time.time()))
    conn.commit()

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
    return (_utcnow() - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

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
        ic = gh_get(f"https://api.github.com/repos/{owner}/{repo}/issues/comments", {"since": since, "per_page": 100})
        pc = gh_get(f"https://api.github.com/repos/{owner}/{repo}/pulls/comments",  {"since": since, "per_page": 100})
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
        data = gh_get(f"https://api.github.com/repos/{owner}/{repo}/commits", {"since": since_date(), "per_page": 100})
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
        data = gh_get("https://api.github.com/search/code", {"q": f"path:.github/workflows repo:{key}", "per_page": 1})
    except Exception as exc:
        log.warning("ci_check failed for %s: %s", key, exc)
        return False
    result = isinstance(data, dict) and data.get("total_count", 0) > 0
    ci_cache_set(key, result)
    return result

def stars_gained(repo: dict) -> int:
    created = datetime.datetime.strptime(repo["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    age = max((_utcnow() - created).days, 1)
    return repo["stargazers_count"] if age <= DAYS_BACK else int(repo["stargazers_count"] * DAYS_BACK / age)

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

# ── New functions for BizOps Score signals ───────────────────────────────────
def get_contributor_count(owner: str, repo: str) -> int:
    cache_key = f"contributors:{owner}/{repo}"
    cached = cache_get(cache_key, METRIC_TTL_HOURS * 3600)
    if cached is not None:
        return int(cached)
    try:
        url = f"https://api.github.com/repos/{owner}/{repo}/contributors"
        resp = requests.get(url, headers=GH_HEADERS, params={"per_page": 1}, timeout=20)
        if resp.status_code == 200 and "Link" in resp.headers:
            import re
            links = resp.headers["Link"]
            match = re.search(r'page=(\d+)>; rel="last"', links)
            if match:
                count = int(match.group(1))
            else:
                count = 1
        else:
            data = gh_get(url, params={"per_page": 100})
            count = len(data) if isinstance(data, list) else 0
    except Exception as exc:
        log.warning("contributor_count failed for %s/%s: %s", owner, repo, exc)
        count = 1
    cache_set(cache_key, str(count))
    return count

def get_last_commit_days(owner: str, repo: str) -> int:
    cache_key = f"last_commit:{owner}/{repo}"
    cached = cache_get(cache_key, METRIC_TTL_HOURS * 3600)
    if cached is not None:
        return int(cached)
    try:
        data = gh_get(f"https://api.github.com/repos/{owner}/{repo}/commits", params={"per_page": 1})
        if isinstance(data, list) and data:
            last_commit_str = data[0]["commit"]["committer"]["date"]
            last_commit_date = datetime.datetime.strptime(last_commit_str, "%Y-%m-%dT%H:%M:%SZ")
            days = max((_utcnow() - last_commit_date).days, 0)
        else:
            days = 30
    except Exception as exc:
        log.warning("last_commit_days failed for %s/%s: %s", owner, repo, exc)
        days = 30
    cache_set(cache_key, str(days))
    return days

def get_avg_issue_response_hours(owner: str, repo: str) -> float:
    """Real implementation – fetch last 10 closed issues, compute avg hours to first comment."""
    cache_key = f"issue_response:{owner}/{repo}"
    cached = cache_get(cache_key, METRIC_TTL_HOURS * 3600)
    if cached is not None:
        return float(cached)
    try:
        issues = gh_get(f"https://api.github.com/repos/{owner}/{repo}/issues",
                        params={"state": "closed", "per_page": 10, "sort": "updated", "direction": "desc"})
        if not isinstance(issues, list) or len(issues) == 0:
            return 48.0
        total_hours = 0.0
        count = 0
        for issue in issues:
            created_at = datetime.datetime.strptime(issue["created_at"], "%Y-%m-%dT%H:%M:%SZ")
            comments_url = issue["comments_url"]
            comments = gh_get(comments_url)
            if isinstance(comments, list) and comments:
                first_comment = comments[0]["created_at"]
                first_comment_dt = datetime.datetime.strptime(first_comment, "%Y-%m-%dT%H:%M:%SZ")
                hours = (first_comment_dt - created_at).total_seconds() / 3600.0
                total_hours += hours
                count += 1
        avg = total_hours / count if count > 0 else 48.0
        cache_set(cache_key, str(avg))
        return round(avg, 1)
    except Exception as exc:
        log.warning("issue_response failed for %s/%s: %s", owner, repo, exc)
        return 48.0

def get_forks_30d(owner: str, repo: str, current_forks: int) -> int:
    cache_key = f"forks_30d_snapshot:{owner}/{repo}"
    snap = cache_get(cache_key, 30 * 86400)
    if snap is not None:
        old_forks = int(snap)
        return max(0, current_forks - old_forks)
    else:
        cache_set(cache_key, str(current_forks))
        return 0

# ── Legacy scoring (kept for Telegram digest) ────────────────────────────────
def compute_score(stars_7d: int, forks_7d: int, comments: int, commits: int, ci: bool) -> float:
    return stars_7d * 0.5 + forks_7d * 2.0 + comments * 1.5 + commits * 1.0 + (10.0 if ci else 0.0)

# ── MarkdownV2 escaping ─────────────────────────────────────────────────────
_MDV2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")
def escape_mdv2(text: str) -> str:
    return _MDV2_SPECIAL.sub(r"\\\1", text)

# ── Idea synthesis (unchanged) ──────────────────────────────────────────────
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

# ── Digest builder (unchanged) ──────────────────────────────────────────────
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

def build_idea_block(idea: dict) -> str:
    e = escape_mdv2
    return (
        "\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 *IDEA ENGINE — This Week's Signal*\n\n"
        f"🚀 *{e(idea.get('title','Unnamed Idea'))}*\n"
        f"_{e(idea.get('tagline',''))}_\n\n"
        f"🔴 *Problem:* {e(idea.get('problem',''))}\n"
        f"🟢 *Solution:* {e(idea.get('solution',''))}\n"
        f"👥 *Audience:* {e(idea.get('audience',''))}\n"
        f"🛡 *Moat:* {e(idea.get('moat',''))}\n\n"
        f"🗺 *Product Flow:*\n"
        f"`{e(render_flowchart(idea.get('flowchart_steps', ['A','B','C','D'])))}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

def send_telegram(text: str, parse_mode: str = "MarkdownV2") -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chunk in [text[i:i+4096] for i in range(0, len(text), 4096)]:
        payload = {"chat_id": TELEGRAM_CHAT, "text": chunk, "parse_mode": parse_mode, "disable_web_page_preview": True}
        resp = requests.post(url, json=payload, timeout=20)
        if not resp.ok:
            log.warning("Telegram send failed (%s): %s — %s", parse_mode, resp.status_code, resp.text[:300])
            if parse_mode:
                log.info("Retrying as plain text …")
                send_telegram(chunk, parse_mode="")
        else:
            log.info("Telegram chunk sent (%d chars)", len(chunk))
        time.sleep(0.5)

# ─────────────────────────────────────────────────────────────────────────────
# Beehiiv integration (draft newsletter)
# ─────────────────────────────────────────────────────────────────────────────
def build_full_digest_html(tools: list[dict], generated_at: str) -> str:
    """Convert full tools list to HTML email body."""
    rows = []
    for t in tools[:50]:  # limit to 50 for email size
        name = t.get('name', '')
        desc = t.get('description', '')[:120]
        score = t.get('bizops_score', 0)
        stars = t.get('stargazers_count', 0)
        url = t.get('html_url', '#')
        rows.append(f"""
        <tr>
            <td style="padding:10px;border-bottom:1px solid #eee;"><a href="{url}" style="font-weight:600;color:#c8952a;">{name}</a></td>
            <td style="padding:10px;border-bottom:1px solid #eee;">{desc}</td>
            <td style="padding:10px;border-bottom:1px solid #eee;text-align:center;">{score}</td>
            <td style="padding:10px;border-bottom:1px solid #eee;">⭐ {stars:,}</td>
        </tr>
        """)
    return f"""
    <h2>BizOps Full Digest – {generated_at[:10]}</h2>
    <p>Top {len(tools)} open‑source business tools, ranked by BizOps Score (0‑100).</p>
    <table style="width:100%;border-collapse:collapse;">
        <thead>
            <tr style="background:#f4f4f8;">
                <th style="padding:10px;text-align:left">Tool</th>
                <th>Description</th>
                <th>Score</th>
                <th>Stars</th>
            </tr>
        </thead>
        <tbody>{"".join(rows)}</tbody>
    </table>
    <p style="margin-top:20px;">Unsubscribe or manage plan at <a href="https://bizopstool.com">BizOpsTool</a>.</p>
    """

def post_beehiiv_draft(subject: str, body_html: str) -> None:
    """Create a draft newsletter in Beehiiv using the full dataset."""
    api_key = os.getenv("BEEHIIV_API_KEY")
    pub_id = os.getenv("BEEHIIV_PUB_ID")
    if not api_key or not pub_id:
        log.warning("Beehiiv credentials missing – skipping draft creation")
        return
    url = f"https://api.beehiiv.com/v2/publications/{pub_id}/posts"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "title": subject,
        "content_html": body_html,
        "status": "draft",
        "is_public": False,
        "meta_description": "Weekly BizOps digest of top trending open‑source tools."
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        if resp.status_code in (200, 201):
            log.info("Beehiiv draft created: %s", resp.json().get('id', 'ok'))
        else:
            log.error("Beehiiv draft failed: %s %s", resp.status_code, resp.text)
    except Exception as e:
        log.error("Beehiiv exception: %s", e)

# ─────────────────────────────────────────────────────────────────────────────
# Website output helpers (auto-SEO + free/paid JSON split)
# ─────────────────────────────────────────────────────────────────────────────
import re as _re

def _slugify(name: str) -> str:
    """Convert repo full_name or tool name to a URL-safe slug."""
    slug = name.split("/")[-1]  # use repo name only, not owner
    slug = _re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")
    return slug or "tool"


def _to_public_tool(t: dict) -> dict:
    """Return a clean, minimal dict safe to serve publicly in trending.json."""
    return {
        "name":            t.get("name") or t.get("full_name", "").split("/")[-1],
        "full_name":       t.get("full_name", ""),
        "description":     (t.get("description") or "")[:200],
        "github_url":      t.get("html_url", ""),
        "language":        t.get("language", ""),
        "topics":          t.get("topics", [])[:6],
        "stars":           t.get("stargazers_count") or t.get("stars", 0),
        "forks_30d":       t.get("forks_30d", 0),
        "last_commit_days":t.get("last_commit_days", 0),
        "bizops_score":    t.get("bizops_score", 0),
        "trend_direction": t.get("trend_direction", "stable"),
        "category":        (t.get("topics") or ["general"])[0],
        "slug":            _slugify(t.get("full_name", t.get("name", "tool"))),
    }


_TOOL_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} — BizOps Score {score} | BizOpsTool</title>
<meta name="description" content="{description}">
<link rel="canonical" href="{site_url}/tools/{slug}.html">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Syne:wght@600;800&display=swap" rel="stylesheet">
<style>
:root{{--bg:#0d0d0d;--surface:#141414;--border:#222;--text:#e8e6e0;--muted:#666;--accent:#c8f040;--mono:'DM Mono',monospace;--sans:'Syne',sans-serif}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:16px;line-height:1.7;padding:40px 24px}}
.wrap{{max-width:700px;margin:0 auto}}
.back{{font-family:var(--mono);font-size:12px;color:var(--muted);text-decoration:none;display:inline-block;margin-bottom:32px}}
.back:hover{{color:var(--accent)}}
h1{{font-size:36px;font-weight:800;letter-spacing:-.02em;margin-bottom:8px}}
.score{{display:inline-block;font-family:var(--mono);font-size:28px;font-weight:500;color:var(--accent);border:1px solid rgba(200,240,64,.3);padding:8px 16px;border-radius:3px;margin:16px 0}}
.desc{{font-size:16px;color:var(--muted);margin-bottom:24px;max-width:560px}}
.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--border);border:1px solid var(--border);border-radius:4px;overflow:hidden;margin:24px 0}}
.stat{{background:var(--surface);padding:16px}}
.stat-label{{font-family:var(--mono);font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px}}
.stat-val{{font-size:20px;font-weight:600}}
.tags{{display:flex;flex-wrap:wrap;gap:6px;margin:16px 0}}
.tag{{font-family:var(--mono);font-size:11px;padding:3px 8px;border:1px solid var(--border);color:var(--muted);border-radius:2px}}
.cta{{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:24px;margin-top:32px;text-align:center}}
.cta p{{color:var(--muted);font-size:14px;margin-bottom:16px}}
.btn{{display:inline-block;text-decoration:none;font-family:var(--mono);font-size:12px;padding:10px 20px;border-radius:2px}}
.btn-gh{{border:1px solid var(--border);color:var(--text);margin-right:8px}}
.btn-gh:hover{{border-color:var(--accent);color:var(--accent)}}
.btn-sub{{background:var(--accent);color:#0d0d0d;border:1px solid var(--accent)}}
.btn-sub:hover{{opacity:.85}}
footer{{margin-top:48px;font-family:var(--mono);font-size:11px;color:var(--muted)}}
footer a{{color:var(--muted);text-decoration:none;margin-left:16px}}
</style>
</head>
<body>
<div class="wrap">
  <a class="back" href="/">← back to BizOpsTool</a>
  <h1>{name}</h1>
  <div class="score">{score} / 100</div>
  <p class="desc">{description}</p>
  <div class="stats">
    <div class="stat"><div class="stat-label">GitHub Stars</div><div class="stat-val">{stars}</div></div>
    <div class="stat"><div class="stat-label">Forks / 30d</div><div class="stat-val">{forks_30d}</div></div>
    <div class="stat"><div class="stat-label">Last Commit</div><div class="stat-val">{last_commit_days}d ago</div></div>
  </div>
  <div class="tags">{tags_html}</div>
  <div class="cta">
    <p>Get the full weekly BizOps digest — 50+ tools ranked by score every Monday.</p>
    <a class="btn btn-gh" href="{github_url}" target="_blank" rel="noopener">View on GitHub</a>
    <a class="btn btn-sub" href="{site_url}/#signup">Subscribe free</a>
  </div>
  <footer>
    <span>BizOpsTool · Updated {generated_at}</span>
    <a href="/">Home</a><a href="/stack-grader.html">Stack Grader</a><a href="/score-methodology.html">Methodology</a>
  </footer>
</div>
</body>
</html>"""


def generate_tool_pages(tools: list[dict], generated_at: str) -> None:
    """Generate one static HTML page per tool under docs/tools/."""
    tools_dir = os.path.join(DOCS_DIR, "tools")
    os.makedirs(tools_dir, exist_ok=True)
    count = 0
    for t in tools:
        pub = _to_public_tool(t)
        slug = pub["slug"]
        tags_html = "".join(f'<span class="tag">{topic}</span>' for topic in pub["topics"])
        stars_str = f"{pub['stars']:,}" if isinstance(pub["stars"], int) else str(pub["stars"])
        page = _TOOL_PAGE_TEMPLATE.format(
            name=pub["name"],
            score=pub["bizops_score"],
            description=pub["description"] or "An open-source BizOps tool.",
            slug=slug,
            site_url=SITE_BASE_URL,
            stars=stars_str,
            forks_30d=pub["forks_30d"],
            last_commit_days=pub["last_commit_days"],
            tags_html=tags_html or '<span class="tag">open-source</span>',
            github_url=pub["github_url"],
            generated_at=generated_at[:10],
        )
        with open(os.path.join(tools_dir, f"{slug}.html"), "w") as f:
            f.write(page)
        count += 1
    log.info("Generated %d tool pages in %s/tools/", count, DOCS_DIR)


def generate_sitemap(tools: list[dict], generated_at: str) -> None:
    """Generate sitemap.xml for all tool pages + core pages."""
    today = generated_at[:10]
    urls = [
        f"  <url><loc>{SITE_BASE_URL}/</loc><lastmod>{today}</lastmod><changefreq>daily</changefreq><priority>1.0</priority></url>",
        f"  <url><loc>{SITE_BASE_URL}/stack-grader.html</loc><lastmod>{today}</lastmod><changefreq>weekly</changefreq><priority>0.8</priority></url>",
        f"  <url><loc>{SITE_BASE_URL}/score-methodology.html</loc><lastmod>{today}</lastmod><changefreq>monthly</changefreq><priority>0.6</priority></url>",
    ]
    for t in tools:
        slug = _slugify(t.get("full_name", t.get("name", "tool")))
        urls.append(
            f"  <url><loc>{SITE_BASE_URL}/tools/{slug}.html</loc>"
            f"<lastmod>{today}</lastmod><changefreq>weekly</changefreq><priority>0.5</priority></url>"
        )
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>"
    )
    path = os.path.join(DOCS_DIR, "sitemap.xml")
    with open(path, "w") as f:
        f.write(sitemap)
    log.info("Generated sitemap with %d URLs at %s", len(urls), path)


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


def run(test_mode: bool = False) -> None:
    log.info("=== GitHub Trend Intelligence Engine v3.2 (BizOps Score + Beehiiv) starting ===")
    log_config(test_mode)
    _purge_stale_ci_cache()

    effective_pages = 1 if test_mode else MAX_PAGES
    effective_top_n = 3 if test_mode else TOP_N

    # 1 — Collect candidates
    seen = {}
    for page in range(1, effective_pages + 1):
        results = search_repos(page=page)
        for repo in results:
            seen.setdefault(repo["id"], repo)
        if len(results) < 50:
            break
    raw_repos = list(seen.values())
    log.info("Fetched %d unique candidate repos over %d page(s)%s", len(raw_repos), effective_pages, " [TEST MODE]" if test_mode else "")

    if not raw_repos:
        log.warning("No repos found — check search filters.")
        send_telegram("📭 No trending repos found today\\. Check LANGUAGES, TOPICS, MIN\\_STARS\\.")
        return

    # 2 — Enrich with engagement signals + fetch BizOps Score signals
    enriched = []
    for repo in raw_repos:
        owner = repo["owner"]["login"]
        name = repo["name"]
        log.info("Enriching %s/%s …", owner, name)
        created = datetime.datetime.strptime(repo["created_at"], "%Y-%m-%dT%H:%M:%SZ")
        age = max((_utcnow() - created).days, 1)

        s7d = stars_gained(repo)
        f7d = forks_gained(repo, age)
        cmts = get_comment_count(owner, name)
        coms = get_commit_count(owner, name)
        ci = has_ci_workflow(owner, name)

        stars = repo["stargazers_count"]
        forks_30d = get_forks_30d(owner, name, repo.get("forks_count", 0))
        last_commit_days = get_last_commit_days(owner, name)
        avg_issue_hours = get_avg_issue_response_hours(owner, name)
        contributor_count = get_contributor_count(owner, name)
        ci_passing = ci

        enriched.append({
            **repo,
            "stars_7d": s7d,
            "forks_7d": f7d,
            "comments_7d": cmts,
            "commits_7d": coms,
            "has_ci": ci,
            "trend_score": compute_score(s7d, f7d, cmts, coms, ci),
            "stars": stars,
            "forks_30d": forks_30d,
            "last_commit_days": last_commit_days,
            "avg_issue_hours": avg_issue_hours,
            "ci_passing": ci_passing,
            "contributor_count": contributor_count,
        })
        time.sleep(0.1 if test_mode else 0.3)

    # 3 — Compute BizOps Score
    for r in enriched:
        r["prev_score"] = get_prev_score(r["full_name"])
    enriched = compute_bizops_batch(enriched)
    for r in enriched:
        set_prev_score(r["full_name"], r["bizops_score"])

    # 4 — Select top-N by original trend_score (for Telegram digest)
    top = sorted(enriched, key=lambda r: r["trend_score"], reverse=True)[:effective_top_n]
    log.info("Top %d selected (by trend_score):", len(top))
    for r in top:
        log.info("  score=%.0f  bizops=%d  %s", r["trend_score"], r["bizops_score"], r["full_name"])

    # 5 — Fetch READMEs for the winners
    for r in top:
        log.info("Fetching README for %s …", r["full_name"])
        r["readme_snippet"] = get_readme_snippet(r["owner"]["login"], r["name"])
        time.sleep(0.1 if test_mode else 0.3)

    # 6 — Generate AI outputs
    digest = gpt_digest(top)
    idea = synthesise_idea(top)
    idea_block = build_idea_block(idea)

    # 7 — Output trending.json (free tier: top 5 by bizops_score for GitHub Pages)
    #      and trending_full.json (all tools: never committed, used for Beehiiv email)
    generated_at = _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    top_by_score = sorted(enriched, key=lambda r: r.get("bizops_score", 0), reverse=True)

    # Free tier — top 5 only, served publicly at /trending.json
    free_payload = {
        "generated_at": generated_at,
        "tool_count": len(enriched),
        "tools": [_to_public_tool(t) for t in top_by_score[:5]],
    }
    trending_json_path = os.path.join(DOCS_DIR, "trending.json")
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(trending_json_path, "w") as f:
        json.dump(free_payload, f, indent=2, default=str)
    log.info("Written top-5 free tools to %s", trending_json_path)

    # Full data — all tools, for Beehiiv/paid use, never pushed to Pages
    full_payload = {
        "generated_at": generated_at,
        "tool_count": len(enriched),
        "tools": top_by_score,
    }
    with open("trending_full.json", "w") as f:
        json.dump(full_payload, f, indent=2, default=str)
    log.info("Written %d tools to trending_full.json (not committed to Pages)", len(enriched))

    # 7b — Generate one HTML page per tool (auto-SEO)
    generate_tool_pages(top_by_score, generated_at)

    # 7c — Generate sitemap.xml
    generate_sitemap(top_by_score, generated_at)

    # 8 — Send Telegram (or print in test mode)
    full_message = digest + idea_block
    if test_mode:
        log.info("=== TEST MODE — printing output, not sending to Telegram ===")
        print("\n" + "=" * 60)
        print(full_message)
        print("=" * 60 + "\n")
    else:
        send_telegram(full_message, parse_mode="")
        # 9 — Create Beehiiv draft (only if not test mode and secrets exist)
        full_digest_html = build_full_digest_html(top_by_score, generated_at)
        post_beehiiv_draft(f"BizOps Full Digest – {generated_at[:10]}", full_digest_html)

    log.info("=== Done ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GitHub Trend Intelligence Engine v3.2")
    parser.add_argument("--test", action="store_true", help="Dry-run: 1 page, top 3 repos, print output instead of sending to Telegram")
    parser.add_argument("--unit-tests", action="store_true", help="Run unit tests and exit")
    args = parser.parse_args()
    if args.unit_tests:
        # Quick unit test for score (no external calls)
        from compute_score import compute_score  # noqa: we already have it above
        def run_unit_tests():
            assert compute_score(0,0,0,0,False) == 0.0
            assert compute_score(10,1,5,2,True) == 10*0.5 + 1*2.0 + 5*1.5 + 2*1.0 + 10.0
            log.info("Unit tests passed")
        run_unit_tests()
    else:
        run(test_mode=args.test)
