# Decisions

**Incident narrative and design rationale go here, not in `CLAUDE.md`.**

`CLAUDE.md` is loaded verbatim into every agent session. Between 2026-06-26 and 2026-07-30 it
grew from 4.5 KB to 23 KB because every change appended its own post-mortem, so each turn paid
for five weeks of history it never acted on. The instructions stayed; the history moved here.

The split is by *what the reader does with it*:

- **`CLAUDE.md`** — what changes an agent's next action. A rule, a command, a hazard.
- **`docs/decisions/`** — why the rule exists, what it cost to learn, what was tried and rejected.
  Read on demand, linked from the one-line rule.
- **`docs/architecture/federation-ops-and-invariants.md`** — the write-path spec, and §14's map
  from each invariant to the live test that proves it. A rule's definition is its test.

Nothing here is deleted when a subsystem retires. A retired mechanism's lesson usually outlives
it — HR36 is gone, its rule about reuse stamps is not.

**On the `P#`/`HR#` ids in these records:** they refer to a `docs/world-model/model.yaml`
register retired on 2026-08-14, which had become a sixth restatement of rules stated and enforced
elsewhere. The records are kept as written — renaming history to match a later decision loses the
trail of why it changed — so the ids are read as the labels they were at the time. `HR#` still
resolves: §13b of `docs/architecture/federation-ops-and-invariants.md` is now its one definition
table. `P#` does not, and did not mean one thing even before — the register's invariants, a
development phase counter, and an issue-priority ladder all used it.

Same rule for the test files these records cite. Eleven were named for invariants
(`test_p5_server.py`, `test_p6_daemon.py`, …) and were renamed for what they test
(`test_server.py`, `test_daemon.py`, …) on 2026-08-14; strip the `p<N>_` to read an older
citation. Test *function* names still carry phase numbers and were left alone — they are
identifiers, and §14's coverage map resolves them under a guard.

## Index

| Date | Decision |
|------|----------|
| 2026-08-18 | [The bundle becomes the home](2026-08-18-the-bundle-becomes-the-home.md) |
| 2026-08-17 | [The env var reached only systemd's children](2026-08-17-the-env-var-reached-only-systemds-children.md) |
| 2026-08-17 | [OKF lands here as a signpost, not a corpus](2026-08-17-okf-lands-as-a-signpost.md) |
| 2026-08-16 | [A completeness stamp is not a freshness stamp](2026-08-16-a-completeness-stamp-is-not-a-freshness-stamp.md) |
| 2026-08-14 | [The bit-lane threshold, and the three times it was re-validated](2026-08-14-the-bit-lane-threshold-was-validated-three-times.md) |
| 2026-08-14 | [The extractor revision log](2026-08-14-the-extractor-rev-log.md) |
| 2026-08-14 | [The invariant register was a sixth copy, and its checker read fewer files than it printed](2026-08-14-the-register-was-a-sixth-copy.md) |
| 2026-08-05 | [The repair inherited the blind spot it was fixing](2026-08-05-the-repair-inherited-the-blind-spot-it-was-fixing.md) |
| 2026-08-05 | [The name guard would have published the list](2026-08-05-the-name-guard-would-have-published-the-list.md) |
| 2026-08-05 | [The INVALID nothing could repair, and the count that could hide it](2026-08-05-the-invalid-nothing-could-repair.md) |
| 2026-08-05 | [The full suite was red in the record and green in fact](2026-08-05-the-full-suite-was-red-in-the-record-only.md) |
| 2026-08-05 | [Guards on a function nothing calls](2026-08-05-guards-on-a-function-nothing-calls.md) |
| 2026-08-05 | [A fleet number behind a project-scoped gate](2026-08-05-a-fleet-number-behind-a-project-scoped-gate.md) |
| 2026-08-05 | [A deleted root is not a reindex](2026-08-05-a-deleted-root-is-not-a-reindex.md) |
| 2026-08-04 | [Zero symbols is not the same as unreachable](2026-08-04-zero-symbols-is-not-unreachable.md) |
| 2026-08-04 | [Two diversity rules in the result path, and what each one is worth](2026-08-04-two-diversity-rules-that-cost-no-recall.md) |
| 2026-08-04 | [The vendor prize buys edges nobody reads](2026-08-04-the-vendor-prize-buys-edges-nobody-reads.md) |
| 2026-08-04 | [The three questions the search budget had left open](2026-08-04-the-three-questions-the-search-budget-had-left-open.md) |
| 2026-08-04 | [The residual has no types to resolve](2026-08-04-the-residual-has-no-types-to-resolve.md) |
| 2026-08-04 | [The language axis was already universal](2026-08-04-the-language-axis-was-already-universal.md) |
| 2026-08-04 | [Denying the daemon swap](2026-08-04-denying-the-daemon-swap.md) |
| 2026-08-04 | [A receiver type dissolves the ambiguity rather than trading precision for it](2026-08-04-a-receiver-type-dissolves-the-ambiguity.md) |
| 2026-08-04 | [A failed store is one warning line](2026-08-04-a-failed-store-is-one-warning-line.md) |
| 2026-08-03 | [The embedding track: nomic + task prefixes shipped, the breadcrumb declined](2026-08-03-the-breadcrumb-writes-the-query-into-the-chunk.md) |
| 2026-08-03 | [The extraction track is closed: the last two items were measured and declined](2026-08-03-the-extraction-track-is-closed.md) |
| 2026-08-03 | [Most of the dark set is correctly dark](2026-08-03-most-of-the-dark-set-is-correctly-dark.md) |
| 2026-08-03 | [An import is a declared fact, and a specifier is not code semantics](2026-08-03-an-import-is-a-declared-fact.md) |
| 2026-08-03 | [A container the span cannot see, and an ERROR that was charged for its children](2026-08-03-a-container-the-span-cannot-see.md) |
| 2026-08-03 | [The "zero new code" pack upgrade was an API migration, and only the tests said so](2026-08-03-the-pack-upgrade-was-an-api-migration.md) |
| 2026-08-03 | [Personalized PageRank over the call graph: built, measured, reverted](2026-08-03-personalized-pagerank-was-built-and-reverted.md) |
| 2026-08-03 | [A path is a field, not a line of body text — and only the third probe could see it](2026-08-03-a-path-is-a-field-not-a-line-of-body.md) |
| 2026-08-03 | [The eval harness puts the identifier in the query, so it cannot see a tokenizer change](2026-08-03-the-eval-harness-cannot-see-a-tokenizer.md) |
| 2026-08-01 | [The lint gate stopped at `src/`, and naming the directory would not have fixed it](2026-08-01-the-lint-gate-stopped-at-src.md) |
| 2026-08-01 | [The prefix-free escape hatch is not there, and two of the three backlog items were mis-founded](2026-08-01-the-prefix-free-escape-hatch-is-not-there.md) |
| 2026-08-01 | [An exported edge must name an exported node, and a paired result must stay paired](2026-08-01-an-exported-edge-must-name-an-exported-node.md) |
| 2026-08-01 | [What the `e8` bump actually owed, written after it shipped](2026-08-01-what-the-e8-bump-actually-owed.md) |
| 2026-07-31 | [The fast lane's cost baseline, and the fixture-scope lever it refuted](2026-07-31-the-fast-lane-cost-baseline.md) |
| 2026-07-31 | [The evidence for `EMBED_MODEL` was not in the repo, and the obvious way to measure it saturates](2026-07-31-retrieval-eval-harness.md) |
| 2026-07-31 | [The idle gate was measuring a neighbour, and underneath that it has almost no headroom](2026-07-31-idle-gate-floor.md) |
| 2026-07-31 | [A private key was searchable, and a CSV export was waking the graph](2026-07-31-corpus-hygiene.md) |
| 2026-07-31 | [Call extraction skips tree-sitter injections deliberately](2026-07-31-call-extraction-skips-injections-deliberately.md) |
| 2026-07-31 | [The two levers are additive, and the bill is a fleet rebuild](2026-07-31-two-levers-additive-and-the-bill.md) |
| 2026-07-31 | [The span scan was never hot, and bisect does not answer the same question](2026-07-31-span-scan-is-neither-hot-nor-equivalent.md) |
| 2026-07-31 | [An edge is a resolved call, or it is not an edge](2026-07-31-an-edge-is-a-resolved-call.md) |
| 2026-07-31 | [The challenger's margin was the prefixes](2026-07-31-the-prefix-is-a-precondition-not-a-tuning-knob.md) |
| 2026-07-31 | [87.8% of the `.html` corpus is byte-identical copies](2026-07-31-html-duplication-is-within-project.md) |
| 2026-07-31 | [Releasing a pause lease schedules nothing](2026-07-31-releasing-a-lease-schedules-nothing.md) |
| 2026-07-31 | [A function bound to a name is a definition, and a heading is not an identifier](2026-07-31-e7-named-bindings-and-headings.md) |
| 2026-07-31 | [A re-derive writes through the graph, it does not empty it first](2026-07-31-atomic-graph-rederive.md) |
| 2026-07-31 | [The re-derive that never reached 89% of the fleet](2026-07-31-reconcile-walk-starvation.md) |
| 2026-07-31 | [Paying for context once: the CLAUDE.md trim and the deduplicated MCP block](2026-07-31-context-budget.md) |
| 2026-07-30 | [One live suite at a time, keyed on process not lock name](2026-07-30-one-live-suite-at-a-time.md) |
| 2026-07-30 | [Splitting the `slow` marker into the two reasons it conflated](2026-07-30-slow-marker-split.md) |
| 2026-07-29 | [Refusing to reload the daemon under a sweeps lease](2026-07-29-reload-under-a-sweeps-lease.md) |
| 2026-07-29 | [The fifth MCP tool returned assembled prose](2026-07-29-the-fifth-tool-returned-assembled-prose.md) |
| 2026-07-29 | [The descriptor wedge, and the leak that made it permanent](2026-07-29-descriptor-exhaustion-in-the-federation-fanout.md) |
| 2026-07-29 | [Giving the GPU back before idle](2026-07-29-vram-starvation.md) |
| 2026-07-28 | [What left with tier 3, recorded so it is not re-derived](2026-07-28-tier-3-retirement.md) |
| 2026-07-09 | [Public-release hardening and the runnable-by-anyone contract](2026-07-09-public-release-hardening.md) |
| 2026-07-01 | [Four root causes of idle CPU pinning](2026-07-01-idle-cpu-root-causes.md) |
