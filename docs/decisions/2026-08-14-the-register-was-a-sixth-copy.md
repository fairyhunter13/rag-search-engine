# The invariant register was a sixth copy, and its checker read fewer files than it printed

**2026-08-14** · deleted `docs/world-model/`, `scripts/check_world_model.py`,
`scripts/gen_world_model_skills.py`, both generated skills, `docs/info-hierarchy.md`,
`docs/reference/world-model.md`, `docs/CONFORMANCE_EVALUATION.md` — 1,148 lines across ten files

The world model was meant to be the single source of truth for the repo's laws. Measured against
the tree, it was the sixth statement of them. `P0` ("GPU-only inference") was defined in
`model.yaml`, in `docs/world-model/README.md`, in the generated `.claude/skills/world-model/SKILL.md`,
in `docs/CONFORMANCE_EVALUATION.md`, in `docs/audits/2026-07-09-root-federation-audit.md`, and in
§1a of the architecture document. The copies had drifted: two audits said `HR1-HR40` against a
register that ran to HR41, and the L2 component map still pointed at a `kb/` module deleted in July.

## What the checker actually checked

`check_world_model.py --all` enumerated `ROOT.rglob("*.py")`. P18's `check:` declared
`paths: "src/rag_search/ docs/ scripts/"`, so the `docs/` half of its coverage never opened a
single `.md` file — and printed `[CONFORMS] P18` for the files it did not read. That is the
failure mode the repo's own FE11 lesson names: a guard that scans nothing reports the same green
as a clean tree.

Two further facts settled it rather than motivating a repair. **CI never ran it** — zero mentions
in `ci.yml`, a fact `2026-07-31-context-budget.md` had already recorded. And its `_strip_prose()`
helper existed solely to stop the checker matching sentences that *describe* the invariants: 39
lines of AST walking to keep a grep from finding its own documentation.

Each of the four rules that fails silently rather than loudly already has a live guard that owes
the register nothing — `core/gpu.py` plus `test_gpu_autodetect.py`, `test_no_code_semantic_regex.py`,
`test_no_mocks_or_fakes.py`, `test_public_hygiene.py`. All four were run green before the deletion,
precisely so the claim "nothing enforcing survives in the deleted set" was measured rather than
assumed.

## `docs/info-hierarchy.md`: a spend doctrine with nothing left to allocate

The file taught "spend LLM tokens only to climb Information→Knowledge→Wisdom". Since the tier-3
deletion of 2026-07-28 every rung of its own ladder reads `LLM cost: $0`. A ladder with uniform
cost does not rank anything.

Read section by section, one of six was unique. The extraction ban and the publishability rule are
stated in `CLAUDE.md` and enforced by tests. The compute-spend doctrine was a *third* copy —
HR35–HR40 appear 21× in `federation-ops-and-invariants.md` and 14× in
`2026-07-01-idle-cpu-root-causes.md`, which cited back to `info-hierarchy.md`, closing a citation
loop between three statements of one rule. The WS-B section was dated history carrying its own
amendment saying its subject was gone. The "How to use" section named `ask`, `check_world_model.py`
and `gen_world_model_skills.py`: one retired, two deleted here.

"Keep it up to date instead" was the option already tried: the file was rewritten wholesale on
2026-07-28, amended again since, and still misdescribed three subsystems on the day it was deleted.
The cost was never the 118 lines — it was that a reader who trusted them was misinformed.

## One reversal, recorded rather than silent

`docs/reference/world-model.md` carries an explicit **"Kept, not deleted, on 2026-07-28"** ruling.
That ruling turned on RSE's own world model being "tier-2 governance machinery that survives
whole", which stopped being true here. A repo-agnostic essay on what a world model is, in a repo
that has none, documents nothing this tree contains.

## What was kept, and made stronger

`test_l3_rtm_all_tests_resolve` read the L3 register and asserted every `test:` name resolved to a
live `def test_…`. That mechanism was the one part earning its keep, so it was **retargeted, not
deleted**: `test_coverage_map_names_resolve` now reads §14 of `federation-ops-and-invariants.md`
and holds it to the same standard, with the table's own strikethrough notation as the single
escape hatch.

The ops document had asked for exactly this at §14: a probe once found **70 names in that table
that no longer resolve**, it noted that the L3 register had a gate while the table did not, and it
called building one "the standing follow-up". Retargeting closed it. Five names in the table were
found unresolved by the new guard on its first run — all five prose mentions of tests that had
genuinely left — and were struck through, which is what the table's own convention already
required. The register is gone and the number of machine-checked claims went up.
