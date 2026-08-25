---
type: Defect
resource: src/coderag/discover.py, tests/test_discover.py
title: A committed file symlink was read through, in the lane that runs by default
description: "`git ls-files` lists a file symlink as an ordinary path, and `is_file()` follows it. So a repo containing `notes.md -> ~/private/notes.md` indexed that content and attributed it to the containing project. The walk lane refuses it, the git lane accepts it, and the git lane is the default."
tags: [discovery, scope, content-escape, resolved]
status: stable
generated: { by: claude/opus-5, at: 2026-08-20T23:40:00Z }
---

# The two lanes disagreed, and only one of them was tested

`respect_gitignore=True` is the default, so discovery comes from `git ls-files`. Git records a
symlink as a regular entry. `full.is_file()` follows the link and answers True, and `read_bytes()`
then reads the target. Any caller pinned to that project could retrieve content from outside it,
labeled with the project's own path. The walk lane refused the same file — the check has been
there all along, in the other branch.

The only symlink test used a *directory* link in a project that was never `git init`-ed. So it
exercised the walk lane alone, and the git lane had no coverage at all. Both lanes now refuse a
symlink, and there is a test in each.

# Reach is not authorization, one layer down

This is the same claim [the search unit is the caller's own
workspace](../constraints/the-search-unit-is-the-callers-own-workspace.md) makes about federation
members, applied to file content. The indexer can reach the target, and that is not a reason to
index it. `read` refuses one too, so the refusal does not depend on which
enumerator found the path.
