---
okf_version: "0.2"
---

# Constraint

* [Joining a root is a configuration change to an existing project, and it has to reach the index](constraints/joining-a-root-is-a-config-change-to-an-existing-project.md) - One registry row per resolved path records why it is there, and the config signature turns the next index pass into a reconcile rather than a no-op.
* [A model carries three things, and pooling is the one that fails invisibly](constraints/a-model-carries-three-things-and-pooling-is-the-invisible-one.md) - Prefixes and context limits fail loudly; a mispooled model returns a plausible unit vector and reads as the model losing.
* [This host cannot produce an admissible latency number while it throttles](constraints/this-host-cannot-produce-an-admissible-latency-number.md) - The card runs three degrees past its own throttle point at ~15% of rated clock, so a timing figure measured here is a cooling measurement; recall survives, latency does not.

# Decision

* [One chunker ships, it is third-party, and the part we wrote is the header](decisions/one-chunker-and-it-is-third-party.md) - Six splitter libraries and four boundary strategies compared on August 2026 evidence, and why the measured gain is in the scope header rather than the boundary.
* [Two MCP tools, and everything an operator needs is on the CLI](decisions/two-tools-and-the-operator-surface-is-the-cli.md) - The 4-tool, 16-route, 20-command surface contracted to two actions, and the refusals that hold it there.
* [sqlite-vec, with the measurement that reverses it stated up front](decisions/sqlite-vec-survives-only-because-search-is-scoped.md) - Brute force survives only because search is scoped to one project plus its members; the scoped p95 that would overturn it.

# Defect

* [A floating range on onnxruntime-gpu changed the CUDA major version](defects/a-floating-range-changed-the-cuda-major.md) - A resolver bump moved the linked CUDA from 12 to 13, every GPU test failed on a missing shared object, and only the fourth assertion caught the CPU fallback.

# Runbook

* [Running anything that touches the real GPU](runbooks/running-the-live-suite.md) - Both preconditions, the one-at-a-time rule, and how to read a bake-off table without reading the levels.
* [Restoring the registry, the one file that cannot be re-derived](runbooks/restoring-the-registry.md) - Where the backups are, why no re-index follows a restore, and the two traps that produced the losses.
