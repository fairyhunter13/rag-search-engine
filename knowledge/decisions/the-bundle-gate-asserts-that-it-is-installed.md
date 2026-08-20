---
type: Decision
resource: tests/test_okf_bundle.py
title: The bundle gate asserts that it is wired, not only that the bundle passes
description: The bundle tests are a gate only if something runs them, so one arm reads the workflow and fails when the step that invokes them is gone, excused, unpinned, or attached to a trigger that never fires on a change.
tags: [okf, knowledge, gates, ci]
status: stable
generated: { by: claude/opus-5, at: 2026-08-20T21:40:00Z }
---

# The gap the other arms leave

`tests/test_okf_bundle.py` grades the bundle: it conforms, every link resolves, every `resource:`
exists, and the checker refuses a concept with no `type` key. All five arms pass in a repo where CI
has stopped running the file. That green and a real one are indistinguishable, which is the failure
mode a gate exists to deny.

So one arm reads `.github/workflows/ci.yml` and requires that the step invoking these tests still
exists, that the strict `-Werror` check runs beside it, that the checker is installed at the pinned
version, that no step is `continue-on-error` — a swallowed failure reports the same green as a pass
— and that a trigger fires on a change rather than a calendar.

It matches the `continue-on-error:` key at the start of a line, not the word: one step carries a
comment saying it is deliberately *not* excused, and matching the word would grade the prose.

Proved by excusing the step and by repointing it at another file, each time watching the arm name
what it lost.

# What it still cannot see

Whether the workflow ran. That is a property of the forge, not of this checkout, and asking it here
would make a test depend on a token and a network.
