---
type: Log
title: coderag knowledge history
---

# Bundle history

## 2026-08-19

- **Update**: the bundle audited against the shipped engine rather than against the plan's table.
  All 21 paths named in a `resource:` key resolve, so nothing here describes deleted code — which
  was the failure mode of the v1 bundle and is the check `tests/test_okf_bundle.py` now holds. Nine
  concepts against a target of thirteen, and the four remaining gaps were closed as refusals, each
  after reading the file that would be restated: **a federation is discovery, never a merged index**
  (`federation.py` carries the resolved-path rule and the 202→135 dedup, `watch.py` carries the
  inotify-symlink trap, the joining-a-root Constraint carries the excludes union, and the sqlite-vec
  Decision carries why there is no merged index — four homes, none of them missing); **the two-tool
  MCP surface as an Interface** (the contraction and its refusals are a Decision already written,
  and the signatures are `tools.py`, so the file would be a schema copy that goes stale silently);
  **the registry lost update as a Defect** (`runbooks/restoring-the-registry.md` already carries the
  34-of-180 under its traps, and the runbook is where the reader who needs it arrives). The target
  of thirteen was an estimate made before the code was written; the count that matters is how many
  survive the gates, and four fewer is the gates working rather than the bundle being unfinished.
- **Deprecation**: `docs/` — all 70 files, the previous engine's architecture records and dated decisions. It was kept through the v1 bundle delete as the source the load-bearing numbers were transcribed from; every one of those numbers now sits in a concept here or in a module docstring, so the directory was a second prose plane describing code deleted in `365a235`. `knowledge/` is the only one. The content is in git history at `365a235^`, and a copy was zipped out of the tree rather than committed: a tracked binary duplicates what git already holds and is opaque to the `NAME_BAN` scan, which reads tracked *text*.
- **Creation**: [a model carries three things, and pooling is the one that fails invisibly](constraints/a-model-carries-three-things-and-pooling-is-the-invisible-one.md). Found while answering "why not the same Alibaba-NLP family for both stages" — `gte-modernbert-base` and both `bge` arms are CLS-pooled and `embed.py` hardcoded a masked mean, so three arms of the running bake-off were void. Written as a Constraint rather than an `embed.py` comment because its consequence is a procedure for adding an arm, and the person adding one is reading `tests/eval.py`.
- **Update**: `tests/eval.py` gains `jina-code`. The plan's claim that no code-specific embedder clears both the licence and official-ONNX filters was wrong: `jina-embeddings-v2-base-code` is apache-2.0 with an fp16 ONNX sibling, 768 dims and mean-pooled, so it is a drop-in arm. Missed by anchoring on the newer CC-BY-NC `jina-code-embeddings-0.5b`.
- **Creation**: [this host cannot produce an admissible latency number](constraints/this-host-cannot-produce-an-admissible-latency-number.md), written from a measurement taken while the bake-off ran: `GPU T.Limit` at −3, SM clock ~460 MHz of a rated 3090, 60 W drawn of 175 available, chassis 97 °C. Written as a concept rather than a `gpu.py` comment because it invalidates numbers in three other places — the sqlite-vec kill criterion, the live suite's recorded p50/p95, and every per-arm wall clock — and a comment beside `cool_down()` reaches none of those readers. The question that produced it was "should we use lighter models", and the honest answer is that the throttle costs ~6× against a shortlist spread of ~1 pp, so the model was the smaller term.
- **Update**: [the sqlite-vec decision](decisions/sqlite-vec-survives-only-because-search-is-scoped.md) gains the distinction between a criterion that is met and one that cannot currently be measured. Its whole argument is that the reversal condition is a number, and a number this host cannot produce would buy an ANN index to fix a fan.

- **Deprecation**: the v1 bundle — all 31 concepts — was **deleted** rather than deprecated in
  place. The `okf-knowledge-bundle` rule is augment-never-shrink, and this is the exception it does
  not cover: that rule governs a bundle whose subject still exists. Here the subject was deleted in
  `365a235`. Of the 31 files, the 7 Components, 2 Interfaces and 5 Defects described `graph/`,
  `sweeps.py`, `routes_chat.py`, `bounded_parse.py` and 16 HTTP routes that no longer exist, and
  every concept linked into `docs/architecture/federation-ops-and-invariants.md` and
  `docs/decisions/`, whose `HR#` rows were held by a test deleted with the suite. What survived
  would have been majority tombstone with entirely broken links. Git holds the originals; the
  measured numbers were transcribed forward into the concepts below before the delete.
- **Creation**: the bundle for the rebuilt engine. Written against shipped code, not intended work.
  Seven concepts, not the thirteen the plan targeted. The gap is the not-already-covered gate doing
  its job: this engine's module docstrings were written to carry their own why, so a concept
  restating one is the drift a bundle exists to prevent. Refused for that reason, each after
  reading the file: **GPU-only inference** (`gpu.py`'s docstring already names what each of the four
  assertions closes; the incident that proves the fourth is a Defect and is written), **the VRAM
  lifecycle** (`server.py` holds the 12.2 GB measurement, `os._exit` and the idle timer at their
  call sites), **indexing is always background** (`index.py`'s first paragraph), **the store's
  rowid contract** (`store.py`'s docstring, including the cascade that does not reach the virtual
  tables), **the publishable tree** (`tests/test_public_hygiene.py`, including why an unset ban
  fails), and **the derived query set** (`tests/eval.py`). What survived is what no single file can
  hold: evidence with rejected alternatives, a lifecycle spanning four modules, an incident, and
  two procedures.
- **Creation**: `tests/test_okf_bundle.py` — the bundle's gate. This repo has no CI and no git
  hooks, so pytest is the only gate there is, and a missing `okf` binary **fails** rather than
  skips. It also holds the two checks `okf check` treats as warnings and this bundle treats as
  errors: a link to a file that does not exist, and a `resource:` naming a path that does not — the
  second being exactly how the deleted bundle above ended up describing code that was gone.
- **Amendment** to `this-host-cannot-produce-an-admissible-latency-number`: the claim held, the
  cause did not. The card was never thermally saturated — it was pinned to 46% of its power budget,
  and the "SW Thermal Slowdown: Active, 330,867 s" that anchored the original diagnosis was
  `nvidia-smi`'s *lifetime counter* in microseconds read as instantaneous state. Amended rather than
  rewritten: the misreading is the reusable part, because an error carrying a unit and six
  significant figures does not read like a guess.
- **Creation**: `runbooks/restoring-dynamic-boost.md`. Written because the fix is two files Ubuntu's
  driver package installs nowhere systemd or D-Bus will look, and because the obvious verification
  probe (`nvidia-smi --query-gpu=power.limit`) reports `[N/A]` on a working system — a runbook that
  only listed the commands would have been re-derived and then disbelieved.
- **Refused**: a concept for the incremental persistence added to `tests/eval.py`. The reason lives
  at the call site in three words and the harness is not load-bearing; the durable lesson is already
  in `running-the-live-suite`, which says to read the deltas rather than the levels.
- **Second amendment** to the same constraint, hours after the first: lifting the power cap moved
  the binding constraint rather than removing it. At 130 W the card draws 83–90 W and hits 87 °C,
  `SW Power Cap` goes Not Active and `SW Thermal Slowdown` goes Active. Both readings are correct at
  their own power limit, which is the durable lesson — **a throttle-reason diagnosis is only valid
  at the cap it was taken under.** The first amendment was right about that window and wrong to
  imply heat was never a constraint here.
- **Creation**: `tests/test_live_agent.py` — layer 3, the layer the pyramid names and this repo did
  not have. It asserts on the transcript, never on the answer: a `tool_use` block naming the server,
  a `root` in its arguments, and locations from the `tool_result` that resolve on disk. Asserting
  that a correct answer arrived would pass with the server switched off, because the session would
  open the file itself and be right.
- **Refused**: a concept for layer 3 and for the conformance suite. Both rationales are at their
  call sites and neither has an alternative that was rejected on evidence — the four gates stop at
  the first. The MCP revision the wire must negotiate (`2026-07-28`) is now asserted rather than
  written down, which is the form that cannot go stale silently.
- **Correction** to `runbooks/restoring-dynamic-boost.md`, same day as its creation. The policy I
  shipped in it was one I wrote defensively — a DOCTYPE, explicit `deny` under the default context,
  extra allows under root — not the file the driver actually ships. Upstream's 595 policy is a
  216-byte form that *allows* `send_destination` in the default context, and the difference matters
  in both directions: a pre-2022 copy is [CVE-2022-31608](https://forums.developer.nvidia.com/t/nvidia-dbus-conf-lead-to-high-security-concerns/215303),
  and a malformed one makes dbus-broker refuse to start and takes logind and NetworkManager with it
  ([nixpkgs #545966](https://github.com/NixOS/nixpkgs/issues/545966)) — an unbootable machine from a
  file written to make a GPU faster. The runbook now validates the XML and proves the bus survived
  *before* the reboot that would otherwise discover it.
  Three smaller things the first draft had wrong or missing: the scriptable probe is
  `enforced.power.limit`, and `power.limit` reads `[N/A]` because it is the *Requested* limit and no
  software-settable limit exists on a mobile board — normal before and after the fix, not a
  Dynamic Boost symptom; the unit needs `SuccessExitStatus=1`, which the noble doc copy omits; and
  the honest expectation is **+5 to +25 W**, not the 80 → 175 W the numbers invite, because the rest
  of that gap is platform power mode and this machine has no `platform_profile` to reach it.
- **Added** `decisions/progress-is-a-file-not-a-protocol-notification.md` and
  `decisions/the-thermal-pause-is-not-what-indexing-costs.md`. The first records two refusals with
  named alternatives: `notifications/progress` needs a `progressToken` on a live request and `index`
  returns immediately by design, and the Tasks extension is poll-based — which is the shape `index`
  already has, so adopting it adds a second status surface for a semantic already exposed. The
  second is a profile that contradicted the question that prompted it: the thermal cooldown is one
  sample in 2,359 and the forward pass is 80.7%, so *keeping* the governor is the decision, and the
  batch-size finding (7.63 chunks per call against a ceiling of 128) is recorded where the next
  person looking to speed up indexing will meet it.
- **Refused**: a concept for `progress.snapshot`'s ETA algebra. The reasoning is three lines at the
  call site and there is no rejected alternative — the "clever" form reduced to the naive one.
- **Corrected** `decisions/the-thermal-pause-is-not-what-indexing-costs.md` the same day it was
  written. Its profile stood, but the cause it inferred did not: batching the embed call across 64
  files instead of one per file measured **2–3% slower** on an ABBA run with a warm model
  (32.7/33.0 s against 32.0/32.0 s), and was reverted. Vectors were equivalent — min cosine
  0.9999997 — so the result is throughput, not correctness. A profile localises time; it does not
  name a cause, and eight minutes of A/B beat a plausible reading of a flame graph.
- **Added** `defects/chunks-are-cut-before-the-model-sees-them.md`, which is what the refuted
  experiment found instead. Padding waste at batch 128 is 0.0% because every chunk arrives at
  exactly `EMBED_MAX_TOKENS`; untruncated, they run p50 904 / p95 1373, so 70% of chunks are cut and
  27% of tokens never reach the dense lane. Two arms (`gte-win1536`, `gte-chunk1000`) are in flight
  to decide whether it costs recall, and both constants are frozen until they land.
