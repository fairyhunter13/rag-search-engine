---
type: Decision
resource: src/coderag/scope.py, tests/test_scope.py, src/coderag/systemd.py
title: The pin rollout needed a reason, not a count, and the flag stays 0 until journald carries one
description: "`workspace pin: 0 root(s)` 4498 times against 23 real pins looked like a guard that never fires. The count cannot say which of `_ask`'s two branches returned empty, so it could not distinguish 'one client setting away' from 'unreachable'. `_ask` now logs the branch — and a live call from Claude Code today produced a real pin and a correct refusal."
tags: [scope, security, rollout, observability]
status: stable
generated: { by: claude/opus-5, at: 2026-08-20T21:40:00Z }
---

# What the count could not say

`enforce` logged one line per call and it was the rollout's only observable. Two branches in `_ask`
return an empty result — no `roots` client capability, or a negotiated version below `MRTR` — and
both arrive at `enforce` as `0 root(s)`. The first means the pin can never arrive from that client;
the second means it is a client-config change away. The exit criterion Part D wrote — "a real pin
from all five profiles" — was unreadable against a counter that conflates them.

`_ask` now logs which branch, once per call, at the same volume as the count.

# What the measurement shows so far

A `search` issued from a Claude Code session today, naming a root outside that session's workspace:

```
workspace pin: 1 root(s)
{"error": "... is outside this session's workspace ..."}
```

So the capability and the era are both there in at least one profile, and the containment arm works
end to end against the real daemon. A legacy-protocol client on the same daemon logs
`no ask, client advertises no roots capability` and falls through to no pin, as designed.

# The decision

`CODERAG_REQUIRE_CLIENT_ROOTS=0` stays until journald shows a real pin from all five profiles. That
is now a thing that can be read rather than inferred: any profile still short will say which of the
two reasons it is. Flipping now would refuse every call from a client that has not been checked, and
nothing has checked four of them.

The check is close to a formality — all five profiles carry a byte-identical `coderag` entry
(`{"type": "http", "url": "http://127.0.0.1:8765/mcp"}`), so what negotiates the era is the Claude
Code binary and not the profile. Close to, not the same as: the era is negotiated per session, and
the reason a criterion is written per profile is that "should be identical" is what the four
unchecked ones already look like.
