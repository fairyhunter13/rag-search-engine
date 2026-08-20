---
type: Constraint
resource: src/coderag/watch.py, src/coderag/tools.py
title: "`watching` answers for one project, and arming lands seconds after registration"
description: "The `index` reply's `watching` field used to be thread liveness, which is true the moment the watcher starts and says nothing about whether this project's inotify watches exist. It is `watch.armed(project)` now -- membership in the armed set -- because the rebuild lands up to WATCH_POLL_MS plus ~5 s after registration and a write inside that window is gone for good."
tags: [watcher, inotify, tools]
status: active
generated: { by: claude/opus-5, at: 2026-08-20T18:10:00Z }
---

Registration sets a flag; the watcher thread rebuilds its set on its own poll. Between the two,
inotify holds no watch on the new project, and inotify replays nothing — so a create or delete in
that window is not late, it is lost.

`watching: watch.watching()` reported thread liveness, so it was `True` immediately and for every
project at once. A live test that read it as "safe to write now" wrote into the blind window and
then waited 300 s for a change that was never observed.

`watch.armed(project)` answers the question actually being asked. The discriminator for it is that
the thread must be alive across both halves of the assertion while the answer still changes:
`tests/test_watch.py::test_a_live_thread_is_not_the_same_answer_as_this_project_being_watched`.

Consequence for tests: any live fixture that writes into a freshly registered project must poll
`index(...)["watching"]` first, and must not assert it synchronously right after `index` returns —
`tests/test_live_federation.py` asserts it under `until(...)` for that reason.
