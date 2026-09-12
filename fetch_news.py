#!/usr/bin/env python3
"""
fetch_news.py

Pulls headlines from a list of San Diego news outlets (via RSS/Atom feeds),
groups stories that multiple outlets are covering (by keyword overlap in
the headline), and writes the result to news.json for the static site to
consume.

Run this from GitHub Actions on a schedule (see update-news.yml). It needs
real internet access to news sites, so it will NOT work inside a sandboxed
dev container with restricted egress -- that's expected, run it in CI or
on your own machine.

Usage:
    pip install feedparser python-dateutil
    python3 fetch_news.py
"""

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import feedparser
from dateutil import parser as dateparser

# ---------------------------------------------------------------------------
# 1. SOURCES
#
# Each source lists candidate feed URLs to try, in order. Most WordPress
# sites expose /feed/, which is why you'll see that guessed for sites that
# don't advertise an RSS link. If a source has no working feed, it's logged
# to the "failed" list in news.json's debug block instead of crashing the
# run -- check that list periodically and fix/remove sources.
#
# Sources that are directories/meta-lists rather than actual outlets
# (Wikipedia, the sandiego.org media page, the UCSD libguide, w3newspapers,
# the Reddit thread) are intentionally left out here -- they don't have
# their own headlines to pull. Consider linking them as a static
# "More sources" list elsewhere on the News tab instead.
# ---------------------------------------------------------------------------

SOURCES = [
    # --- City/county-wide ---
    {"name": "KPBS", "candidates": ["https://www.kpbs.org/feeds/news"]},
    {"name": "Voice of San Diego", "candidates": ["https://voiceofsandiego.org/feed/"]},
    {"name": "San Diego Union-Tribune", "candidates": [
        "https://www.sandiegouniontribune.com/feed/",
        "https://www.sandiegouniontribune.com/rss/headlines/most-recent/",
    ]},
    {"name": "The Coast News", "candidates": ["https://thecoastnews.com/feed/"]},
    {"name": "San Diego Reader", "candidates": ["https://www.sandiegoreader.com/rss/news/"]},
    {"name": "Times of San Diego", "candidates": ["https://timesofsandiego.com/feed/"]},
    {"name": "CalMatters (San Diego tag)", "candidates": ["https://calmatters.org/tag/san-diego/feed/"]},
    {"name": "Inside San Diego", "candidates": ["https://www.insidesandiego.org/feed/"]},

    # --- Local / neighborhood ---
    {"name": "OB Rag", "candidates": ["https://obrag.org/?feed=rss2"]},
    {"name": "Coronado Times", "candidates": ["https://coronadotimes.com/feed/"]},
    {"name": "North Coast Current", "candidates": ["https://www.northcoastcurrent.com/feed/"]},
    {"name": "Times-Advocate (Escondido)", "candidates": ["https://www.times-advocate.com/feed/"]},
    {"name": "Imperial Beach News CA", "candidates": ["https://www.imperialbeachnewsca.com/feed/"]},
    {"name": "Del Mar Sandpiper", "candidates": ["https://delmarsandpiper.org/feed/"]},
    {"name": "La Jolla Light", "candidates": ["https://www.sandiegouniontribune.com/la-jolla-light/feed/"]},
    {"name": "Point Loma-OB Monthly", "candidates": ["https://www.sandiegouniontribune.com/pb-monthly/feed/"]},

    # Government "news" pages rarely have RSS. Kept here so failures show up
    # in the debug log rather than silently vanishing -- you'll likely need
    # to write a small custom scraper per site if these consistently fail.
    {"name": "El Cajon City News", "candidates": ["https://www.elcajon.gov/i-want-to/view/city-news/feed/"]},
    {"name": "Lemon Grove City News", "candidates": ["https://www.lemongrove.ca.gov/news/feed/"]},
    {"name": "Solana Beach City News", "candidates": ["https://cityofsolanabeach.ca.gov/en/news/feed"]},
    {"name": "Chula Vista City News", "candidates": ["https://www.chulavistaca.gov/businesses/smart-city/news/feed"]},
    {"name": "Del Mar Weekly", "candidates": ["https://www.delmar.ca.us/352/Del-Mar-Weekly/feed"]},
    {"name": "County News Center", "candidates": ["https://www.countynewscenter.com/feed/"]},
    {"name": "SD Sheriff News Releases", "candidates": ["https://www.sdsheriff.gov/bureaus/media-relations/news-release/feed"]},
]

MAX_AGE_DAYS = 10          # ignore stories older than this
OVERLAP_THRESHOLD = 0.35   # jaccard similarity to consider two headlines "the same story"
REQUEST_TIMEOUT = 15
OUTPUT_PATH = "news.json"

STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "is",
    "are", "was", "were", "be", "with", "by", "from", "as", "it", "its",
    "this", "that", "after", "over", "into", "up", "out", "new", "san",
    "diego", "county", "city", "says", "amid", "how", "what", "why",
}


def tokenize(title):
    words = re.findall(r"[a-z0-9']+", title.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 2}


def jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def fetch_source(source):
    """Try each candidate feed URL in order; return (feed_url, entries) or (None, [])."""
    for url in source["candidates"]:
        try:
            parsed = feedparser.parse(url, request_headers={"User-Agent": "HealthyLivingSD-NewsBot/1.0"})
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {source['name']}: error fetching {url}: {exc}", file=sys.stderr)
            continue
        if parsed.entries:
            return url, parsed.entries
    return None, []


def normalize_entries(source_name, entries):
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    out = []
    for e in entries:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue

        raw_date = e.get("published") or e.get("updated") or e.get("pubDate")
        try:
            published = dateparser.parse(raw_date) if raw_date else None
            if published and published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            published = None

        if published and published < cutoff:
            continue

        out.append({
            "source": source_name,
            "title": title,
            "link": link,
            "published": published.isoformat() if published else None,
            "_sort_date": published or datetime.min.replace(tzinfo=timezone.utc),
            "_tokens": tokenize(title),
        })
    return out


def group_by_overlap(items):
    """Union-find style grouping: items from DIFFERENT sources with headline
    token overlap above OVERLAP_THRESHOLD are merged into one cluster."""
    n = len(items)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    for i in range(n):
        for j in range(i + 1, n):
            if items[i]["source"] == items[j]["source"]:
                continue  # only merge across different outlets
            if jaccard(items[i]["_tokens"], items[j]["_tokens"]) >= OVERLAP_THRESHOLD:
                union(i, j)

    clusters = {}
    for i in range(n):
        root = find(i)
        clusters.setdefault(root, []).append(items[i])
    return list(clusters.values())


def build_news_json():
    all_items = []
    failed = []

    for source in SOURCES:
        print(f"Fetching {source['name']}...")
        feed_url, entries = fetch_source(source)
        if not feed_url:
            failed.append(source["name"])
            continue
        all_items.extend(normalize_entries(source["name"], entries))
        time.sleep(0.5)  # be polite

    clusters = group_by_overlap(all_items)

    stories = []
    for cluster in clusters:
        cluster.sort(key=lambda x: x["_sort_date"], reverse=True)
        primary = cluster[0]
        sources_in_cluster = sorted({c["source"] for c in cluster})
        stories.append({
            "headline": primary["title"],
            "published": primary["published"],
            "outlet_count": len(sources_in_cluster),
            "coverage": [
                {"source": c["source"], "title": c["title"], "link": c["link"], "published": c["published"]}
                for c in cluster
            ],
        })

    stories.sort(key=lambda s: s["published"] or "", reverse=True)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "story_count": len(stories),
        "stories": stories[:60],
        "debug": {
            "sources_configured": len(SOURCES),
            "sources_ok": len(SOURCES) - len(failed),
            "sources_failed": failed,
        },
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nWrote {len(stories)} stories to {OUTPUT_PATH}")
    if failed:
        print(f"Sources with no working feed ({len(failed)}): {', '.join(failed)}", file=sys.stderr)


if __name__ == "__main__":
    build_news_json()
