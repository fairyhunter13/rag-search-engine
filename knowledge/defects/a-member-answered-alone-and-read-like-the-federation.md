---
type: Defect
resource: src/coderag/federation.py, src/coderag/search.py, tests/test_federation.py, tests/test_search.py
title: A member answered alone, and the reply read like the federation's
description: "A search from a member's own directory covered 1 project of 143. The reply carried no sign of it, and the caller had no second call to make: `enforce` refuses a member that names its root."
tags: [federation, search, scoping, registry]
status: stable
generated: { by: claude/opus-5, at: 2026-08-25T12:45:00Z }
---

# One project of 143, and nothing said so

`search` ran against `federation.expand(root)`. That is the root together with the members it
claims. A root asked from its own directory got all of them. A member asked from its own
directory got itself.

Measured on the live tree, from one member of a 143-project federation:

```
"searched": {"projects": 1, "files": 612, "chunks": 1880}
```

The federation holds 143 projects and 57,864 files. So the answer covered 1.1% of the files the
question was asked about. The reply shape is the same either way, and `searched.projects` is the
only field that differs.

A live agent run took that answer at face value. It searched six times, found nothing, fell back
to `find` and `git ls-files`, and reported the code was not checked out locally. The code was
checked out. It sat in a project the search never opened.

# The caller could not widen it

Naming the root is the obvious repair and it is refused. `scope.enforce` allows a target the
caller's workspace contains or sits inside. A member lives outside its root's tree, so a session
inside the member may not name the root. The SessionStart card states the rule and offers no way
out of it.

So the narrow answer was the only answer available, and it was wrong rather than merely narrow.

# The unit is now the caller's directory plus what claims it

`federation.unit` replaces `expand` in the search path. It reads the registry row for the
directory, and for every enabled root in `roots` it adds that root's federation.

Three things it keeps. The caller's own project stays first, because `rank.pool_cut` gives the
first project half the slots. A root is unchanged: `unit` and `expand` agree there, and a second
pass would be the transitive walk this engine refuses. A member of a disabled root answers alone,
because an unflagged row has no store to read.

# The cost, measured before the change shipped

From `gen3-app-c`, warm, on 2026-08-25:

| unit | projects | time |
|---|---|---|
| the member alone | 1 | 0.65 s |
| the member and its root's federation | 143 | 5.5 s to 17.6 s |

The wide reading is 8 to 27 times the narrow one. A root call in the same federation already
costs that. So this is an existing cost charged to more callers, and not a new one. The cheap
answer was the wrong answer.

Watch it in the fleet. A member search that times out is worse than a member search that is
narrow, and this table is the only reading taken so far.

# What this does not fix

The corpus is still the primary clones. For 31 services in that federation the indexed clone is older
than the deployed worktree, median gap 382 days. Widening the unit reaches 142 more projects of
that same corpus. A question whose answer lives only in a worktree leaf is still unanswerable
here.
