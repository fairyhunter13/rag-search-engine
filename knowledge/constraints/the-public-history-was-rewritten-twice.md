---
type: Constraint
resource: tests/test_public_hygiene.py
title: The tracked tree is clean because the history was rewritten twice, and the commit identity is pinned to that fact
description: Two `filter-repo` rewrites made this public history publishable, on 2026-06-19 and 2026-08-28. `--replace-text` left every banned term in the commit messages. The tree looks clean because it was made clean, and none of that is derivable from the repo.
tags: [hygiene, git, history, publishing]
status: stable
generated: { by: claude/opus-5, at: 2026-08-31T12:00:00Z }
---

# Two rewrites, and what each one taught

**2026-06-19, 602 commits.** A scratch diagnostic hardcoded absolute home paths that also named
the maintainer's client organisations. `git filter-repo` redacted the path prefix and the names,
and every ref was force-pushed. The same pass remapped author and committer email to
`fairyhunter13@users.noreply.github.com`.

**2026-08-28, 1,212 commits.** A whole-history blob scan, using this repo's own `hits()` matcher,
found 170 (path, term) hits across about 150 files and 12 of the 15 banned terms. Almost all of
them sat in the deleted v1 engine. HEAD was already clean, so the first rewrite had not covered the
terms added to the ban list after it. Two passes were needed:

- `--replace-text` rewrites **blob content only**. It left every banned term in the commit
  messages.
- `--replace-message` on the same rules cleared those, and `--name-callback` / `--email-callback`
  normalised three outlier identities.

The HEAD tree came out byte-identical at `df2fb78`, so the second rewrite touched history alone.
`main` went `2a6cb7b` to `fbcb7db`, and `archive/pre-rewrite` and the tag `v0.2-legacy` were
force-pushed too, because both still pointed at old objects. `filter-repo` deletes the `origin`
remote and rewrites any backup tag you made, so the only usable backup is a
`git bundle create --all` taken beforehand.

# What follows from it

**The commit email is pinned.** Local `user.email` is `fairyhunter13@users.noreply.github.com`.
A commit from this repo under the personal address re-leaks the identity the first rewrite removed.

**A clean `git log` does not prove a purge.** Pre-rewrite commits stay reachable by SHA, and
through any fork, until GitHub collects them. Purging them fully needs GitHub Support and no forks.
So a leak that predates 2026-08-28 is not proven gone.

**The guard cannot see an untracked file.** `tracked()` runs `git ls-files`, so
`tests/test_public_hygiene.py` passed green over a violation that was still unstaged, and the
violation was then pushed. Stage first, then run it.

**A third rewrite invalidates every SHA anyone has referenced since.** Two have already happened.
