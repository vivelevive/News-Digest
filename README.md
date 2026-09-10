# News Digest

A personal, mobile-first daily news dashboard. A GitHub Actions job runs every
morning, pulls stories from RSS feeds (and a small GNews API quota), filters
out fitness/health and cybersecurity content, groups everything by topic, and
deploys the result straight to GitHub Pages -- a static site you can "Add to
Home Screen" on Android Chrome.

No paid services are required anywhere in this pipeline. One step (real AI
summaries via Gemini) is optional and still free, but needs your own API key
-- see [Optional: real AI summaries](#optional-real-ai-summaries-via-gemini).

## How it works

```
.github/workflows/daily-digest.yml   -- cron job (daily) + manual trigger; builds,
                                         then deploys docs/ to Pages as a build artifact
scripts/build_digest.py              -- fetches feeds, filters, ranks, writes docs/data/digest.json
config/sources.json                  -- the list of RSS feeds / GNews queries per topic
docs/                                -- the static dashboard (GitHub Pages root)
  index.html / style.css / app.js    -- mobile-first UI, reads data/digest.json
  data/digest.json                   -- generated on every run; gitignored, not committed
```

**`digest.json` is never committed to the repo.** Each run builds it fresh and
the workflow deploys `docs/` directly to Pages as a build artifact
(`actions/upload-pages-artifact` + `actions/deploy-pages`). Earlier versions
of this project committed the generated file back to `main`, which meant the
daily bot commit regularly raced local development and caused rebase
conflicts -- deploying as an artifact instead removes that class of problem
entirely rather than needing tooling to paper over it.

Each card shows a concise **Summary** and a **Why it matters** line. Both are
**rule-based/extractive by default** (cleaned/trimmed RSS description capped
at 2 sentences; a short template per topic category) -- free, no API calls.
If you add a `GEMINI_API_KEY` secret (see below), both are instead generated
per-story by Gemini's free tier, with the rule-based version as the fallback
for any item where that call fails or isn't configured.

The home page opens with **"Today in Brief"**: the single top-ranked story
from each topic (13 headlines with a one-line takeaway each), meant to be
readable in under 5 minutes. The full per-category sections with all 5 items
each follow underneath, reachable by scrolling or via the topic dropdown.

A story is only ever shown **once** across the whole digest, even when two
categories share a source (e.g. Technology (Personal Use) and Major Tech
Trends -- US both read TechCrunch/The Verge) -- see the global dedup notes
in `scripts/build_digest.py`. Within a category, a single prolific source
can't sweep every slot either (`diversify_by_source()`); items are ranked by
recency blended with a light keyword "impact" signal, plus a stronger
category-specific `boostKeywords` signal where configured (e.g. Stock Index
Movements boosting literal "ASX 200"/"S&P 500" mentions).

Cards from sources whose native RSS was discontinued (Reuters, AFR, ASIC, The
Australian -- substituted with Google News RSS, see `sources.json`'s
`_notes`) sometimes show "No preview available -- tap through to read"
instead of a summary. That's not a bug: Google News' article links are a
client-side JS redirect, so a plain HTTP fetch can't reach the real page to
extract a preview from (it works fine for an actual person tapping the link
in a browser, which is why the link itself is left alone). Direct-feed
sources don't have this limitation.

## One-time setup

1. **Create the GitHub repo** (done) and push this code to it (see below).
2. **Add the GNews secret**: repo Settings → Secrets and variables → Actions
   → New repository secret → name `GNEWS_API_KEY`, value: your key from
   [gnews.io](https://gnews.io) (free tier, ~100 requests/day; this pipeline
   uses at most ~3/day).
3. **Enable GitHub Pages**: repo Settings → Pages → Source: **"GitHub
   Actions"** (not "Deploy from a branch" -- this project deploys as a build
   artifact, not from a committed folder). Your dashboard will be live at
   `https://<your-username>.github.io/<repo-name>/`.
4. **Run it once manually**: Actions tab → "Daily News Digest" → Run
   workflow. This deploys the dashboard for the first time so it isn't empty
   while you wait for tomorrow's scheduled run.
5. **Add to Home Screen** on your Android phone: open the Pages URL in
   Chrome → menu (⋮) → "Add to Home screen".

## Optional: real AI summaries via Gemini

By default, summaries and "why it matters" are rule-based (see above) --
this needs nothing extra and always works. If you want genuinely
story-specific writing instead:

1. Get a free API key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
   (Google AI Studio's free tier, no credit card). This is a personal
   account signup step only you can do.
2. Add it as a repo secret named `GEMINI_API_KEY` (same place as the GNews
   secret above).

That's it -- `build_digest.py` picks it up automatically next run. The model
is pinned to `gemini-3.5-flash-lite` (15 requests/min, 1,000/day free --
comfortably above this pipeline's ~70/day) rather than a "-latest" alias:
the first real run hit this the hard way -- `gemini-flash-latest` had
quietly started resolving to a much heavier model with a free quota of just
20 requests *total*, not per-minute, so every single call 429'd. The pin
itself then got deprecated within the same day (Google retired
`gemini-2.5-flash-lite` and pointed at `3.5-flash-lite` instead) -- across
both incidents, the pattern was that any "-flash-lite" model carries the
generous quota regardless of version number. If `GEMINI_MODEL` in
`scripts/build_digest.py` ever 404s again, that's the thing to look for
(the error message itself usually names the replacement); the digest's
`warnings` array will tell you it happened without needing GitHub log
access to find out.

If the key is missing, invalid, or a call fails for any reason (rate limit, network,
malformed response), that item just silently keeps its rule-based summary --
nothing breaks either way. The build log's `Gemini AI summaries: X/Y
succeeded` line tells you how many actually went through.

## Schedule

Runs daily at 19:00 UTC (05:00 AEST / 06:00 AEDT). Edit the `cron` line in
`.github/workflows/daily-digest.yml` to change the time. You can also trigger
a run manually any time from the Actions tab.

A small `.github/keepalive/last-run.txt` timestamp is committed on every run
-- purely to keep the scheduled trigger alive (GitHub auto-disables a
scheduled workflow after 60 days with no repository activity). Nobody edits
this file locally, so unlike the old committed-digest.json approach, it can
never conflict with anything you're working on.

## Adding / changing sources

Edit `config/sources.json`. Each category is either:
- a plain list of RSS `sources` (each with `name`, `url`, optional `region`,
  `paywalled`, and `linkBase` -- the last one only needed if the feed emits
  relative links against a different host than the feed itself, like HBR's
  FeedBurner proxy), or
- `"derivedFrom"` one or more other category ids + `"keywords"` -- it
  re-filters items already fetched for those categories instead of hitting
  the network again (used for Stock Index Movements and ESG), or
- `"type": "gnews"` with a list of `"queries"` (used only for the AI in
  Financial Services topic, to stay within the GNews free-tier quota).

Any category can also set `"boostKeywords"` -- a stronger ranking signal
(separate from the inclusion `"keywords"` filter) for when a category needs
to actively prefer certain stories over just "freshest first", not merely
include anything that matches broadly.

Excluded topics (fitness/health, cybersecurity, distressing content) are
filtered globally via `EXCLUDE_KEYWORDS` in `scripts/build_digest.py`,
regardless of source.

Note that WSJ is configured as a *source* (its Markets feed lives inside the
`finance` category, flagged `paywalled: true`), not its own category -- it
naturally flows into Stock Index Movements too, since that's a keyword
filter over the same finance pool. `the-australian` is the only remaining
dedicated paywalled-outlet category.

## Local testing

You'll need Python 3.11+ locally:

```bash
pip install -r requirements.txt
python scripts/build_digest.py
```

This writes straight to `docs/data/digest.json` (gitignored -- see above),
so you can preview the dashboard locally without it ever touching git. A
tiny zero-dependency static file server is included for that:

```bash
powershell -File scripts/dev-server.ps1 -Port 8080
```

Then open `http://localhost:8080/`. The `preview-digest` Claude Code skill
automates this whole loop, including a mobile-width check in the browser.
