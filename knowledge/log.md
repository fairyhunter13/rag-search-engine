---
type: Log
title: coderag knowledge history
---

# Bundle history

## 2026-08-20

- **Creation**: [nine nodes run on the CPU EP in both exports](constraints/nine-nodes-run-on-the-cpu-and-that-is-the-design.md). The module docstring claimed four assertions; two had zero call sites and the fourth reads `get_providers()[0]`, which ORT fills with CUDA first whatever happened per node. Measured: both exports place **9 of ~960 nodes** on the CPU EP, identical ops, **0.059%** and **0.184%** of node time. `session.disable_cpu_ep_fallback` was the obvious mechanism and is **refuted** -- set, it refuses both models at load over exactly those nine. `check_placement` ships instead, two halves because either alone is defeated: `Gather` is both shape plumbing and an embedding lookup, so the allowlist alone hides a whole lookup, and a time bound alone never names the op that moved. `check_device` deleted (guarded a device parameter that exists nowhere) and replaced by a sufficiency test; `assert_gpu_available` wired into `cli._serve`, before the socket.
- **Creation**: [a deleted file stayed searchable in a federated member](defects/a-deleted-file-stayed-searchable-in-a-member.md) — two independent causes, either sufficient. inotify keys a directory by inode, so a root and the member it reaches through a symlink collapse to one watch descriptor and events arrive under whichever path registered last; and `_tick` re-armed the whole set every 60 s, which is **5.4 s over 151 projects and ~120,000 watches**, measured — a tenth of the watcher's life blind, and inotify replays nothing. The module docstring already carried the *other* symlink trap and it is not this one. `WATCH_POLL_MS` separates surfacing from Rust (free) from re-arming (not), and the comparison is against the **unfiltered** root list: a project dropped for a broken config is still enabled, so the filtered list differs every tick and re-arms forever.
- **Creation**: [a released member kept the excludes of the root that released it](defects/a-released-member-kept-the-roots-excludes.md). Narrowing is applied on join and widening was applied on leave by nothing at all. The resubmit fix then still did nothing, because `unregister` reported only the rows it **deleted** and `registry.release` returns True only on an outright delete — so the member that survives the release, which is the one that stays searchable, was the one it never named. The live test went on timing out for 300 s with the fix in place. The unit test claims the member directly, because without that the correct behaviour is to submit nothing and the test passes broken.
- **Creation**: [served prose does not beat grep](constraints/served-prose-does-not-beat-grep.md). A negative result, recorded rather than tuned away: three escalations of the served `instructions` and the tool description all lost to `grep` on a literal-string question in a navigable repo, with the injection verified end to end. The agent layer was rebuilt around a federated fixture — a root whose only interesting content sits in a member behind a directory symlink, which `grep -r` does not follow — and the session calls the server first time. Two client facts worth the same weight: `--allowedTools` **pre-approves and does not restrict** (a session given one tool still used `Bash` and `Read`), and a `tool_result`'s `content` arrives as a **JSON string**, not the protocol's list of blocks.
- **Fix**: `build_app` nests the SDK's lifespan instead of replacing it, and runs `stateless_http=True`. Assigning over `lifespan_context` meant the session manager's task group was never entered, so every `/mcp` call answered 500 while `/healthz` stayed green — the daemon read as up from every check that was not a request. Stateless is also what makes the `2026-07-28` per-request envelope reachable; the stateful path negotiates only up to the last handshake revision.
- **Update**: the live harness. `Rpc` performs its own handshake lazily, so `-k` on a single test is a valid run rather than one that fails on a session ID some earlier test happened to establish; one transport-only retry covers the server closing an idle keep-alive connection mid-poll, and it deliberately does not retry a *response*, which is what would hide a real intermittent fault. `require_clear_gpu` counts VRAM **held by the daemon under test** as headroom: the process check already excludes it, and the first live search loads 12 GB, after which every later module read the suite's own working set as somebody else's.
- **Update**: the exclude fixture, and the lesson under it. Its vendored file was `vendor/bundle.min.js` — and `vendor/*` and `*.min.js` are both in `DEFAULT_IGNORES`, so no root's exclude was ever what dropped it: one test asserted a mechanism that could not have run and two waited 300 s each for a file the indexer is never allowed to see. **An exclude test's fixture must be a path the defaults would have indexed**, and `test_0` now asserts exactly that, because a future addition to `DEFAULT_IGNORES` would silently retire four tests otherwise.
- **Update**: the installed unit. `OnFailure` moves from `[Service]` to `[Unit]`, where systemd reads it — misplaced, it logs "Unknown key name" and carries on, so it ran for weeks as an alert that never fired and never said so. And `Environment=CODERAG_INDEX_TEMP_C=84`: the governor defaults to off, and the unit is the unattended overnight run on a card that throttles at 87 °C. It costs 0.04% of run time, already measured.
- **Update**: `tests/significance.py` — the paired reading of a bake-off, from the ranks `eval.py` now emits rather than from the four decimals. Exact-binomial McNemar over the discordant queries, a bootstrap CI that resamples *queries* so the pairing survives, and Benjamini-Hochberg because five arms against one baseline is five tests. Its own tests are hand-checked against a closed form (5 discordant pairs one way is p=0.0625) and include the two ways this reads wrong: an arm against itself must come out at exactly zero with no spread, and a rank of 11 in a stored run must not count as a hit at k=10.
- **Verification**: `pytest -m live` is **26 passed in 45 s**, including the five tests that had never been run — the three scripted-agent tests and the `2026-07-28` negotiation and conformance checks. The CPU suite is 259 passed. Four defects in this list were found by running the suite, not by reading the code, which is the argument the plan made for running it before the fleet index rather than after.

- **Refutation**: `MarkdownSplitter` loses its arm. 0.8033 against 0.8133 recall@10 over 300 doc queries on a public repo — −0.0100, CI [−0.0300, +0.0100], 3 won against 6 lost, p = 0.51. It removed every broken code fence and mid-table start in the 50-file sample and no retrieval metric could see it, so `CHUNK_MD_SPLITTER` stays off and [[one-chunker-and-it-is-third-party]] is strengthened on the corpus it was never argued over. The boundaries a reader calls wrong are not the boundaries that lose queries.
- **Correction**: [the header dispatches on type](decisions/the-header-dispatches-on-type-the-splitter-does-not.md) cited arXiv:2605.00318's 0.366 → 0.754 as "measured on CSV and Excel". It is not: the benchmark is MAUD, SEC merger agreements reshaped into row/key-value form, and CSV/Excel is the abstract's motivating domain. The delta also bundles a row tree, key-value block encoding and greedy merging with a 40–56% cut in chunk count, so none of it is attributable to prepending a key path. Second citation in this bundle to claim more than its source — the first is recorded in [[one-chunker-and-it-is-third-party]].
- **Update**: `CHUNK_HEADER` becomes `CHUNK_HEADER_PATH` and `CHUNK_HEADER_DERIVED`, both stamped. One switch removed the path and the derived line together, and **48.6% of doc queries have their heading echoed in the positive's filename** (17.6% on code), so `no-header` scored the derived line and a filename identity shortcut as one number — and the derived line is the half F0 found unevidenced. Two booleans span the 2×2 exactly; a retained master switch would be a third knob for four states. The path's contribution is not a harness defect to correct: a real query legitimately matches a filename and the path is in the chunk in production. Only the reading was wrong.
- **Creation**: [a deliberate stop was indistinguishable from a crash](defects/a-deliberate-stop-was-indistinguishable-from-a-crash.md). `uvicorn.run` never returned — MCP streamable HTTP holds its stream open and graceful shutdown waits for it — so `_shutdown_exit`, the `os._exit(0)` that exists so the CUDA EP cannot abort with 134, was dead code. Every stop took 90 s, ended in SIGKILL, and fired `OnFailure`, which is how the alert that matters gets muted. Neither existing restart test could see it: their `Daemon.stop` kills after 30 s and reports success, so a helper that recovers from the defect made every test above it blind. The new test signals the process directly, holds a live stream open while it does — without that there is nothing to wait on and the defect does not reproduce — and asserts exit code 0 rather than absence.
- **Measurement**: the idle daemon, on a quiet machine, models resident, no index running: **0.20 CPU-seconds over 30 s (0.7% of one core), 77 threads, 1,688 MB RSS**. The previous engine's spin-wait finding does **not** transfer as an idle cost — ORT's intra-op pool spins during a `Run` and then parks, so a daemon sitting on a desktop costs nothing. It is a cost of the index pass, and it is still unmeasured here. What the number does indict is 77 threads (two ORT sessions with `intra_op_num_threads` unset, on 24 cores) and 1.7 GB resident. The 15.7 GiB of VRAM is not a defect: `MODEL_IDLE_UNLOAD_S` is 900 and the daemon had been up five minutes.

## 2026-08-19

- **Refutation**: [the chunk budget and the token window do not fit each other](defects/chunks-are-cut-before-the-model-sees-them.md) is closed by its own two arms. Widening the embedding window past the p95 moved recall by **nothing** — 0.1600 / 0.3200 identical to four decimals — for double the tokens per chunk, and cutting the chunk to fit the window was **worse** at both k. The 70%-truncated measurement was true and the inference from it was wrong: the discarded tail carries no retrievable signal and the lexical lane indexes it anyway. Both constants stay, the freeze the file placed on them is lifted, and `gte-win1536` is on record as a cost with no benefit — the more useful half, because widening the window is the change someone proposes next.
- **Fix**: `is_secret_path` gains `*.env` and `*.env.*`. `fnmatch` anchors the whole name, so the single `.env.*` pattern only ever matched the *leading* form — `.env.local` yes, `prod.env` and `svc.env.enc` no. A sweep of the 148 enabled fleet roots found **456 credential-shaped tracked files** passing the filter, 352 excluding templates. Checked before calling it an incident and it was not one: **zero** carried a non-placeholder value. Closed before the fleet index rather than after, because that run is unattended across 148 repos and this filter is the only thing between a future committed credential and a store that answers natural-language queries. Re-swept after: 0. The guard names its five shapes individually — a test that reads the tuple it guards agrees with an empty tuple.
- **Update**: `*.ipynb`, `*.csv`, `*.tsv` join `DEFAULT_IGNORES`. Not a chunker problem, so not a chunker fix: a notebook is JSON holding base64 output blobs and escaped `\n` that are not newlines, so the splitter's top rung — runs of blank lines — does not exist in it and every cut lands mid-object. Neither shape yields a retrievable chunk under any splitter, and a format-aware one would only make the result tidier. Extracting a notebook's code cells is a real feature and is not this.
- **Creation**: [an eval query is removed from the corpus it is hunting](constraints/an-eval-query-must-not-be-findable-by-identity.md), plus `--corpus docs` and the harness's first tests. `eval.py` produces every retrieval number this repo has and had no test at all. The prose half was excluded from the query set by design and the cost of that went unstated: it made every prose-side change unfalsifiable, which is what let a header ship for months on a citation that said something else. Doc queries are the first heading plus its lead paragraph, stripped from the indexed copy; measured yield 18/19 here and 17/33 in ccw, against an earlier 82% estimate -- half that repo's doc files are a title followed straight by a sub-heading, and a three-word title is refused rather than padded. Two chunker arms land with it: `md-splitter` (`MarkdownSplitter` for doc-langs, off by default) and per-query ranks emitted alongside the aggregates, since 0.02 recall@10 is ~7 queries in 300 and the paired outcome is what a bootstrap CI needs.
- **Fix**: `chunk_algo` and `chunk_md_splitter` are stamped into store meta and compared by `store.incompatible`. `ProjectConfig.signature()` versions excludes and nothing versioned the header, so changing what `scope_header` emits rewrote every embedded string while every store on disk still read as current -- stale in the one direction nothing reports. Free to fix today because nothing is indexed yet; a full re-index of 148 projects later.
- **Correction**: four sites put in quotation marks that arXiv:2605.04763 calls 2,000 non-whitespace characters "a robust default". The paper says no such thing — its finding is that **chunk size has "a weaker, non-monotonic effect"**, and it names no default. The non-whitespace *unit* is cAST's (arXiv:2506.15655), cited correctly elsewhere. Two neighbouring numbers were wrong too: the function-chunking loss is 3.57–5.64 pp, not 3.96–5.64, and cAST's +4.3 Recall@5 is over *naive line-based* chunking, which is the sentence that makes our tie with it legible rather than surprising. A fabricated quotation in a public repo gets corrected whether or not the conclusion survives — and this one survives in better shape: 2,000 is a round number in a flat region, which is a weaker claim and a true one.
- **Creation**: [per-type knowledge enters through the header](decisions/the-header-dispatches-on-type-the-splitter-does-not.md). `scope_header` was three regex arms tuned to the C family, applied to a corpus that is 6.3% doc-langs, 9.4% structured data and 25% unlabeled. It now dispatches on `lang_of()`: heading chain for prose, key path for JSON/YAML/TOML, declaration for code, path alone for unlabeled. The splitter still does not dispatch — the header is one prepended string and costs a re-index to change, where a second chunker doubles the store and multiplies the eval matrix. Measured after through `discover.candidates()`, prose chunks carrying an `in:` line went 1% → 62% with **zero** headers not traceable to a real heading or key. It also subsumes the prose guard rather than sitting beside it: a header built from headings cannot be built from the declaration regex. Still unfalsifiable until `--corpus docs` exists, and that is said in the concept rather than left implied.
- **Fix + Creation**: [a new language costs nothing and cannot be filtered](constraints/a-new-language-costs-nothing-and-cannot-be-filtered.md), and `search` stops answering an unknown `lang` with silence. It narrowed the corpus to nothing and returned that as an ordinary empty result — indistinguishable from an honest answer, and the only refusal `mode` had from the start that `lang` never got. Same difflib "did you mean" as the config gate, and `tools.py` returns the string rather than raising so the hint crosses the MCP boundary. The concept beside it records why a `.zig` file already works: discovery is a denylist and `LANGS` supplies only a label, which is the half of the one-chunker decision a future tree-sitter proposal would quietly spend.
- **Update**: three gates the audit found named but not asserted. The chunker's boundary ladder is now tested at its top rung — and the obvious version of that test does not discriminate: with a two-paragraph budget, cutting on line breaks lands on a paragraph boundary anyway, so the fixture gives the splitter a spare line and demands it leave it on the table. The non-ASCII offset gate now walks to the character offset each chunk's `start_line` implies and requires the chunk to be there; concatenation, which was the whole check before, passes with every offset uniformly wrong. And one test runs at the real `CHUNK_CHARS=2000` rather than the small `size=` every other chunker test passes. The federation collapse moves out of a docstring: 135 repos behind 202 links, reproduced in a tmp tree rather than asserted against a registry that names client paths.
- **Update**: a restart test, and the switch it exposed. The one Phase 5 assertion never built — restart mid-build, queue drains, index converges without a full rebuild — now exists under its own `-m restart` marker, because `live.py` refuses to start or stop the daemon on purpose and that rule is worth more than the convenience of reusing it. It runs a `coderag serve` of its own on a private `CODERAG_STATE_DIR` and a free port rather than the installed unit: a restart of the real daemon sweeps every enabled project into the queue, so a fixture project would wait behind ~148 of them, and a test that writes to the real registry is how that file was lost twice. The observable that separates *resumed* from *rebuilt and converged to the same rows* is `progress.begin`, which is handed the work the pass found to do — the remainder after a resume, the lot after a wipe. Writing it surfaced that `systemctl --user start coderag` **is** the fleet index, so the live suite could not have a daemon up without starting an overnight run on the same card; `CODERAG_RECONCILE_ON_START=0` separates the two intentions and the 60 s tick still collects anything submitted.
- **Creation**: [CI runs no GPU and no live job](decisions/ci-does-not-touch-the-gpu.md). The workflow was inherited from the pre-rebuild tree and had been red on every push since 2026-08-18 — it linted `src/rag_search src/tests scripts`, none of which exist. Rewritten against the real tree. The self-hosted lanes are deleted rather than ported: their fork-PR path was already closed by construction, but a runner group is unavailable on a personal account, and `live-fast` ran on every push against the one card the fleet index is serialised on. Actions pinned to 40-char SHAs, `permissions: contents: read` stated, and the `-Werror` bundle step no longer `continue-on-error` — a gate whose failure is swallowed reports the same green as a passing one.

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

## 2026-08-20

- **Added** `decisions/the-header-is-the-path-and-nothing-else.md`. The code 2x2 completed all four
  cells: the path arm is worth **-0.1233 recall@1 / -0.1700 recall@10**, and the derived line is an
  exact 16/16 discordant tie with path on (p = 1.000) and *negative* with path off, where
  `derived-only` scores 0.1267 against `no-header`'s 0.1467. Deleted `CHUNK_HEADER_DERIVED`, its
  stamp field, four helpers, five regexes and the per-type dispatch; `chunk.py` 248 -> 137 lines,
  `CHUNK_ALGO` 2 -> 3.
- **Refuted** `decisions/the-header-dispatches-on-type-the-splitter-does-not.md`, by the
  falsification condition it wrote for itself: *"If they lose or tie, the path-only prose header and
  the one-splitter decision come back strengthened."* They tied.
- **Refuted the explanation, not just the arm.** A redundancy census over 876 code chunks put the
  `imports:` line at **15.3%** overlap with the body it labels -- 85% novel tokens -- so the
  embedder was handed new text and did not use it, and used it *against* itself when the path was
  absent. Dilution, not redundancy. That generalises where redundancy would only have indicted these
  particular regexes and invited a fourth attempt.
- **Recorded and not closed**: the path arm's magnitude on docs is soft. 48.6% of docs eval queries
  have their heading echoed in the positive's filename, and BLAgent (arXiv:2605.17965), the external
  corroboration at +17.0 pp, presents query-document string identity as its mechanism approvingly.
  The code corpus has no such echo and pays -0.1233 anyway, so the decision stands on code. Re-scoring
  the docs path arm on the low-slug-overlap stratum is zero-GPU and still owed.


## 2026-08-20

- **Wired** `decisions/the-embedder-is-settled-by-a-tie-break.md`, a day after it went `stable`:
  `EMBED_MODEL` to `Alibaba-NLP/gte-modernbert-base`, `EMBED_POOLING` to `cls`, `EMBED_ONNX_FILE` to
  `onnx/model_fp16.onnx`, both prefixes to `""`. fp16 was the tie-break's first criterion, so it was
  verified rather than assumed — the whole `-m gpu` lane (30 tests) passes on the fp16 export.
- **Replaced** `test_the_two_prefixes_produce_different_vectors`, false by construction once the
  prefixes are blank, with one that asserts both halves: the sides agree under blank prefixes and
  stop agreeing under a prefix. Deleting it would have deleted the assertion that `side` is
  load-bearing.

## 2026-08-20

- **Creation**: [a cancelled task group cannot reach a shielded thread](defects/a-cancelled-task-group-cannot-reach-a-shielded-thread.md).
  The stop hung again after the previous fix. `timeout_graceful_shutdown` bounds the connection wait
  and nothing else; the 90 s was the lifespan shutdown waiting on a plain-`def` tool running under
  `anyio.to_thread.run_sync`'s shield, which the session manager's cancel cannot reach — and the tool
  then restarted the threads the finally had just stopped. Fixed with `SHUTDOWN_DEADLINE_S = 15` and
  a `threading.Timer` armed as the first statement after `STOPPING=1`, in the inner context, strictly
  before the session manager is entered.
- **Corrected** [a deliberate stop was indistinguishable from a crash](defects/a-deliberate-stop-was-indistinguishable-from-a-crash.md),
  by running its own test: it fails, `-15 != 0`, and has presumably never passed. uvicorn's
  `capture_signals` restores the handler it replaced and re-raises the caught signal before
  `uvicorn.run` returns, so `_shutdown_exit` was dead code and the CUDA-134 protection the module
  docstring calls a lifecycle fact was not in force. Not a production failure — systemd's
  `is_clean_exit` treats a daemon killed by SIGTERM as clean — but every `returncode == 0` assertion
  in the restart suite was unreachable. One line: install our own SIGTERM handler before
  `uvicorn.run`, so ours is the one uvicorn restores into.
- **Measured** the unbounded stop at **5.7 s** with a cold-load `search` in flight, with the daemon's
  own log showing both ONNX sessions being built after `"StreamableHTTP session manager shutting
  down"`. The first version of the new test asserted 25 s and passed with the deadline neutralised —
  decoration. It now runs its daemon at a 2 s deadline and asserts under 4.5 s, between the two
  numbers, because at the shipped 15 s neither is reachable.

- **Creation**: [the search unit is the caller's own workspace](constraints/the-search-unit-is-the-callers-own-workspace.md)
  and [an unflagged project stayed fully searchable](defects/an-unflagged-project-stayed-fully-searchable.md).
  The scoping downstream of `root` was already right — one level of federation, no fleet-wide mode —
  and the hole was that `root` is a string the model writes. The boundary now comes from the client's
  own `roots` through the SDK's `ListRoots` resolver, which the framework fills and the schema never
  shows. `search`'s own predicate went from "the row exists" to registered **and** enabled **and**
  indexed, which is two live bugs in one line.
- **Verified over the wire**, against a real daemon at the fail-closed default: round one answers
  `input_required` with `{"coderag.scope:_ask": {"method": "roots/list"}}` and an opaque
  `requestState`; the retry carrying the pin is served; the retry carrying a *different* pin is
  refused. `live.Rpc.modern_tool` is that round-trip, and `tests/test_live_protocol.py` asserts both
  arms plus the discriminator — an unpinned call has to fail for a different reason, or "the pin
  bounded the search" is unearned.
- **Recorded** SEP-2577: `2026-07-28` deprecated `roots` in the same revision that made it reachable
  from a stateless server. Twelve-month floor, removal eligible no earlier than 2027-07-28, and the
  successors the spec names are the two things this rejected — a tool parameter and server
  configuration. Not a reason to change course now; a reason to have written the date down.
- **Shipped the flag open in the unit and closed in the code.** Claude Code negotiates the
  `2026-07-28` era for `http` behind a default-off flag, so an unpinned client would be refused
  outright. `scope.enforce` logs the pinned root count on every call; journald decides the flip, and
  the log line goes when the unit line does.
- **Not changed**: the watcher still watches `enabled_projects()`, a superset of the indexed set.
  inotify has no replay, so tightening it for symmetry with the search gate would lose every write
  during a project's first pass.
- **Creation**: [a delete is lost while a project is still settling](defects/a-delete-is-lost-while-a-project-is-still-settling.md), `status: open`. The one red test in the live lane, and not caused by the scoping change beside it. Same tree, same shape, same fleet: a delete a minute after registration survives 300 s, and one after a 90 s quiet period leaves the store in 10 s. Delivery, `_dispatch` and the scoped delete branch are each now held by a test rather than by an argument. Two probe designs inverted the answer before that — polling `index` submits a full pass and repairs what it measures, and `/healthz`'s `completed` counter is fleet-wide, so a `+1` never attributes to the tree under test.
- **Update**: two tests for paths that had none. `test_a_delete_through_a_symlink_still_reaches_the_queue` is the twin of the write test that already existed — the delete direction is the one that fails silently, and its file is created before the watcher starts so only the removal can satisfy it. `test_a_scoped_pass_removes_the_file_the_watcher_named` covers `index_project(project, paths)`, the call shape the watcher actually makes, where the content-hash diff never runs; the pre-existing delete test only ever drove the full walk.
- **Correction**: `test_a_write_reached_only_through_a_symlink_is_noticed` can pass without the watcher firing. The fixture waits on the **root's** chunk count and writes into the **member**, whose own first walk has not run and sweeps the file in. Written the awkward way and still not awkward enough; the member's store has to go quiet first.
