#!/usr/bin/env python3
"""
GitHub Trend Intelligence Engine v3.1 (Mini Edition) – with BizOps Score (self‑contained)
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
    # Placeholder – implement later with real issue data
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
# Unit tests (simplified, keep original behaviour)
# ─────────────────────────────────────────────────────────────────────────────
def run_unit_tests() -> None:
    failures = []
    def check(name, got, expected):
        if got != expected:
            failures.append(f"{name}: expected {expected!r}, got {got!r}")
    check("score_baseline", compute_score(0,0,0,0,False), 0.0)
    check("score_ci_bonus", compute_score(0,0,0,0,True), 10.0)
    # ... add other tests if desired
    if failures:
        log.error("Unit test FAILURES:\n  %s", "\n  ".join(failures))
        raise SystemExit(1)
    log.info("All unit tests passed ✓")

# ─────────────────────────────────────────────────────────────────────────────
# Main orchestration (integrated BizOps Score)
# ─────────────────────────────────────────────────────────────────────────────
def run(test_mode: bool = False) -> None:
    log.info("=== GitHub Trend Intelligence Engine v3.1 (BizOps Score) starting ===")
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

    # 7 — Output trending.json with full data
    trending_json_path = "trending.json"
    with open(trending_json_path, "w") as f:
        json.dump(enriched, f, indent=2, default=str)
    log.info("Written %d tools to %s", len(enriched), trending_json_path)

    # 8 — Send Telegram (or print in test mode)
    full_message = digest + idea_block
    if test_mode:
        log.info("=== TEST MODE — printing output, not sending to Telegram ===")
        print("\n" + "=" * 60)
        print(full_message)
        print("=" * 60 + "\n")
    else:
        send_telegram(full_message, parse_mode="")

    log.info("=== Done ===")

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

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GitHub Trend Intelligence Engine v3.1")
    parser.add_argument("--test", action="store_true", help="Dry-run: 1 page, top 3 repos, print output instead of sending to Telegram")
    parser.add_argument("--unit-tests", action="store_true", help="Run unit tests and exit")
    args = parser.parse_args()
    if args.unit_tests:
        run_unit_tests()
    else:
        run(test_mode=args.test)
