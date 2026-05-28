#!/usr/bin/env python3
"""
GitHub Trend Intelligence Engine v4.0
- All v3.x bug fixes applied
- 3 new scoring signals: PR merge rate, test coverage, release recency
- generate_category_pages() for SEO pillar pages
- Improved meta titles and descriptions
- Affiliate links on tool pages
- Submit-a-tool link
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
from collections import defaultdict
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
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Environment ───────────────────────────────────────────────────────────────
_REQUIRED = ["GITHUB_TOKEN", "OPENAI_API_KEY"]
_missing = [k for k in _REQUIRED if not os.getenv(k)]
if _missing:
    log.error("Missing required env vars: %s", ", ".join(_missing))
    raise SystemExit(1)

GITHUB_TOKEN       = os.environ["GITHUB_TOKEN"]
OPENAI_API_KEY     = os.environ["OPENAI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT      = os.getenv("TELEGRAM_CHAT_ID")
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT:
    log.warning("Telegram credentials missing – will skip sending messages")

TOPICS    = [t.strip() for t in os.getenv("TOPICS", "").split(",") if t.strip()]
TOP_N     = int(os.getenv("TOP_N",      "10"))
DAYS_BACK = int(os.getenv("DAYS_BACK",  "30"))
MIN_STARS = int(os.getenv("MIN_STARS",  "150"))
MAX_PAGES = int(os.getenv("MAX_PAGES",  "3"))

SKIP_CI_CHECK       = os.getenv("SKIP_CI_CHECK", "true").lower() == "true"
DOCS_DIR            = os.getenv("DOCS_DIR", "docs")
SITE_BASE_URL       = os.getenv("SITE_BASE_URL", "https://bizopstool.com")
README_FETCH_CHARS  = int(os.getenv("README_FETCH_CHARS",  "5000"))
README_PROMPT_CHARS = int(os.getenv("README_PROMPT_CHARS", "2500"))
CACHE_DB            = os.getenv("CACHE_DB", ".cache.db")
README_TTL_DAYS     = int(os.getenv("README_TTL_DAYS",  "7"))
METRIC_TTL_HOURS    = int(os.getenv("METRIC_TTL_HOURS", "24"))
MAX_ISSUE_SAMPLES   = int(os.getenv("MAX_ISSUE_SAMPLES", "3"))

GITHUB_REPO_URL = "https://github.com/ShopFarnow/BizOpsTool"

# ── Affiliate map (tool_name_lowercase → affiliate_url) ──────────────────────
AFFILIATE_LINKS: dict[str, str] = {
    "n8n":        "https://n8n.io?ref=bizopstool",
    "supabase":   "https://supabase.com?ref=bizopstool",
    "appwrite":   "https://appwrite.io?ref=bizopstool",
    "directus":   "https://directus.io?ref=bizopstool",
    "plane":      "https://plane.so?ref=bizopstool",
    "cal.com":    "https://cal.com?ref=bizopstool",
    "nocodb":     "https://nocodb.com?ref=bizopstool",
    "metabase":   "https://metabase.com?ref=bizopstool",
}

# ── GitHub + OpenAI clients ───────────────────────────────────────────────────
GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
_http = httpx.Client()
ai = OpenAI(api_key=OPENAI_API_KEY, http_client=_http)

def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

# ─────────────────────────────────────────────────────────────────────────────
# BizOps Score Engine v4.0 (9 signals)
# ─────────────────────────────────────────────────────────────────────────────
def _minmax(values: list[float]) -> list[float]:
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]

def _recency_score(days: float) -> float:
    return round(max(0.0, min(1.0, math.exp(-max(days, 0) / 30))), 4)

def _issue_response_score(avg_hours: float) -> float:
    return round(max(0.0, min(1.0, math.exp(-max(avg_hours, 0) / 48))), 4)

def _release_score(days_since_release: int) -> float:
    if days_since_release >= 90:
        return 0.0
    return round(max(0.0, min(1.0, math.exp(-days_since_release / 60))), 4)

def _pr_rate_score(rate: float) -> float:
    """PR merge rate: merged/open. >2 = healthy."""
    return round(min(1.0, rate / 3.0), 4)

def compute_bizops_batch(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not tools:
        return tools
    stars_norm   = _minmax([float(t.get("stars", 0))             for t in tools])
    forks_norm   = _minmax([float(t.get("forks_30d", 0))         for t in tools])
    contrib_norm = _minmax([float(t.get("contributor_count", 1)) for t in tools])

    for i, tool in enumerate(tools):
        recency  = _recency_score(float(tool.get("last_commit_days", 30)))
        issue_r  = _issue_response_score(float(tool.get("avg_issue_hours", 48)))
        ci       = 1.0 if tool.get("ci_passing", False) else 0.3
        release  = _release_score(int(tool.get("days_since_release", 90)))
        pr_rate  = _pr_rate_score(float(tool.get("pr_merge_rate", 1.0)))
        tests    = 1.0 if tool.get("has_tests", False) else 0.2

        breakdown = {
            "stars":           round(stars_norm[i], 3),
            "fork_velocity":   round(forks_norm[i], 3),
            "commit_recency":  recency,
            "issue_response":  issue_r,
            "ci_status":       ci,
            "contributors":    round(contrib_norm[i], 3),
            "pr_merge_rate":   pr_rate,
            "has_tests":       tests,
            "release_recency": release,
        }

        raw_score = (
            breakdown["stars"]           * 0.15 +
            breakdown["fork_velocity"]   * 0.15 +
            breakdown["commit_recency"]  * 0.20 +
            breakdown["issue_response"]  * 0.12 +
            breakdown["ci_status"]       * 0.08 +
            breakdown["contributors"]    * 0.10 +
            breakdown["pr_merge_rate"]   * 0.08 +
            breakdown["has_tests"]       * 0.07 +
            breakdown["release_recency"] * 0.05
        )
        score = round(raw_score * 100)
        prev  = tool.get("prev_score")
        trend = "new" if prev is None else ("rising" if score > prev + 3 else ("falling" if score < prev - 3 else "stable"))

        tool["bizops_score"]    = score
        tool["score_breakdown"] = breakdown
        tool["trend_direction"] = trend
    return tools

# ─────────────────────────────────────────────────────────────────────────────
# SQLite cache
# ─────────────────────────────────────────────────────────────────────────────
_conn: sqlite3.Connection | None = None

def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(CACHE_DB, check_same_thread=False)
        _conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, stored_at REAL NOT NULL)")
        _conn.execute("CREATE TABLE IF NOT EXISTS ci_cache (repo TEXT PRIMARY KEY, has_ci INTEGER NOT NULL, stored_at REAL NOT NULL)")
        _conn.execute("CREATE TABLE IF NOT EXISTS prev_scores (repo TEXT PRIMARY KEY, score INTEGER NOT NULL, stored_at REAL NOT NULL)")
        _conn.commit()
        atexit.register(_conn.close)
    return _conn

def cache_get(key: str, ttl_seconds: float) -> str | None:
    row = _get_conn().execute("SELECT value, stored_at FROM cache WHERE key = ?", (key,)).fetchone()
    return row[0] if row and (time.time() - row[1]) < ttl_seconds else None

def cache_set(key: str, value: str) -> None:
    _get_conn().execute("INSERT OR REPLACE INTO cache(key,value,stored_at) VALUES(?,?,?)", (key, value, time.time()))
    _get_conn().commit()

def ci_cache_get(repo: str) -> bool | None:
    row = _get_conn().execute("SELECT has_ci FROM ci_cache WHERE repo = ?", (repo,)).fetchone()
    return bool(row[0]) if row else None

def ci_cache_set(repo: str, has_ci: bool) -> None:
    _get_conn().execute("INSERT OR REPLACE INTO ci_cache(repo,has_ci,stored_at) VALUES(?,?,?)", (repo, int(has_ci), time.time()))
    _get_conn().commit()

def _purge_stale_ci_cache(max_age_days: int = 30) -> None:
    cutoff = time.time() - max_age_days * 86400
    deleted = _get_conn().execute("DELETE FROM ci_cache WHERE stored_at < ?", (cutoff,)).rowcount
    _get_conn().commit()
    if deleted:
        log.info("Purged %d stale CI cache entries", deleted)

def get_prev_score(repo: str) -> int | None:
    row = _get_conn().execute("SELECT score FROM prev_scores WHERE repo = ?", (repo,)).fetchone()
    return row[0] if row else None

def set_prev_score(repo: str, score: int) -> None:
    _get_conn().execute("INSERT OR REPLACE INTO prev_scores(repo,score,stored_at) VALUES(?,?,?)", (repo, score, time.time()))
    _get_conn().commit()

# ─────────────────────────────────────────────────────────────────────────────
# GitHub HTTP helper
# ─────────────────────────────────────────────────────────────────────────────
def _is_rate_limit(exc: Exception) -> bool:
    resp = getattr(exc, "response", None)
    if resp is None:
        return False
    if resp.status_code in (403, 429):
        reset = resp.headers.get("X-RateLimit-Reset")
        if reset:
            wait = max(int(reset) - time.time(), 1)
            log.warning("Rate-limit. Sleeping %.0fs …", wait)
            time.sleep(wait)
        return True
    return False

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=60),
       retry=retry_if_exception(_is_rate_limit), before_sleep=before_sleep_log(log, logging.WARNING), reraise=True)
def gh_get(url: str, params: dict | None = None) -> dict | list:
    resp = requests.get(url, headers=GH_HEADERS, params=params, timeout=20)
    if resp.status_code == 422:
        log.warning("422 Unprocessable for %s", url)
        return {}
    resp.raise_for_status()
    return resp.json()

# ─────────────────────────────────────────────────────────────────────────────
# GitHub helpers
# ─────────────────────────────────────────────────────────────────────────────
def since_date(days: int = DAYS_BACK) -> str:
    return (_utcnow() - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

def search_repos(page: int = 1) -> list[dict]:
    """One search per topic, deduplicated."""
    cutoff = (_utcnow() - datetime.timedelta(days=DAYS_BACK * 4)).strftime("%Y-%m-%d")
    base   = f"created:>{cutoff} stars:>{MIN_STARS}"
    all_items: dict[int, dict] = {}
    for topic in (TOPICS if TOPICS else [""]):
        query = f"{base} topic:{topic}" if topic else base
        log.info("Search page %d topic=%s", page, topic or "any")
        try:
            data = gh_get("https://api.github.com/search/repositories",
                          params={"q": query, "sort": "stars", "order": "desc", "per_page": 30, "page": page})
            for item in (data.get("items", []) if isinstance(data, dict) else []):
                all_items.setdefault(item["id"], item)
        except Exception as exc:
            log.error("search_repos page %d topic %s: %s", page, topic, exc)
        time.sleep(0.5)
    log.info("search_repos: %d unique repos", len(all_items))
    return list(all_items.values())

def stars_gained(repo: dict) -> int:
    created = datetime.datetime.strptime(repo["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    age = max((_utcnow() - created).days, 1)
    return repo["stargazers_count"] if age <= DAYS_BACK else int(repo["stargazers_count"] * DAYS_BACK / age)

def forks_gained(repo: dict, age_days: int) -> int:
    fc = repo.get("forks_count", 0)
    return fc if age_days <= DAYS_BACK else int(fc * DAYS_BACK / age_days)

def get_readme_snippet(owner: str, repo: str) -> str:
    key = f"readme:{owner}/{repo}"
    cached = cache_get(key, README_TTL_DAYS * 86400)
    if cached:
        return cached
    try:
        data = gh_get(f"https://api.github.com/repos/{owner}/{repo}/readme")
        if isinstance(data, dict) and "content" in data:
            raw   = base64.b64decode(data["content"]).decode("utf-8")
            clean = re.sub(r"\n{2,}", "\n", raw)
            snippet = clean[:README_FETCH_CHARS] + ("…" if len(clean) > README_FETCH_CHARS else "")
            cache_set(key, snippet)
            return snippet
    except Exception as exc:
        log.warning("README failed %s/%s: %s", owner, repo, exc)
    return "No README found."

def get_comment_count(owner: str, repo: str) -> int:
    key = f"comments:{owner}/{repo}:{since_date()}"
    cached = cache_get(key, METRIC_TTL_HOURS * 3600)
    if cached is not None:
        return int(cached)
    try:
        ic = gh_get(f"https://api.github.com/repos/{owner}/{repo}/issues/comments", {"since": since_date(), "per_page": 100})
        pc = gh_get(f"https://api.github.com/repos/{owner}/{repo}/pulls/comments",  {"since": since_date(), "per_page": 100})
        human = lambda lst: sum(1 for c in lst if c.get("user", {}).get("type") != "Bot")
        count = human(ic if isinstance(ic, list) else []) + human(pc if isinstance(pc, list) else [])
        cache_set(key, str(count))
        return count
    except:
        return 0

def get_commit_count(owner: str, repo: str) -> int:
    key = f"commits:{owner}/{repo}:{since_date()}"
    cached = cache_get(key, METRIC_TTL_HOURS * 3600)
    if cached is not None:
        return int(cached)
    try:
        data  = gh_get(f"https://api.github.com/repos/{owner}/{repo}/commits", {"since": since_date(), "per_page": 100})
        count = len(data) if isinstance(data, list) else 0
        cache_set(key, str(count))
        return count
    except:
        return 0

def has_ci_workflow(owner: str, repo: str) -> bool:
    if SKIP_CI_CHECK:
        return True
    key    = f"{owner}/{repo}"
    cached = ci_cache_get(key)
    if cached is not None:
        return cached
    try:
        data   = gh_get("https://api.github.com/search/code", {"q": f"path:.github/workflows repo:{key}", "per_page": 1})
        result = isinstance(data, dict) and data.get("total_count", 0) > 0
        ci_cache_set(key, result)
        return result
    except:
        return False

def get_contributor_count(owner: str, repo: str) -> int:
    key    = f"contributors:{owner}/{repo}"
    cached = cache_get(key, METRIC_TTL_HOURS * 3600)
    if cached is not None:
        return int(cached)
    try:
        url  = f"https://api.github.com/repos/{owner}/{repo}/contributors"
        resp = requests.get(url, headers=GH_HEADERS, params={"per_page": 1}, timeout=20)
        if resp.status_code == 200 and "Link" in resp.headers:
            match = re.search(r'page=(\d+)>; rel="last"', resp.headers["Link"])  # FIX: re not _re
            count = int(match.group(1)) if match else 1
        else:
            data  = gh_get(url, params={"per_page": 100})
            count = len(data) if isinstance(data, list) else 0
        cache_set(key, str(count))
        return count
    except Exception as exc:
        log.warning("contributor_count %s/%s: %s", owner, repo, exc)
        return 1

def get_last_commit_days(owner: str, repo: str) -> int:
    key    = f"last_commit:{owner}/{repo}"
    cached = cache_get(key, METRIC_TTL_HOURS * 3600)
    if cached is not None:
        return int(cached)
    try:
        data = gh_get(f"https://api.github.com/repos/{owner}/{repo}/commits", params={"per_page": 1})
        if isinstance(data, list) and data:
            dt   = datetime.datetime.strptime(data[0]["commit"]["committer"]["date"], "%Y-%m-%dT%H:%M:%SZ")
            days = max((_utcnow() - dt).days, 0)
        else:
            days = 30
        cache_set(key, str(days))
        return days
    except:
        return 30

def get_avg_issue_response_hours(owner: str, repo: str) -> float:
    key    = f"issue_response:{owner}/{repo}"
    cached = cache_get(key, METRIC_TTL_HOURS * 3600)
    if cached is not None:
        return float(cached)
    try:
        issues = gh_get(f"https://api.github.com/repos/{owner}/{repo}/issues",
                        params={"state": "closed", "per_page": MAX_ISSUE_SAMPLES, "sort": "updated", "direction": "desc"})
        if not isinstance(issues, list) or not issues:
            return 48.0
        total, count = 0.0, 0
        for issue in issues:
            created = datetime.datetime.strptime(issue["created_at"], "%Y-%m-%dT%H:%M:%SZ")
            comments = gh_get(issue["comments_url"])
            if isinstance(comments, list) and comments:
                first = datetime.datetime.strptime(comments[0]["created_at"], "%Y-%m-%dT%H:%M:%SZ")
                total += (first - created).total_seconds() / 3600.0
                count += 1
        avg = round(total / count if count else 48.0, 1)
        cache_set(key, str(avg))
        return avg
    except:
        return 48.0

def get_forks_30d(owner: str, repo: str, current_forks: int) -> int:
    key  = f"forks_30d_snapshot:{owner}/{repo}"
    snap = cache_get(key, 30 * 86400)
    if snap is not None:
        return max(0, current_forks - int(snap))
    cache_set(key, str(current_forks))
    return 0

# ── New signals v4.0 ─────────────────────────────────────────────────────────

def get_pr_merge_rate(owner: str, repo: str) -> float:
    """Ratio of closed:open PRs. Higher = healthier project."""
    key    = f"pr_rate:{owner}/{repo}"
    cached = cache_get(key, METRIC_TTL_HOURS * 3600)
    if cached is not None:
        return float(cached)
    try:
        open_data   = gh_get(f"https://api.github.com/repos/{owner}/{repo}/pulls", params={"state": "open",   "per_page": 1})
        closed_data = gh_get(f"https://api.github.com/repos/{owner}/{repo}/pulls", params={"state": "closed", "per_page": 1})
        open_count   = 1 if isinstance(open_data,   list) and open_data   else 0
        closed_count = 1 if isinstance(closed_data, list) and closed_data else 0
        # Use Link header for total if available — fallback to list length
        def _count_from_resp(url, state):
            r = requests.get(url, headers=GH_HEADERS, params={"state": state, "per_page": 1}, timeout=15)
            if r.status_code == 200 and "Link" in r.headers:
                m = re.search(r'page=(\d+)>; rel="last"', r.headers["Link"])
                return int(m.group(1)) if m else 1
            items = r.json() if r.status_code == 200 else []
            return len(items) if isinstance(items, list) else 0
        open_n   = _count_from_resp(f"https://api.github.com/repos/{owner}/{repo}/pulls", "open")
        closed_n = _count_from_resp(f"https://api.github.com/repos/{owner}/{repo}/pulls", "closed")
        rate = round(closed_n / max(open_n, 1), 2)
        cache_set(key, str(rate))
        return rate
    except Exception as exc:
        log.warning("pr_merge_rate %s/%s: %s", owner, repo, exc)
        return 1.0

def get_has_tests(owner: str, repo: str) -> bool:
    """Check if repo has test files — quality signal."""
    key    = f"has_tests:{owner}/{repo}"
    cached = cache_get(key, README_TTL_DAYS * 86400)
    if cached is not None:
        return cached == "1"
    try:
        data   = gh_get("https://api.github.com/search/code",
                        {"q": f"repo:{owner}/{repo} path:test", "per_page": 1})
        result = isinstance(data, dict) and data.get("total_count", 0) > 0
        cache_set(key, "1" if result else "0")
        return result
    except:
        return False

def get_days_since_release(owner: str, repo: str) -> int:
    """Days since last GitHub release — active release cadence = healthy."""
    key    = f"release_days:{owner}/{repo}"
    cached = cache_get(key, METRIC_TTL_HOURS * 3600)
    if cached is not None:
        return int(cached)
    try:
        data = gh_get(f"https://api.github.com/repos/{owner}/{repo}/releases", params={"per_page": 1})
        if isinstance(data, list) and data and data[0].get("published_at"):
            dt   = datetime.datetime.strptime(data[0]["published_at"], "%Y-%m-%dT%H:%M:%SZ")
            days = max((_utcnow() - dt).days, 0)
        else:
            days = 90
        cache_set(key, str(days))
        return days
    except:
        return 90

# ── Spam filter ───────────────────────────────────────────────────────────────
_SPAM_PATTERNS = [
    "-skill", "skills", "awesome-", "trading-bot", "pump-",
    "tweet-fetcher", "pumpfun", "titanbot", "cangjie",
    "openclaw", "vibe-skill", "geo-skill", "taste-skill",
]

def _is_relevant(repo: dict) -> bool:
    combined = f"{repo.get('name','').lower()} {(repo.get('description') or '').lower()}"
    return not any(pat in combined for pat in _SPAM_PATTERNS)

# ── Category assignment ───────────────────────────────────────────────────────
def assign_category(tool: dict) -> str:
    text = f"{tool.get('description') or ''} {' '.join(tool.get('topics') or [])} {tool.get('language') or ''}".lower()
    rules = [
        ("crm",            "CRM"),
        (" erp ",          "ERP"),          # FIX: spaces prevent false match
        ("automation",     "Automation"),
        ("workflow",       "Automation"),
        (" bi ",           "Analytics/BI"), # FIX: spaces prevent false match on "bi" substring
        ("analytics",      "Analytics/BI"),
        ("dashboard",      "Analytics/BI"),
        ("low-code",       "Low-code"),
        ("nocode",         "Low-code"),
        ("database",       "Database"),
        (" data ",         "Database"),
        ("devops",         "DevOps"),
        ("ci/cd",          "DevOps"),
        ("llm",            "AI/ML"),
        ("machine learning","AI/ML"),
        (" ml ",           "AI/ML"),
        (" ai ",           "AI/ML"),
    ]
    for kw, cat in rules:
        if kw in text:
            return cat
    return "Other"

# ── Legacy scoring ────────────────────────────────────────────────────────────
def compute_score(stars_7d: int, forks_7d: int, comments: int, commits: int, ci: bool) -> float:
    return stars_7d * 0.5 + forks_7d * 2.0 + comments * 1.5 + commits * 1.0 + (10.0 if ci else 0.0)

# ── Telegram ──────────────────────────────────────────────────────────────────
_MDV2 = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")
def escape_mdv2(t: str) -> str:
    return _MDV2.sub(r"\\\1", t)

def send_telegram(text: str, parse_mode: str = "") -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chunk in [text[i:i+4096] for i in range(0, len(text), 4096)]:
        resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT, "text": chunk, "parse_mode": parse_mode, "disable_web_page_preview": True}, timeout=20)
        if not resp.ok:
            log.warning("Telegram failed: %s", resp.text[:200])
        time.sleep(0.5)

# ── GPT digest + idea synthesis ───────────────────────────────────────────────
def synthesise_idea(repos: list[dict]) -> dict:
    summaries = [f"• {r['full_name']} — {r.get('description','no desc')} [stars:{r['stargazers_count']:,} topics:{','.join(r.get('topics',[])[:4]) or 'none'}]" for r in repos]
    prompt = f"""You are a world-class venture technologist. Below are top trending GitHub repositories:

{chr(10).join(summaries)}

Return ONLY a JSON object with these exact keys:
{{"title":"Short punchy product name (≤6 words)","tagline":"One-line value proposition (≤12 words)","problem":"Crisp problem description (≤2 sentences)","solution":"What the product does (≤2 sentences)","audience":"Precise target user (≤1 sentence)","moat":"Why defensible in 18 months (≤1 sentence)","flowchart_steps":["Step A","Step B","Step C","Step D"]}}"""
    try:
        resp = ai.chat.completions.create(model="gpt-4o-mini", messages=[{"role":"user","content":prompt}], temperature=0.7, max_tokens=600, response_format={"type":"json_object"})
        return json.loads(resp.choices[0].message.content.strip())
    except:
        return {"title":"Trend-Derived Idea","tagline":"Synthesised from this week's signals","problem":"See digest.","solution":"Cross-pollinate the top repos.","audience":"Developers","moat":"Data flywheel","flowchart_steps":["Discover","Validate","Build","Grow"]}

def gpt_digest(repos: list[dict]) -> str:
    today = _utcnow().strftime("%d %b %Y")
    blocks = []
    for i, r in enumerate(repos, 1):
        blocks.append(f"#{i} {r['full_name']} — {r.get('description','no desc')}\n  stars:{r['stargazers_count']:,} | bizops_score:{r.get('bizops_score',0)} | topics:{','.join(r.get('topics',[])[:4]) or 'none'}\n  README: {r.get('readme_snippet','')[:README_PROMPT_CHARS]}\n  url: {r['html_url']}")
    prompt = f"Write a concise GitHub intelligence digest for {today}. For each repo: name, one sharp technical insight, key topics. End with a 2-sentence weekly summary.\n\n{'='*40}\n{chr(10).join(blocks)}"
    try:
        resp = ai.chat.completions.create(model="gpt-4o-mini", messages=[{"role":"user","content":prompt}], temperature=0.35, max_tokens=2800)
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        log.warning("gpt_digest failed: %s", exc)
        return f"GitHub Intelligence Digest — {today}\n\n" + "\n".join(f"• {r['full_name']}: {r.get('description','')}" for r in repos)

# ── Beehiiv ───────────────────────────────────────────────────────────────────
def post_beehiiv_draft(subject: str, body_html: str) -> None:
    api_key = os.getenv("BEEHIIV_API_KEY")
    pub_id  = os.getenv("BEEHIIV_PUB_ID")
    if not api_key or not pub_id:
        log.warning("Beehiiv credentials missing")
        return
    try:
        resp = requests.post(
            f"https://api.beehiiv.com/v2/publications/{pub_id}/posts",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"title": subject, "body_html": body_html, "status": "draft", "is_public": False},
            timeout=30)
        if resp.status_code in (200, 201):
            log.info("Beehiiv draft created: %s", resp.json().get("id","ok"))
        else:
            log.error("Beehiiv failed: %s %s", resp.status_code, resp.text[:200])
    except Exception as e:
        log.error("Beehiiv exception: %s", e)

def build_full_digest_html(tools: list[dict], generated_at: str) -> str:
    rows = "".join(f'<tr><td><a href="{t.get("html_url","#")}" style="color:#b8821e;font-weight:600">{t.get("name","")}</a></td><td>{(t.get("description") or "")[:100]}</td><td style="text-align:center">{t.get("bizops_score",0)}</td><td>⭐ {t.get("stargazers_count",0):,}</td></tr>' for t in tools[:50])
    return f'<h2>BizOps Full Digest – {generated_at[:10]}</h2><p>{len(tools)} tools ranked.</p><table style="width:100%;border-collapse:collapse"><thead><tr style="background:#f4f4f8"><th>Tool</th><th>Description</th><th>Score</th><th>Stars</th></tr></thead><tbody>{rows}</tbody></table>'

# ─────────────────────────────────────────────────────────────────────────────
# Website output helpers
# ─────────────────────────────────────────────────────────────────────────────
def _slugify(name: str) -> str:
    slug = name.split("/")[-1]
    slug = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")
    return slug or "tool"

def _to_public_tool(t: dict) -> dict:
    return {
        "name":              t.get("name") or t.get("full_name","").split("/")[-1],
        "full_name":         t.get("full_name",""),
        "description":       (t.get("description") or "")[:200],
        "github_url":        t.get("html_url",""),
        "language":          t.get("language",""),
        "topics":            t.get("topics",[])[:6],
        "stars":             t.get("stargazers_count") or t.get("stars",0),
        "forks_30d":         t.get("forks_30d",0),
        "last_commit_days":  t.get("last_commit_days",0),
        "bizops_score":      t.get("bizops_score",0),
        "trend_direction":   t.get("trend_direction","stable"),
        "category":          t.get("category","Other"),
        "slug":              _slugify(t.get("full_name", t.get("name","tool"))),
        "has_tests":         t.get("has_tests", False),
        "days_since_release":t.get("days_since_release", 90),
        "pr_merge_rate":     t.get("pr_merge_rate", 1.0),
    }

# ── Tool page template ────────────────────────────────────────────────────────
_TOOL_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} Review 2026 — BizOps Score {score}/100, {stars} Stars | BizOpsTool</title>
<meta name="description" content="{name} is an open-source {category} tool with {stars} GitHub stars and a BizOps Score of {score}/100. Last commit {last_commit_days} days ago. {description}">
<link rel="canonical" href="{site_url}/tools/{slug}.html">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{{--ink:#0d0d12;--ink-2:#1c1c27;--stone:#5c5c72;--fog:#8e8ea8;--mist:#b8b8cc;--veil:#e2e2e8;--paper:#ffffff;--snow:#fafafc;--white:#ffffff;--gold:#c49a2a;--gold-light:#d9b04a;--gold-bg:rgba(196,154,42,0.10);--gold-bd:rgba(196,154,42,0.26);--green:#1e7b4e;--red:#c2412c;--serif:'Instrument Serif',Georgia,serif;--sans:'Geist',-apple-system,sans-serif;--mono:'Geist Mono','SF Mono',monospace;--shadow-sm:0 1px 2px rgba(0,0,0,0.04),0 1px 4px rgba(0,0,0,0.02);--shadow-md:0 4px 8px rgba(0,0,0,0.05),0 2px 4px rgba(0,0,0,0.03);--radius-card:10px}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--paper);color:var(--ink);font-family:var(--sans);font-size:15px;line-height:1.65;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:820px;margin:0 auto;padding:0 24px}}
header{{position:sticky;top:0;z-index:100;background:rgba(255,255,255,.92);backdrop-filter:blur(18px);border-bottom:1px solid var(--veil)}}
.header-inner{{max-width:1040px;margin:0 auto;padding:0 28px;height:60px;display:flex;align-items:center;justify-content:space-between}}
.logo{{font-family:var(--mono);font-size:13px;font-weight:600;letter-spacing:.1em;color:var(--ink-2);text-decoration:none}}
.logo em{{font-style:normal;color:var(--gold)}}
nav{{display:flex;align-items:center;gap:24px}}
nav a{{font-size:13px;font-weight:500;color:var(--stone);text-decoration:none;padding:6px 12px;border-radius:7px;transition:background .15s,color .15s}}
nav a:hover{{background:var(--paper);color:var(--ink)}}
nav a.active{{font-weight:600;color:var(--ink);border-bottom:2px solid var(--gold)}}
.nav-cta{{font-family:var(--mono)!important;font-size:11px!important;font-weight:600!important;background:var(--ink)!important;color:#fff!important;padding:8px 16px!important;border-radius:8px!important;margin-left:8px;transition:background .18s!important}}
.nav-cta:hover{{background:var(--gold)!important}}
.page-hero{{background:var(--white);padding:52px 0 40px;border-bottom:1px solid var(--veil)}}
.eyebrow{{font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:.16em;text-transform:uppercase;color:var(--gold);margin-bottom:14px;display:block}}
h1{{font-family:var(--serif);font-size:clamp(32px,5vw,52px);font-weight:400;line-height:1.1;letter-spacing:-.01em;color:var(--ink);margin-bottom:10px}}
h1 em{{font-style:italic;color:var(--gold-light)}}
.score-display{{display:inline-flex;align-items:center;gap:12px;margin:16px 0}}
.score-num{{font-family:var(--serif);font-style:italic;font-size:56px;line-height:1;color:var(--gold)}}
.score-label{{font-family:var(--mono);font-size:10px;color:var(--fog);letter-spacing:.12em;text-transform:uppercase}}
.trend-badge{{font-family:var(--mono);font-size:10px;padding:3px 10px;border-radius:4px;border:1px solid var(--veil);color:var(--stone)}}
.trend-badge.rising{{background:rgba(30,123,78,.08);color:var(--green);border-color:rgba(30,123,78,.2)}}
.desc{{font-size:15px;color:var(--stone);margin-bottom:24px;max-width:580px;line-height:1.7}}
.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--veil);border:1px solid var(--veil);border-radius:var(--radius-card);overflow:hidden;margin:24px 0;box-shadow:var(--shadow-sm)}}
.stat{{background:var(--white);padding:20px 16px}}
.stat-label{{font-family:var(--mono);font-size:9.5px;color:var(--fog);text-transform:uppercase;letter-spacing:.1em;margin-bottom:6px}}
.stat-val{{font-family:var(--serif);font-style:italic;font-size:28px;line-height:1;color:var(--ink)}}
.signals{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:20px 0}}
.signal{{background:var(--white);border:1px solid var(--veil);border-radius:12px;padding:16px;text-align:center;box-shadow:var(--shadow-sm)}}
.signal-icon{{font-size:20px;margin-bottom:6px}}
.signal-label{{font-family:var(--mono);font-size:9px;color:var(--fog);text-transform:uppercase;letter-spacing:.08em}}
.signal-val{{font-size:13px;font-weight:600;margin-top:4px;color:var(--ink)}}
.tags{{display:flex;flex-wrap:wrap;gap:6px;margin:16px 0}}
.tag{{font-family:var(--mono);font-size:10px;padding:3px 10px;border:1px solid var(--veil);color:var(--fog);border-radius:4px;background:var(--snow)}}
.cta{{background:var(--white);border:1px solid var(--veil);border-radius:var(--radius-card);padding:28px;margin-top:32px;text-align:center;box-shadow:var(--shadow-sm)}}
.cta p{{color:var(--stone);font-size:14px;margin-bottom:20px}}
.btn-row{{display:flex;justify-content:center;gap:10px;flex-wrap:wrap}}
.btn{{display:inline-block;text-decoration:none;font-family:var(--mono);font-size:11px;font-weight:600;padding:10px 22px;border-radius:8px;transition:all .18s}}
.btn-gh{{border:1px solid var(--veil);color:var(--ink);background:var(--paper)}}
.btn-gh:hover{{border-color:var(--ink);background:var(--white)}}
.btn-sub{{background:var(--ink);color:#fff;border:1px solid var(--ink)}}
.btn-sub:hover{{background:var(--gold);border-color:var(--gold)}}
.btn-cloud{{background:var(--gold-bg);color:var(--gold);border:1px solid var(--gold-bd)}}
.btn-cloud:hover{{background:var(--gold);color:#fff}}
.compare-link{{margin-top:14px;display:block;font-family:var(--mono);font-size:11px;color:var(--mist);text-decoration:none}}
.compare-link:hover{{color:var(--gold)}}
footer{{margin-top:56px;padding:32px 0;border-top:1px solid var(--veil);background:var(--white)}}
.footer-inner{{max-width:1040px;margin:0 auto;padding:0 28px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:16px}}
.footer-brand,.footer-links a{{font-family:var(--mono);font-size:11px;color:var(--mist);text-decoration:none}}
.footer-links{{display:flex;gap:20px;flex-wrap:wrap}}
.footer-links a:hover{{color:var(--gold)}}
@media(max-width:768px){{.stats,.signals{{grid-template-columns:1fr}}nav a:not(.nav-cta){{display:none}}}}
</style>
</head>
<body>
<header><div class="header-inner">
  <a class="logo" href="/">BIZOPS<em>TOOL</em></a>
  <nav>
    <a href="/tools.html">Tools</a>
    <a href="/compare.html">Compare</a>
    <a href="/pricing.html">Pricing</a>
    <a href="/stack-grader.html" class="nav-cta">GRADE STACK →</a>
  </nav>
</div></header>
<div style="background:var(--white)"><div class="wrap">
  <div class="page-hero">
    <span class="eyebrow">{category}</span>
    <h1>{name}</h1>
    <div class="score-display">
      <div>
        <div class="score-label">BizOps Score</div>
        <div class="score-num">{score}</div>
      </div>
      <span class="trend-badge">{trend_label}</span>
    </div>
    <p class="desc">{description}</p>
  </div>
</div></div>
<div class="wrap" style="padding:28px 24px 64px">
  <div class="stats">
    <div class="stat"><div class="stat-label">GitHub Stars</div><div class="stat-val">{stars}</div></div>
    <div class="stat"><div class="stat-label">Forks / 30d</div><div class="stat-val">{forks_30d}</div></div>
    <div class="stat"><div class="stat-label">Last Commit</div><div class="stat-val">{last_commit_days}d ago</div></div>
  </div>
  <div class="signals">
    <div class="signal"><div class="signal-icon">{tests_icon}</div><div class="signal-label">Tests</div><div class="signal-val">{tests_val}</div></div>
    <div class="signal"><div class="signal-icon">{release_icon}</div><div class="signal-label">Last Release</div><div class="signal-val">{release_val}</div></div>
    <div class="signal"><div class="signal-icon">🔀</div><div class="signal-label">PR Health</div><div class="signal-val">{pr_val}</div></div>
  </div>
  <div class="tags">{tags_html}</div>
  <div class="cta">
    <p>Get the full weekly BizOps digest — 100+ tools ranked every Monday.</p>
    <div class="btn-row">
      <a class="btn btn-gh" href="{github_url}" target="_blank" rel="noopener">View on GitHub</a>
      {affiliate_btn}
      <a class="btn btn-sub" href="{site_url}/#signup">Subscribe free</a>
    </div>
    <a class="compare-link" href="{site_url}/compare.html?a={slug}">Compare with another tool →</a>
  </div>
</div><!-- /wrap -->
<footer><div class="footer-inner">
  <div class="footer-brand">© 2026 BizOpsTool · Updated {generated_at}</div>
  <div class="footer-links">
    <a href="/">Home</a>
    <a href="/tools.html">All Tools</a>
    <a href="/categories/{cat_slug}.html">{category}</a>
    <a href="/compare.html">Compare</a>
    <a href="/score-methodology.html">Methodology</a>
    <a href="/pricing.html">Pricing</a>
  </div>
</div></footer>
</body>
</html>"""

def _trend_label(direction: str) -> str:
    return {"rising":"↑ Rising","falling":"↓ Falling","new":"★ New"}.get(direction,"→ Stable")

def _cat_slug(cat: str) -> str:
    return cat.lower().replace("/","-").replace(" ","-")

def generate_tool_pages(tools: list[dict], generated_at: str) -> None:
    tools_dir = os.path.join(DOCS_DIR, "tools")
    os.makedirs(tools_dir, exist_ok=True)
    count = 0
    for t in tools:
        pub   = _to_public_tool(t)
        slug  = pub["slug"]
        name_lower = pub["name"].lower()

        tags_html    = "".join(f'<span class="tag">{tp}</span>' for tp in pub["topics"]) or '<span class="tag">open-source</span>'
        stars_str    = f"{pub['stars']:,}" if isinstance(pub["stars"], int) else str(pub["stars"])
        affiliate_btn = ""
        if name_lower in AFFILIATE_LINKS:
            affiliate_btn = f'<a class="btn btn-cloud" href="{AFFILIATE_LINKS[name_lower]}" target="_blank" rel="noopener">Try {pub["name"]} Cloud →</a>'

        tests_icon = "✅" if pub.get("has_tests") else "❌"
        tests_val  = "Present" if pub.get("has_tests") else "None found"
        dsr        = pub.get("days_since_release", 90)
        release_icon = "🟢" if dsr < 30 else ("🟡" if dsr < 90 else "🔴")
        release_val  = f"{dsr}d ago" if dsr < 90 else "No releases"
        pr_rate      = pub.get("pr_merge_rate", 1.0)
        pr_val       = f"{pr_rate:.1f}x" if pr_rate else "N/A"

        page = _TOOL_PAGE_TEMPLATE.format(
            name=pub["name"], score=pub["bizops_score"],
            description=pub["description"] or "An open-source BizOps tool.",
            slug=slug, site_url=SITE_BASE_URL,
            stars=stars_str, forks_30d=pub["forks_30d"],
            last_commit_days=pub["last_commit_days"],
            tags_html=tags_html, github_url=pub["github_url"],
            generated_at=generated_at[:10],
            category=pub["category"], cat_slug=_cat_slug(pub["category"]),
            trend_label=_trend_label(pub["trend_direction"]),
            tests_icon=tests_icon, tests_val=tests_val,
            release_icon=release_icon, release_val=release_val,
            pr_val=pr_val, affiliate_btn=affiliate_btn,
        )
        with open(os.path.join(tools_dir, f"{slug}.html"), "w") as f:
            f.write(page)
        count += 1
    log.info("Generated %d tool pages", count)

# ── Category pillar pages ─────────────────────────────────────────────────────
_CATEGORY_META = {
    "CRM":          ("Best Open Source CRM Tools 2026", "customer relationship management, sales, contacts"),
    "ERP":          ("Best Open Source ERP Tools 2026", "enterprise resource planning, business management"),
    "Automation":   ("Best Open Source Automation Tools 2026", "workflow automation, task automation, n8n, zapier alternative"),
    "Analytics/BI": ("Best Open Source Analytics & BI Tools 2026", "business intelligence, dashboards, data visualization"),
    "Low-code":     ("Best Open Source Low-Code Tools 2026", "no-code, low-code, app builder, visual development"),
    "Database":     ("Best Open Source Database Tools 2026", "database, data management, SQL, NoSQL"),
    "DevOps":       ("Best Open Source DevOps Tools 2026", "CI/CD, deployment, infrastructure, developer tools"),
    "AI/ML":        ("Best Open Source AI & ML Tools 2026", "artificial intelligence, machine learning, LLM, AI agents"),
    "Other":        ("Trending Open Source Business Tools 2026", "open source tools, business software, productivity"),
}

def generate_category_pages(tools: list[dict], generated_at: str) -> None:
    cats_dir = os.path.join(DOCS_DIR, "categories")
    os.makedirs(cats_dir, exist_ok=True)

    by_cat: dict[str, list] = defaultdict(list)
    for t in tools:
        by_cat[t.get("category","Other")].append(t)

    for cat, cat_tools in by_cat.items():
        cat_tools_sorted = sorted(cat_tools, key=lambda x: x.get("bizops_score",0), reverse=True)
        slug    = _cat_slug(cat)
        meta    = _CATEGORY_META.get(cat, (f"Best Open Source {cat} Tools 2026", cat.lower()))
        title   = meta[0]
        kws     = meta[1]

        cards = ""
        for i, t in enumerate(cat_tools_sorted):
            pub  = _to_public_tool(t)
            sc   = "hi" if pub["bizops_score"] >= 70 else "mid" if pub["bizops_score"] >= 40 else "lo"
            trend_raw = pub.get("trend_direction","stable")
            trend_map = {"rising":"↑ Rising","falling":"↓ Falling","new":"★ New"}
            cards += f"""<a class="tool-card" href="/tools/{pub['slug']}.html">
              <div class="card-top"><span class="rank">#{i+1}</span><span class="score {sc}">{pub['bizops_score']}</span></div>
              <div class="card-name">{pub['name']}</div>
              <div class="card-desc">{pub['description'][:110]}</div>
              <div class="card-foot">
                <span class="trend {trend_raw}">{trend_map.get(trend_raw,'→ Stable')}</span>
                <span class="stars">★ {pub['stars']:,}</span>
              </div>
            </a>"""

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} | BizOpsTool</title>
<meta name="description" content="Compare the best open-source {cat} tools in 2026. Ranked by BizOps Score using GitHub stars, commit activity, issue response time and more. {len(cat_tools_sorted)} tools compared.">
<meta name="keywords" content="{kws}">
<link rel="canonical" href="{SITE_BASE_URL}/categories/{slug}.html">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{{--ink:#0d0d12;--ink-2:#1c1c27;--stone:#5c5c72;--fog:#8e8ea8;--mist:#b8b8cc;--veil:#e2e2e8;--paper:#ffffff;--snow:#fafafc;--white:#fff;--gold:#c49a2a;--gold-light:#d9b04a;--gold-bg:rgba(196,154,42,0.10);--gold-bd:rgba(196,154,42,0.26);--green:#1e7b4e;--green-bg:rgba(30,123,78,0.08);--red:#c2412c;--serif:'Instrument Serif',Georgia,serif;--sans:'Geist',-apple-system,sans-serif;--mono:'Geist Mono','SF Mono',monospace;--radius-card:10px;--shadow-sm:0 1px 2px rgba(0,0,0,0.04),0 1px 4px rgba(0,0,0,0.02);--shadow-md:0 4px 8px rgba(0,0,0,0.05),0 2px 4px rgba(0,0,0,0.03)}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--paper);color:var(--ink);font-family:var(--sans);font-size:15px;line-height:1.6}}
.wrap{{max-width:1040px;margin:0 auto;padding:0 28px}}
header{{position:sticky;top:0;z-index:100;background:rgba(255,255,255,.9);backdrop-filter:blur(18px);border-bottom:1px solid var(--veil)}}
.header-inner{{max-width:1040px;margin:0 auto;padding:0 28px;height:60px;display:flex;align-items:center;justify-content:space-between}}
.logo{{font-family:var(--mono);font-size:13px;font-weight:600;letter-spacing:.1em;color:var(--ink);text-decoration:none}}
.logo em{{font-style:normal;color:var(--gold)}}
nav a{{font-family:var(--sans);font-size:13px;font-weight:500;color:var(--stone);text-decoration:none;padding:6px 12px;border-radius:7px;transition:background .15s,color .15s}}nav a:hover{{background:var(--paper);color:var(--ink)}}nav a.active{{font-weight:600;color:var(--ink);border-bottom:2px solid var(--gold)}}
nav a:hover{{background:var(--paper)}}
.nav-cta{{font-family:var(--mono)!important;font-size:11px!important;font-weight:600!important;background:var(--ink)!important;color:#fff!important;padding:8px 16px!important;border-radius:8px!important;margin-left:8px}}
.nav-cta:hover{{background:var(--gold)!important}}
.page-hero{{background:var(--white);padding:48px 0 36px;border-bottom:1px solid var(--veil);margin-bottom:32px}}
.breadcrumb{{font-family:var(--mono);font-size:11px;color:var(--fog);margin-bottom:12px}}
.breadcrumb a{{color:var(--fog);text-decoration:none}}
.breadcrumb a:hover{{color:var(--gold)}}
h1{{font-family:var(--serif);font-size:clamp(32px,5vw,52px);font-weight:400;line-height:1.1;margin-bottom:10px}}
.sub{{font-size:15px;color:var(--stone);max-width:540px;line-height:1.6;margin-bottom:16px}}
.count-badge{{display:inline-block;font-family:var(--mono);font-size:10px;padding:3px 10px;border-radius:4px;background:var(--gold-bg);color:var(--gold);border:1px solid var(--gold-bd)}}
.tools-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px;padding-bottom:64px}}
.tool-card{{background:var(--white);border:1px solid var(--veil);border-radius:14px;padding:22px;text-decoration:none;color:inherit;display:block;transition:transform .2s,box-shadow .2s,border-color .2s;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
.tool-card:hover{{transform:translateY(-3px);box-shadow:0 12px 32px rgba(0,0,0,.1);border-color:var(--gold-bd)}}
.card-top{{display:flex;justify-content:space-between;margin-bottom:12px}}
.rank{{font-family:var(--serif);font-style:italic;font-size:20px;color:var(--veil)}}
.tool-card:hover .rank{{color:var(--gold)}}
.score{{font-family:var(--serif);font-style:italic;font-size:34px;line-height:1}}
.score.hi{{color:var(--gold)}}.score.mid{{color:#b5821a}}.score.lo{{color:var(--red)}}
.card-name{{font-size:17px;font-weight:700;letter-spacing:-.02em;margin-bottom:6px}}
.card-desc{{font-size:13px;color:var(--stone);line-height:1.5;margin-bottom:12px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}}
.card-foot{{display:flex;justify-content:space-between;font-family:var(--mono);font-size:10px;color:var(--mist);padding-top:12px;border-top:1px solid var(--paper)}}
.trend.rising{{color:var(--green)}}.trend.falling{{color:var(--red)}}.trend.new{{color:var(--gold)}}
.other-cats{{background:var(--white);border-top:1px solid var(--veil);padding:40px 0}}
.other-cats-inner{{max-width:1040px;margin:0 auto;padding:0 28px}}
.other-cats h2{{font-family:var(--serif);font-size:24px;font-weight:400;margin-bottom:16px}}
.cat-links{{display:flex;flex-wrap:wrap;gap:8px}}
.cat-link{{font-family:var(--mono);font-size:11px;padding:5px 14px;border:1px solid var(--veil);border-radius:7px;color:var(--stone);text-decoration:none;transition:all .15s}}
.cat-link:hover{{border-color:var(--gold-bd);color:var(--gold);background:var(--gold-bg)}}
footer{{padding:32px 0;border-top:1px solid var(--veil);background:var(--white)}}
.footer-inner{{max-width:1040px;margin:0 auto;padding:0 28px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:16px}}
.footer-brand{{font-family:var(--mono);font-size:11px;color:var(--mist)}}
.footer-links{{display:flex;gap:24px}}
.footer-links a{{font-family:var(--mono);font-size:11px;color:var(--mist);text-decoration:none}}
.footer-links a:hover{{color:var(--gold)}}
@media(max-width:720px){{.tools-grid{{grid-template-columns:1fr}}nav a:not(.nav-cta){{display:none}}}}
</style>
</head>
<body>
<header><div class="header-inner">
  <a class="logo" href="/">BIZOPS<em>TOOL</em></a>
  <nav>
    <a href="/tools.html">All tools</a>
    <a href="/compare.html">Compare</a>
    <a href="/stack-grader.html" class="nav-cta">GRADE MY STACK →</a>
  </nav>
</div></header>

<div style="background:var(--white)"><div class="wrap">
  <div class="page-hero">
    <div class="breadcrumb"><a href="/">Home</a> / <a href="/tools.html">Tools</a> / {cat}</div>
    <h1>{title}</h1>
    <p class="sub">Compare {len(cat_tools_sorted)} open-source {cat} tools ranked by BizOps Score — a composite of GitHub stars, commit activity, issue response time, contributor health, and more.</p>
    <span class="count-badge">{len(cat_tools_sorted)} tools · Updated {generated_at[:10]}</span>
  </div>
</div></div>

<div class="wrap">
  <div class="tools-grid">{cards}</div>
</div>

<div class="other-cats"><div class="other-cats-inner">
  <h2>Browse other categories</h2>
  <div class="cat-links">
    {" ".join(f'<a class="cat-link" href="/categories/{_cat_slug(c)}.html">{c}</a>' for c in _CATEGORY_META if c != cat)}
  </div>
</div></div>

<footer><div class="footer-inner">
  <div class="footer-brand">© 2026 BizOpsTool · Built with a bot, scored with data.</div>
  <div class="footer-links">
    <a href="/score-methodology.html">Methodology</a>
    <a href="{GITHUB_REPO_URL}" target="_blank">GitHub</a>
    <a href="https://bizopstool.beehiiv.com" target="_blank">Newsletter</a>
  </div>
</div></footer>
</body></html>"""

        with open(os.path.join(cats_dir, f"{slug}.html"), "w", encoding="utf-8") as f:
            f.write(html)

    log.info("Generated %d category pages", len(by_cat))

# ── Sitemap ───────────────────────────────────────────────────────────────────
def generate_sitemap(tools: list[dict], generated_at: str) -> None:
    today = generated_at[:10]
    by_cat: dict[str, list] = defaultdict(list)
    for t in tools:
        by_cat[t.get("category","Other")].append(t)

    urls = [
    f'  <url><loc>{SITE_BASE_URL}/</loc><lastmod>{today}</lastmod><changefreq>daily</changefreq><priority>1.0</priority></url>',
    f'  <url><loc>{SITE_BASE_URL}/tools.html</loc><lastmod>{today}</lastmod><changefreq>daily</changefreq><priority>0.9</priority></url>',
    f'  <url><loc>{SITE_BASE_URL}/compare.html</loc><lastmod>{today}</lastmod><changefreq>weekly</changefreq><priority>0.8</priority></url>',
    f'  <url><loc>{SITE_BASE_URL}/stack-grader.html</loc><lastmod>{today}</lastmod><changefreq>weekly</changefreq><priority>0.8</priority></url>',
    f'  <url><loc>{SITE_BASE_URL}/savings-calculator.html</loc><lastmod>{today}</lastmod><changefreq>weekly</changefreq><priority>0.8</priority></url>',
    f'  <url><loc>{SITE_BASE_URL}/about.html</loc><lastmod>{today}</lastmod><changefreq>monthly</changefreq><priority>0.7</priority></url>',
    f'  <url><loc>{SITE_BASE_URL}/pricing.html</loc><lastmod>{today}</lastmod><changefreq>monthly</changefreq><priority>0.7</priority></url>',
    f'  <url><loc>{SITE_BASE_URL}/score-methodology.html</loc><lastmod>{today}</lastmod><changefreq>monthly</changefreq><priority>0.6</priority></url>',
    f'  <url><loc>{SITE_BASE_URL}/privacy-policy.html</loc><lastmod>{today}</lastmod><changefreq>monthly</changefreq><priority>0.3</priority></url>',
    f'  <url><loc>{SITE_BASE_URL}/terms-of-service.html</loc><lastmod>{today}</lastmod><changefreq>monthly</changefreq><priority>0.3</priority></url>',
]
    for cat in by_cat:
        urls.append(f'  <url><loc>{SITE_BASE_URL}/categories/{_cat_slug(cat)}.html</loc><lastmod>{today}</lastmod><changefreq>daily</changefreq><priority>0.8</priority></url>')
    for t in tools:
        slug = _slugify(t.get("full_name", t.get("name","tool")))
        urls.append(f'  <url><loc>{SITE_BASE_URL}/tools/{slug}.html</loc><lastmod>{today}</lastmod><changefreq>weekly</changefreq><priority>0.5</priority></url>')

    sitemap = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + "\n".join(urls) + "\n</urlset>"
    with open(os.path.join(DOCS_DIR, "sitemap.xml"), "w") as f:
        f.write(sitemap)
    log.info("Generated sitemap: %d URLs", len(urls))

# ── All-tools page ────────────────────────────────────────────────────────────
def generate_all_tools_page(tools: list[dict], generated_at: str) -> None:
    categories = sorted(set(t.get("category","Other") for t in tools))
    cat_opts   = "\n".join(f'<option value="{c}">{c}</option>' for c in categories)
    rows = []
    for i, t in enumerate(tools):
        pub       = _to_public_tool(t)
        sc        = "hi" if pub["bizops_score"] >= 70 else "mid" if pub["bizops_score"] >= 40 else "lo"
        trend_raw = pub.get("trend_direction","stable")
        trend_map = {"rising":"↑ Rising","falling":"↓ Falling","new":"★ New"}
        rows.append(f"""<div class="tool-card" data-category="{pub['category']}" data-score="{pub['bizops_score']}" data-stars="{pub['stars']}">
<a href="/tools/{pub['slug']}.html" style="text-decoration:none;color:inherit;display:block">
  <div class="card-top"><div class="card-rank">#{i+1}</div><span class="card-score {sc}">{pub['bizops_score']}</span></div>
  <div class="card-name">{pub['name']}</div>
  <div class="card-desc">{pub['description'][:120]}</div>
  <div class="card-footer">
    <span class="tag cat">{pub['category']}</span>
    <div class="card-stats">
      <span class="stat-trend {trend_raw}">{trend_map.get(trend_raw,'→ Stable')}</span>
      <span>★ {pub['stars']:,}</span>
    </div>
  </div>
</a></div>""")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>All Open Source BizOps Tools 2026 — {len(tools)} Ranked | BizOpsTool</title>
<meta name="description" content="Browse {len(tools)} open-source business tools ranked by BizOps Score. Filter by category, sort by score or stars. Updated daily.">
<link rel="canonical" href="{SITE_BASE_URL}/tools.html">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{{--ink:#0d0d12;--ink-2:#1c1c27;--stone:#5c5c72;--fog:#8e8ea8;--mist:#b8b8cc;--veil:#e2e2e8;--paper:#ffffff;--snow:#fafafc;--white:#fff;--gold:#c49a2a;--gold-light:#d9b04a;--gold-bg:rgba(196,154,42,0.10);--gold-bd:rgba(196,154,42,0.26);--green:#1e7b4e;--red:#c2412c;--serif:'Instrument Serif',Georgia,serif;--sans:'Geist',-apple-system,sans-serif;--mono:'Geist Mono','SF Mono',monospace;--radius:8px;--radius-card:10px;--shadow-sm:0 1px 2px rgba(0,0,0,0.04),0 1px 4px rgba(0,0,0,0.02);--shadow-md:0 4px 8px rgba(0,0,0,0.05),0 2px 4px rgba(0,0,0,0.03)}}
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--paper);color:var(--ink);font-family:var(--sans);font-size:15px;line-height:1.6}}
.wrap{{max-width:1040px;margin:0 auto;padding:0 28px}}
header{{position:sticky;top:0;z-index:100;background:rgba(255,255,255,.9);backdrop-filter:blur(18px);border-bottom:1px solid var(--veil)}}
.header-inner{{max-width:1040px;margin:0 auto;padding:0 28px;height:60px;display:flex;align-items:center;justify-content:space-between}}
.logo{{font-family:var(--mono);font-size:13px;font-weight:600;letter-spacing:.1em;color:var(--ink-2);text-decoration:none}}
.logo em{{font-style:normal;color:var(--gold)}}
nav{{display:flex;align-items:center;gap:24px}}
nav a{{font-family:var(--sans);font-size:13px;font-weight:500;color:var(--stone);text-decoration:none;padding:6px 12px;border-radius:7px;transition:background .15s}}
nav a:hover{{background:var(--paper)}}
.nav-cta{{font-family:var(--mono)!important;font-size:11px!important;font-weight:600!important;background:var(--ink)!important;color:#fff!important;padding:8px 16px!important;border-radius:8px!important;margin-left:8px}}
.nav-cta:hover{{background:var(--gold)!important}}
.page-hero{{background:var(--white);padding:40px 0 32px;border-bottom:1px solid var(--veil);margin-bottom:24px}}
h1{{font-family:var(--serif);font-size:clamp(34px,5vw,48px);font-weight:400;line-height:1.1;margin-bottom:8px}}
.sub{{font-family:var(--sans);font-size:15px;color:var(--stone)}}
.filter-bar{{background:var(--white);padding:16px 20px;border-radius:var(--radius);margin-bottom:24px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;border:1px solid var(--veil)}}
.filter-label{{font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:.08em;color:var(--fog)}}
.filter-select{{font-family:var(--sans);font-size:13px;padding:8px 12px;border:1px solid var(--veil);border-radius:8px;background:var(--white);color:var(--ink);cursor:pointer}}
.filter-select:focus{{outline:none;border-color:var(--gold)}}
.sort-select{{font-family:var(--sans);font-size:13px;padding:8px 12px;border:1px solid var(--veil);border-radius:8px;background:var(--white);color:var(--ink);cursor:pointer}}
.reset-btn{{font-family:var(--mono);font-size:10px;background:transparent;border:1px solid var(--veil);border-radius:8px;padding:6px 12px;cursor:pointer;color:var(--stone)}}
.reset-btn:hover{{border-color:var(--gold);color:var(--gold)}}
.result-count{{font-family:var(--mono);font-size:11px;color:var(--fog);margin-left:auto}}
.tools-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px;padding-bottom:48px}}
.tool-card{{background:var(--white);border:1px solid var(--veil);border-radius:var(--radius);padding:22px;box-shadow:0 1px 3px rgba(0,0,0,.06);transition:transform .2s,box-shadow .2s,border-color .2s}}
.tool-card:hover{{transform:translateY(-3px);box-shadow:0 12px 32px rgba(0,0,0,.1);border-color:var(--gold-bd)}}
.card-top{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:14px}}
.card-rank{{font-family:var(--serif);font-style:italic;font-size:22px;color:var(--veil)}}
.tool-card:hover .card-rank{{color:var(--gold)}}
.card-score{{font-family:var(--serif);font-style:italic;font-size:36px;line-height:1}}
.card-score.hi{{color:var(--gold)}}.card-score.mid{{color:#b5821a}}.card-score.lo{{color:var(--red)}}
.card-name{{font-size:17px;font-weight:700;letter-spacing:-.02em;margin-bottom:6px}}
.card-desc{{font-size:13px;color:var(--stone);line-height:1.55;margin-bottom:14px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}}
.card-footer{{display:flex;justify-content:space-between;align-items:center;padding-top:14px;border-top:1px solid var(--paper);flex-wrap:wrap;gap:6px}}
.tag{{font-family:var(--mono);font-size:9.5px;padding:2px 7px;border-radius:4px;border:1px solid var(--veil);color:var(--fog);background:var(--snow)}}
.tag.cat{{background:var(--gold-bg);color:var(--gold);border-color:var(--gold-bd);font-size:9px;letter-spacing:.09em;text-transform:uppercase}}
.card-stats{{font-family:var(--mono);font-size:10px;color:var(--mist);display:flex;align-items:center;gap:8px}}
.stat-trend{{font-size:9.5px;font-weight:600}}
.stat-trend.rising{{color:var(--green)}}.stat-trend.falling{{color:var(--red)}}.stat-trend.new{{color:var(--gold)}}.stat-trend.stable{{color:var(--fog)}}
footer{{padding:40px 0 32px;border-top:1px solid var(--veil);background:var(--white)}}
.footer-inner{{max-width:1040px;margin:0 auto;padding:0 28px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:16px}}
.footer-brand,.footer-links a{{font-family:var(--mono);font-size:11px;color:var(--mist);text-decoration:none}}
.footer-links{{display:flex;gap:24px}}
.footer-links a:hover{{color:var(--gold)}}
@media(max-width:720px){{.tools-grid{{grid-template-columns:1fr}};nav a:not(.nav-cta){{display:none}}}}
</style>
</head>
<body>
<header><div class="header-inner">
  <a class="logo" href="/">BIZOPS<em>TOOL</em></a>
  <nav>
    <a href="/compare.html">Compare</a>
    <a href="/stack-grader.html">Grade stack</a>
    <a href="/stack-grader.html" class="nav-cta">GET STARTED →</a>
  </nav>
</div></header>

<div style="background:var(--white)"><div class="wrap">
  <div class="page-hero">
    <h1>All ranked tools</h1>
    <p class="sub">{len(tools)} open-source BizOps tools · Updated {generated_at[:10]}</p>
  </div>
</div></div>

<div class="wrap">
  <div class="filter-bar">
    <span class="filter-label">Category:</span>
    <select id="catFilter" class="filter-select">
      <option value="all">All categories</option>
      {cat_opts}
    </select>
    <span class="filter-label">Sort:</span>
    <select id="sortFilter" class="sort-select">
      <option value="score">BizOps Score</option>
      <option value="stars">Stars</option>
    </select>
    <button id="resetBtn" class="reset-btn">Reset</button>
    <span class="result-count" id="resultCount">{len(tools)} tools</span>
  </div>
  <div class="tools-grid" id="tools-grid">{''.join(rows)}</div>
</div>

<footer><div class="footer-inner">
  <div class="footer-brand">© 2026 BizOpsTool · Built with a bot, scored with data.</div>
  <div class="footer-links">
    <a href="/score-methodology.html">Methodology</a>
    <a href="{GITHUB_REPO_URL}" target="_blank">GitHub</a>
    <a href="https://bizopstool.beehiiv.com" target="_blank">Newsletter</a>
  </div>
</div></footer>

<script>
const catFilter  = document.getElementById('catFilter');
const sortFilter = document.getElementById('sortFilter');
const resetBtn   = document.getElementById('resetBtn');
const grid       = document.getElementById('tools-grid');
const countEl    = document.getElementById('resultCount');
const allCards   = Array.from(document.querySelectorAll('.tool-card'));

function applyFilters() {{
  const cat  = catFilter.value;
  const sort = sortFilter.value;
  let visible = allCards.filter(c => cat === 'all' || c.dataset.category === cat);
  visible.sort((a,b) => sort === 'score'
    ? parseInt(b.dataset.score) - parseInt(a.dataset.score)
    : parseInt(b.dataset.stars) - parseInt(a.dataset.stars));
  allCards.forEach(c => c.style.display = 'none');
  visible.forEach(c => {{ c.style.display = 'block'; grid.appendChild(c); }});
  countEl.textContent = visible.length + ' tools';
}}

catFilter.addEventListener('change', applyFilters);
sortFilter.addEventListener('change', applyFilters);
resetBtn.addEventListener('click', () => {{ catFilter.value='all'; sortFilter.value='score'; applyFilters(); }});
</script>
</body></html>"""

    with open(os.path.join(DOCS_DIR, "tools.html"), "w", encoding="utf-8") as f:
        f.write(html)
    log.info("Generated tools.html with %d tools", len(tools))

# ─────────────────────────────────────────────────────────────────────────────
# Main run
# ─────────────────────────────────────────────────────────────────────────────
def run(test_mode: bool = False) -> None:
    log.info("=== GitHub Trend Intelligence Engine v4.0 starting ===")
    _purge_stale_ci_cache()

    effective_pages = 1 if test_mode else MAX_PAGES
    effective_top_n = 3 if test_mode else TOP_N

    # 1 — Collect
    seen = {}
    for page in range(1, effective_pages + 1):
        results = search_repos(page=page)
        for repo in results:
            seen.setdefault(repo["id"], repo)
        if len(results) == 0:
            break
    raw_repos = list(seen.values())
    raw_repos = [r for r in raw_repos if _is_relevant(r)]
    raw_repos = list({r["name"].lower(): r for r in raw_repos}.values())
    log.info("After filter+dedup: %d repos", len(raw_repos))

    if not raw_repos:
        log.warning("No repos found.")
        send_telegram("No trending repos found today. Check TOPICS, MIN_STARS.")
        return

    # 2 — Enrich
    enriched = []
    for repo in raw_repos:
        owner = repo["owner"]["login"]
        name  = repo["name"]
        log.info("Enriching %s/%s …", owner, name)
        created = datetime.datetime.strptime(repo["created_at"], "%Y-%m-%dT%H:%M:%SZ")
        age     = max((_utcnow() - created).days, 1)

        enriched.append({
            **repo,
            "stars_7d":          stars_gained(repo),
            "forks_7d":          forks_gained(repo, age),
            "comments_7d":       get_comment_count(owner, name),
            "commits_7d":        get_commit_count(owner, name),
            "has_ci":            has_ci_workflow(owner, name),
            "trend_score":       compute_score(stars_gained(repo), forks_gained(repo,age), get_comment_count(owner,name), get_commit_count(owner,name), has_ci_workflow(owner,name)),
            "stars":             repo["stargazers_count"],
            "forks_30d":         get_forks_30d(owner, name, repo.get("forks_count",0)),
            "last_commit_days":  get_last_commit_days(owner, name),
            "avg_issue_hours":   get_avg_issue_response_hours(owner, name),
            "ci_passing":        has_ci_workflow(owner, name),
            "contributor_count": get_contributor_count(owner, name),
            # v4.0 new signals
            "pr_merge_rate":     get_pr_merge_rate(owner, name),
            "has_tests":         get_has_tests(owner, name),
            "days_since_release":get_days_since_release(owner, name),
            "category":          assign_category(repo),
        })
        time.sleep(0.1 if test_mode else 0.3)

    # 3 — Score
    for r in enriched:
        r["prev_score"] = get_prev_score(r["full_name"])
    enriched = compute_bizops_batch(enriched)
    for r in enriched:
        set_prev_score(r["full_name"], r["bizops_score"])

    top_by_score = sorted(enriched, key=lambda r: r.get("bizops_score",0), reverse=True)
    top          = sorted(enriched, key=lambda r: r["trend_score"], reverse=True)[:effective_top_n]

    # 4 — READMEs for digest
    for r in top:
        r["readme_snippet"] = get_readme_snippet(r["owner"]["login"], r["name"])
        time.sleep(0.1 if test_mode else 0.3)

    # 5 — AI outputs
    digest     = gpt_digest(top)
    idea       = synthesise_idea(top)
    idea_lines = f"\n\n💡 IDEA ENGINE\n{idea.get('title','')} — {idea.get('tagline','')}\nProblem: {idea.get('problem','')}\nSolution: {idea.get('solution','')}\nFlow: {' → '.join(idea.get('flowchart_steps',[]))}"

    # 6 — Write JSON
    generated_at = _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs(DOCS_DIR, exist_ok=True)

    free_payload = {"generated_at": generated_at, "tool_count": len(enriched), "tools": [_to_public_tool(t) for t in top_by_score[:5]]}
    with open(os.path.join(DOCS_DIR, "trending.json"), "w") as f:
        json.dump(free_payload, f, indent=2, default=str)

    with open("trending_full.json", "w") as f:
        json.dump({"generated_at": generated_at, "tool_count": len(enriched), "tools": top_by_score}, f, indent=2, default=str)

    # 7 — Generate all pages
    generate_tool_pages(top_by_score, generated_at)
    generate_category_pages(top_by_score, generated_at)
    generate_all_tools_page(top_by_score, generated_at)
    generate_sitemap(top_by_score, generated_at)

    log.info("Written %d tools | %d tool pages | sitemap updated", len(enriched), len(top_by_score))

    # 8 — Notify
    full_message = digest + idea_lines
    if test_mode:
        print("\n" + "="*60)
        print(full_message)
        print("="*60 + "\n")
    else:
        send_telegram(full_message)
        post_beehiiv_draft(f"BizOps Full Digest – {generated_at[:10]}", build_full_digest_html(top_by_score, generated_at))

    log.info("=== Done ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--unit-tests", action="store_true")
    args = parser.parse_args()
    if args.unit_tests:
        assert compute_score(0,0,0,0,False) == 0.0
        assert compute_score(10,1,5,2,True) == 10*0.5+1*2.0+5*1.5+2*1.0+10.0
        log.info("Unit tests passed")
    else:
        run(test_mode=args.test)
