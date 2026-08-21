---
type: Defect
resource: src/coderag/ignores.py, src/coderag/filters.py, src/coderag/discover.py
title: The ignore list only ever matched at the root, because fnmatch anchors
description: "`node_modules/*` matched `node_modules/a.js` and not `packages/a/node_modules/a.js`, and the same for vendor, dist, build, target, __pycache__ and .git. 278 of 70,218 indexed files sat under a nested copy of a directory the list claims to exclude. Gitignore's own spelling for that list is `node_modules/`, with no leading slash, which matches at any depth."
tags: [discovery, filters, indexing]
status: stable
generated: { by: claude/opus-5, at: 2026-08-21T00:00:00Z }
---

# The bug

`filters.matches_any` is `fnmatch` against the whole relative path. `fnmatch` anchors both ends, so
a pattern spelled `node_modules/*` is a *root-anchored* pattern: `node_modules/a.js` matches,
`packages/a/node_modules/a.js` does not. Every directory entry in `DEFAULT_IGNORES` was spelled that
way, so `vendor/`, `dist/`, `build/`, `target/`, `__pycache__/` and `.git/` all excluded exactly one
copy each — the one at the top of the tree.

Measured on the live fleet: **278 of 70,218 indexed files** were under a nested copy of a directory
the list says is excluded. Small because most repos put those directories at the root, and silent
because a wrongly-*included* file raises nothing either.

# The fix, and why it is a second list rather than a better glob

`ignores.IGNORE_DIRS` is now a frozenset of **bare directory names**, tested against `Path(rel).parts`
by `filters.in_ignored_dir`. Depth-independent, O(segments) per path, and the set grows without the
match getting slower — where the glob list is O(patterns) with a full `fnmatch` each.

`DEFAULT_IGNORES` keeps the true globs and keeps its anchored reading, which is not a bug there: it
shares its matcher with a project's own `exclude:`, where a user writing `wiki/*` means the one at
the root. Two lists because they answer two questions, not because one is a workaround.

Both moved to `ignores.py` in the same change — data, not knobs, and they were a third of `config`.

# What it cost to fix, which was nothing

No store invalidation, and none should be added. `ProjectConfig.signature()` hashes
`use_default_ignores` as a flag, not the list's contents, so nothing forced a rebuild — and nothing
had to: `discover.changed()` diffs present-against-stored, so a newly-excluded path lands in its
delete list on the next pass. `RECONCILE_ON_START` cleared the 278 on the next restart.

# The half of the test that matters

The obvious test asserts the newly-excluded paths are excluded. The one that catches the real
failure asserts the **refusals**: `lib/main.dart`, `public/index.php`, `testdata/golden.json`,
`go.mod`, `types/api.d.ts` and `src/query.sql` all still index. Upstream lists prune every one of
those — `linguist/vendor.yml` vendors `testdata` and `fixtures` because it is deciding what to leave
out of *language statistics*, not what is worth reading — and an over-broad ignore list fails
exactly the way this one did: without a word.
