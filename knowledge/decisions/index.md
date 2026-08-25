# Decision

* [CI runs no GPU and no live job, and the self-hosted runner is
  deregistered](ci-does-not-touch-the-gpu.md) - A self-hosted runner on a public personal-account
  repo cannot be scoped by a runner group. The GPU suites contend for the one card everything else
  is serialized on. Both reasons point the same way.
* [Membership is a directory symlink at depth four or less, and the sweep is what makes that
  automatic](membership-is-a-symlink-and-the-depth-limit-is-measured.md) - Both halves of the
  membership predicate were inherited from the deleted v1 engine and argued nowhere. The depth
  limit is now measured to cost zero on the live tree, and a declarative member list stays
  refused. Re-discovery moved off the explicit index call onto an hourly sweep.
* [One chunker ships, it is third-party, and the part we wrote is the header](one-chunker-and-it-is-third-party.md) - Six splitter libraries and four boundary strategies were compared on August 2026 evidence. Semantic-text-splitter wins on zero dependencies and offsets, and the measured gain is in the scope header, not the boundary.
* [Per-type knowledge enters through the header, not through a second splitter](the-header-dispatches-on-type-the-splitter-does-not.md) - A quarter of the corpus is prose and structured data that the code-only evidence never covered. The fix is four arms inside scope_header, because that is reversible and a second chunker costs a full re-index to compare.
* [Progress is a file, not a protocol
  notification](progress-is-a-file-not-a-protocol-notification.md) - MCP has two mechanisms for
  long-running work. Both are the wrong shape for an indexer whose work outlives the request that
  started it. So the counter is a throttled JSON file, which any reader can poll without
  importing the GPU stack.
* [sqlite-vec, with the measurement that reverses it stated up
  front](sqlite-vec-survives-only-because-search-is-scoped.md) - Brute-force vector scan in SQLite
  is chosen over an ANN index, because search is scoped to one project plus its members (~4k
  chunks). The kill criterion is a scoped p95 above ~200 ms.
* [The bundle gate asserts that it is wired, not only that the bundle
  passes](the-bundle-gate-asserts-that-it-is-installed.md) - The bundle tests are a gate only if
  something runs them. So one arm reads the workflow. It fails when the step that invokes them is
  gone, excused, unpinned, or attached to a trigger that never fires on a change.
* [The bundle gate runs before the push, not after](the-bundle-gate-runs-before-the-push-not-after.md) - CI was this bundle's only enforcement, so every ungated change was already published by the time anything read it. A pre-push runs the same check at the moment the change is made, and four tests assert the hook itself is live.
* [The content-hash skip is declined, because a store cannot see another project's duplicate](the-content-hash-skip-is-declined-because-stores-are-per-project.md) - Each project owns its own sqlite file, so two near-identical deployed refs are two stores and neither `files(sha256)` index can see the other. The path-keyed skip in `discover.plan` already covers the within-project case and uses no index. The dead index line is deleted, and the fleet prize is measured at 1147 MiB of worktree stores.
* [The embedder is settled by a tie-break, because the two finalists are not
  distinguishable](the-embedder-is-settled-by-a-tie-break.md) - Ten arms over 300 paired queries.
  bge-base and gte-modernbert differ by 0.023 recall@10 with p=0.39. So the pre-committed order
  decides it: fp16 ONNX, then window, then incumbency. The header arm is the one result that is not a tie.
* [The pin rollout needed a reason, not a count, and the flag stays 0 until journald carries one](the-pin-rollout-needed-a-reason-not-a-count.md) - `workspace pin: 0 root(s)` 4498 times against 23 real pins looked like a guard that never fires. The count cannot say which of `_ask`'s two branches returned empty, so it could not distinguish 'one client setting away' from 'unreachable'. `_ask` now logs the branch — and a live call from Claude Code today produced a real pin and a correct refusal.
* [The scope header is the path, and the derived line is deleted](the-header-is-the-path-and-nothing-else.md) - Ablated on docs and on code. Flat both times, and below no-header at all on its own. The census refuted the redundancy explanation, which makes the result harder rather than softer.
* [The thermal pause is not what indexing costs, and the profile says
  so](the-thermal-pause-is-not-what-indexing-costs.md) - A 25-second py-spy sample of a live
  indexing worker put 80.7% of self-time inside the ONNX forward pass, and 0.04% inside cool_down.
  So the cooldown stays, and the lever is batch size.
* [Two MCP tools, and everything an operator needs is on the CLI](two-tools-and-the-operator-surface-is-the-cli.md) - The old engine exposed 4 tools, 16 HTTP routes and 20 CLI commands. The rebuild exposes two actions to the agent, and the refusals — no wait, no fleet fan-out, no auto-index — are the load-bearing part.
