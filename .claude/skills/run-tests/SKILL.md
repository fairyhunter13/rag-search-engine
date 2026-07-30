# run-tests skill

Run the fast test suite and report results.

## What this skill does

1. Run `.venv/bin/pytest src/tests/live/ -m "live and not slow" -q --ignore=src/tests/live/test_browser.py`
2. Report: total passed, failed, skipped, time taken
3. If any failures: show the short traceback and identify root cause
4. If all pass: confirm "N passed, 0 failed"

## Rules

- Never skip failures — investigate every red test
- Never mock — all tests use real daemon, real GPU (no local generative LLM)
- If a test fails due to infrastructure (daemon down, GPU not warm): restart the service and rerun
- After reporting results, suggest what to fix if anything failed

## After running

- If 0 failures: report "Fast suite clean: N passed" and done
- If failures exist: for each failure, show the error, identify whether it's a code bug or infrastructure issue, and fix it

Run it now.
