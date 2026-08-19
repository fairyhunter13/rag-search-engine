---
type: Log
title: coderag knowledge history
---

# Bundle history

## 2026-08-19

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
