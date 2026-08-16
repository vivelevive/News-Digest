#!/usr/bin/env python3
"""
Builds docs/data/digest.json from the RSS feeds and GNews queries listed in
config/sources.json.

Design notes (see README.md for the full picture):
- Every network call is wrapped so a single dead feed or API hiccup can never
  crash the whole run -- it's logged as a warning and the run continues with
  whatever sources succeeded.
- Summaries are EXTRACTIVE (trimmed/cleaned RSS description, capped at 5
  sentences) and "why it matters" is a small RULE-BASED template keyed by
  topic category. No paid AI API is called, per the free-tier-only
  constraint.
- Global exclude filters drop fitness/health and cybersecurity stories from
  every category, even if a source's RSS mixes them in.
- "Derived" categories (stock index movements, ESG) don't fetch their own
  feeds; they re-filter items already pulled for other categories by
  keyword, so they cost zero extra requests.
- GNews (used only for the AI-in-financial-services category) is capped at a
  small, fixed number of queries per run to comfortably stay under the free
  tier's ~100 requests/day limit even with manual re-runs.
- Each category is capped at MAX_ITEMS_PER_CATEGORY (5). Within a category,
  items are ranked by relevance_score() -- recency first, with a light
  rule-based "impact keyword" signal as a tiebreaker -- not just raw
  published-date order.
"""

import json
import os
import re
import sys
import time
import html
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import feedparser
import requests
from dateutil import parser as dateparser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_PATH = os.path.join(ROOT, "config", "sources.json")
OUTPUT_PATH = os.path.join(ROOT, "docs", "data", "digest.json")

USER_AGENT = "Mozilla/5.0 (compatible; PersonalNewsDigestBot/1.0; +https://github.com/)"
REQUEST_TIMEOUT = 15
MAX_ITEMS_PER_CATEGORY = 5
GNEWS_MAX_PER_QUERY = 6
GNEWS_TIMEOUT = 15

# Stories mentioning these terms are dropped everywhere, regardless of
# source or category (explicitly excluded content per the project brief).
EXCLUDE_KEYWORDS = [
    # fitness / health
    "workout", "fitness tracker", "weight loss", "diet plan", "marathon training",
    "gym routine", "yoga pose", "calorie", "bodybuilding",
    # cybersecurity
    "ransomware", "data breach", "cyberattack", "cyber attack", "zero-day",
    "zero day", "phishing", "malware", "vulnerability disclosed", "hacker group",
    "hacked", "cybersecurity",
    # distressing/sensitive content not appropriate for an unattended daily digest
    "child sexual abuse", "csam", "child exploitation", "sextortion",
    "explicit imagery of a minor", "child abuse material",
]

WHY_IT_MATTERS = {
    "self-development": "Sharpens strategic thinking and leadership practice you can apply directly at work.",
    "tech-personal": "Worth knowing for your own devices, tools, and everyday tech decisions.",
    "tech-us": "Signals where US tech investment, product strategy, and regulation are heading.",
    "tech-asia": "Tracks major tech and business shifts across Asia that can affect regional markets and supply chains.",
    "tech-europe": "Flags European tech and regulatory moves that often set the tone for global tech policy.",
    "tech-australia": "Local tech-industry news relevant to the Australian market and workplace.",
    "finance": "Moves markets and macro conditions that affect savings, investments, and the broader economy.",
    "stock-index": "Directly relevant to ASX 200 / S&P 500-linked holdings, with analyst rationale for context.",
    "regulatory": "Regulatory and compliance shifts that affect how financial-services firms must operate.",
    "ma-deals": "Corporate deals and geopolitical shifts that can ripple into market pricing, hiring, and leadership moves.",
    "property": "Relevant to Sydney/NSW property market conditions if you're tracking prices or planning a move.",
    "ai-finance": "Tracks how AI adoption is reshaping financial services and enterprise operations more broadly.",
    "esg": "Brief ESG/sustainable-finance headline for awareness -- lowest priority, headline only.",
    "paywalled": "Big-picture headline from a paywalled outlet -- follow through on your own subscription to read in full.",
}

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])")
TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
WORDPRESS_BOILERPLATE_RE = re.compile(r"\s*The post .+? appeared first on .+?\.?\s*$")
TRAILING_SOURCE_SUFFIX_RE = re.compile(r"\s+[-|]\s+[\w.\s]+$")

# Google News RSS occasionally surfaces non-article pages (stock-quote pages,
# entries with no real headline) instead of news stories. These patterns
# catch the common shapes so they get filtered out rather than shown as cards.
JUNK_TITLE_PATTERNS = [
    re.compile(r"^[A-Z0-9]{1,8}\.[A-Z]{1,4}\b"),  # ticker symbols, e.g. "MCGAU.OQ - Reuters"
    re.compile(r"stock price\s*&?\s*latest news", re.IGNORECASE),
    re.compile(r"^-\s"),  # blank headline, Google News left only "- Source Name"
]


def is_junk_title(title):
    t = (title or "").strip()
    if not t:
        return True
    return any(p.search(t) for p in JUNK_TITLE_PATTERNS)


# Words that tend to mark a story as a bigger deal than routine coverage --
# used as a (rule-based) proxy for "relevance / interest" when picking which
# 5 items per category make the cut. Not a substitute for real editorial
# judgement, just a deterministic tiebreaker alongside recency.
IMPACT_KEYWORDS = [
    "record", "surge", "surges", "plunge", "plunges", "soar", "soars", "crash", "crashes",
    "billion", "collapse", "warns", "warning", "fine", "fined", "penalty", "penalties",
    "acquisition", "acquires", "merger", "takeover", "buyout", "ceo", "resigns", "resignation",
    "steps down", "ipo", "regulator", "lawsuit", "sues", "investigation", "probe",
    "breakthrough", "unveils", "launches", "cuts rates", "raises rates", "rate hike",
    "rate cut", "layoffs", "job cuts", "profit", "loss", "earnings", "guidance",
    "downgrade", "upgrade", "ban", "banned", "sanctions", "exclusive", "deal",
]


def relevance_score(item, now):
    """Higher is better. Blends recency (dominant factor -- this is a *daily*
    digest) with a light keyword-based "impact" signal so that, among
    similarly-fresh stories, the ones that read as more consequential rank
    first."""
    published = item.get("published")
    if published:
        try:
            age_hours = max(0.0, (now - dateparser.parse(published)).total_seconds() / 3600)
        except (ValueError, TypeError, OverflowError):
            age_hours = 999.0
    else:
        age_hours = 999.0  # undated items (e.g. Nikkei Asia) rank behind dated ones
    recency_score = 1.0 / (1.0 + age_hours / 24.0)  # ~1.0 fresh -> ~0.2 at a week old

    haystack = f"{item['title']} {item.get('raw_summary', '')}".lower()
    impact_hits = sum(1 for kw in IMPACT_KEYWORDS if kw in haystack)
    impact_score = min(impact_hits, 4) * 0.12  # capped so recency still dominates

    return recency_score + impact_score


def log(msg):
    print(f"[build_digest] {msg}", file=sys.stderr)


def clean_text(raw):
    if not raw:
        return ""
    text = html.unescape(TAG_RE.sub(" ", raw))
    return WHITESPACE_RE.sub(" ", text).strip()


_PUNCT_RE = re.compile(r"[^\w\s]")


def _looks_like_title_repeat(title, summary):
    """True if the summary is just the title again -- optionally with a
    trailing source name tacked on, with or without a separating dash -- as
    seen from Google News and some WordPress feeds. Not worth showing twice
    on a card."""
    def norm(s):
        s = TRAILING_SOURCE_SUFFIX_RE.sub("", s)
        s = _PUNCT_RE.sub("", s).lower()
        return re.sub(r"\s+", " ", s).strip()

    norm_title, norm_summary = norm(title), norm(summary)
    if not norm_title or not norm_summary:
        return False
    return norm_summary == norm_title or norm_summary.startswith(norm_title)


def make_summary(raw, title="", max_sentences=5):
    text = clean_text(raw)
    text = WORDPRESS_BOILERPLATE_RE.sub("", text).strip()
    if not text:
        return ""
    if title and _looks_like_title_repeat(title, text):
        return ""
    sentences = SENTENCE_SPLIT_RE.split(text)
    summary = " ".join(sentences[:max_sentences]).strip()
    # Guard against a single giant "sentence" (some feeds have no punctuation).
    if len(summary) > 900:
        summary = summary[:880].rsplit(" ", 1)[0] + "…"
    return summary


def contains_excluded_keyword(*texts):
    joined = " ".join(t or "" for t in texts).lower()
    if any(kw in joined for kw in EXCLUDE_KEYWORDS):
        return True
    # Child-sexual-abuse-adjacent content: no single keyword is reliable, so
    # require a minor/family term alongside an explicit-imagery term before
    # excluding (keeps unrelated "explicit content" stories about adults).
    minor_terms = ("child", "minor", "stepfather", "stepdaughter", "daughter", "son", "childhood", "kid ", "teen")
    explicit_terms = ("explicit imagery", "explicit image", "explicit photo", "nude image", "deepfake")
    if any(m in joined for m in minor_terms) and any(e in joined for e in explicit_terms):
        return True
    return False


def contains_any_keyword(keywords, *texts):
    joined = " ".join(t or "" for t in texts).lower()
    return any(kw.lower() in joined for kw in keywords)


def normalize_url(url):
    """Strip tracking query params / fragment so the same story from two
    feeds (or a re-run) dedupes correctly."""
    if not url:
        return url
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def parse_published(entry):
    for key in ("published", "updated", "created"):
        val = entry.get(key)
        if val:
            try:
                dt = dateparser.parse(val)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except (ValueError, TypeError, OverflowError):
                continue
    return None


def fetch_rss(source, warnings):
    url = source["url"]
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as exc:  # noqa: BLE001 - a single dead feed must not kill the run
        warnings.append(f"{source['name']}: feed unreachable ({exc.__class__.__name__})")
        log(f"WARN fetch failed for {source['name']} ({url}): {exc}")
        return []

    if not parsed.entries:
        warnings.append(f"{source['name']}: feed returned no items")
        log(f"WARN no entries for {source['name']} ({url})")
        return []

    items = []
    for entry in parsed.entries:
        title = clean_text(entry.get("title", ""))
        link = entry.get("link", "")
        if not title or not link:
            continue
        summary_raw = entry.get("summary") or entry.get("description") or ""
        published = parse_published(entry)
        items.append({
            "title": title,
            "url": link,
            "source": source["name"],
            "published": published.isoformat() if published else None,
            "raw_summary": summary_raw,
            "region": source.get("region"),
            "paywalled": bool(source.get("paywalled", False)),
        })
    return items


def fetch_gnews(query, api_key, warnings, lang="en"):
    try:
        resp = requests.get(
            "https://gnews.io/api/v4/search",
            params={
                "q": query,
                "lang": lang,
                "max": GNEWS_MAX_PER_QUERY,
                "apikey": api_key,
            },
            timeout=GNEWS_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"GNews query '{query}' failed ({exc.__class__.__name__})")
        log(f"WARN GNews query failed for '{query}': {exc}")
        return []

    items = []
    for article in data.get("articles", []):
        title = clean_text(article.get("title", ""))
        link = article.get("url", "")
        if not title or not link:
            continue
        items.append({
            "title": title,
            "url": link,
            "source": (article.get("source") or {}).get("name") or "GNews",
            "published": article.get("publishedAt"),
            "raw_summary": article.get("description") or "",
            "region": None,
            "paywalled": False,
        })
    return items


def dedupe(items):
    """Drop items with a URL we've already seen, and also drop exact-title
    repeats (common with syndicated wire stories appearing under several
    outlet names via GNews)."""
    seen_urls = set()
    seen_titles = set()
    out = []
    for item in items:
        url_key = normalize_url(item["url"])
        title_key = re.sub(r"\s+", " ", item["title"].strip().lower())
        if url_key in seen_urls or title_key in seen_titles:
            continue
        seen_urls.add(url_key)
        seen_titles.add(title_key)
        out.append(item)
    return out


def finalize_items(raw_items, category_id, cap, now):
    items = dedupe(raw_items)
    items = [i for i in items if not is_junk_title(i["title"])]
    items = [i for i in items if not contains_excluded_keyword(i["title"], i.get("raw_summary", ""))]
    # Blend of recency + a light "impact" keyword signal -- see relevance_score().
    items.sort(key=lambda i: relevance_score(i, now), reverse=True)
    items = items[:cap]

    result = []
    for i in items:
        result.append({
            "title": i["title"],
            "url": i["url"],
            "source": i["source"],
            "published": i["published"],
            "summary": make_summary(i.get("raw_summary", ""), i["title"]),
            "why_it_matters": WHY_IT_MATTERS.get(category_id, ""),
            "region": i.get("region"),
            "paywalled": i.get("paywalled", False),
        })
    return result


def build():
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    warnings = []
    raw_pool = {}  # category_id -> list of raw items (pre-filter), for "derived" categories
    output_categories = []
    gnews_key = os.environ.get("GNEWS_API_KEY", "").strip()
    now = datetime.now(timezone.utc)

    for cat in config["categories"]:
        cat_id = cat["id"]
        cap = MAX_ITEMS_PER_CATEGORY

        if cat.get("derivedFrom"):
            pool = []
            for src_cat in cat["derivedFrom"]:
                pool.extend(raw_pool.get(src_cat, []))
            keywords = cat.get("keywords", [])
            filtered = [i for i in pool if contains_any_keyword(keywords, i["title"], i.get("raw_summary", ""))]
            items = finalize_items(filtered, cat_id, cap, now)

        elif cat.get("type") == "gnews":
            if not gnews_key:
                warnings.append(f"{cat['title']}: GNEWS_API_KEY not set, topic skipped")
                items = []
            else:
                raw = []
                for query in cat.get("queries", []):
                    raw.extend(fetch_gnews(query, gnews_key, warnings))
                    time.sleep(1)  # be polite to the free-tier API
                items = finalize_items(raw, cat_id, cap, now)

        else:
            raw = []
            for source in cat.get("sources", []):
                raw.extend(fetch_rss(source, warnings))
            raw_pool[cat_id] = raw
            # Optional post-fetch keyword filter (e.g. restrict a national
            # property feed down to Sydney/NSW stories) -- any category can
            # set "keywords", not just derived ones.
            keywords = cat.get("keywords")
            if keywords:
                raw = [i for i in raw if contains_any_keyword(keywords, i["title"], i.get("raw_summary", ""))]
            items = finalize_items(raw, cat_id, cap, now)

        output_categories.append({
            "id": cat_id,
            "title": cat["title"],
            "navLabel": cat.get("navLabel", cat["title"]),
            "subtitle": cat.get("subtitle"),
            "collapsedByDefault": bool(cat.get("collapsedByDefault", False)),
            "items": items,
        })

    digest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "warnings": warnings,
        "categories": output_categories,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(digest, f, ensure_ascii=False, indent=2)

    total_items = sum(len(c["items"]) for c in output_categories)
    log(f"Wrote {OUTPUT_PATH}: {total_items} items across {len(output_categories)} categories, {len(warnings)} warnings")
    if warnings:
        for w in warnings:
            log(f"  - {w}")


if __name__ == "__main__":
    build()
