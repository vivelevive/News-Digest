---
name: release-digest
description: Push local News Digest repo changes to origin/main, safely handling the recurring conflict where the daily GitHub Actions job commits its own fresh docs/data/digest.json between your local edits and your push. Rebases onto origin/main, auto-resolves a digest.json-only conflict (regenerating it fresh from current code rather than trying to merge two generated versions), commits if it changed, and pushes. Invoke explicitly as /release-digest -- this pushes to main, so it never fires on its own.
disable-model-invocation: true
---

# Release Digest

## Why this exists

`docs/data/digest.json` is a generated file, and it's regenerated in two
places: the daily GitHub Actions cron/`workflow_dispatch` run, and locally
whenever you or Claude run `scripts/build_digest.py` to preview a change.
Because it's committed by both, a plain `git push` after editing
`scripts/build_digest.py` or `config/sources.json` locally frequently gets
rejected -- the Action beat you to it and origin/main has a newer
`digest.json` commit you don't have.

Trying to `git merge`/resolve that conflict by hand is pointless work: the
two versions are both machine-generated snapshots of the same feeds at
different moments, so there's nothing meaningful to reconcile -- the right
move is always "throw both away and regenerate a fresh one from whatever
code is about to ship." That's what this skill automates.

## What it does

Run `.claude/skills/release-digest/scripts/release.sh`, optionally passing a
short phrase describing what changed (used in the regen commit message):

```bash
bash .claude/skills/release-digest/scripts/release.sh "5-item cap and relevance ranking"
```

The script, in order:

1. **Checks preconditions** -- must be on `main`, and nothing outside
   `docs/data/digest.json` may be uncommitted (if something else is dirty,
   it stops and asks you to commit or stash first, rather than guessing
   what you meant to include).
2. **Fetches and rebases** onto `origin/main` if it has moved.
3. **If the rebase conflicts in `docs/data/digest.json` only**, resolves it
   by taking the incoming version and continuing -- it's about to be
   overwritten by a fresh regeneration anyway, so which version "wins" the
   conflict doesn't matter. A conflict touching *any other file* is left
   for you: the script stops and reports it rather than guessing.
4. **Regenerates `docs/data/digest.json`** by running
   `python scripts/build_digest.py` against whatever code is now on the
   branch. If `GNEWS_API_KEY` isn't set in the shell, it warns that the AI
   in Financial Services category will be skipped in this copy (the real
   key from the repo secret still applies to the scheduled Actions run --
   this only affects what this script itself pushes).
5. **Commits the regenerated file** only if it actually differs from what
   was already staged/committed -- an identical regeneration is a no-op.
6. **Pushes** whatever is now ahead of `origin/main` (the rebased commits,
   plus the regen commit if one was made).

Every step prints what it's doing and why. If anything goes wrong outside
the digest.json case (a real conflict, a push race, no working Python
interpreter), it stops with a clear message and next step rather than
leaving the repo in a half-rebased state -- it's safe to just re-run the
script after fixing whatever it flagged, since fetch+rebase is the first
thing it does every time.

## When to use this vs. a plain push

Use this whenever you've committed changes to `scripts/build_digest.py`,
`config/sources.json`, or anything under `docs/` and want them live. A
plain `git push` still works fine if origin/main hasn't moved (e.g. you
just pushed five minutes ago and no Actions run has fired since) -- this
skill is only doing extra work when that assumption doesn't hold, and it
degrades gracefully (steps 2-3 just no-op) when it does.
