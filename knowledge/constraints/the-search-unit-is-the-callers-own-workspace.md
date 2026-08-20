---
type: Constraint
resource: src/coderag/scope.py, src/coderag/tools.py, src/coderag/systemd.py, tests/test_scope.py
title: The search unit is the caller's own workspace plus what it federates, and that is containment rather than authorization
description: "The root was a string the model wrote, so any of ~159 registered projects was reachable from any session by naming it. The boundary now comes from the client's own roots, through a parameter the model cannot see — and the honest claim for it is narrow."
tags: [mcp, roots, scoping, security, federation]
status: stable
generated: { by: claude/opus-5, at: 2026-08-20T18:00:00Z }
---

# What was already right, and it was most of it

`search` searches exactly `federation.expand(root)`, which is `[root, *members_of(root)]` — **one
level, not transitive**, so A federating B federating C does not reach C. There is no fleet-wide
fan-out mode at all, and the reason is a number: 164.78 s unscoped against 7.01 s scoped, answering
a question about one repo with a member's vendored JavaScript. `registry.resolve` is
`expanduser().resolve()`, so a symlink is never a key.

The hole was upstream of all of it. `root` is a caller-supplied string, and the daemon keeps no
session to check it against (`stateless_http=True`). Any client could name **any** registered root
and get that project and its members — no guessing required, because the model is handed the paths
it needs by its own working tree.

# The boundary is the client's, and it arrives where the model cannot reach it

MCP's `roots` is the one channel carrying the client's workspace without the model writing it, and
the SDK's `ListRoots` resolver marker keeps it out of the LLM-visible schema — asserted, not assumed:
`test_the_workspace_pin_is_not_a_parameter_the_model_can_write`, over the wire in the live suite too.
A parameter the model can write is a parameter the model can get wrong, and it would *read* as
authoritative while being the same guess that caused the problem.

`enforce` allows a target the workspace contains **or sits inside**. The ancestor arm is not
laxity: an editor opened on `repo/backend` has to be able to name `repo`, and it cannot walk out
past it because search still requires the row to be registered, enabled and indexed, and
`FORBIDDEN_ROOTS` means `/` and `$HOME` never become one.

Rejected sources, both of which work: **headers** (`.mcp.json` expands `${VAR}` from `process.env`
only — `${CLAUDE_PROJECT_DIR}` is injected into stdio children and does not expand for `http`, and
`${PWD}` is the launch shell's — so it means a file in ~159 repos and a prompt for each, and the
SDK's own docstring says a header is client-supplied input, never an identity assertion); and a
**tool parameter**, which is the thing being fixed.

# Containment, not authorization — and the difference matters

The daemon is localhost, unauthenticated, single-user. A `curl` to `:8765` with a fabricated roots
response defeats this completely, and nothing short of per-project daemons changes that — rejected,
because it is five editors holding five copies of a 12 GB model. Federation members sit outside the
pin by design; that is the feature. The client's own `Bash` tool is not path-scoped either.

So the claim is narrow and it is worth stating in those words: **coderag stops being an easier path
out of the workspace than the tools the client already gates.** That is also the argument for reusing
the client's boundary rather than inventing a second one.

# The clock on `roots`

SEP-2577 deprecated `roots` in `2026-07-28` — the same revision that made it reachable from a
stateless server, via `InputRequiredResult` instead of a back channel. Twelve-month floor, removal
eligible no earlier than **2027-07-28**. The successors the spec names are a tool parameter and
server configuration, i.e. the two things refused above, so when the removal lands this needs a
decision rather than a swap. `MRTR` in `scope.py` is where the era check lives.

# The rollout, and how it ended

`REQUIRE_CLIENT_ROOTS` defaults to `1` in code, and as of 2026-08-20 the unit no longer overrides it:
a call that arrives with no workspace pin is refused. Claude Code advertises `roots` on every
transport and now negotiates the `2026-07-28` era (`tengu_mcp_protocol_negotiation_http`) from every
profile on this machine, which is what made the flip safe — see [the pin rollout needed a reason, not
a count](../decisions/the-pin-rollout-needed-a-reason-not-a-count.md) for the census.

`scope._ask` still gates on the era as well as the capability and returns an empty result rather than
raising, because the refusal belongs in `enforce` where it can be returned as an envelope. A pin that
*did* arrive was always enforced regardless of the flag, and
`test_the_flag_never_softens_a_pin_that_did_arrive` is what stops an empty list from becoming a
bypass any client can trigger.

One era cannot be pinned at all: below `2026-07-28` the stateless transport is built
`can_send_request=False`, so `roots/list` has nowhere to ride. Those callers are `branch=legacy-path`
and they are refused now. The one on this machine was a test harness — `claude -p --mcp-config`
*without* `--strict-mcp-config`, which connects on `2025-11-25`.

# The watcher predicate is `enabled`, not `indexed`, and must not be tightened

`watch._roots()` reads `registry.enabled_projects()` — a *superset* of the indexed set, and correctly
so. inotify has no replay, so a project claimed but still queued must already be watched or writes
during its first pass are lost until the next reconcile. Do not tighten this for symmetry with the
search gate; the symmetry is the bug.

# Amendment, 2026-08-20: the load-bearing claim was carried by prose alone

"A member sits outside the pin and is reached through its root anyway" is what this document is
for, and until now nothing tested it in either direction. Every federation test runs *unpinned* —
`Rpc.tool` uses the legacy handshake and sends no roots, so `enforce` short-circuits while the flag
ships `0` — and every other scope test calls below the daemon. A change making `expand` transitive
and a change making it pin-filtered break the design in opposite directions, and the suite stayed
green for both.

`tests/test_live_scoping.py` carries it now: one pinned call over the wire, asserting the member's
own resolved path in the results and `searched.projects == 2`, with the root's own content as the
control. It also states the boundary exactly — **a member is reachable through its root and is not
nameable directly**. Reach is not authorization: the same pinned session that is already searching
the member through the root is refused when it names the member as `root`, because the member is
physically outside the pin.

Still untested, in descending consequence: the ancestor arm composed with federation, `index`'s
out-of-pin side effects, `default_root` picking `roots[0]` of a multi-folder workspace, and the
two-claiming-roots exclude union, which no fleet row exercises.
