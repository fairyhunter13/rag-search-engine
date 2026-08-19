# The bundle becomes the home, and the gated tables stay put

**2026-08-18** · reverses [OKF lands here as a signpost](2026-08-17-okf-lands-as-a-signpost.md)
after one day · `knowledge/` goes from 2 files to a populated bundle

## The stated reversal condition did not fire

Yesterday's record named one: *grow the bundle if the three homes stop being gated*. They have
not. §13b, §14 and `docs/decisions/README.md` are all still held by
`test_coverage_map_traceability.py`, and nothing about that changed overnight.

This reversal is a directed one — the repo owner asked for the bundle to become the single place
new knowledge is written. Recording that plainly matters more than dressing it as a triggered
condition, because the next reader needs to know which of the two it was before deciding whether
to reverse again.

## What actually moved, and what deliberately did not

The signpost's argument was that a bundle here would hold copies. That argument was right about
§13b and it is still right, so **§13b and §14 stay exactly where they are**. They are the
machine-readable contract: `test_hr_ids_resolve_in_the_definition_table` reads the id column,
`test_every_defined_hr_id_is_mapped` reads both tables against each other, and
`test_coverage_map_names_resolve` walks the guard names. Moving any of that into `knowledge/`
trades a test for `okf check`, which validates structure and link resolution and has no way to
know an invariant lost its proof.

What the bundle adds is the view those tables cannot give: **§13b is 29 live rows in id order,
and the system has about eleven subjects.** HR32, HR35, HR37, HR38 and HR40 are five rows and one
story — four cooperative gates and the kernel ceiling that made them unnecessary to trust. HR6,
HR26 and HR41 are one GPU lane read three times. A reader arriving at §13b in id order meets that
story in five disconnected pieces, and there is no row that says they are one.

So each concept holds a subject, names the `HR#` ids it covers, and carries `sources:` back to the
guard that proves each. Nothing loses a test; §13b keeps every row.

## The cost, stated

Tracked markdown grows, and `_TRACKED_MD_MAX_LINES` is raised in this commit to admit it. That
ceiling exists to stop exactly this kind of growth, so raising it is the deliberate act the guard's
own message asks for rather than an incidental fix. The bet is that a grouped, navigable second
view earns its lines where a per-row restatement would not — which is why there are eleven
constraint concepts and not twenty-nine.

`docs/audits/` is deleted in the same commit. Its three files were 917 lines, over half of them
about subsystems the 2026-07-28 tier-3 purge removed, and 51 of the ~104 paths they cite no longer
exist. What survived is in `knowledge/defects/`; the five records that cited the directory are
re-pointed at those concepts rather than left dangling.

## Reversal condition

Delete the bundle if a concept and its `HR#` row ever disagree and the row is right — that means
the second view has become a second source of truth, which is the failure
[the register was a sixth copy](2026-08-14-the-register-was-a-sixth-copy.md) already cost this repo
once. The check is cheap and manual: every concept names its ids, so the diff is readable.
