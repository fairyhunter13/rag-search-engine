---
okf_version: "0.2"
---

# Constraint

* [An eval query is removed from the corpus it is hunting, and the corpus contains what the arm changes](constraints/an-eval-query-must-not-be-findable-by-identity.md) - The two ways this harness prints a number that means nothing, and the doc protocol's real yield.
* [A new language is indexed with no change, and the price is that it cannot be named in a filter](constraints/a-new-language-costs-nothing-and-cannot-be-filtered.md) - Discovery is a denylist, so an unknown extension is searchable today; the cost is an unselectable `lang=""`, and the unknown-`lang` filter that used to answer with silence.
* [Joining a root is a configuration change to an existing project, and it has to reach the index](constraints/joining-a-root-is-a-config-change-to-an-existing-project.md) - One registry row per resolved path records why it is there, and the config signature turns the next index pass into a reconcile rather than a no-op.
* [A model carries three things, and pooling is the one that fails invisibly](constraints/a-model-carries-three-things-and-pooling-is-the-invisible-one.md) - Prefixes and context limits fail loudly; a mispooled model returns a plausible unit vector and reads as the model losing.
* [This host cannot produce an admissible latency number while it throttles](constraints/this-host-cannot-produce-an-admissible-latency-number.md) - The card runs at ~15% of rated clock, so a timing figure measured here measures the host; recall survives, latency does not — and the amendment corrects a lifetime counter read as live state.

# Decision

* [Per-type knowledge enters through the header, not through a second splitter](decisions/the-header-dispatches-on-type-the-splitter-does-not.md) - A quarter of the corpus is prose and structured data the code-only evidence never covered, and the four-arm header is the reversible half of the fix.
* [One chunker ships, it is third-party, and the part we wrote is the header](decisions/one-chunker-and-it-is-third-party.md) - Six splitter libraries and four boundary strategies compared on August 2026 evidence, and why the measured gain is in the scope header rather than the boundary.
* [Two MCP tools, and everything an operator needs is on the CLI](decisions/two-tools-and-the-operator-surface-is-the-cli.md) - The 4-tool, 16-route, 20-command surface contracted to two actions, and the refusals that hold it there.
* [sqlite-vec, with the measurement that reverses it stated up front](decisions/sqlite-vec-survives-only-because-search-is-scoped.md) - Brute force survives only because search is scoped to one project plus its members; the scoped p95 that would overturn it.
* [Progress is a file, not a protocol notification](decisions/progress-is-a-file-not-a-protocol-notification.md) - MCP's two mechanisms for long-running work both need a live request or a second status surface; the counter is a throttled JSON file instead.
* [CI runs no GPU and no live job, and the self-hosted runner is deregistered](decisions/ci-does-not-touch-the-gpu.md) - A runner group is unavailable on a personal account and the GPU lanes contended for the one card; both reasons point the same way.
* [The thermal pause is not what indexing costs, and the profile says so](decisions/the-thermal-pause-is-not-what-indexing-costs.md) - 80.7% of self-time is the ONNX forward pass and 0.04% is cool_down, so the cooldown stays -- and the batching fix that profile seemed to imply was measured, refuted and reverted.

# Defect

* [A floating range on onnxruntime-gpu changed the CUDA major version](defects/a-floating-range-changed-the-cuda-major.md) - A resolver bump moved the linked CUDA from 12 to 13, every GPU test failed on a missing shared object, and only the fourth assertion caught the CPU fallback.

* [The chunk budget and the token window do not fit each other](defects/chunks-are-cut-before-the-model-sees-them.md) - REFUTED: 70% of chunks are cut and it costs no recall — widening the window past the p95 moved the number by nothing, and cutting the chunk to fit made it worse.

# Runbook

* [Running anything that touches the real GPU](runbooks/running-the-live-suite.md) - Both preconditions, the one-at-a-time rule, and how to read a bake-off table without reading the levels.
* [Restoring the registry, the one file that cannot be re-derived](runbooks/restoring-the-registry.md) - Where the backups are, why no re-index follows a restore, and the two traps that produced the losses.
* [Restoring Dynamic Boost, and the two files the driver package does not install](runbooks/restoring-dynamic-boost.md) - Why the GPU sat at 46% of its budget, the systemd unit and D-Bus policy Ubuntu ships nowhere useful, and the nvidia-smi field that reports failure on a working system.
