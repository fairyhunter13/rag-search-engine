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

# Amendment, 2026-08-20: the criterion is replaced, because it was unreadable

"A real pin from all five profiles" cannot be checked. journald names no client at all — no
`clientInfo` was logged, the formatter dropped `%(name)s`, and the access log is off — and the five
profiles' `coderag` entries are kept byte-identical by `sync_global_mcp` at SessionStart, so there
was never a per-profile variable to observe. Waiting longer produces no evidence.

What the journal did show is a third case this decision did not name: of ~1865 zero-root pins, 97
took the no-capability branch, **none** took the era branch, and the remaining ~1768 were `_ask`
asking and the client answering with an **empty list**. At `REQUIRE_CLIENT_ROOTS=1` every one of
those is refused.

So the criterion is now: flip when the answered-empty count reaches zero. `scope` distinguishes
"answered empty" from "answered with roots" in the log and carries the client's name and version, so
that population is attributable for the first time. See [empty root answers from clients nothing
identifies](../defects/empty-root-answers-from-clients-nothing-identifies.md). The flag stays at
`0` until then.

# Second amendment, 2026-08-20: the criterion is a census, and the flag is flipped

The replacement criterion — "flip when the answered-empty count reaches zero" — was as unreadable as
the one before it, for three reasons in the instrumentation that shipped with it. All three are named
in the defect above. The short version: nothing was ever named, the branch record was false, and
"answered empty" and "no resolver ran" logged identically. A day of waiting could not have closed it.

**Passive observation cannot close this at all.** journald keeps seven days, the caps-less traffic is
bursty and self-generated (the live suite's own bursts of 19/13/10), and a count with no denominator
answers "zero out of what" with nothing. What replaces it is a census of a closed population, which
is available because the population *is* closed: the daemon binds `127.0.0.1` only, all five profiles
carry a byte-identical `coderag` entry, and no other MCP client on this machine references it.

The census, taken 2026-08-20 against the fixed build, one `search` from each profile:

```
5  claude-code/2.1.235  2026-07-28  asked  roots=1   (five distinct peer pids)
0  answered-empty (branch=asked, roots=0)
```

Every interactive caller pins. So the unit's `Environment=CODERAG_REQUIRE_CLIENT_ROOTS=0` is deleted
and the code default of `1` stands.

# What the census caught that the count never would have

One caller was refused by the flip: the layer-3 agent test's own `claude -p`, on `2025-11-25`,
`branch=legacy-path`, which cannot carry a pin at any setting. Four candidate causes were tested
one at a time against the running daemon, and the one that moves the era is **`--strict-mcp-config`**:
a server passed with `--mcp-config` and merged with the profile's config connects on `2025-11-25`,
and the same server under `--strict-mcp-config` connects on `2026-07-28`. A fresh config dir, the
model, `--verbose` and a git-repo cwd all make no difference — each was ruled out by measurement, not
by argument.

That is a test gap, not a rollout blocker, and the flag it needed was one an isolation test should
have been passing anyway. Two live assertions also changed, because the flip made their premises
false rather than because they broke: an unpinned call is now refused rather than answered, and the
agent session is no longer required to write a `root` — with a pin, `root=""` *is* the workspace.
