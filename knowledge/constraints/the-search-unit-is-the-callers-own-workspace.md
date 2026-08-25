---
type: Constraint
resource: src/coderag/scope.py, src/coderag/tools.py, src/coderag/systemd.py, tests/test_scope.py, scripts/reach_census.py
title: The search unit is the caller's own workspace plus what it federates, and that is containment rather than authorization
description: "The root was a string the model wrote, so any of ~159 registered projects was reachable from any session by naming it. The boundary now comes from the client's own roots, through a parameter the model cannot see — and the honest claim for it is narrow."
tags: [mcp, roots, scoping, security, federation]
status: stable
generated: { by: claude/opus-5, at: 2026-08-20T18:00:00Z }
---

# What was already right, and it was most of it

`search` searches `federation.unit(root)`. For a root that is `[root, *members_of(root)]` — **one
level, not transitive**, so A federating B federating C does not reach C. For a member it is that
member followed by the federation of every enabled root claiming it, which the last amendment
below explains. There is no fleet-wide
fan-out mode at all. The reason is a number: 164.78 s unscoped against 7.01 s scoped. The unscoped
run answered a question about one repo with a member's vendored JavaScript. `registry.resolve` is
`expanduser().resolve()`, so a symlink is never a key.

The hole was upstream of all of it. `root` is a caller-supplied string, and the daemon keeps no
session to check it against (`stateless_http=True`). Any client could name **any** registered root
and get that project and its members. No guessing required, because the model is handed the paths
it needs by its own working tree.

# The boundary is the client's, and it arrives where the model cannot reach it

MCP's `roots` is the one channel that carries the client's workspace without the model writing it.
The SDK's `ListRoots` resolver marker keeps it out of the LLM-visible schema. A test asserts that,
so it is not assumed. `test_the_workspace_pin_is_not_a_parameter_the_model_can_write`, over the wire in the
live suite too.
A parameter the model can write is a parameter the model can get wrong. It would *read* as
authoritative while it is the same guess that caused the problem.

`enforce` allows a target the workspace contains **or sits inside**. The ancestor arm is not
laxity. An editor opened on `repo/backend` has to be able to name `repo`. It cannot walk out past
it, because search still requires the row to be registered, enabled and indexed. `FORBIDDEN_ROOTS`
means `/` and `$HOME` never become such a row.

Rejected sources, both of which work: **headers** (`.mcp.json` expands `${VAR}` from `process.env`
only — `${CLAUDE_PROJECT_DIR}` is injected into stdio children and does not expand for `http`, and
`${PWD}` is the launch shell's — so it means a file in ~159 repos and a prompt for each, and the
SDK's own docstring says a header is client-supplied input, never an identity assertion). And a
**tool parameter**, which is the thing being fixed.

# Containment, not authorization — and the difference matters

The daemon is localhost, unauthenticated, single-user. A `curl` to `:8765` with a fabricated roots
response defeats this fully. Nothing short of per-project daemons changes that. Per-project
daemons are rejected, because they are five editors holding five copies of a 12 GB model.

Federation members sit outside the pin by design. That is the feature. The client's own `Bash`
tool is not path-scoped either.

So the claim is narrow and it is worth stating in those words. **coderag stops being an easier
path out of the workspace than the tools the client already gates**. That is also the argument for
reusing the client's boundary rather than inventing a second one.

# The clock on `roots`

SEP-2577 deprecated `roots` in `2026-07-28` — the same revision that made it reachable from a
stateless server, through `InputRequiredResult` instead of a back channel. Twelve-month floor, removal
eligible no earlier than **2027-07-28**. The successors the spec names are a tool parameter and
server configuration. Those are the two things refused above. When the removal lands, this needs a
decision rather than a swap. `MRTR` in `scope.py` is where the era check lives.

# The rollout, and how it ended

`REQUIRE_CLIENT_ROOTS` defaults to `1` in code. As of 2026-08-20 the unit no longer overrides it:
a call that arrives with no workspace pin is refused. Claude Code advertises `roots` on every
transport. It now negotiates the `2026-07-28` era (`tengu_mcp_protocol_negotiation_http`) from
every profile on this machine. That is what made the flip safe. See [the pin rollout needed a
reason, not a count](../decisions/the-pin-rollout-needed-a-reason-not-a-count.md) for the census.

`scope._ask` still gates on the era as well as the capability, and returns an empty result rather
than raising. The refusal belongs in `enforce`, where it can be returned as an envelope. A pin
that *did* arrive was always enforced regardless of the flag.
`test_the_flag_never_softens_a_pin_that_did_arrive` is what stops an empty list from becoming a
bypass any client can trigger.

One era cannot be pinned at all: below `2026-07-28` the stateless transport is built
`can_send_request=False`, so `roots/list` has nowhere to ride. Those callers are `branch=legacy-path`
and they are refused now. The one on this machine was a test harness — `claude -p --mcp-config`
*without* `--strict-mcp-config`, which connects on `2025-11-25`.

# The watcher predicate is `enabled`, not `indexed`, and must not be tightened

`watch._roots()` reads `registry.enabled_projects()` — a *superset* of the indexed set, and correctly
so. inotify has no replay. So a project claimed but still queued must already be watched, or
writes during its first pass are lost until the next reconcile. Do not tighten this for symmetry with the
search gate. The symmetry is the bug.

# Amendment, 2026-08-20: the load-bearing claim was carried by prose alone

"A member sits outside the pin and is reached through its root anyway" is what this document is
for, and until now nothing tested it in either direction. Every federation test runs *unpinned*:
`Rpc.tool` uses the legacy handshake and sends no roots, so `enforce` short-circuits while the
flag ships `0`. Every other scope test calls below the daemon. A change making `expand` transitive
and a change making it pin-filtered break the design in opposite directions, and the suite stayed
green for both.

`tests/test_live_scoping.py` carries it now. One pinned call goes over the wire. It asserts the
member's own resolved path in the results and `searched.projects == 2`, with the root's own
content as the control. It also states the boundary exactly — **a member is reachable through its
root and is not nameable directly**.

Reach is not authorization. The same pinned session is already searching the member through the
root. It is refused when it names the member as `root`, because the member is physically outside
the pin.

Still untested, in descending consequence: the ancestor arm composed with federation, `index`'s
out-of-pin side effects, and `default_root` picking `roots[0]` of a multi-folder workspace. Last
is the two-claiming-roots exclude union, which no fleet row exercises.

# Amendment, 2026-08-22: the ancestor arm was sanctioned and never calculated

`enforce` has always allowed a target the pin sits inside, and the paragraph above says why — "an
editor opened on `repo/backend` has to be able to name `repo`". Nothing walked the pin up to
*find* that root. `default_root` looked `roots[0]` up in the registry verbatim. So a session
pinned one directory below a registered project was told its own project was not indexed. The
reply named the subdirectory as the thing to index.

The measurement resolved every session's cwd on this machine against the 149 enabled rows, over
14,098 sessions. **7.2% pinned a registered root** and **86.6% pinned somewhere inside one**: a
subdirectory, or a worktree under `.claude/worktrees`. The remaining 6.2% were in no indexed tree
at all. One error sends a caller back to grep for the rest of the session. The reachable share was
therefore the ceiling on every number downstream of it.

`registry.enclosing(path)` returns the nearest *enabled* project containing a path, **longest
match first** — the same rule `watch._owner` already uses, and for the same reason. A member can
live under its root's tree, and the shorter match hands back the wrong project. `default_root` falls through to
it when the pin has no row of its own. This is a `default_root` change and not a scope change;
`enforce` is untouched.

Three things stay as they were. A pin in no registered tree is still refused. The ancestor walk
stops at the nearest *registered* row, and `FORBIDDEN_ROOTS` keeps `/` and `$HOME` from ever being
one. A disabled ancestor does not answer for its subdirectory — an unflagged row has no
store to read. And a worktree resolves to its main checkout, so the results are the main
checkout's.

The uncommitted edits in the worktree are not in them, and the caller has to be told rather than
left to infer it. The reply did **not** say so. The disclosure was one sentence in
`claude-code-workflows/hooks/claude/coderag_reach.py:70`, printed once per session by a
SessionStart hook, which reaches Claude Code and no other client. See the next amendment.

Confirmed live on the day it shipped, against real interactive clients rather than a test harness.
A pin four levels down a root resolved to the root. A pin inside a worktree under that root did
the same. Both returned a hit in a federated member. A path outside the working tree the caller's own
tools can see. Same answer from four separate client profiles.

# Second amendment, 2026-08-22: the census was the ceiling, not the effect

The 93.8% quoted for the change was `7.2 + 86.6` out of the **pre-change** census. The same
arithmetic on the same data, which was as true before the change as after. It measured the share a
rule of this shape *could* reach, never that it reached it. The quantity the change moves is what a
session gets: before, only a pin on a registered root was answerable (**7.2%**). After, a pin
inside one is too.

`scripts/reach_census.py` re-derived this against the current code and registry. It calls
`registry.enclosing` rather than re-implementing the walk: 15,693 sessions, 244 rows and **156
enabled**. That is exactly the +7 the hourly sweep claimed, which cross-confirms the sweep end to
end. **14.1% direct, 79.0% resolves-up, 6.9% unresolved → 93.1% answerable**.

The 0.7 pt against the old figure is the unresolved tail growing slightly faster than the
registry, not a regression. Two traps the script has to keep: all five `~/.claude*/projects` are
symlinks to one store, so a per-profile walk multiplies every session by five. And the walk must
filter on `enabled`, or it counts rows with no store as answers.

The residue is mostly irreducible — **713 of 1,087 are bare `$HOME`**, the exact case `default_root`
exists to refuse, plus per-session scratchpads under `/tmp`. About 200 sessions are recoverable by
registering roughly six more roots.

**A divergence the census turned up**. `default_root` short-circuited on `registry.get(pin)`.
`get` reads disabled rows, while `enclosing` filters on `enabled`. So a pin equal to a disabled
row got that row back instead of resolving up to an enabled ancestor. It got a root with no store
rather than the one that has it.

Zero of 15,693 sessions hit it, so it was latent. 88 disabled rows and a sweep that adds more is
why it is fixed rather than noted. The fix is a deletion. `enclosing` already returns the pin
itself when the pin is enabled. So the short-circuit was redundant in the arm where it was right,
and wrong in the arm where it was not.

**The up walk now says so in the reply.** `scope.resolution_note` fills the existing `hint` slot
when the pin is a checkout of its own. `.git` present at the pin, which is git's marker and not
one client's directory convention. This is a new
category for this repo: the two precedents are actionable *errors*, not an advisory on a successful
reply. The reason it is worth a code change rather than another record is `bridge.py`. Every
client that is not Claude Code gets no SessionStart hook and had no disclosure anywhere. Rejected
alternatives: refusing a worktree pin (trades the reach win back for a staleness now disclosed),
auto-indexing the worktree (this already happened by accident — 144 orphan stores at 436 MiB,
"mostly deleted git worktrees"), and an overlay index of the diff (a new index kind for a live
population of zero).

# Third amendment, same day: disagreeing roots was the wrong predicate

The advisory shipped keyed on `resolved != pinned`, and that is not the condition it wanted.
`registry.enclosing` only ever returns an **ancestor**, so the two disagreeing means exactly "the
pin is a subdirectory of the answering project" — and a subdirectory's files are already in that
project's index. The sentence the advisory prints, *edits present only in yours are not in them*,
is false there.

Measured, not reasoned: `reach_census.py` now splits `resolves-up` on whether the pin carries its
own `.git`. **12,395 of 12,395 are plain subdirectories. Zero are their own checkout**. So as
shipped the note was wrong on 79% of this machine's sessions, and silent on none of them. The case
it was written for has no live sessions at all. That case is a worktree, whose content the root's
index genuinely lacks because `.claude/worktrees/` is gitignored.

The predicate is now `.git` at the pin. A worktree carries it as a file, a nested clone as a
directory, and a plain subdirectory not at all. A nested clone its parent does not gitignore is
over-warned. That is the safe direction. The test's load-bearing arms are the two silent ones: a
plain subdirectory and the root itself. The noisy version passes an unconditional string.

# Fourth amendment, 2026-08-25: a member reaches upward, and that widens the boundary

"Federation members sit outside the pin by design. That is the feature." The sentence held in one
direction only. A root reached its members. A member reached nothing, and it could not ask for the
root instead, because the root does not contain it. So a session inside a member searched 1
project of 143 and the reply looked the same as the federation's. See [a member answered alone](../defects/a-member-answered-alone-and-read-like-the-federation.md).

`federation.unit` is the search path now. A member's unit is the member, then the federation of
every **enabled** root whose key sits in the member's `roots`. A root's unit is unchanged.

State the widening plainly, because it is a change to this document's claim. A session inside one
member now reads results from 142 projects it never named and never opened. Three things bound
that. The projects are exactly what one registered root already claims. The row has to be enabled,
so a disabled root is not a route. And the containment claim survives: the reachable set is still
what the registry holds for the caller's own directory, not the fleet.

The cost is real and it is recorded in the defect: 0.65 s narrow against 5.5 s to 17.6 s wide, on
the same query from `gen3-app-c`. A root call in that federation already pays it.

