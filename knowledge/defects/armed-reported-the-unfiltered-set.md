---
type: Defect
resource: src/coderag/watch.py, tests/test_watch.py, tests/test_live_federation.py
title: watch.armed() answered for projects the watcher had just refused to watch
description: "`_armed` was assigned the unfiltered intent list. So a project dropped four lines earlier for an unparseable config still reported `watching: True` through `tools._status`. The live federation test waits on exactly that flag, so its green never proved an inotify watch existed."
tags: [watcher, tests, resolved]
status: stable
generated: { by: claude/opus-5, at: 2026-08-20T23:40:00Z }
---

# Two lists, and the wrong one was published

The batch loop filters `roots` down to the projects whose config parses, then armed the watches
from the filtered list and published the **unfiltered** one. `armed()` is the only thing
`tools._status` has to answer `watching` with. So a project with a `.coderag.yaml` typo reported
as watched, while no watch descriptor existed for it.

`_armed` is now the filtered tuple. The unfiltered list stays as the rearm comparison on purpose.
[a deleted file stayed searchable in a member](a-deleted-file-stayed-searchable-in-a-member.md)
records why: comparing against the filtered list differs every tick and re-arms 120,000 watches
forever.

# Everything that touched it passed on the broken path

The existing watcher test registered a project with a broken config and asserted only that the good
one was armed. Adding `assert not watch.armed(broken)` fails on the old code. The live federation
test polls `watching` before searching, so it was waiting on a flag that is true whether or not the
watcher did anything.

Two gaps stay open and are now written into the warning rather than fixed. Fixing the typo changes
no registry row, so `rearm_if_changed` never fires and the project stays unwatched **until the
daemon restarts**. And `_intent = tuple(roots)` still has no test, because both rearm tests
monkeypatch it directly.
