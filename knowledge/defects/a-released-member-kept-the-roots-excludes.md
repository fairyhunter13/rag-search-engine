---
type: Defect
resource: src/coderag/federation.py, src/coderag/tools.py, tests/test_server_tools.py
title: A released member kept the excludes of the root that released it
description: Joining a root narrows a member and submits it for re-index. Leaving one widens it and submitted nothing. Unregister reported only the rows it deleted, so the one member that survives the release was the one it never named.
tags: [federation, registry, excludes, index]
status: stable
generated: { by: claude/opus-5, at: 2026-08-20T02:00:00Z }
---

# What occurred

`index(root, enabled=False)` released the root's members and returned. Nothing re-walked them, so
a member went on answering under excludes that no longer applied to it. Until some unrelated write
occurred to it, or forever. The narrowing was applied when the member joined. The widening was
never applied at all.

Fixing the obvious half was not enough. `federation.unregister` returned "the rows removed", and
`registry.release` returns True **only when the row is deleted outright**. A member that was also
claimed directly survives the release — and that is precisely the member that stays in the corpus
and needs re-walking. So the resubmit loop, added on top of that return value, submitted nothing in
the only case that mattered. The live test went on timing out after 300 s with the fix in place.

`unregister` now reports every row it released, and the caller submits the survivors:

```python
if project != target and registry.get(project) is not None:
    index.submit(project, reason="index tool")
```

An orphaned member is out of the registry fully. Indexing it would rebuild a store no search
will read.

# The general shape

**A function that answers "what changed" by reporting only its most destructive outcome hides the
rows that survived. The survivors are usually the ones that need follow-up work**. "Removed"
and "released" read as synonyms in a docstring and are not the same set.

The asymmetry below is worth stating on its own. **every configuration change that narrows a
project on the way in has to widen it on the way out. The two paths are different code**. The config signature makes the widening cheap once it is asked for.
`index_project` sees a changed signature and walks the whole project instead of
the requested subset. So the entire defect was
the missing call, not the reconcile.

# How the test discriminates

The unit test claims the member **directly** before joining it to the root. Without that, the
member is orphaned by the release and drops out of the registry, and the correct behavior is to
submit nothing. So the version of the test that did not claim it directly passed on the broken
code. Probed by restoring the `and registry.release(...)` short-circuit: the test fails.
