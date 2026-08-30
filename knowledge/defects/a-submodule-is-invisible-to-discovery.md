---
type: Defect
resource: src/coderag/discover.py, tests/test_discover.py
title: A populated submodule was invisible to discovery, so a third of the Gen-3 PHP corpus was never indexed
description: "`git ls-files` lists a gitlink as one entry and never descends, so every file inside a checked-out submodule was absent from the index. 22 Acme worktrees hold one, and 2,584 PHP files sat behind them. `--recurse-submodules` is not the fix, because git refuses it beside `--others`."
tags: [discovery, submodule, git, corpus, coverage, resolved]
status: stable
generated: { by: claude/opus-5, at: 2026-08-30T00:00:00Z }
---

# What happened

`respect_gitignore=True` is the default, so discovery comes from git. `_git_files` ran
`git ls-files --cached --others --exclude-standard`, and `ls-files` reports a submodule as a single
index entry at mode `160000`. It never walks inside. A gitlink is not a file, so it dropped out of
`indexable`, and every path under it dropped with it.

The failure was silent in both directions. The project indexed, the run reported success, and a
search over it answered. It answered from two thirds of the tree.

# Why `--recurse-submodules` is not the fix

Git refuses that flag beside `--others`. Discovery needs `--others` to reach an untracked file that
gitignore does not exclude, so the two cannot be combined. The fix has to run the same command
inside each submodule and prefix the results back.

# The fix

`_gitlinks` at `discover.py:41` reads `git ls-files --stage -z` and keeps the entries whose mode is
`GITLINK_MODE = "160000"`. It reads the index and not `.gitmodules`, because `.gitmodules` declares
a submodule the tree may never have checked out.

`_git_files` at `discover.py:67` then recurses into each one that is a real directory, is not a
symlink, and is not empty. It prefixes each inner path back to the outer project, so every caller
still receives one flat list relative to that project. Three bounds hold it:

- `MAX_SUBMODULE_DEPTH = 4`.
- A visited-realpath set shared across the whole recursion. A link back to an ancestor is
  enumerated once, and two links to one target are enumerated once.
- A submodule that is not a git work tree falls back to `_walked_files`.

# What it recovered

Measured across the 419 enabled projects that hold an index, on 2026-08-30. 22 worktrees hold a
populated gitlink. Those 22 hold 7,795 PHP files, and **2,584 of them are inside a gitlink** where
the count before the fix was 0.

| Worktree | PHP files | Inside a gitlink |
|---|---:|---:|
| `gen3-app-support/1.12.1` | 651 | 492 |
| `gen3-app-a/submodule-pin_2.1` | 531 | 128 |
| `gen3-app-vendor-portal/1.6.2` | 526 | 252 |
| `gen3-app-kpi/5.4.6` | 505 | 102 |
| `gen3-app-bms/1.1.1` | 128 | 10 |

`gen3-app-a/submodule-pin_2.1` went from 403 files to 531. Every submodule is mounted at `Domain/` or
`Shared/`, which is where the aggregates live, so the missing third was not incidental code.

# A count in the commit message that is wrong

`2ea7ab4` says *23 worktrees hold a populated submodule*. **The number is 22.**
`gen3-app-production/mitme-1.1` holds gitlink directories whose `.git` file points at a missing gitdir,
so it is neither empty nor a checkout, and it enumerates nothing. The commit is left as it stands,
because it records what was believed then. This concept is where a reader looks.

# The sibling engine, and the second defect this uncovered

graph-search-engine carries the identical defect and the identical fix, in its own
`knowledge/defects/a-submodule-is-invisible-to-discovery.md`. Its file counts differ from these by
22, one per worktree: the extensionless Laravel `artisan`, which only coderag maps as PHP.

That engine also found a second defect underneath the first, and coderag has none of it. coderag
ranks text and holds no resolver, so a recovered file is a searchable file the moment it is
indexed. graphrag has to join a call to a definition, and there the recovered files bought almost
nothing: 2,645 calls naming a symbol now defined under `Domain/` still resolve to `external`
against 203 that do not, and not one edge crosses from the outer project into the submodule.

Related: [a committed file symlink was read through, in the lane that runs by
default](the-git-lane-read-through-a-file-symlink.md).
