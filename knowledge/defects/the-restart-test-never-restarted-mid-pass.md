---
type: Defect
resource: tests/test_restart.py
title: The mid-index restart test never restarted mid-pass, and failed at random on a stale progress file
description: "The pass committed all 240 files during uvicorn's graceful stop, so the resume path was never exercised. When the second daemon had nothing to do it wrote no progress.json, and the test read the first pass's files_total=240."
tags: [testing, restart, shutdown, progress, resolved]
status: stable
generated: { by: claude/opus-5, at: 2026-09-25T21:30:00+07:00 }
---

# The symptom

`test_a_restart_mid_index_drains_the_queue_without_rebuilding` failed at random with
`assert 240 <= (240 - 64)`. It failed in 1 to 4 of every 7 or 8 runs at `584d1e9d`, at `8f501c4f`
and at `54a5b91e`, so no change in those commits caused it.

# The cause

An instrumented copy counted the store after `daemon.stop()`. The count was 240 of 240 in every run
at all three commits.

- SIGTERM goes to uvicorn first. Its graceful shutdown ran for 0.6 s to 1.2 s before our handler's
  `os._exit(0)`, and the indexing worker kept committing through it.
- One-function files finished the last 176 files in 0.9 s, so the pass ended inside the stop.
- The test counted `killed_at` before the stop. Its "the kill has to land mid-pass" check therefore
  guarded a moment that had already passed.
- The second daemon found nothing to do. It sometimes ran an empty pass that wrote `files_total=0`,
  and the test passed without testing anything. Otherwise it wrote nothing, and the test read the
  first daemon's `files_total=240`.

# The fix

- The fixture writes 40 functions a file for this test only. The last 176 files then take 5.7 s,
  which is longer than the stop.
- The store is counted after the stop, and the check that the pass was cut runs there.
- The first daemon's `progress.json` is deleted before the restart. The assertion then reads only the
  second daemon's announcement.

The fixed test passed 10 of 10 runs. With the old one-function fixture, the same test now fails with
`the pass finished during the stop, so this asserts nothing`, which is the empty test reported
instead of passed.
