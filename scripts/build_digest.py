#!/usr/bin/env python3
"""
Builds docs/data/digest.json from the RSS feeds and GNews queries listed in
config/sources.json.

Design notes (see README.md for the full picture):
- Every network call is wrapped so a single dead feed or API hiccup can never
  crash the whole run -- it's logged as a warning and the run continues with
  whatever sources succeeded.
- Summaries are EXTRACTIVE (trimmed/cleaned RSS description, capped at
  MAX_SUMMARY_SENTENCES) and "why it matters" is a small RULE-BASED template
  keyed by topic category. No paid AI API is called, per the free-tier-only
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
- A story is only ever shown ONCE across the whole digest. Categories are
  processed in config order and each claims its picks from a global
  seen-URL/seen-title set (see `claimed` in build()); a later category that
  shares a source with an earlier one (e.g. Technology (Personal Use) and
  Major Tech Trends -- US both read TechCrunch/The Verge) simply gets the
  next-best distinct stories instead of repeating the first category's picks.
- The top-level "brief" array is a ~5-minute-read digest-of-the-digest: the
  single top-ranked story from each non-collapsed category, for the home
  page. Full per-category sections still follow underneath.
"""

import json
import os
import re
import sys
import time
import html
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, urljoin

import feedparser
import requests
from dateutil import parser as dateparser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_PATH = os.path.join(ROOT, "config", "sources.json")
OUTPUT_PATH = os.path.join(ROOT, "docs", "data", "digest.json")

USER_AGENT = "Mozilla/5.0 (compatible; PersonalNewsDigestBot/1.0; +https://github.com/)"
REQUEST_TIMEOUT = 15
MAX_ITEMS_PER_CATEGORY = 5
MAX_SUMMARY_SENTENCES = 2  # concise, exec-level -- not a full extractive dump
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
    "the-australian": "Big-picture Australian headline -- follow through on your own subscription to read in full.",
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


_PERCENT_ENCODING_RE = re.compile(r"%[0-9A-Fa-f]{2}")


def is_junk_title(title):
    t = (title or "").strip()
    if not t:
        return True
    if any(p.search(t) for p in JUNK_TITLE_PATTERNS):
        return True
    # Occasionally Google News' RSS itself hands back a corrupted title --
    # a hash-looking prefix glued directly onto still-URL-encoded text, e.g.
    # "9718d10...49Funds%20run%20by%20listed%20group%20Dexus...". A real
    # headline never contains literal percent-encoding; seeing it more than
    # once is a reliable enough signal this is unrecoverable upstream junk,
    # not something worth trying to repair.
    if len(_PERCENT_ENCODING_RE.findall(t)) >= 2:
        return True
    return False


# Words that tend to mark a story as a bigger deal than routine coverage --
# used as a (rule-based) proxy for "relevance / interest" when picking which
# 5 items per category make the cut. Not a substitute for real editorial
# judgement, just a deterministic tiebreaker alongside recency. Matched as
# whole words (see IMPACT_KEYWORD_RES) so e.g. "ban" doesn't fire inside
# "banking" or "fine" inside "define".
IMPACT_KEYWORDS = [
    "record", "surge", "surges", "plunge", "plunges", "soar", "soars", "crash", "crashes",
    "billion", "collapse", "warns", "warning", "fine", "fined", "penalty", "penalties",
    "acquisition", "acquires", "merger", "takeover", "buyout", "ceo", "resigns", "resignation",
    "steps down", "ipo", "regulator", "lawsuit", "sues", "investigation", "probe",
    "breakthrough", "unveils", "launches", "cuts rates", "raises rates", "rate hike",
    "rate cut", "layoffs", "job cuts", "profit", "loss", "earnings", "guidance",
    "downgrade", "upgrade", "ban", "banned", "sanctions", "exclusive", "deal",
]
IMPACT_KEYWORD_RES = [re.compile(r"\b" + re.escape(kw) + r"\b") for kw in IMPACT_KEYWORDS]


# Shopping-deal roundups and personal-blog listicles ("This $14 cable is my
# secret to...", "Best early Labor Day deals") are common filler on some
# feeds (ZDNet in particular) but aren't "trends" news -- pushed down rather
# than hard-excluded, so a category still has something to show if that's
# all a feed returned on a given day. Deliberately anchored/narrow: an
# earlier, unanchored version of "here's how" matched inside completely
# normal headlines ("Here's how the Fed's decision will hit mortgages"),
# quietly punishing legitimate explainer stories along with the clickbait.
LOW_VALUE_PATTERNS = [
    re.compile(r"\bdeals?\b", re.IGNORECASE),
    re.compile(r"%\s?off", re.IGNORECASE),
    re.compile(r"\bcoupon\b", re.IGNORECASE),
    re.compile(r"\bmy secret\b", re.IGNORECASE),
    re.compile(r"^here.?s how", re.IGNORECASE),
    re.compile(r"^how i\b", re.IGNORECASE),
    re.compile(r"\bbuying guide\b", re.IGNORECASE),
]
# Kept below IMPACT_KEYWORD_RES's max possible bonus (min(hits,4)*0.15=0.6)
# on purpose -- a listicle-shaped title that's ALSO genuinely high-impact
# (rare, but possible) should still be able to claw its way back up rather
# than the penalty being an unconditional veto.
LOW_VALUE_PENALTY = 0.3


def relevance_score(item, now, boost_keywords=None):
    """Higher is better. Blends recency (dominant factor -- this is a *daily*
    digest) with a light keyword-based "impact" signal so that, among
    similarly-fresh stories, the ones that read as more consequential rank
    first. `boost_keywords`, when given, is a stronger category-specific
    signal (e.g. Stock Index Movements boosting literal "ASX 200"/"S&P 500"
    mentions) that can outrank recency -- without it, a category whose
    keyword *filter* is necessarily broad (to have anything to show most
    days) just surfaces whatever's freshest, not what's actually on-topic."""
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
    impact_hits = sum(1 for pat in IMPACT_KEYWORD_RES if pat.search(haystack))
    impact_score = min(impact_hits, 4) * 0.15  # capped so recency still dominates

    low_value_penalty = LOW_VALUE_PENALTY if any(p.search(item["title"]) for p in LOW_VALUE_PATTERNS) else 0.0

    boost_score = 0.0
    if boost_keywords:
        boost_hits = sum(1 for kw in boost_keywords if kw.lower() in haystack)
        boost_score = min(boost_hits, 3) * 0.5

    return recency_score + impact_score - low_value_penalty + boost_score


def diversify_by_source(items, cap):
    """items must already be sorted by relevance (desc). Round-robins across
    distinct sources so one prolific feed (Bloomberg posts far more often
    than RBA ever will) can't quietly fill every slot in a category -- each
    source still contributes its own best items first, in relevance order,
    just interleaved rather than let the single busiest source sweep the
    board. Returns at most `cap` items; re-sort by relevance afterward if
    you want display order to ignore which source an item came from."""
    from collections import defaultdict, deque
    queues = defaultdict(deque)
    source_order = []
    for i in items:
        src = i["source"]
        if src not in queues:
            source_order.append(src)
        queues[src].append(i)

    out = []
    while len(out) < cap and any(queues[s] for s in source_order):
        for s in source_order:
            if len(out) >= cap:
                break
            if queues[s]:
                out.append(queues[s].popleft())
    return out


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


def first_sentence(text):
    if not text:
        return ""
    return SENTENCE_SPLIT_RE.split(text.strip())[0].strip()


def normalize_title(title):
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def make_summary(raw, title="", max_sentences=MAX_SUMMARY_SENTENCES):
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


JUNK_SUMMARY_PATTERNS = [
    re.compile(r"^breadcrumb\b", re.IGNORECASE),
    re.compile(r"skip to (main )?content", re.IGNORECASE),
    re.compile(r"^on this page\b", re.IGNORECASE),
]


def is_junk_summary(text):
    """Some sources' "description" field is scraped page furniture, not
    article content (APRA/ASIC in particular emit breadcrumb/nav text).
    Treat that the same as an empty summary -- worth trying to backfill,
    not worth showing to the user."""
    t = (text or "").strip()
    if not t:
        return True
    return any(p.search(t) for p in JUNK_SUMMARY_PATTERNS)


def is_google_news_url(url):
    try:
        return urlsplit(url).netloc.lower().endswith("news.google.com")
    except (ValueError, AttributeError):
        return False


def strip_source_suffix(title):
    """Google News titles are "Real Headline - Source Name". We already
    show the source name in the card's byline, so a raw title makes it
    read as "Real Headline - Source Name" twice on the same card."""
    stripped = TRAILING_SOURCE_SUFFIX_RE.sub("", title).strip()
    return stripped or title  # never return an empty title


_OG_DESC_RE = re.compile(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']*)["\']', re.IGNORECASE)
_META_DESC_RE = re.compile(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']', re.IGNORECASE)
ARTICLE_FETCH_TIMEOUT = 6


def fetch_article_meta(url):
    """Best-effort: GET the article page (following redirects -- this is
    also how Google News' opaque /rss/articles/... links resolve to the
    real publisher URL, without needing to reverse-engineer Google's
    encoding) and pull its og:description/meta description. Returns
    (final_url, description); either half may be empty/None on any failure.
    Never raises -- a blocked or slow publisher must not fail the build."""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=ARTICLE_FETCH_TIMEOUT, allow_redirects=True)
        body = resp.text[:200_000]  # cap read size; we only need the <head>
        m = _OG_DESC_RE.search(body) or _META_DESC_RE.search(body)
        desc = html.unescape(m.group(1)).strip() if m else ""
        return resp.url, desc
    except Exception:  # noqa: BLE001 - best-effort only
        return None, ""


# --- Optional real AI summary / why-it-matters via the Gemini free tier ---
#
# Everything else in this file is deliberately rule-based/extractive, per
# the project's free-tier-only constraint. This is the one genuinely
# optional exception: Google AI Studio's Gemini free tier (Flash/Flash-Lite)
# needs no credit card and no billing account -- just a free Google account
# and an API key the user generates themselves (github.com/settings, sorry,
# aistudio.google.com/apikey) and adds as the GEMINI_API_KEY repo secret.
# Entirely optional: with no key set, or on ANY failure (network, quota,
# malformed response), a category/item just keeps its existing rule-based
# summary and why_it_matters -- this must never be what breaks a build.
GEMINI_MODEL = "gemini-flash-latest"
GEMINI_TIMEOUT = 20
GEMINI_RATE_LIMIT_DELAY = 4.5  # seconds between calls; free tier is ~15 req/min

# Context about the reader, used so "why it matters" can name a specific,
# personal read-through instead of restating the category. Deliberately
# just a paraphrase of the project brief (ASX 200/S&P 500 exposure, AUD,
# Sydney/NSW property, career in financial services) -- not new information
# invented about the user.
READER_PROFILE = (
    "a Sydney-based professional with an interest in ASX 200 and S&P 500 "
    "exposure, AUD movements, the Sydney/NSW property market, AI adoption "
    "in financial services, and their own career/leadership development"
)

GEMINI_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING", "description": "1-2 sentence, exec-level summary of the article's actual content."},
        "why_it_matters": {"type": "STRING", "description": "1 sentence on why THIS specific story matters to the reader, naming the concrete detail (company, number, index, policy) that makes it relevant -- not a generic category restatement."},
    },
    "required": ["summary", "why_it_matters"],
}


def generate_ai_content(item, category_title, api_key):
    """Returns (summary, why_it_matters, error_detail). On success,
    error_detail is None. On any failure, summary/why_it_matters are None
    and error_detail is a short diagnostic string -- callers fall back to
    the rule-based versions either way (nothing ever ships blank), but the
    error is worth surfacing (see build()'s use of ai_stats) rather than
    silently discarding it: a wrong secret name or invalid key would
    otherwise fail all 69 calls with zero trace of why. Source text is
    whatever we already have (title + raw_summary), which is often thin (a
    title-only Google News item) -- the prompt is written to still produce
    something useful, but a genuinely title-only story will get a thinner
    AI summary too. That's an honest reflection of the input, not a bug to
    chase."""
    source_text = clean_text(item.get("raw_summary", "")) or "(no article text available -- title only)"
    prompt = (
        f"You are writing one card in a daily news digest for {READER_PROFILE}. "
        f"This story is filed under the \"{category_title}\" category.\n\n"
        f"Headline: {item['title']}\n"
        f"Source: {item['source']}\n"
        f"Available article text: {source_text}\n\n"
        "Write a concise, exec-level 1-2 sentence summary of what the article actually says "
        "(not the headline restated), and a single sentence on why this specific story -- not "
        "the category in general -- matters to this reader. Be concrete: name the company, "
        "number, index, or policy involved where relevant. If the available text is too thin "
        "to summarize meaningfully, say so briefly rather than inventing detail."
    )
    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            params={"key": api_key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseSchema": GEMINI_RESPONSE_SCHEMA,
                    "temperature": 0.3,
                    "maxOutputTokens": 300,
                },
            },
            timeout=GEMINI_TIMEOUT,
        )
        if not resp.ok:
            # Surface the API's own message (e.g. "API key not valid",
            # "RESOURCE_EXHAUSTED") rather than just an HTTP status code.
            try:
                api_msg = resp.json().get("error", {}).get("message", "")
            except Exception:  # noqa: BLE001
                api_msg = ""
            return None, None, f"HTTP {resp.status_code}{': ' + api_msg if api_msg else ''}"
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        summary = clean_text(parsed.get("summary", "")).strip()
        why = clean_text(parsed.get("why_it_matters", "")).strip()
        if not summary or not why:
            return None, None, "response parsed but summary/why_it_matters was empty"
        return summary, why, None
    except requests.exceptions.Timeout:
        return None, None, f"timed out after {GEMINI_TIMEOUT}s"
    except Exception as exc:  # noqa: BLE001 - any failure just means "use the rule-based version"
        return None, None, f"{exc.__class__.__name__}: {exc}"


def enrich_item(item):
    """Mutates item in place. Only called on the small number of items that
    actually made a category's final cut (~5 x category count per run), so
    the extra request this can cost per item is bounded.

    IMPORTANT caveat about Google News links (used as a substitute for
    several sources whose native RSS was discontinued -- see sources.json):
    their .../rss/articles/CBMi... URL is a client-side (JavaScript)
    redirect, not an HTTP one. It resolves correctly for a real user in a
    real browser -- verified directly, a phone tapping the link lands on
    the actual publisher article -- but `requests` can't execute JS, so a
    plain GET just returns Google's interstitial shell page: no real
    og:description to scrape, and following redirects doesn't reach the
    real URL either. Decoding it properly would mean reverse-engineering
    Google's internal batchexecute endpoint (undocumented, and arguably
    outside what their RSS interface sanctions) or running a full headless
    browser in the Actions job for every such item. Given the link already
    works for the person actually using this app, that cost isn't worth
    paying -- so for Google News items this function only cleans up the
    title's redundant "- Source Name" suffix (the source is already shown
    in the byline) and does NOT attempt to touch the URL or backfill the
    summary. For everything else -- direct RSS/GNews links, which point at
    the real page and respond to a plain GET -- it still tries to backfill
    an empty/junk summary from the article's own og:description."""
    if is_google_news_url(item["url"]):
        item["title"] = strip_source_suffix(item["title"])
        return

    prelim_summary = make_summary(item.get("raw_summary", ""), item["title"])
    if not is_junk_summary(prelim_summary):
        return

    _final_url, desc = fetch_article_meta(item["url"])
    if desc and not is_junk_summary(desc):
        item["raw_summary"] = desc


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
        # Some feeds (HBR's FeedBurner proxy in particular) emit bare
        # root-relative links like "/2026/09/some-post" with no host and no
        # xml:base, so feedparser can't resolve them at all -- they'd
        # otherwise ship as dead links. Resolving against the *feed's own*
        # URL is wrong here too (feeds.feedburner.com doesn't host the
        # article), so a source can declare the real site's base via
        # "linkBase" in sources.json; that's tried first, the feed URL only
        # as a last resort for the common case where they're the same host.
        if not urlsplit(link).scheme:
            link = urljoin(source.get("linkBase", url), link)
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
        title_key = normalize_title(item["title"])
        if url_key in seen_urls or title_key in seen_titles:
            continue
        seen_urls.add(url_key)
        seen_titles.add(title_key)
        out.append(item)
    return out


def finalize_items(raw_items, category_id, category_title, cap, now, claimed, boost_keywords=None, gemini_key=None, ai_stats=None):
    """claimed is a (urls: set, titles: set) pair shared across the whole
    build -- a story already picked for an earlier category is excluded here
    and never shown twice across the digest. ai_stats, if given, is a dict
    this increments "attempted"/"succeeded" counters on, so build() can log
    a one-line summary of how the optional Gemini pass went."""
    claimed_urls, claimed_titles = claimed
    items = dedupe(raw_items)
    items = [i for i in items if not is_junk_title(i["title"])]
    items = [i for i in items if not contains_excluded_keyword(i["title"], i.get("raw_summary", ""))]
    items = [
        i for i in items
        if normalize_url(i["url"]) not in claimed_urls and normalize_title(i["title"]) not in claimed_titles
    ]
    # Blend of recency + a light "impact" keyword signal -- see relevance_score().
    items.sort(key=lambda i: relevance_score(i, now, boost_keywords), reverse=True)
    # Enforce source diversity (a single prolific feed shouldn't sweep every
    # slot), then re-sort the chosen set back to relevance order for display
    # -- diversify_by_source's interleaving is a selection mechanism, not
    # the order the user should actually read them in.
    items = diversify_by_source(items, cap)
    items.sort(key=lambda i: relevance_score(i, now, boost_keywords), reverse=True)

    result = []
    for i in items:
        claimed_urls.add(normalize_url(i["url"]))
        claimed_titles.add(normalize_title(i["title"]))
        # Only ever called on items that made the final cut -- see
        # enrich_item's docstring for why that bound matters.
        enrich_item(i)

        summary = make_summary(i.get("raw_summary", ""), i["title"])
        why_it_matters = WHY_IT_MATTERS.get(category_id, "")

        if gemini_key:
            if ai_stats is not None:
                ai_stats["attempted"] = ai_stats.get("attempted", 0) + 1
            ai_summary, ai_why, ai_error = generate_ai_content(i, category_title, gemini_key)
            time.sleep(GEMINI_RATE_LIMIT_DELAY)
            if ai_summary and ai_why:
                summary, why_it_matters = ai_summary, ai_why
                if ai_stats is not None:
                    ai_stats["succeeded"] = ai_stats.get("succeeded", 0) + 1
            elif ai_stats is not None and ai_error and "last_error" not in ai_stats:
                # Only keep the first distinct failure -- if all 69 calls
                # are failing the same way (a bad key, say), one example is
                # plenty; we don't need it repeated 69 times in warnings.
                ai_stats["last_error"] = ai_error

        result.append({
            "title": i["title"],
            "url": i["url"],
            "source": i["source"],
            "published": i["published"],
            "summary": summary,
            "why_it_matters": why_it_matters,
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
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip() or None
    ai_stats = {"attempted": 0, "succeeded": 0}
    now = datetime.now(timezone.utc)
    # Shared across every category so the same story is never selected twice
    # across the whole digest -- see finalize_items().
    claimed = (set(), set())

    for cat in config["categories"]:
        cat_id = cat["id"]
        cap = MAX_ITEMS_PER_CATEGORY
        # Strong category-specific ranking signal, e.g. Stock Index
        # Movements boosting literal "ASX 200"/"S&P 500" mentions above
        # generic market chatter -- see relevance_score().
        boost_keywords = cat.get("boostKeywords")

        if cat.get("derivedFrom"):
            pool = []
            for src_cat in cat["derivedFrom"]:
                pool.extend(raw_pool.get(src_cat, []))
            keywords = cat.get("keywords", [])
            filtered = [i for i in pool if contains_any_keyword(keywords, i["title"], i.get("raw_summary", ""))]
            items = finalize_items(filtered, cat_id, cat["title"], cap, now, claimed, boost_keywords, gemini_key, ai_stats)

        elif cat.get("type") == "gnews":
            if not gnews_key:
                warnings.append(f"{cat['title']}: GNEWS_API_KEY not set, topic skipped")
                items = []
            else:
                raw = []
                for query in cat.get("queries", []):
                    raw.extend(fetch_gnews(query, gnews_key, warnings))
                    time.sleep(1)  # be polite to the free-tier API
                items = finalize_items(raw, cat_id, cat["title"], cap, now, claimed, boost_keywords, gemini_key, ai_stats)

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
            items = finalize_items(raw, cat_id, cat["title"], cap, now, claimed, boost_keywords, gemini_key, ai_stats)

        output_categories.append({
            "id": cat_id,
            "title": cat["title"],
            "navLabel": cat.get("navLabel", cat["title"]),
            "subtitle": cat.get("subtitle"),
            "collapsedByDefault": bool(cat.get("collapsedByDefault", False)),
            "items": items,
        })

    # "Brief": a ~5-minute-read digest-of-the-digest for the home page -- the
    # single top-ranked (already-selected) story from each non-collapsed
    # category, in the same order as the full sections below.
    brief = []
    for cat in output_categories:
        if cat["collapsedByDefault"] or not cat["items"]:
            continue
        top = cat["items"][0]
        # First sentence only -- the brief needs to stay skimmable; the full
        # (up to MAX_SUMMARY_SENTENCES) summary is still on the card below.
        takeaway = first_sentence(top["summary"]) or top["why_it_matters"]
        brief.append({
            "categoryId": cat["id"],
            "categoryTitle": cat["title"],
            "title": top["title"],
            "url": top["url"],
            "source": top["source"],
            "takeaway": takeaway,
            "paywalled": top["paywalled"],
        })

    if gemini_key:
        # Surface a diagnosable message right in warnings (which lands in
        # digest.json -- readable without any GitHub auth) if Gemini never
        # worked at all this run, so "0/69 succeeded" doesn't have to be
        # dug for in the Actions log.
        if ai_stats["attempted"] and not ai_stats["succeeded"]:
            warnings.append(f"Gemini: 0/{ai_stats['attempted']} calls succeeded -- {ai_stats.get('last_error', 'unknown error')} (using rule-based summaries instead)")
        elif ai_stats["succeeded"] < ai_stats["attempted"]:
            warnings.append(f"Gemini: {ai_stats['succeeded']}/{ai_stats['attempted']} calls succeeded -- some items fell back to rule-based summaries")

    digest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "warnings": warnings,
        "brief": brief,
        "categories": output_categories,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(digest, f, ensure_ascii=False, indent=2)

    total_items = sum(len(c["items"]) for c in output_categories)
    log(f"Wrote {OUTPUT_PATH}: {total_items} items across {len(output_categories)} categories "
        f"({len(brief)} in the brief), {len(warnings)} warnings")
    if gemini_key:
        log(f"Gemini AI summaries: {ai_stats['succeeded']}/{ai_stats['attempted']} succeeded "
            f"(everything else used the rule-based summary/why-it-matters as a fallback)")
    if warnings:
        for w in warnings:
            log(f"  - {w}")


if __name__ == "__main__":
    build()
