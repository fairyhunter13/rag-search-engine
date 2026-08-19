# The invariant register was a sixth copy, and its checker read fewer files than it printed

**2026-08-14** · deleted `docs/world-model/`, `scripts/check_world_model.py`,
`scripts/gen_world_model_skills.py`, both generated skills, `docs/info-hierarchy.md`,
`docs/reference/world-model.md`, `docs/CONFORMANCE_EVALUATION.md` — 1,148 lines across ten files

The world model was meant to be the single source of truth for the repo's laws. Measured against
the tree, it was the sixth statement of them. `P0` ("GPU-only inference") was defined in
`model.yaml`, in `docs/world-model/README.md`, in the generated `.claude/skills/world-model/SKILL.md`,
in `docs/CONFORMANCE_EVALUATION.md`, in `docs/audits/2026-07-09-root-federation-audit.md` (deleted
2026-08-18; git history), and in
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

## Two reversals, recorded rather than silent

`docs/reference/world-model.md` carries an explicit **"Kept, not deleted, on 2026-07-28"** ruling.
That ruling turned on RSE's own world model being "tier-2 governance machinery that survives
whole", which stopped being true here. A repo-agnostic essay on what a world model is, in a repo
that has none, documents nothing this tree contains.

The second reversal is the larger one, and it went unrecorded until 2026-08-15. The plan's stage B
called for replacing §13b's `HR#` id column with each rule's name, on the reasoning that "a row
that names its guard needs no number". **§13b names no guards.** It is two columns wide — id and
requirement — and the guards are named in §14, a different table whose own first column is the id.
Executing the instruction would have keyed §14 to nothing. The same plan also exempted the 51 dated
records in `docs/decisions/` and `docs/audits/` (the latter deleted 2026-08-18) *because* they cite
these ids as they stood, which
is an exemption that only holds while the ids still resolve somewhere; stage B would have removed
the last place they do. So §13b keeps its ids and becomes the one definition table — stated as
policy in `README.md` here, and gated since 2026-08-15 by
`test_hr_ids_resolve_in_the_definition_table`, which fails on any id cited in the tree that no row
defines. A retired requirement's row is struck through and still counts as defined: the records
citing it are kept as written, so its id must go on resolving.

Two consequences of that reversal, both deliberate, neither obvious from the tree alone:

- **The plan's `git grep -InE '\b(P[0-9]+|HR[0-9]+)\b' -- src docs/architecture README.md CLAUDE.md`
  → 0 criterion is retired, not failed.** Its `P#` half passed and holds: zero `P` ids remain in
  those paths. Its `HR#` half now reads 235 and never can read 0, because the definition table and
  the coverage map are required to be keyed by id. The guard above replaces it, and asks the
  stronger question — not *are the ids absent* but *do the ids resolve*.
- **§13b gets no name column; the ten rows that lacked a name got one inline.** Stage B1's
  readability intent survives its reversal, and the obvious way to honour it — a third column
  holding each rule's name — was measured against the table and declined. **30 of the 41 rows
  already open with their name in bold** (`**Tree-sitter only.**`, `**GPU execution provider
  auto-detect**`, `**Two-tier CPU budget…**`); a column would restate that text three words to its
  left, on rows running to 3,111 characters, and would add 41 hand-written strings that no test can
  hold to the requirement beside them. What was actually missing was the convention, not a column:
  HR1–HR10 predate it and opened as plain prose. Each now bolds the naming clause it already had,
  so the id column keeps its one job and the name is where the other 30 rows put it. HR27 was
  struck through in the same pass — it was the only one of the ten retired rows announcing its
  retirement in prose without the strikethrough the other nine use, and strikethrough is what the
  guards read.
- **Gating cited-but-undefined exposed its mirror, defined-but-unmapped.** §14 opens by claiming
  "Each §13 invariant has a corresponding live test", and on 2026-08-15 that was false for HR31,
  HR34 and HR41 — the three rows this overhaul restated into §13b, which carried their guards
  inline in the requirement prose and never reached the map. The whole public-hygiene family and
  the VRAM-release proof were absent from it while being fully proven in `src/tests/`. Coverage was
  never the gap; the map was. Rows added, and `test_every_defined_hr_id_is_mapped` now holds §14 to
  its own first sentence. Struck §13b rows are exempt — a retired requirement needs no live proof,
  which is the same strikethrough contract read the way each table reads it.
- **§14 cites files as well as test names, and only the names were read.** The retargeted guard
  below resolves `def test_…` citations; nothing read the File column, so renaming a module while
  keeping its defs — the ordinary shape of a split or move — left the map naming a path that is
  gone with every guard green. Measured before the gate: 60 unstruck refs, 59 resolving. The one
  that did not was HR27's, the same lone retired row announcing its retirement in prose; struck to
  match the other eleven, so `test_coverage_map_files_resolve` needs no allowlist. The File column
  is read alone — prose in the other two cells legitimately names modules that are gone, and
  "`test_bpre.py` is deleted" is a true sentence.
- **The 31 bare `(HR38)`-style tags left in `src/` were kept on purpose.** The instruction to
  strip them assumed the ids were about to become meaningless. They did not, so each tag is now a
  one-hop reference to a definition that exists and is machine-checked.

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
