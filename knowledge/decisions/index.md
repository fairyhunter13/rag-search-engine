# Decision

* [A dead row takes its store, and the delete is a move](a-dead-row-takes-its-store-and-the-delete-is-a-move.md) - Removing a row freed no disk at all: 197 of 616 store directories had no row and 748 MB sat waiting for a hand-typed prune. The reaper now takes the store too, behind the idle floor and a shape-of-the-answer refusal, and the removal is a move into a week-long quarantine.
* [A guard is placed by its coupling, not by its topic](a-guard-is-placed-by-its-coupling-not-by-its-topic.md) - A guard that needs this device, a private project or a local path lives in the private companion repo. Everything device-neutral stays in the public tree. The split is per assertion, so one feature's guards routinely land in both.
* [A row leaves on a delete event and never on a scan](a-row-leaves-on-a-delete-event-and-never-on-a-scan.md) - The registry refused to prune a missing path, because an unmount and a deletion look the same to a scan. They do not look the same to a delete event, so removal became automatic on the event, behind a parent test and a grace period.
* [A search writes one structured row, and the fleet log level stays where it
  is](a-search-writes-one-row-and-the-log-level-stays.md) - The pool cut starved 307 projects and
  finding it needed an offline replay that rebuilt the pool by hand, because `search.py` and
  `rank.py` hold zero log calls and the reply carries only `took_ms` and a project count. A louder
  journal was already refused on a measurement, so the record is a JSONL row per search rather
  than prose at INFO.
* [An absent directory is three answers, and only one is a deletion](an-absent-directory-is-three-answers-and-only-one-is-a-deletion.md) - inotify has no replay, so a repo deleted while the daemon was down reaches no event. The reconciliation that closes that hole answers `deleted`, `unmounted` or `unknown` from the `st_dev` recorded at enrolment, reports by default, and acts on `deleted` alone. The hourly sweep backfills the device for any present, occupied path, and leaves an empty directory blind because that is what a bare mount point looks like.
* [CI runs no GPU and no live job, and the self-hosted runner is
  deregistered](ci-does-not-touch-the-gpu.md) - A self-hosted runner on a public personal-account
  repo cannot be scoped by a runner group. The GPU suites contend for the one card everything else
  is serialized on. Both reasons point the same way.
* [jemalloc is refused, because three arms lost and the last one was worse than
  glibc](jemalloc-lost-the-number-it-was-chosen-for.md) - Gate −25% `RssAnon`, written first. The
  subprocess arm gave −5.3%, the live daemon −12% at 40 minutes and **+3.7% at 55**, rising the
  whole way against a 2,586 MB glibc baseline. The mechanism is workload shape, not maintenance:
  Meta un-archived jemalloc in March 2026, and `malloc_trim` under it is inert, which would have
  silently cost `release_models` its 2,082 MiB reclaim.
* [Membership is a directory symlink at depth four or less, and the sweep is what makes that
  automatic](membership-is-a-symlink-and-the-depth-limit-is-measured.md) - Both halves of the
  membership predicate were inherited from the deleted v1 engine and argued nowhere. The depth
  limit is now measured to cost zero on the live tree, and a declarative member list stays
  refused. Re-discovery moved off the explicit index call onto an hourly sweep.
* [One chunker ships, it is third-party, and the part we wrote is the header](one-chunker-and-it-is-third-party.md) - Six splitter libraries and four boundary strategies were compared on August 2026 evidence. Semantic-text-splitter wins on zero dependencies and offsets, and the measured gain is in the scope header, not the boundary.
* [The module ceiling is counted in statements, not lines](the-module-ceiling-is-counted-in-statements-not-lines.md) - A 300-line physical cap and `ruff format` owned the same number, so CI stayed red with no green available: the formatter rewrote `tools.py` from 283 lines to 324 without adding a statement. The cap now counts executable lines, which the formatter cannot move.
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
* [The daemon records its own work, and the library that filled the journal goes
  quiet](the-daemon-records-its-own-work-and-the-library-goes-quiet.md) - 454 index passes ran in
  24 h and the journal described none of them, while 3,800 of its 5,912 lines were `watchfiles`
  announcing a change count with no project name. One JSONL row per index pass, watch batch, sweep
  and re-arm replaces that, and `watchfiles` drops to WARNING, so the journal gets quieter rather
  than louder.
* [The embedder is settled by a tie-break, because the two finalists are not
  distinguishable](the-embedder-is-settled-by-a-tie-break.md) - Ten arms over 300 paired queries.
  bge-base and gte-modernbert differ by 0.023 recall@10 with p=0.39. So the pre-committed order
  decides it: fp16 ONNX, then window, then incumbency. The header arm is the one result that is not a tie.
* [The pin rollout needed a reason, not a count, and the flag stays 0 until journald carries one](the-pin-rollout-needed-a-reason-not-a-count.md) - `workspace pin: 0 root(s)` 4498 times against 23 real pins looked like a guard that never fires. The count cannot say which of `_ask`'s two branches returned empty, so it could not distinguish 'one client setting away' from 'unreachable'. `_ask` now logs the branch — and a live call from Claude Code today produced a real pin and a correct refusal.
* [The scope header is the path, and the derived line is deleted](the-header-is-the-path-and-nothing-else.md) - Ablated on docs and on code. Flat both times, and below no-header at all on its own. The census refuted the redundancy explanation, which makes the result harder rather than softer.
* [The sweep releases what the config denies, and nothing else](the-sweep-releases-what-the-config-denies-and-nothing-else.md) - The hourly sweep only ever added, so 294 rows outlived the `federation.exclude` that would have refused them. The release is the difference between two walks of the same tree, which is why a link that is merely gone can never land in it.
* [The thermal pause is not what indexing costs, and the profile says
  so](the-thermal-pause-is-not-what-indexing-costs.md) - A 25-second py-spy sample of a live
  indexing worker put 80.7% of self-time inside the ONNX forward pass, and 0.04% inside cool_down.
  So the cooldown stays, and the lever is batch size.
* [Two MCP tools, and everything an operator needs is on the CLI](two-tools-and-the-operator-surface-is-the-cli.md) - The old engine exposed 4 tools, 16 HTTP routes and 20 CLI commands. The rebuild exposes two actions to the agent, and the refusals — no wait, no fleet fan-out, no auto-index — are the load-bearing part.
