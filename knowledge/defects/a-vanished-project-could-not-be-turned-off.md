---
type: Defect
resource: src/coderag/tools.py, tests/test_server_tools.py
title: A project whose directory was gone could not be turned off
description: "`index_project` refused every call for a path that is not a directory, and the refusal sat above the unflag branch. So the row an operator most needs to disable was the one row the surface could not act on. Two of them survived every restart, and reconcile retried and logged a traceback for each at every start."
tags: [tools, registry, reconcile, resolved]
status: stable
generated: { by: claude/opus-5, at: 2026-08-20T20:10:00Z }
---

# What it looked like

`/healthz` reported `failed: 2` after every start, with nothing but the counter naming which
projects. journald had the answer:

```
indexing /tmp/pytest-of-<user>/pytest-42/typo0 failed
FileNotFoundError: /tmp/pytest-of-<user>/pytest-42/typo0 is not a directory
```

Both rows were leaked by a live test (`a-live-test-skipped-the-disable-teardown`). Disabling them
through the daemon's own path returned a result whose every field was `None`, and the row stayed
enabled. That path is the only one allowed, because the daemon holds `projects.lock`.

# Why

```python
if not target.is_dir():
    return {"error": f"{target} is not a directory"}
if not enabled:
    ...
```

The guard is right for the indexing path and wrong for the whole tool. A row pointing at a deleted
directory is precisely the row that has to be turnable off, and the guard made "deleted" a
permanent state. The directory cannot come back, so neither can the row's exit.

# The fix, and the test that holds it

The guard moved below the unflag branch. The regression test registers a project, deletes its
directory, disables it, and asserts it is no longer in `enabled_projects()`. Not that the row
reads `enabled: False`, because `federation.unregister` drops a member nothing else claims out of
the registry fully.

- Those two rows failed reconcile at every start and the registry still read clean between sweeps
  — [a failure that resolved itself left no
  trace](a-failure-that-resolved-itself-left-no-trace.md).