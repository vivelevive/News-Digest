#!/usr/bin/env bash
# release.sh -- rebase local main onto origin/main (auto-resolving the
# expected docs/data/digest.json conflict caused by the daily Action
# committing its own regenerated copy), regenerate the digest fresh from
# current code, commit it if it changed, and push. Safe to re-run: if
# nothing has diverged and nothing changed, it just reports that and exits.
#
# Usage: scripts/release.sh ["short reason for the regen"]
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
cd "$REPO_ROOT" || exit 1

DIGEST_FILE="docs/data/digest.json"
REASON="${1:-}"

fail() { echo "release-digest: $1" >&2; exit 1; }

# 0. Preconditions -- refuse to guess on anything unexpected.
# Only tracked-file changes are a real risk here (the script only ever
# `git add`s $DIGEST_FILE specifically, never `-A`/`.`), so untracked files
# elsewhere in the tree are just noted, not treated as blocking.
branch="$(git branch --show-current)"
[ "$branch" = "main" ] || fail "not on main (on '$branch') -- switch to main before releasing."

dirty="$( { git diff --name-only -- . ":!$DIGEST_FILE"; git diff --cached --name-only -- . ":!$DIGEST_FILE"; } | sort -u)"
if [ -n "$dirty" ]; then
  fail "you have uncommitted changes to tracked files outside $DIGEST_FILE -- commit or stash them first:
$dirty"
fi

untracked="$(git status --porcelain --untracked-files=all -- . ":!$DIGEST_FILE" | grep '^??' || true)"
if [ -n "$untracked" ]; then
  echo "==> Note: untracked file(s) present, not touched by this script:"
  echo "$untracked"
fi

# Any leftover local edit to $DIGEST_FILE itself is safe to discard --
# it's about to be regenerated fresh in step 2 regardless, and `git rebase`
# below refuses to start at all while ANY tracked file (this one included)
# has unstaged changes.
if ! git diff --quiet -- "$DIGEST_FILE" || ! git diff --cached --quiet -- "$DIGEST_FILE"; then
  echo "==> Discarding a stale local edit to $DIGEST_FILE (it'll be regenerated fresh below)."
  git checkout -- "$DIGEST_FILE" 2>/dev/null || git restore --staged --worktree -- "$DIGEST_FILE"
fi

# 1. Fetch + rebase (this is what handles the bot-committed-digest.json race).
echo "==> Fetching origin..."
git fetch origin || fail "git fetch failed"

local_head="$(git rev-parse HEAD)"
remote_head="$(git rev-parse origin/main)"

if [ "$local_head" != "$remote_head" ]; then
  echo "==> origin/main has moved -- rebasing..."
  if ! git rebase origin/main; then
    conflicted="$(git diff --name-only --diff-filter=U)"
    if [ "$conflicted" = "$DIGEST_FILE" ]; then
      echo "==> Conflict in $DIGEST_FILE only (expected -- it'll be regenerated fresh in step 2 anyway). Taking the incoming version and continuing."
      git checkout --theirs "$DIGEST_FILE" || fail "checkout --theirs failed"
      git add "$DIGEST_FILE"
      git rebase --continue || fail "rebase --continue failed after resolving $DIGEST_FILE"
    elif [ -z "$conflicted" ]; then
      fail "rebase failed for a reason other than a file conflict -- run 'git status' to see what happened, or 'git rebase --abort' to back out, then re-run this skill."
    else
      fail "rebase conflict in unexpected file(s), not just $DIGEST_FILE:
$conflicted
This needs a human look -- resolve manually (see 'git status'), or 'git rebase --abort' to back out, then re-run this skill."
    fi
  fi
else
  echo "==> Already up to date with origin/main, no rebase needed."
fi

# 2. Regenerate the digest from current code.
echo "==> Regenerating $DIGEST_FILE..."
PYTHON=""
for cand in python python3 "/c/Users/kelvi/AppData/Local/Programs/Python/Python312/python.exe"; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" --version >/dev/null 2>&1; then
    PYTHON="$cand"
    break
  fi
done
[ -n "$PYTHON" ] || fail "no working Python interpreter found (checked PATH and the known local install) -- install Python or fix PATH, then re-run."

if [ -z "${GNEWS_API_KEY:-}" ]; then
  echo "==> WARNING: GNEWS_API_KEY is not set in this shell -- the 'AI in Financial Services' category will be skipped in this regeneration. (It'll still pick up the real key from the repo secret on the next scheduled/manual Actions run -- this only affects the copy pushed by this script.)"
fi

"$PYTHON" scripts/build_digest.py || fail "build_digest.py failed -- see output above."

# 3. Commit only if the regenerated digest actually differs.
git add "$DIGEST_FILE"
if git diff --cached --quiet -- "$DIGEST_FILE"; then
  echo "==> $DIGEST_FILE unchanged after regeneration -- nothing new to commit there."
else
  msg="Regenerate digest.json"
  [ -n "$REASON" ] && msg="Regenerate digest.json with $REASON"
  git commit -q -m "$msg

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>" || fail "commit failed"
  echo "==> Committed: $msg"
fi

# 4. Push whatever is now ahead of origin/main (rebased commits, plus maybe the regen commit).
ahead="$(git rev-list --count origin/main..HEAD)"
if [ "$ahead" -eq 0 ]; then
  echo "==> Nothing to push -- local main matches origin/main."
  exit 0
fi

echo "==> Pushing $ahead commit(s) to origin/main..."
git push origin main || fail "push failed -- origin/main may have moved again in the meantime; just re-run this skill, it's safe to repeat."
echo "==> Done. origin/main is up to date."
