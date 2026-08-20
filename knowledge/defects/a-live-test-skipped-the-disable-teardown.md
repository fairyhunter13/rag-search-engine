---
type: Defect
resource: tests/test_live_federation.py, tests/conftest.py, tests/live.py
title: One live test skipped the disable-don't-prune teardown, and it was the whole of doctor's red
description: "The suite's rule is that a live test disables what it registers and never prunes. Ten tests in the module did; one did not, and its two leaked rows were the entire 151-enabled/149-indexed gap, the entire `failed: 2` on `/healthz`, and the two `MISSING` lines `doctor` exited 1 on."
tags: [tests, registry, federation, resolved]
status: resolved
generated: { by: claude/opus-5, at: 2026-08-20T20:15:00Z }
---

# The policy was not the problem

A first reading blamed the disable-don't-prune rule. Measurement refused it: of 88 disabled rows,
**zero** pointed at a path that no longer exists and **zero** sat under `/tmp`. The rule works, and
it exists because a live test that pruned rows destroyed the fleet's 236 rows once already.

The leak was one test -- `test_7_a_typo_in_the_config_is_an_error_that_names_the_nearest_key` --
which registered a tmp project to provoke a config error and never disabled it. Two runs, two rows,
retried and logged with a traceback at every daemon start since.

# Two fixes, because the teardown alone repeats

The test's disable now sits in a `finally`: the row it leaves behind is one the daemon can never
index, so the cost of skipping it is loudest exactly when the test failed.

And `conftest.fleet_unchanged` counts the daemon's enabled rows before and after every `live`
module, per module so the red names the file. `isolated_state` cannot cover this -- it redirects the
in-process paths, and a live test reaches the real registry through the daemon.

The count comes from `/healthz`, the only read of the real registry a test is allowed. Reading
`projects.json` from a test is the shape of the incident.
