# run-all-tests skill

Run the complete test suite: fast + slow + browser. Report comprehensive results.

## Execution order

1. **Fast suite** (~380 tests, no LLM, ~5–7 min):
   ```
   .venv/bin/pytest src/tests/live/ -m "live and not slow" -q --ignore=src/tests/live/test_browser.py
   ```

2. **Slow suite** (~93 LLM-heavy tests; ~40 min on a cool GPU, but **1.5–2 h when the
   laptop is heat-soaked** — thermal pauses dominate). Prefer the whole non-browser
   set so fast + slow share fixtures:
   ```
   .venv/bin/pytest src/tests/live/ --ignore=src/tests/live/test_browser.py -q -rfE
   ```

3. **Browser suite** (Playwright/Chromium, ~20 min):
   ```
   .venv/bin/pytest src/tests/live/test_browser.py -q --browser chromium
   ```

## Thermal-aware execution (laptop GPU hosts)

- **Check GPU temp before the slow suite.** If GPU > ~70°C (heat-soaked), idle-cool
  first — a hot start makes the slow run much longer (thermal pauses).
- **Don't run heavy suites back-to-back** without a cooldown; in "cool mode", run the
  fast suite for iteration and defer the slow suite until the GPU has cooled.
- Software thermal guards are 85°C (inference) / 82°C (indexing); tune `RSE_GPU_TEMP_MAX` for your device.

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
