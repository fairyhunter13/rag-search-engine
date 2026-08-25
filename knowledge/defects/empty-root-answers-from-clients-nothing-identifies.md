---
type: Defect
resource: src/coderag/scope.py, src/coderag/server.py
title: 1768 zero-root pins were an answered-empty case the log could not name
description: Of ~1865 zero-root pins only 97 took the no-capability branch and none took the protocol-era branch. The rest asked and got an empty list back, a third case named nowhere. No `clientInfo` was logged, so the flag's rollout criterion was gated on evidence the instrumentation could not produce.
tags: [scope, mcp, observability, resolved]
status: stable
generated: { by: claude/opus-5, at: 2026-08-20T23:40:00Z }
---

# The branch logging stopped one level short

`_ask` logged *why* it had no roots, which retired the ambiguity [the pin rollout needed a reason,
not a count](../decisions/the-pin-rollout-needed-a-reason-not-a-count.md) was written about. It
then re-created the same ambiguity below. "asked, answered empty" and "asked, answered with roots"
logged `0 root(s)` and `N root(s)`. Nothing distinguished the empty answer from the branches that
never asked.

The suspected cause was tested live, and **it is not broken for Claude Code**. That cause is
`ctx.client_capabilities` being a *session* attribute on a `stateless_http=True` server, while the
2026-07-28 era carries capabilities per request. A rootless search from a 2.1.235 session returned
results scoped to this repo. journald logged `workspace pin: 1 root(s)` for that call. A search
naming a project outside the workspace was refused. So the population is some *other* client, and
nothing in the log said which.

# Attribution first, code change only if a real client needs one

`scope` now records which branch answered in a `ContextVar`, and logs it with the pin. The log
line carries the client's own `clientInfo` name and version. That turns an anonymous count into an
attributable one.

The rollout criterion is replaced accordingly: `CODERAG_REQUIRE_CLIENT_ROOTS` flips when the
answered-empty count reaches zero, not when pins have been observed from five profiles. The log
could never have satisfied that criterion. All five profiles share one generated `mcpServers`
entry, and none of them is identifiable in the journal.

# Amendment, 2026-08-20: the fix shipped with three defects, and the new criterion was unreadable too

The instrumentation above could not produce the evidence its own criterion asked for. Three defects,
each confirmed against the running daemon before being fixed:

1. **The client was never named.** `_client` read `params.clientInfo`. The pydantic attribute is
   `client_info`. `clientInfo` is a serialization alias and `InitializeRequestParams` forbids extras,
   so the read was unconditionally `None` — 197 of 197 lines said `client unidentified`. The unit
   test could not catch it, because the fake mirrored the wrong name back
   (`SimpleNamespace(clientInfo=info)`). That is the shape of a test that asserts its own fixture.
2. **The branch most needing a name could not have one**. `Connection.from_envelope` synthesizes
   `client_params` only when clientInfo *and* capabilities both validate. So the caps-less branch,
   the population being counted, has no session to read from even after (1).
3. **The branch record was false.** `_ASKED` was a `ContextVar` written in the resolver and read in
   `enforce`. A live probe logged `1 root(s), not asked, no resolver ran` while a root
   demonstrably arrived. A **sync** resolver runs through `anyio.to_thread.run_sync`, which
   executes it in a fresh `copy_context()` and discards it. So "answered empty" and "no resolver ran" were the same log
   line — the exact distinction the criterion counted.

All three are fixed. Attribution reads this request's `_meta`. The verdict is one shared `Resolve`
dependency, read by both the resolver and the tool. And `peers.of` names the caller by pid from
`/proc/net/tcp`, so the census does not rest on self-report. A `pinledger` JSONL gives the count a
denominator that survives journald's seven days. See [who called is a per-request
read](../constraints/who-called-is-a-per-request-read.md).

The criterion itself is replaced a second time — by a census rather than a wait — and the flag is
flipped. That is [the pin rollout needed a reason, not a
count](../decisions/the-pin-rollout-needed-a-reason-not-a-count.md), second amendment.
