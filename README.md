# News Digest

A personal, mobile-first daily news dashboard. A GitHub Actions job runs every
morning, pulls stories from RSS feeds (and a small GNews API quota), filters
out fitness/health and cybersecurity content, groups everything by topic, and
writes the result to `docs/data/digest.json`. GitHub Pages serves `docs/` as
a static site you can "Add to Home Screen" on Android Chrome.

No paid services are used anywhere in this pipeline.

## How it works

```
.github/workflows/daily-digest.yml   -- cron job (daily) + manual trigger
scripts/build_digest.py              -- fetches feeds, filters, writes docs/data/digest.json
config/sources.json                  -- the list of RSS feeds / GNews queries per topic
docs/                                -- the static dashboard (GitHub Pages root)
  index.html / style.css / app.js    -- mobile-first UI, reads data/digest.json
  data/digest.json                   -- generated daily; committed back to the repo by the Action
```

Summaries are **extractive** (cleaned/trimmed RSS description, capped at 5
sentences) and "why it matters" is a short **rule-based** template per topic
category -- there's no paid AI summarization step, to stay within the
free-tier-only constraint.

## One-time setup

1. **Create the GitHub repo** (done) and push this code to it (see below).
2. **Add the GNews secret**: repo Settings → Secrets and variables → Actions
   → New repository secret → name `GNEWS_API_KEY`, value: your key from
   [gnews.io](https://gnews.io) (free tier, ~100 requests/day; this pipeline
   uses at most ~3/day).
3. **Enable GitHub Pages**: repo Settings → Pages → Source: "Deploy from a
   branch" → Branch: `main`, folder: `/docs` → Save. Your dashboard will be
   live at `https://<your-username>.github.io/<repo-name>/`.
4. **Run it once manually**: Actions tab → "Daily News Digest" → Run
   workflow. This populates `docs/data/digest.json` for the first time so
   the dashboard isn't empty while you wait for tomorrow's scheduled run.
5. **Add to Home Screen** on your Android phone: open the Pages URL in
   Chrome → menu (⋮) → "Add to Home screen".

## Schedule

Runs daily at 19:00 UTC (05:00 AEST / 06:00 AEDT). Edit the `cron` line in
`.github/workflows/daily-digest.yml` to change the time. You can also trigger
a run manually any time from the Actions tab.

## Adding / changing sources

Edit `config/sources.json`. Each category is either:
- a plain list of RSS `sources` (each with `name`, `url`, optional `region`
  and `paywalled`), or
- `"derivedFrom"` one or more other category ids + `"keywords"` -- it
  re-filters items already fetched for those categories instead of hitting
  the network again (used for Stock Index Movements and ESG), or
- `"type": "gnews"` with a list of `"queries"` (used only for the AI in
  Financial Services topic, to stay within the GNews free-tier quota).

Excluded topics (fitness/health, cybersecurity) are filtered globally via the
`EXCLUDE_KEYWORDS` list in `scripts/build_digest.py`, regardless of source.

## Local testing

You'll need Python 3.11+ locally:

```bash
pip install -r requirements.txt
python scripts/build_digest.py
```

This writes straight to `docs/data/digest.json`, so you can preview the
dashboard locally. A tiny zero-dependency static file server is included for
that:

```bash
powershell -File scripts/dev-server.ps1 -Port 8080
```

Then open `http://localhost:8080/`.
