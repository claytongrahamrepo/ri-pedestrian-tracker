"""Discover candidate news stories about pedestrian crashes in Rhode Island.

Sources:
  1. Google News RSS search queries (covers all RI outlets)
  2. Direct outlet RSS feeds, filtered by keywords

New article URLs are appended to data/candidates.json (the queue consumed by
extract.py). Every URL ever encountered is recorded in data/seen_urls.json so
it is never queued twice.
"""
import os
import re
import time
import urllib.parse
from datetime import datetime, timezone

import feedparser

from common import CONFIG, DATA, load_json, save_json

from datetime import timedelta

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"


def google_windows(lookback_days: int) -> list[str]:
    """Google News's when: operator only reaches back ~30 days; for longer
    backfills, slice the range into 30-day after:/before: windows."""
    if lookback_days <= 30:
        return [f"when:{lookback_days}d"]
    windows = []
    end = datetime.now(timezone.utc).date()
    cur = end - timedelta(days=lookback_days)
    while cur < end:
        nxt = min(cur + timedelta(days=30), end)
        windows.append(f"after:{cur.isoformat()} before:{nxt.isoformat()}")
        cur = nxt
    return windows


def decode_google_url(url: str) -> str:
    """Google News RSS links point at news.google.com; decode to the real URL."""
    if "news.google.com" not in url:
        return url
    try:
        from googlenewsdecoder import new_decoderv1

        result = new_decoderv1(url, interval=1)
        if result.get("status"):
            return result["decoded_url"]
    except Exception as e:
        print(f"  ! could not decode {url[:80]}: {e}")
    return url


def matches_keywords(text: str) -> bool:
    text = text.lower()
    return any(kw in text for kw in CONFIG["keyword_filter"])


def matches_title_filter(title: str) -> bool:
    """Looser filter for Google News results, which match on article *body*:
    the title must at least be crash-adjacent, or it's police-blotter noise."""
    title = title.lower()
    return any(kw in title for kw in CONFIG["title_filter"])


def normalize_title(title: str) -> str:
    """Strip the ' - Outlet Name' suffix so syndicated copies (Yahoo/MSN/AOL
    reprints of the same wire story) dedupe."""
    return re.sub(r"\s+[-|–]\s+[^-|–]{2,40}$", "", title).strip().lower()


def entry_timestamp(entry) -> str:
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            return datetime(*parsed[:6], tzinfo=timezone.utc).isoformat()
    return ""


def collect_google_news(lookback_days: int):
    for query in CONFIG["google_news_queries"]:
        for window in google_windows(lookback_days):
            q = urllib.parse.quote(f"{query} {window}")
            url = GOOGLE_NEWS_RSS.format(query=q)
            feed = feedparser.parse(url, agent="Mozilla/5.0")
            print(f"Google News [{query} | {window}]: {len(feed.entries)} entries")
            for entry in feed.entries:
                yield entry, f"google-news:{query}"
            time.sleep(1)


def collect_outlet_feeds():
    for feed_url in CONFIG["outlet_feeds"]:
        feed = feedparser.parse(feed_url, agent="Mozilla/5.0")
        if feed.bozo and not feed.entries:
            print(f"Feed failed, skipping: {feed_url}")
            continue
        print(f"Outlet feed {feed_url}: {len(feed.entries)} entries")
        for entry in feed.entries:
            text = f"{entry.get('title', '')} {entry.get('summary', '')}"
            if matches_keywords(text):
                yield entry, feed_url


def main():
    lookback_days = int(os.environ.get("LOOKBACK_DAYS", CONFIG["lookback_days"]))
    seen = load_json(DATA / "seen_urls.json", {})
    candidates = load_json(DATA / "candidates.json", [])
    queued = {c["url"] for c in candidates}
    added = 0

    seen_titles = {normalize_title(c["title"]) for c in candidates}

    entries = list(collect_google_news(lookback_days)) + list(collect_outlet_feeds())
    for entry, source in entries:
        raw_url = entry.get("link", "")
        title = entry.get("title", "")
        if not raw_url or raw_url in seen:
            continue
        if source.startswith("google-news:") and not matches_title_filter(title):
            seen[raw_url] = datetime.now(timezone.utc).date().isoformat()
            continue
        norm = normalize_title(title)
        if norm in seen_titles:
            seen[raw_url] = datetime.now(timezone.utc).date().isoformat()
            continue
        seen_titles.add(norm)
        seen[raw_url] = datetime.now(timezone.utc).date().isoformat()

        url = decode_google_url(raw_url)
        # Strip tracking params so the same story from two queries dedupes.
        url = re.sub(r"[?#].*$", "", url)
        if url in seen or url in queued:
            continue
        seen[url] = seen[raw_url]

        candidates.append(
            {
                "url": url,
                "title": entry.get("title", ""),
                "published": entry_timestamp(entry),
                "source": source,
                "attempts": 0,
            }
        )
        queued.add(url)
        added += 1
        print(f"  + {entry.get('title', '')[:90]}")

    save_json(DATA / "seen_urls.json", seen)
    save_json(DATA / "candidates.json", candidates)
    print(f"\n{added} new candidate(s); {len(candidates)} in queue.")


if __name__ == "__main__":
    main()
