---
type: Defect
resource: src/coderag/scope.py, src/coderag/server.py
title: 1768 zero-root pins were an answered-empty case the log could not name
description: "Of ~1865 zero-root pins only 97 took the no-capability branch and none took the protocol-era branch; the rest asked and got an empty list back — a third case named nowhere — and no `clientInfo` was logged, so the flag's rollout criterion was gated on evidence the instrumentation could not produce."
tags: [scope, mcp, observability, resolved]
status: resolved
generated: { by: claude/opus-5, at: 2026-08-20T23:40:00Z }
---

# The branch logging stopped one level short

`_ask` logged *why* it had no roots, which retired the ambiguity [the pin rollout needed a reason,
not a count](../decisions/the-pin-rollout-needed-a-reason-not-a-count.md) was written about. It
then re-created the same ambiguity underneath: "asked, answered empty" and "asked, answered with
roots" both logged `0 root(s)` versus `N root(s)` with nothing distinguishing the empty answer from
the branches that never asked.

The suspected cause — `ctx.client_capabilities` being a *session* attribute on a
`stateless_http=True` server while the 2026-07-28 era carries capabilities per request — was tested
live and **is not broken for Claude Code**. A rootless search from a 2.1.235 session returned
results scoped to this repo, journald logged `workspace pin: 1 root(s)` for that call, and a search
naming a project outside the workspace was refused. So the population is some *other* client, and
nothing in the log said which.

# Attribution first, code change only if a real client needs one

`scope` now records which branch answered in a `ContextVar` and logs it with the pin, and the log
line carries the client's own `clientInfo` name and version. That turns an anonymous count into an
attributable one.

The rollout criterion is replaced accordingly: `CODERAG_REQUIRE_CLIENT_ROOTS` flips when the
answered-empty count reaches zero, not when pins have been observed from five profiles — a
criterion the log could never have satisfied, since all five profiles share one generated
`mcpServers` entry and none of them is identifiable in the journal.
