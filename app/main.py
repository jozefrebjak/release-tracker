import os
import re
import json
import asyncio
import logging
import ipaddress
import socket
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse

import hmac

import aiosqlite
import httpx
import anthropic
from fastapi import FastAPI, HTTPException, Query, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tracker")

DB_PATH = os.getenv("DB_PATH", "data/tracker.db")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SIGNAL_API_URL = os.getenv("SIGNAL_API_URL", "")
SIGNAL_SENDER = os.getenv("SIGNAL_SENDER", "")
SIGNAL_GROUP_ID = os.getenv("SIGNAL_GROUP_ID", "")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "60"))
SUMMARY_LANGUAGE = os.getenv("SUMMARY_LANGUAGE", "sk")
API_TOKEN = os.getenv("API_TOKEN", "")

scheduler = AsyncIOScheduler()

if not API_TOKEN:
    log.warning("API_TOKEN is not set — all endpoints are publicly accessible without authentication")



def _is_safe_url(url: str) -> bool:
    """Reject URLs pointing to private/internal networks (SSRF protection)."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return False
        # Resolve hostname to IP and check if it's private
        for info in socket.getaddrinfo(hostname, None):
            addr = info[4][0]
            ip = ipaddress.ip_address(addr)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        return True
    except (socket.gaierror, ValueError):
        return False



async def require_auth(request: Request):
    if not API_TOKEN:
        return
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, API_TOKEN):
        raise HTTPException(401, "Unauthorized")



@asynccontextmanager
async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
    finally:
        await db.close()


async def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with get_db() as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS repos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL,
                repo TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(owner, repo)
            );
            CREATE TABLE IF NOT EXISTS releases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_id INTEGER REFERENCES repos(id) ON DELETE CASCADE,
                tag_name TEXT NOT NULL,
                name TEXT,
                published_at TIMESTAMP,
                body TEXT,
                summary TEXT,
                notified_at TIMESTAMP,
                UNIQUE(repo_id, tag_name)
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS ai_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cost_usd REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS feeds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                feed_url TEXT,
                feed_type TEXT DEFAULT 'unknown',
                scrape_enabled INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS feed_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_id INTEGER REFERENCES feeds(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                url TEXT,
                published_at TIMESTAMP,
                body TEXT,
                summary TEXT,
                notified_at TIMESTAMP,
                UNIQUE(feed_id, title)
            );
        """)
        await db.commit()


async def load_settings_dict() -> dict:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT key, value FROM settings")
        return {row["key"]: row["value"] for row in rows}



async def fetch_releases(owner: str, repo: str) -> list[dict]:
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    async with httpx.AsyncClient() as client:
        # Try releases first
        r = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/releases",
            headers=headers,
            params={"per_page": 5},
        )
        if r.status_code != 200:
            log.warning(f"GitHub API error for {owner}/{repo}: {r.status_code}")
            return []
        releases = r.json()
        if releases:
            return releases

        # Fallback to tags if no releases
        log.info(f"No releases for {owner}/{repo}, falling back to tags")
        r = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/tags",
            headers=headers,
            params={"per_page": 5},
        )
        if r.status_code != 200:
            return []
        tags = r.json()
        # Convert tags to release-like format
        results = []
        for tag in tags:
            # Fetch commit date
            commit_r = await client.get(tag["commit"]["url"], headers=headers)
            committed = ""
            if commit_r.status_code == 200:
                commit_data = commit_r.json()
                committed = (commit_data.get("commit", {}).get("committer", {}).get("date", ""))
            results.append({
                "tag_name": tag["name"],
                "name": tag["name"],
                "published_at": committed,
                "body": "",
            })
        return results



def _decode_html_entities(text: str) -> str:
    text = text.replace('&amp;', '&').replace('&#39;', "'").replace('&quot;', '"')
    text = text.replace('&lt;', '<').replace('&gt;', '>').replace('&apos;', "'")
    return text.replace('&#x27;', "'").replace('&#x2F;', '/').replace('&nbsp;', ' ')


def _strip_html(html: str) -> str:
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
    text = re.sub(r'<!\[CDATA\[|\]\]>', '', text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return _decode_html_entities(text)


_CONTENT_PATTERNS = [
    r'<div[^>]*class="[^"]*bbWrapper[^"]*"[^>]*>(.*?)</div>',
    r'<article[^>]*>(.*?)</article>',
    r'<div[^>]*class="[^"]*(?:post-body|entry-content|article-content|blog-content|post-content|message-body|editor-content)[^"]*"[^>]*>(.*?)</div>',
    r'<main[^>]*>(.*?)</main>',
]


async def _fetch_page_text(url: str) -> str:
    """Fetch a page and extract main text content. Follows forum links if page only contains a redirect."""
    if not _is_safe_url(url):
        log.warning(f"Blocked SSRF attempt: {url}")
        return ""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return ""
            text = _extract_content(r.text)
            # If content is just a forum link, follow it
            if text and len(text) < 200:
                forum_link = re.search(r'https?://forum\.[^\s<"]+', text)
                if forum_link:
                    r2 = await client.get(forum_link.group(0))
                    if r2.status_code == 200:
                        forum_text = _extract_content(r2.text)
                        if forum_text and len(forum_text) > len(text):
                            return forum_text[:3000]
            return text[:3000] if text else ""
    except Exception:
        return ""


def _extract_content(html: str) -> str:
    for pattern in _CONTENT_PATTERNS:
        match = re.search(pattern, html, re.DOTALL)
        if match:
            return _strip_html(match.group(1))
    return ""


async def detect_feed_url(url: str) -> tuple[str | None, str]:
    """Try to find an RSS/Atom feed for a given URL. Returns (feed_url, feed_type)."""
    if not _is_safe_url(url):
        log.warning(f"Blocked SSRF attempt in feed detection: {url}")
        return None, 'scrape'
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    # First, check if the URL itself is already a feed
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
            r = await client.get(url)
            if r.status_code == 200:
                head = r.text[:500]
                if '<rss' in head or '<feed' in head or '<channel' in head:
                    feed_type = 'atom' if '<feed' in head else 'rss'
                    return url, feed_type
    except Exception:
        pass

    # Common feed URL patterns to try
    candidates = [
        url.rstrip('/') + '/feed/',
        url.rstrip('/') + '/feed',
        url.rstrip('/') + '/rss/',
        url.rstrip('/') + '/rss',
        url.rstrip('/') + '/atom.xml',
        url.rstrip('/') + '/feed.xml',
        url.rstrip('/') + '/rss.xml',
        url.rstrip('/') + '/index.xml',
        base + '/feed/',
        base + '/feed',
        base + '/rss/',
        base + '/rss',
        base + '/atom.xml',
        base + '/feed.xml',
        base + '/rss.xml',
        base + '/index.xml',
        base + '/blog/feed/',
        base + '/blog/rss/',
    ]
    # Deduplicate while preserving order
    seen = set()
    unique_candidates = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique_candidates.append(c)

    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
        # First check the page itself for <link rel="alternate"> feed references
        try:
            r = await client.get(url)
            if r.status_code == 200:
                for match in re.finditer(
                    r'<link[^>]*type="application/(?:rss|atom)\+xml"[^>]*href="([^"]*)"',
                    r.text,
                ):
                    feed_href = match.group(1)
                    if feed_href.startswith('/'):
                        feed_href = base + feed_href
                    elif not feed_href.startswith('http'):
                        continue
                    # Verify it's a valid feed
                    try:
                        fr = await client.get(feed_href)
                        if fr.status_code == 200 and ('<rss' in fr.text[:500] or '<feed' in fr.text[:500] or '<channel' in fr.text[:500]):
                            feed_type = 'atom' if '<feed' in fr.text[:500] else 'rss'
                            return feed_href, feed_type
                    except Exception:
                        pass
        except Exception:
            pass

        # Try common feed URL patterns
        for candidate in unique_candidates:
            try:
                r = await client.get(candidate)
                if r.status_code == 200 and ('<rss' in r.text[:500] or '<feed' in r.text[:500] or '<channel' in r.text[:500]):
                    feed_type = 'atom' if '<feed' in r.text[:500] else 'rss'
                    return candidate, feed_type
            except Exception:
                continue

    return None, 'scrape'


def parse_rss_feed(xml: str) -> list[dict]:
    """Parse RSS/Atom XML into a list of posts."""
    posts = []

    # Detect feed type
    is_atom = '<feed' in xml[:500]

    if is_atom:
        # Atom feed
        for entry in re.findall(r'<entry>(.*?)</entry>', xml, re.DOTALL):
            title_m = re.search(r'<title[^>]*>(.*?)</title>', entry, re.DOTALL)
            link_m = re.search(r'<link[^>]*href="([^"]*)"', entry)
            published_m = re.search(r'<(?:published|updated)>(.*?)</(?:published|updated)>', entry)
            content_m = re.search(r'<(?:content|summary)[^>]*>(.*?)</(?:content|summary)>', entry, re.DOTALL)

            title = _strip_html(title_m.group(1)) if title_m else ""
            if not title:
                continue

            posts.append({
                "title": title,
                "url": link_m.group(1) if link_m else "",
                "published_at": published_m.group(1)[:10] if published_m else "",
                "body": _strip_html(content_m.group(1))[:3000] if content_m else "",
            })
    else:
        # RSS feed
        for item in re.findall(r'<item>(.*?)</item>', xml, re.DOTALL):
            title_m = re.search(r'<title[^>]*>(.*?)</title>', item, re.DOTALL)
            link_m = re.search(r'<link[^>]*>(.*?)</link>', item, re.DOTALL)
            if not link_m:
                link_m = re.search(r'<link[^>]*href="([^"]*)"', item)
            pub_m = re.search(r'<pubDate>(.*?)</pubDate>', item)
            desc_m = re.search(r'<(?:description|content:encoded)[^>]*>(.*?)</(?:description|content:encoded)>', item, re.DOTALL)

            title = _strip_html(title_m.group(1)) if title_m else ""
            if not title:
                continue

            link = ""
            if link_m:
                link = link_m.group(1).strip()
                link = _decode_html_entities(link)

            published = ""
            if pub_m:
                # Try to extract date from RFC 2822 format
                date_m = re.search(r'(\d{1,2}) (\w{3}) (\d{4})', pub_m.group(1))
                if date_m:
                    months = {'Jan':'01','Feb':'02','Mar':'03','Apr':'04','May':'05','Jun':'06',
                              'Jul':'07','Aug':'08','Sep':'09','Oct':'10','Nov':'11','Dec':'12'}
                    day = date_m.group(1).zfill(2)
                    month = months.get(date_m.group(2), '01')
                    year = date_m.group(3)
                    published = f"{year}-{month}-{day}"

            posts.append({
                "title": title,
                "url": link,
                "published_at": published,
                "body": _strip_html(desc_m.group(1))[:3000] if desc_m else "",
            })

    return posts[:10]


async def scrape_blog_posts(url: str) -> list[dict]:
    """Fallback: scrape blog posts from HTML page."""
    if not _is_safe_url(url):
        log.warning(f"Blocked SSRF attempt in blog scraping: {url}")
        return []
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return []

        html = r.text
        parsed_url = urlparse(url)
        base = f"{parsed_url.scheme}://{parsed_url.netloc}"
        posts = []
        seen_titles = set()

        for href, text_html in re.findall(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', html, re.DOTALL):
            title = re.sub(r'<[^>]+>', '', text_html).strip()
            title = _decode_html_entities(title)

            if len(title) < 15 or title in seen_titles:
                continue
            if any(skip in href for skip in ['/author/', '/tag/', '/category/', '#', 'javascript:']):
                continue
            if any(skip in title.lower() for skip in ['read full', 'read more', 'continue reading', 'load more']):
                continue

            if href.startswith('/'):
                href = base + href
            elif not href.startswith('http'):
                continue

            link_parsed = urlparse(href)
            if link_parsed.netloc != parsed_url.netloc:
                continue
            path_parts = [p for p in link_parsed.path.split('/') if p]
            if len(path_parts) < 2:
                continue

            date_match = re.search(r'/(\d{4})/(\d{2})/(\d{2})/', href)
            published = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}" if date_match else ""

            seen_titles.add(title)
            posts.append({"title": title, "url": href, "published_at": published, "body": ""})
            if len(posts) >= 10:
                break

        # Fetch body for top 5 posts
        for post in posts[:5]:
            try:
                post["body"] = await _fetch_page_text(post["url"])
            except Exception:
                pass

        return posts
    except Exception as e:
        log.error(f"Blog scrape error for {url}: {e}")
        return []


async def fetch_feed_posts(feed_id: int, url: str, feed_url: str | None, feed_type: str, scrape_enabled: bool = False) -> list[dict]:
    """Fetch posts from a feed — RSS/Atom if available, scrape as opt-in fallback."""
    # Auto-detect on first run
    if feed_type == 'unknown':
        detected_url, detected_type = await detect_feed_url(url)
        async with get_db() as db:
            await db.execute(
                "UPDATE feeds SET feed_url=?, feed_type=? WHERE id=?",
                (detected_url, detected_type, feed_id),
            )
            await db.commit()
        feed_url = detected_url
        feed_type = detected_type
        log.info(f"Feed {url}: detected type={detected_type}, feed_url={detected_url}")

    # Use RSS/Atom if available
    if feed_type in ('rss', 'atom') and feed_url:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
                r = await client.get(feed_url)
                if r.status_code == 200:
                    posts = parse_rss_feed(r.text)
                    # Fetch body for posts that have a link but no content (e.g. Proxmox)
                    for post in posts[:5]:
                        if post["url"] and not post["body"]:
                            post["body"] = await _fetch_page_text(post["url"])
                    return posts
        except Exception as e:
            log.warning(f"RSS fetch failed for {feed_url}: {e}")

    # Blog scraping only if explicitly enabled
    if scrape_enabled:
        return await scrape_blog_posts(url)

    if feed_type == 'scrape':
        log.info(f"Feed {url}: no RSS/Atom found, blog scraping is disabled")
    return []



# Cost per 1M tokens (USD) - updated 2025
MODEL_COSTS = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
}

def calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    costs = MODEL_COSTS.get(model, {"input": 3.0, "output": 15.0})
    return (input_tokens * costs["input"] + output_tokens * costs["output"]) / 1_000_000


def extract_plain_summary(body: str) -> str:
    lines = [l.strip() for l in body.strip().splitlines() if l.strip() and not l.strip().startswith('#')]
    return "\n".join(lines[:5])


def _strip_markdown(text: str) -> str:
    """Convert markdown to plain text for Signal notifications."""
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'^\s*[-*]\s+', '• ', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+', '• ', text, flags=re.MULTILINE)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _build_ai_system_prompt(lang: str) -> str:
    lang_name = "Slovak" if lang == "sk" else "English"
    return (
        f"You are a technical release notes analyst for senior DevOps engineers and sysadmins. "
        f"Write in {lang_name}. Be direct, no fluff."
    )


def _build_release_user_prompt(context: str, title: str, body: str) -> str:
    return (
        f"Analyze this release changelog for {context} {title}.\n\n"
        f"Provide a structured summary:\n"
        f"1. One-line TL;DR of the release\n"
        f"2. Breaking changes or security fixes (if any) — mark with ⚠️\n"
        f"3. Key changes that affect deployment, config, or infrastructure\n"
        f"4. Notable bug fixes worth knowing about\n\n"
        f"Skip categories that don't apply. Keep it under 6 lines total.\n\n"
        f"---\n{body[:3000]}"
    )


def _build_feed_user_prompt(feed_name: str, title: str, body: str) -> str:
    return (
        f"Summarize this post from {feed_name}: \"{title}\"\n\n"
        f"1. One-line TL;DR\n"
        f"2. Key changes or announcements\n"
        f"3. Action items for sysadmins/devops (if any)\n\n"
        f"Keep it under 6 lines.\n\n"
        f"---\n{body[:3000]}"
    )


async def _call_ai(api_key: str, model: str, system: str, user_content: str) -> tuple[str, int, int, float]:
    """Call Claude API and log usage. Returns (summary, input_tokens, output_tokens, cost)."""
    client = anthropic.AsyncAnthropic(api_key=api_key)
    msg = await client.messages.create(
        model=model,
        max_tokens=400,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    summary = msg.content[0].text
    input_tokens = msg.usage.input_tokens
    output_tokens = msg.usage.output_tokens
    cost = calc_cost(model, input_tokens, output_tokens)
    async with get_db() as db:
        await db.execute(
            "INSERT INTO ai_usage (model, input_tokens, output_tokens, cost_usd) VALUES (?, ?, ?, ?)",
            (model, input_tokens, output_tokens, cost),
        )
        await db.commit()
    log.info(f"AI summary: {model} | {input_tokens}+{output_tokens} tokens | ${cost:.6f}")
    return summary, input_tokens, output_tokens, cost


async def summarize_release(owner: str, repo: str, tag: str, body: str) -> str:
    if not body:
        return ""
    settings = await load_settings_dict()
    ai_enabled = settings.get("ai_enabled", "false") == "true"
    if not ai_enabled:
        return extract_plain_summary(body)
    api_key = settings.get("anthropic_api_key", "") or ANTHROPIC_API_KEY
    if not api_key:
        return extract_plain_summary(body)
    model = settings.get("ai_model", "claude-haiku-4-5-20251001")
    lang = settings.get("summary_language", SUMMARY_LANGUAGE) or "sk"
    try:
        context = f"{owner}/{repo}" if repo else owner
        summary, _, _, _ = await _call_ai(
            api_key, model,
            _build_ai_system_prompt(lang),
            _build_release_user_prompt(context, tag, body),
        )
        return summary
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return extract_plain_summary(body)



async def send_signal(message: str):
    settings = await load_settings_dict()
    if settings.get("signal_enabled", "false") != "true":
        log.info("Signal disabled, skipping notification")
        return
    api_url = settings.get("signal_api_url", "") or SIGNAL_API_URL
    sender = settings.get("signal_sender", "") or SIGNAL_SENDER
    recipient = settings.get("signal_group_id", "") or SIGNAL_GROUP_ID
    if not all([api_url, sender, recipient]):
        log.info("Signal not configured, skipping notification")
        return
    if not _is_safe_url(api_url):
        log.warning(f"Blocked Signal API call to unsafe URL: {api_url}")
        return
    try:
        payload = json.dumps({
            "message": message,
            "number": sender,
            "recipients": [recipient],
        }, ensure_ascii=False)
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{api_url}/v2/send",
                content=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=10,
            )
    except Exception as e:
        log.error(f"Signal send error: {e}")



async def check_releases():
    log.info("Checking for new releases...")
    try:
        async with get_db() as db:
            rows = await db.execute_fetchall("SELECT id, owner, repo FROM repos")
            for row in rows:
                repo_id, owner, repo = row["id"], row["owner"], row["repo"]
                releases = await fetch_releases(owner, repo)
                for rel in releases:
                    tag = rel.get("tag_name", "")
                    if not tag:
                        continue

                    existing = await db.execute_fetchall(
                        "SELECT id FROM releases WHERE repo_id=? AND tag_name=?",
                        (repo_id, tag),
                    )
                    if existing:
                        continue

                    body = rel.get("body", "") or ""
                    name = rel.get("name", "")
                    published = rel.get("published_at", "")

                    summary = await summarize_release(owner, repo, tag, body)

                    await db.execute(
                        """INSERT INTO releases (repo_id, tag_name, name, published_at, body, summary, notified_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (repo_id, tag, name, published, body, summary, datetime.now(timezone.utc).isoformat()),
                    )
                    await db.commit()

                    message = f"📦 {owner}/{repo} {tag}\n\n{_strip_markdown(summary)}\n\n🔗 https://github.com/{owner}/{repo}/releases/tag/{tag}"
                    await send_signal(message)
                    log.info(f"New release: {owner}/{repo} {tag}")

            # Check web feeds
            feed_rows = await db.execute_fetchall("SELECT id, name, url, feed_url, feed_type, scrape_enabled FROM feeds")
            for frow in feed_rows:
                feed_id, feed_name = frow["id"], frow["name"]
                posts = await fetch_feed_posts(
                    feed_id, frow["url"], frow["feed_url"],
                    frow["feed_type"] or "unknown", bool(frow["scrape_enabled"]),
                )
                for post in posts:
                    title = post.get("title", "")
                    if not title:
                        continue
                    existing = await db.execute_fetchall(
                        "SELECT id FROM feed_entries WHERE feed_id=? AND title=?",
                        (feed_id, title),
                    )
                    if existing:
                        continue

                    body = post.get("body", "") or ""
                    post_url = post.get("url", "")
                    published = post.get("published_at", "")

                    summary = await summarize_release(feed_name, "", title, body)

                    await db.execute(
                        """INSERT INTO feed_entries (feed_id, title, url, published_at, body, summary, notified_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (feed_id, title, post_url, published, body, summary, datetime.now(timezone.utc).isoformat()),
                    )
                    await db.commit()

                    message = f"📰 {feed_name}: {title}\n\n{_strip_markdown(summary)}\n\n🔗 {post_url}"
                    await send_signal(message)
                    log.info(f"New feed entry: {feed_name} - {title}")
    except Exception as e:
        log.error(f"Check releases error: {e}")



@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    scheduler.add_job(check_releases, "interval", minutes=CHECK_INTERVAL_MINUTES, id="check_releases")
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)



@app.get("/")
async def index():
    return FileResponse("app/static/index.html")


@app.get("/api/repos")
async def list_repos():
    async with get_db() as db:
        rows = await db.execute_fetchall("""
            SELECT r.id, r.owner, r.repo, r.created_at,
                   (SELECT tag_name FROM releases WHERE repo_id=r.id ORDER BY published_at DESC LIMIT 1) as last_tag,
                   (SELECT published_at FROM releases WHERE repo_id=r.id ORDER BY published_at DESC LIMIT 1) as last_release_at
            FROM repos r ORDER BY r.created_at DESC
        """)
        return [dict(row) for row in rows]


@app.get("/api/auth/check")
async def auth_check():
    return {"required": bool(API_TOKEN)}


@app.post("/api/repos", dependencies=[Depends(require_auth)])
async def add_repo(data: dict):
    owner = data.get("owner", "").strip()
    repo = data.get("repo", "").strip()
    if not owner or not repo:
        raise HTTPException(400, "owner and repo required")
    if not re.match(r'^[a-zA-Z0-9._-]+$', owner) or not re.match(r'^[a-zA-Z0-9._-]+$', repo):
        raise HTTPException(400, "Invalid owner or repo name")
    async with get_db() as db:
        try:
            await db.execute("INSERT INTO repos (owner, repo) VALUES (?, ?)", (owner, repo))
            await db.commit()
        except aiosqlite.IntegrityError:
            raise HTTPException(409, "Repository already tracked")
        row = await db.execute_fetchall(
            "SELECT id, owner, repo, created_at FROM repos WHERE owner=? AND repo=?", (owner, repo)
        )
        return dict(row[0])


@app.delete("/api/repos/{repo_id}", dependencies=[Depends(require_auth)])
async def delete_repo(repo_id: int):
    async with get_db() as db:
        await db.execute("DELETE FROM repos WHERE id=?", (repo_id,))
        await db.commit()
        return {"ok": True}


@app.get("/api/feeds")
async def list_feeds():
    async with get_db() as db:
        rows = await db.execute_fetchall("""
            SELECT f.id, f.name, f.url, f.feed_url, f.feed_type, f.scrape_enabled, f.created_at,
                   (SELECT title FROM feed_entries WHERE feed_id=f.id ORDER BY notified_at DESC LIMIT 1) as last_title,
                   (SELECT published_at FROM feed_entries WHERE feed_id=f.id ORDER BY notified_at DESC LIMIT 1) as last_published_at
            FROM feeds f ORDER BY f.created_at DESC
        """)
        return [dict(row) for row in rows]


@app.post("/api/feeds", dependencies=[Depends(require_auth)])
async def add_feed(data: dict):
    name = data.get("name", "").strip()
    url = data.get("url", "").strip()
    if not name or not url:
        raise HTTPException(400, "name and url required")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(400, "Invalid URL")
    async with get_db() as db:
        try:
            await db.execute("INSERT INTO feeds (name, url) VALUES (?, ?)", (name, url))
            await db.commit()
        except aiosqlite.IntegrityError:
            raise HTTPException(409, "Feed URL already tracked")
        row = await db.execute_fetchall(
            "SELECT id, name, url, created_at FROM feeds WHERE url=?", (url,)
        )
        return dict(row[0])


@app.patch("/api/feeds/{feed_id}", dependencies=[Depends(require_auth)])
async def update_feed(feed_id: int, data: dict):
    async with get_db() as db:
        if "scrape_enabled" in data:
            await db.execute(
                "UPDATE feeds SET scrape_enabled=? WHERE id=?",
                (1 if data["scrape_enabled"] else 0, feed_id),
            )
        await db.commit()
        return {"ok": True}


@app.delete("/api/feeds/{feed_id}", dependencies=[Depends(require_auth)])
async def delete_feed(feed_id: int):
    async with get_db() as db:
        await db.execute("DELETE FROM feeds WHERE id=?", (feed_id,))
        await db.commit()
        return {"ok": True}


@app.get("/api/feed-entries")
async def all_feed_entries(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0)):
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT e.*, f.name as feed_name, f.url as feed_url FROM feed_entries e JOIN feeds f ON e.feed_id = f.id ORDER BY e.notified_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [dict(row) for row in rows]


@app.get("/api/repos/{repo_id}/releases")
async def repo_releases(repo_id: int, limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0)):
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM releases WHERE repo_id=? ORDER BY published_at DESC LIMIT ? OFFSET ?",
            (repo_id, limit, offset),
        )
        return [dict(row) for row in rows]


@app.get("/api/releases")
async def all_releases(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0)):
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT rel.*, r.owner, r.repo FROM releases rel JOIN repos r ON rel.repo_id = r.id ORDER BY rel.published_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [dict(row) for row in rows]


@app.post("/api/releases/{release_id}/summarize", dependencies=[Depends(require_auth)])
async def summarize_release_endpoint(release_id: int):
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT rel.*, r.owner, r.repo FROM releases rel JOIN repos r ON rel.repo_id = r.id WHERE rel.id=?",
            (release_id,),
        )
        if not rows:
            raise HTTPException(404, "Release not found")
        rel = dict(rows[0])
        if not rel.get("body"):
            raise HTTPException(400, "No content to summarize")
        settings = await load_settings_dict()
        api_key = settings.get("anthropic_api_key", "") or ANTHROPIC_API_KEY
        if not api_key:
            raise HTTPException(400, "Anthropic API key not configured")
        model = settings.get("ai_model", "claude-haiku-4-5-20251001")
        lang = settings.get("summary_language", SUMMARY_LANGUAGE) or "sk"
        try:
            context = f"{rel['owner']}/{rel['repo']}"
            summary, _, _, cost = await _call_ai(
                api_key, model,
                _build_ai_system_prompt(lang),
                _build_release_user_prompt(context, rel['tag_name'], rel['body']),
            )
            await db.execute("UPDATE releases SET summary=? WHERE id=?", (summary, release_id))
            await db.commit()
            return {"summary": summary, "cost": cost}
        except Exception as e:
            log.error(f"Summarize release error: {e}")
            raise HTTPException(500, "AI summarization failed")


@app.post("/api/feed-entries/{entry_id}/summarize", dependencies=[Depends(require_auth)])
async def summarize_feed_entry_endpoint(entry_id: int):
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT e.*, f.name as feed_name FROM feed_entries e JOIN feeds f ON e.feed_id = f.id WHERE e.id=?",
            (entry_id,),
        )
        if not rows:
            raise HTTPException(404, "Entry not found")
        entry = dict(rows[0])
        if not entry.get("body"):
            raise HTTPException(400, "No content to summarize")
        settings = await load_settings_dict()
        api_key = settings.get("anthropic_api_key", "") or ANTHROPIC_API_KEY
        if not api_key:
            raise HTTPException(400, "Anthropic API key not configured")
        model = settings.get("ai_model", "claude-haiku-4-5-20251001")
        lang = settings.get("summary_language", SUMMARY_LANGUAGE) or "sk"
        try:
            summary, _, _, cost = await _call_ai(
                api_key, model,
                _build_ai_system_prompt(lang),
                _build_feed_user_prompt(entry['feed_name'], entry['title'], entry['body']),
            )
            await db.execute("UPDATE feed_entries SET summary=? WHERE id=?", (summary, entry_id))
            await db.commit()
            return {"summary": summary, "cost": cost}
        except Exception as e:
            log.error(f"Summarize feed entry error: {e}")
            raise HTTPException(500, "AI summarization failed")


@app.post("/api/signal/test", dependencies=[Depends(require_auth)])
async def test_signal():
    settings = await load_settings_dict()
    api_url = settings.get("signal_api_url", "") or SIGNAL_API_URL
    sender = settings.get("signal_sender", "") or SIGNAL_SENDER
    recipient = settings.get("signal_group_id", "") or SIGNAL_GROUP_ID
    if not all([api_url, sender, recipient]):
        raise HTTPException(400, "Signal not fully configured")
    if not _is_safe_url(api_url):
        raise HTTPException(400, "Signal API URL points to a private/internal network")
    try:
        payload = json.dumps({
            "message": "🧪 Test from GitHub Release Tracker",
            "number": sender,
            "recipients": [recipient],
        }, ensure_ascii=False)
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{api_url}/v2/send",
                content=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=10,
            )
        if r.status_code >= 400:
            raise HTTPException(502, f"Signal API returned {r.status_code}")
        return {"ok": True}
    except httpx.ConnectError:
        raise HTTPException(502, "Cannot connect to Signal API")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(500, "Signal test failed")


@app.post("/api/check", dependencies=[Depends(require_auth)])
async def trigger_check():
    asyncio.create_task(check_releases())
    return {"ok": True, "message": "Check started"}


@app.get("/api/settings", dependencies=[Depends(require_auth)])
async def get_settings():
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT key, value FROM settings")
        return {row["key"]: row["value"] for row in rows}


@app.get("/api/ai-usage", dependencies=[Depends(require_auth)])
async def ai_usage_stats():
    async with get_db() as db:
        total = await db.execute_fetchall(
            "SELECT COALESCE(SUM(input_tokens),0) as input_tokens, COALESCE(SUM(output_tokens),0) as output_tokens, COALESCE(SUM(cost_usd),0) as total_cost, COUNT(*) as requests FROM ai_usage"
        )
        monthly = await db.execute_fetchall(
            "SELECT COALESCE(SUM(input_tokens),0) as input_tokens, COALESCE(SUM(output_tokens),0) as output_tokens, COALESCE(SUM(cost_usd),0) as total_cost, COUNT(*) as requests FROM ai_usage WHERE created_at >= date('now', 'start of month')"
        )
        return {
            "total": dict(total[0]),
            "monthly": dict(monthly[0]),
        }


ALLOWED_SETTINGS_KEYS = {
    "check_interval", "summary_language",
    "ai_enabled", "ai_model", "anthropic_api_key",
    "signal_enabled", "signal_api_url", "signal_sender", "signal_group_id",
}


@app.put("/api/settings", dependencies=[Depends(require_auth)])
async def update_settings(data: dict):
    async with get_db() as db:
        for key, value in data.items():
            if key not in ALLOWED_SETTINGS_KEYS:
                continue
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=?",
                (key, str(value), str(value)),
            )
        await db.commit()
        return {"ok": True}


app.mount("/static", StaticFiles(directory="app/static"), name="static")
