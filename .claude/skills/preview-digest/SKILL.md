---
name: preview-digest
description: Locally regenerate and visually preview the News Digest dashboard (docs/) in the Browser pane before pushing -- runs scripts/build_digest.py, serves docs/ via the "news-digest-static" launch config, and checks it at mobile width since this is a mobile-first PWA. Use this whenever the user asks to preview, check, verify, or "see" a change to the dashboard UI (docs/index.html, docs/app.js, docs/style.css), the digest categories, or config/sources.json -- or after Claude itself makes such a change and should confirm it renders correctly before reporting done. This is LOCAL-ONLY: it never touches git or the live GitHub Pages site (see the release-digest skill for shipping changes).
---

# Preview Digest

## What this does

A fast loop for answering "does this change actually look right?" without
waiting for a GitHub Actions run or a Pages deploy:

1. **Regenerate the data.** Run `python scripts/build_digest.py` from the
   repo root. If `GNEWS_API_KEY` isn't set in the environment, proceed
   anyway and note that the AI in Financial Services category will come out
   empty in this preview -- that's expected locally and isn't a bug to
   chase.
2. **Serve `docs/`.** Call `preview_start` with `name: "news-digest-static"`
   (already defined in `.claude/launch.json` -- it runs
   `scripts/dev-server.ps1` on port 8080, a small dependency-free static
   file server, since the project has no Node/npm dev server). If a server
   from a previous preview is already running, `preview_start` reuses it.
3. **Navigate and check mobile first.** `navigate` to `http://localhost:8080/`,
   then `resize_window` to `preset: "mobile"` (375x812) -- this dashboard is
   explicitly mobile-first / "Add to Home Screen" on Android Chrome, so a
   change that looks fine at desktop width but breaks at 375px is the
   failure mode that actually matters here.
4. **Capture and inspect.** Take a `computer` screenshot. If the screenshot
   tool times out because the Browser pane isn't displayed client-side
   (this happens in headless/background sessions -- it's a harness
   limitation, not a bug in the page), fall back to `read_page` and
   `get_page_text` to verify structure and content instead of giving up on
   verification.
5. **Sanity-check for the failure modes that have actually happened in this
   project before:**
   - A category rendering with zero items when it shouldn't (check
     `docs/data/digest.json` warnings array, or `read_console_messages` for
     a fetch error)
   - Duplicate stories appearing across two category sections
   - The "Today in Brief" section missing or empty
   - Layout overflow at 375px width (a card or the topic dropdown running
     off-screen)
   - Dark mode, if the change touched `style.css` colors -- reload with
     `resize_window` `colorScheme: "dark"` and re-screenshot
6. **Report** what changed and what it looks like now, calling out anything
   that looks broken rather than only confirming the happy path.

## Boundaries

This never runs `git`, never pushes, and never touches the live
`https://vivelevive.github.io/News-Digest/` site -- that only updates via
the GitHub Actions job or the **release-digest** skill. If the preview
looks good and the change is ready to ship, say so and offer to run
release-digest next rather than doing it as part of this skill.

## When NOT to bother with a full preview

A change confined to `scripts/build_digest.py` logic that doesn't affect
what gets rendered (e.g. tweaking a log message, adjusting the GNews query
string) doesn't need a visual check -- running the script and confirming it
exits cleanly with the expected category/item counts is enough. Reach for
the full browser preview when the change could plausibly affect what a
viewer sees: new/changed categories, summary or brief formatting, CSS, or
`app.js` rendering logic.
