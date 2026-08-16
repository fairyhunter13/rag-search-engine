# OKF lands here as a signpost, not a corpus

**2026-08-17** · added `knowledge/index.md` and `knowledge/log.md`; no concept files

OKF v0.2 became the default knowledge backbone across this machine's repos: a global doctrine
line, a globally symlinked `okf-knowledge-bundle` skill, and a SessionStart hook that prints
`knowledge/index.md` to every session that has one. The question for this repo was not whether to
adopt the format — it was what a seeded bundle would contain.

## Measured against the tree, it would contain copies

Every candidate concept already has a home, and the home is gated:

| Candidate | Where it already lives | What holds it there |
|---|---|---|
| GPU-only, no-mocks, no-regex, public-hygiene | `CLAUDE.md` + §13b | `test_gpu_autodetect.py`, `test_no_mocks_or_fakes.py`, `test_no_code_semantic_regex.py`, `test_public_hygiene.py` |
| Every invariant | §13b, one row per `HR#` | `test_hr_ids_resolve_in_the_definition_table`, `test_every_defined_hr_id_is_mapped` |
| Each invariant's proof | §14 map | `test_coverage_map_names_resolve`, `test_coverage_map_files_resolve` |
| Why a rule exists | `docs/decisions/`, 51 dated records | `docs/decisions/README.md`'s split |
| Operational hazards | `CLAUDE.md`, each linked to its record | `test_public_hygiene.py`'s 8 KB cap |

That is the exact shape [the register was a sixth copy](2026-08-14-the-register-was-a-sixth-copy.md)
deleted three days ago, and its two named failures are the two a bundle here would repeat: a
statement of rules that are stated and enforced elsewhere, and a checker whose green covers files
it never opened. `okf check` avoids the second — it opens every file it reports on and reports
nothing else — but only because it would have almost nothing to open.

## What was written instead

`knowledge/index.md` maps the OKF type vocabulary onto the three homes this repo already has, so
an agent arriving with the global doctrine finds the corpus rather than minting a parallel one.
Roughly fifteen lines against a whole second corpus, and it is the cheapest way to answer the one
question the doctrine now makes every session ask.

The bundle is a real bundle, not a stub: `okf check knowledge` passes, and a concept that fits
none of the index's rows lands here with its reason in `knowledge/log.md`. The prediction is that
few will, and that is the outcome to expect rather than the one to fix.

## Reversal condition

Drop `knowledge/` if it stays empty long enough that the index goes stale — an index nobody
maintains is a fourth home pretending to be a map. Grow it into a real bundle if the three homes
above ever stop being gated, because then the format's `okf check` is doing work no test is.
