---
type: Defect
resource: src/coderag/discover.py, tests/test_discover.py
title: The watcher lane skipped the size cap, and one file starved the daemon
description: "`index.py` reads the watcher's changed paths with `discover.read()`, and only the walk checked `MAX_FILE_BYTES`. A build grew a 7.8 MB one-line `search.json`, the chunker's Python size callback held the GIL, and no request was answered for 15 minutes."
tags: [discovery, watcher, availability, resolved]
status: stable
generated: { by: claude/opus-5, at: 2026-09-25T18:40:00+07:00 }
---

# The symptom

From 18:07 on 2026-09-25, coderag answered nothing, `/healthz` included, and it logged nothing.
The process stayed alive. `py-spy dump` showed the `indexer` thread as `active+gil` in
`chunk_text` → `nonwhitespace` on `tour/public/search.json`, 7,842,730 bytes on one line. The main
thread was waiting for the GIL inside the `stat()` call that `/healthz` makes.

# The cause

The walk drops a file over `MAX_FILE_BYTES` in `candidates()`. The watcher lane in `index.py`
skips the walk and calls `read()` on each changed path, and `read()` had no cap. The splitter
calls the Python sizer for each candidate chunk, so on a single long line it holds the GIL for
minutes.

# The fix

`read()` refuses a file over the cap, by `stat` before the read and by length after it. So
neither lane can pass one. In the watcher lane an over-cap file drops out of `write`, and its old
rows go to `delete`. This is the same shape as [the symlink
defect](the-git-lane-read-through-a-file-symlink.md): the check lived in one enumerator, and
`read` is the only place both lanes share.

# The second fix: bounded windows

The cap limits a file's size, not its worst case. Measured on the same file, the time grows with
the length of the single line: 0.95 s at 1.5 MB, 2.32 s at 3 MB and over 175 s at 7.7 MB. Each
window of 1.5 MB alone took under 1 s. `chunk_text` now splits a text with a line over
`LONG_LINE` into windows first, so no candidate outgrows a window. The full file now takes 2.08 s.
See [the chunker decision](../decisions/one-chunker-and-it-is-third-party.md).
