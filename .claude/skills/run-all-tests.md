# run-all-tests skill

Run the complete test suite: fast + slow + browser. Report comprehensive results.

## Execution order

1. **Fast suite** (330 tests, ~7 min):
   ```
   .venv/bin/pytest src/tests/live/ -m "live and not slow" -q --ignore=src/tests/live/test_browser.py
   ```

2. **Slow suite** (281 tests, LLM-heavy, ~40 min):
   ```
   .venv/bin/pytest src/tests/live/ -m "slow" -q --ignore=src/tests/live/test_browser.py
   ```

3. **Browser suite** (136 tests, Playwright/Chromium, ~20 min):
   ```
   .venv/bin/pytest src/tests/live/test_browser.py -q --browser chromium
   ```

## Rules

- Run all three even if the fast suite has failures (collect full picture first)
- Never skip tests; never continue-on-error silently
- For each failure: show traceback, classify as code/infra/flaky, fix code bugs immediately
- Infrastructure failures (Ollama cold, GPU not warm, daemon down): restart and rerun
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
