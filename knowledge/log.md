---
type: Log
title: coderag knowledge history
---

# Bundle history

## 2026-08-19

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
