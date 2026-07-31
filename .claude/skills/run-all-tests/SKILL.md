---
name: run-all-tests
description: "Run the complete test suite — fast, full, and browser — and report comprehensive results."
---

# run-all-tests skill

Run the complete test suite: fast + slow + browser. Report comprehensive results.

## Execution order

1. **Fast suite** (~380 tests, no LLM, ~5–7 min):
   ```
   .venv/bin/pytest src/tests/live/ -m "live and not costly and not exclusive" -q -ra --strict-markers --strict-config --ignore=src/tests/live/test_browser.py
   ```

2. **Slow suite** (~93 LLM-heavy tests, ~40 min). Prefer the whole non-browser
   set so fast + slow share fixtures:
   ```
   .venv/bin/pytest src/tests/live/ --ignore=src/tests/live/test_browser.py -q -rfE --strict-markers --strict-config
   ```

3. **Browser suite** (Playwright/Chromium, ~20 min):
   ```
   .venv/bin/pytest src/tests/live/test_browser.py -q --browser chromium
   ```

## GPU pacing — there is none, deliberately

Nothing in `src/` sleeps on temperature. The driver (~86°C) and the hardware (~88°C)
own throttling; they are finer-grained than a Python sleep and, unlike us, can see the
hotspot sensor. `gpu_temp_c()` survives as observability only — read it, never wait on it.
Run the suites back-to-back.

## Inference-efficiency when fixing/adding slow tests (no mocks, no coverage loss)

- Reuse one real LLM artifact across many assertions (session-scoped fixtures): build the
  KB once, judge a shared golden answer once, canonicalize near-duplicate queries,
  classify-only for pure routing tests. Fewer *redundant* calls — never fewer scenarios,
  never mocks, never skips.

## Rules

- Run all three even if the fast suite has failures (collect full picture first)
- Never skip tests; never continue-on-error silently
- For each failure: show traceback, classify as code/infra/flaky, fix code bugs immediately
- Infrastructure failures (GPU not warm, daemon down): restart and rerun
- GPU enforcement: no CPU fallback permitted

## Report format

```
Fast:    N passed  / M failed
Slow:    N passed  / M failed  
Browser: N passed  / M failed
Total:   N passed  / M failed
```

Then list any failures with root cause and action taken.

Run all three suites now.
