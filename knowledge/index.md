---
okf_version: "0.2"
---

# Constraint

* [An eval query is removed from the corpus it is hunting, and the corpus contains what the arm changes](constraints/an-eval-query-must-not-be-findable-by-identity.md) - The two ways this harness prints a number that means nothing, and the doc protocol's real yield.
* [A new language is indexed with no change, and the price is that it cannot be named in a filter](constraints/a-new-language-costs-nothing-and-cannot-be-filtered.md) - Discovery is a denylist, so an unknown extension is searchable today; the cost is an unselectable `lang=""`, and the unknown-`lang` filter that used to answer with silence.
* [Joining a root is a configuration change to an existing project, and it has to reach the index](constraints/joining-a-root-is-a-config-change-to-an-existing-project.md) - One registry row per resolved path records why it is there, and the config signature turns the next index pass into a reconcile rather than a no-op.
* [A model carries three things, and pooling is the one that fails invisibly](constraints/a-model-carries-three-things-and-pooling-is-the-invisible-one.md) - Prefixes and context limits fail loudly; a mispooled model returns a plausible unit vector and reads as the model losing.
* [This host cannot produce an admissible latency number while it throttles](constraints/this-host-cannot-produce-an-admissible-latency-number.md) - The card runs at ~15% of rated clock, so a timing figure measured here measures the host; recall survives, latency does not — and the amendment corrects a lifetime counter read as live state.
* [The search unit is the caller's own workspace plus what it federates](constraints/the-search-unit-is-the-callers-own-workspace.md) - The root was a string the model wrote, so any of ~159 registered projects was reachable by naming it; the boundary now comes from the client's own roots, and the honest claim for it is containment rather than authorization.
* [No amount of served prose makes a client prefer this server over its own grep](constraints/served-prose-does-not-beat-grep.md) - Three escalations lost to grep on a literal string, so the agent layer asks what the client's own tools cannot answer — plus what `--allowedTools` does not do.
* [Nine nodes run on the CPU EP in both exports, so the GPU rule is written about tensor math rather than about nodes](constraints/nine-nodes-run-on-the-cpu-and-that-is-the-design.md) - `disable_cpu_ep_fallback` refuses both models over shape plumbing ORT pins to CPU on purpose; an op allowlist plus a 1% time bound is what enforces the rule instead.
* [The CPU side of an indexing pass is already flat, and six arms failed to move it](constraints/the-cpu-side-of-an-indexing-pass-is-already-flat.md) - A pass costs ~1.1 mean cores and none of six knobs cleared its pre-committed threshold; `MALLOC_ARENA_MAX=2`, the one people reach for first, made it 27% worse.
* [`watching` answers for one project, and arming is asynchronous](constraints/watching-is-per-project-and-arming-is-asynchronous.md) - Thread liveness was true the instant the watcher started and said nothing about this project; the rebuild lands seconds after registration and inotify replays nothing, so a write inside that window is lost.

* [What a root and 135 federated members cost](constraints/what-a-root-and-135-members-cost.md) - p50 1.46 s, 91% of one core, 2.31 GiB anon — and why `memory.events high == 0` is the wrong thing for a verification block to assert.

# Decision

* [The bundle gate asserts that it is wired](decisions/the-bundle-gate-asserts-that-it-is-installed.md) - The bundle tests are a gate only if something runs them, so one arm reads the workflow and fails when the step that invokes them is gone, excused, unpinned, or attached to a trigger that never fires on a change.
* [The embedder is settled by a tie-break, because the two finalists are not distinguishable](decisions/the-embedder-is-settled-by-a-tie-break.md) - bge-base and gte-modernbert differ by 0.023 recall@10 at p=0.39, so a pre-committed order decides it; the header arm is the one result that is not a tie.
* [The scope header is the path, and the derived line is deleted](decisions/the-header-is-the-path-and-nothing-else.md) - Flat on docs and on code, and below no-header at all on its own; the census refuted redundancy, which leaves dilution.
* [Per-type knowledge enters through the header, not through a second splitter](decisions/the-header-dispatches-on-type-the-splitter-does-not.md) - REFUTED by its own falsification condition: the four arms ran and tied, and the dispatch they justified is gone.
* [One chunker ships, it is third-party, and the part we wrote is the header](decisions/one-chunker-and-it-is-third-party.md) - Six splitter libraries and four boundary strategies compared on August 2026 evidence, and why the measured gain is in the scope header rather than the boundary.
* [Two MCP tools, and everything an operator needs is on the CLI](decisions/two-tools-and-the-operator-surface-is-the-cli.md) - The 4-tool, 16-route, 20-command surface contracted to two actions, and the refusals that hold it there.
* [sqlite-vec, with the measurement that reverses it stated up front](decisions/sqlite-vec-survives-only-because-search-is-scoped.md) - Brute force survives only because search is scoped to one project plus its members; the scoped p95 that would overturn it.
* [Progress is a file, not a protocol notification](decisions/progress-is-a-file-not-a-protocol-notification.md) - MCP's two mechanisms for long-running work both need a live request or a second status surface; the counter is a throttled JSON file instead.
* [CI runs no GPU and no live job, and the self-hosted runner is deregistered](decisions/ci-does-not-touch-the-gpu.md) - A runner group is unavailable on a personal account and the GPU lanes contended for the one card; both reasons point the same way.
* [The thermal pause is not what indexing costs, and the profile says so](decisions/the-thermal-pause-is-not-what-indexing-costs.md) - 80.7% of self-time is the ONNX forward pass and 0.04% is cool_down, so the cooldown stays -- and the batching fix that profile seemed to imply was measured, refuted and reverted.

* [The pin rollout needed a reason, not a count](decisions/the-pin-rollout-needed-a-reason-not-a-count.md) - 4498 zero-pins against 23 real ones could not say whether the pin was one client setting away or unreachable; `_ask` now logs the branch, and the flag waits on that.

# Defect

* [A floating range on onnxruntime-gpu changed the CUDA major version](defects/a-floating-range-changed-the-cuda-major.md) - A resolver bump moved the linked CUDA from 12 to 13, every GPU test failed on a missing shared object, and only the fourth assertion caught the CPU fallback.

* [A deleted file stayed searchable in a federated member, from two independent causes](defects/a-deleted-file-stayed-searchable-in-a-member.md) - inotify keys a directory by inode and reports the last-registered path; and the 60 s tick re-armed 120,000 watches unconditionally, which is a blind window with no replay.

* [A delete is lost while a project is still settling](defects/a-delete-is-lost-while-a-project-is-still-settling.md) - RESOLVED: the `index` tool re-armed all ~120,000 inotify watches on every call, and the test polled it, so the events fell in a blind window the probe was opening itself.

* [A rootless call with no pin told the caller to index their home directory](defects/a-rootless-call-told-the-caller-to-index-their-home-directory.md) - `default_root` fell back to the daemon's cwd, so a real session's `search` came back as advice to index `$HOME` -- a root `FORBIDDEN_ROOTS` would have refused anyway.

* [The watcher woke the indexer for files git ignores, forever](defects/the-watcher-woke-the-indexer-for-files-git-ignores.md) - The shared `indexable` predicate never sees gitignore, so a gitignored build cache submitted a full-project diff every few seconds while the fleet index was running.

* [A released member kept the excludes of the root that released it](defects/a-released-member-kept-the-roots-excludes.md) - Narrowing on join was never mirrored by widening on leave, and `unregister` reported only the rows it deleted — hiding the one member that survives the release.

* [A deliberate stop was indistinguishable from a crash](defects/a-deliberate-stop-was-indistinguishable-from-a-crash.md) - uvicorn waited on MCP's open streams, so systemd SIGKILLed at 90 s and fired OnFailure on every ordinary stop; the restart tests' own stop helper hid it. Corrected 08-20: the exit it claimed to reach was still unreachable.
* [A cancelled task group cannot reach a shielded thread](defects/a-cancelled-task-group-cannot-reach-a-shielded-thread.md) - The stop hung again. timeout_graceful_shutdown bounds the connection wait and nothing else, and a plain-`def` tool runs under anyio's shield where the cancel cannot reach it.

* [An unflagged project stayed fully searchable](defects/an-unflagged-project-stayed-fully-searchable.md) - `registry.get` returns disabled rows and unflagging deletes no store, so an explicitly turned-off project answered by name; and 147 registered-but-never-indexed rows passed a gate whose message said they did not.

* [A project whose directory was gone could not be turned off](defects/a-vanished-project-could-not-be-turned-off.md) - The `is_dir` refusal sat above the unflag branch, so the row an operator most needs to disable was the one row the surface could not act on; two survived every restart.

* [One live test skipped the disable-don't-prune teardown](defects/a-live-test-skipped-the-disable-teardown.md) - The policy was fine and one test was not: its two leaked rows were the whole of `doctor`'s red, and `fleet_unchanged` now counts rows around every live module.

* [doctor walked rows to stores and never stores to rows](defects/doctor-only-walked-the-registry.md) - 144 index directories at 436 MiB had no row to be found from, under a check five consecutive plans quoted as proof of no orphans.

* [The chunk budget and the token window do not fit each other](defects/chunks-are-cut-before-the-model-sees-them.md) - REFUTED: 70% of chunks are cut and it costs no recall — widening the window past the p95 moved the number by nothing, and cutting the chunk to fit made it worse.

# Runbook

* [Running anything that touches the real GPU](runbooks/running-the-live-suite.md) - Both preconditions, the one-at-a-time rule, and how to read a bake-off table without reading the levels.
* [Restoring the registry, the one file that cannot be re-derived](runbooks/restoring-the-registry.md) - Where the backups are, why no re-index follows a restore, and the two traps that produced the losses.
* [Restoring Dynamic Boost, and the two files the driver package does not install](runbooks/restoring-dynamic-boost.md) - Why the GPU sat at 46% of its budget, the systemd unit and D-Bus policy Ubuntu ships nowhere useful, and the nvidia-smi field that reports failure on a working system.
