---
okf_version: "0.2"
---

# Constraint

* [An eval query is removed from the corpus it is hunting, and the corpus contains what the arm changes](constraints/an-eval-query-must-not-be-findable-by-identity.md) - The two ways this harness prints a number that means nothing, and the doc protocol's real yield.
* [A new language is indexed with no change, and the price is that it cannot be named in a filter](constraints/a-new-language-costs-nothing-and-cannot-be-filtered.md) - Discovery is a denylist, so an unknown extension is searchable today; the cost is an unselectable `lang=""`, and the unknown-`lang` filter that used to answer with silence.
* [Joining a root is a configuration change to an existing project, and it has to reach the index](constraints/joining-a-root-is-a-config-change-to-an-existing-project.md) - One registry row per resolved path records why it is there, and the config signature turns the next index pass into a reconcile rather than a no-op.
* [A model carries three things, and pooling is the one that fails invisibly](constraints/a-model-carries-three-things-and-pooling-is-the-invisible-one.md) - Prefixes and context limits fail loudly; a mispooled model returns a plausible unit vector and reads as the model losing.
* [This host cannot produce an admissible latency number while it throttles](constraints/this-host-cannot-produce-an-admissible-latency-number.md) - The card runs at ~15% of rated clock, so a timing figure measured here measures the host; recall survives, latency does not — and the amendment corrects a lifetime counter read as live state.
* [No amount of served prose makes a client prefer this server over its own grep](constraints/served-prose-does-not-beat-grep.md) - Three escalations lost to grep on a literal string, so the agent layer asks what the client's own tools cannot answer — plus what `--allowedTools` does not do.

* [Nine nodes run on the CPU EP in both exports, so the GPU rule is written about tensor math rather than about nodes](constraints/nine-nodes-run-on-the-cpu-and-that-is-the-design.md) - `disable_cpu_ep_fallback` refuses both models over shape plumbing ORT pins to CPU on purpose; an op allowlist plus a 1% time bound is what enforces the rule instead.
# Decision

* [The embedder is settled by a tie-break, because the two finalists are not distinguishable](decisions/the-embedder-is-settled-by-a-tie-break.md) - bge-base and gte-modernbert differ by 0.023 recall@10 at p=0.39, so a pre-committed order decides it; the header arm is the one result that is not a tie.
* [The scope header is the path, and the derived line is deleted](decisions/the-header-is-the-path-and-nothing-else.md) - Flat on docs and on code, and below no-header at all on its own; the census refuted redundancy, which leaves dilution.
* [Per-type knowledge enters through the header, not through a second splitter](decisions/the-header-dispatches-on-type-the-splitter-does-not.md) - REFUTED by its own falsification condition: the four arms ran and tied, and the dispatch they justified is gone.
* [One chunker ships, it is third-party, and the part we wrote is the header](decisions/one-chunker-and-it-is-third-party.md) - Six splitter libraries and four boundary strategies compared on August 2026 evidence, and why the measured gain is in the scope header rather than the boundary.
* [Two MCP tools, and everything an operator needs is on the CLI](decisions/two-tools-and-the-operator-surface-is-the-cli.md) - The 4-tool, 16-route, 20-command surface contracted to two actions, and the refusals that hold it there.
* [sqlite-vec, with the measurement that reverses it stated up front](decisions/sqlite-vec-survives-only-because-search-is-scoped.md) - Brute force survives only because search is scoped to one project plus its members; the scoped p95 that would overturn it.
* [Progress is a file, not a protocol notification](decisions/progress-is-a-file-not-a-protocol-notification.md) - MCP's two mechanisms for long-running work both need a live request or a second status surface; the counter is a throttled JSON file instead.
* [CI runs no GPU and no live job, and the self-hosted runner is deregistered](decisions/ci-does-not-touch-the-gpu.md) - A runner group is unavailable on a personal account and the GPU lanes contended for the one card; both reasons point the same way.
* [The thermal pause is not what indexing costs, and the profile says so](decisions/the-thermal-pause-is-not-what-indexing-costs.md) - 80.7% of self-time is the ONNX forward pass and 0.04% is cool_down, so the cooldown stays -- and the batching fix that profile seemed to imply was measured, refuted and reverted.

# Defect

* [A floating range on onnxruntime-gpu changed the CUDA major version](defects/a-floating-range-changed-the-cuda-major.md) - A resolver bump moved the linked CUDA from 12 to 13, every GPU test failed on a missing shared object, and only the fourth assertion caught the CPU fallback.

* [A deleted file stayed searchable in a federated member, from two independent causes](defects/a-deleted-file-stayed-searchable-in-a-member.md) - inotify keys a directory by inode and reports the last-registered path; and the 60 s tick re-armed 120,000 watches unconditionally, which is a blind window with no replay.

* [A released member kept the excludes of the root that released it](defects/a-released-member-kept-the-roots-excludes.md) - Narrowing on join was never mirrored by widening on leave, and `unregister` reported only the rows it deleted — hiding the one member that survives the release.

* [A deliberate stop was indistinguishable from a crash](defects/a-deliberate-stop-was-indistinguishable-from-a-crash.md) - uvicorn waited on MCP's open streams, so systemd SIGKILLed at 90 s and fired OnFailure on every ordinary stop; the restart tests' own stop helper hid it. Corrected 08-20: the exit it claimed to reach was still unreachable.
* [A cancelled task group cannot reach a shielded thread](defects/a-cancelled-task-group-cannot-reach-a-shielded-thread.md) - The stop hung again. timeout_graceful_shutdown bounds the connection wait and nothing else, and a plain-`def` tool runs under anyio's shield where the cancel cannot reach it.

* [The chunk budget and the token window do not fit each other](defects/chunks-are-cut-before-the-model-sees-them.md) - REFUTED: 70% of chunks are cut and it costs no recall — widening the window past the p95 moved the number by nothing, and cutting the chunk to fit made it worse.

# Runbook

* [Running anything that touches the real GPU](runbooks/running-the-live-suite.md) - Both preconditions, the one-at-a-time rule, and how to read a bake-off table without reading the levels.
* [Restoring the registry, the one file that cannot be re-derived](runbooks/restoring-the-registry.md) - Where the backups are, why no re-index follows a restore, and the two traps that produced the losses.
* [Restoring Dynamic Boost, and the two files the driver package does not install](runbooks/restoring-dynamic-boost.md) - Why the GPU sat at 46% of its budget, the systemd unit and D-Bus policy Ubuntu ships nowhere useful, and the nvidia-smi field that reports failure on a working system.
