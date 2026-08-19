# coderag — Claude Code instructions

The engine is being rebuilt from zero in `src/coderag/`. The old package, its tests, its docs and
its knowledge bundle are gone. `knowledge/` is the only prose plane; the previous engine's records
live in git history at `365a235^` and are not a description of anything that exists.

## While the rebuild is in progress

- Build order: `config` → `gpu` → `registry` → `projcfg` → `federation` → `store` → `chunk` →
  `discover` → `embed` → `index` → `search` → `watch` → `server` → `tools` → `cli` → `systemd`.
  Each module lands with its tests.
- **No mocks.** A test either calls the real model on the real GPU or touches no model at all.
- Every file under 300 lines. The package is 3,278 lines by `wc -l` and **1,874 executable**
  by AST; the rest is docstrings and comments carrying the whys this repo keeps out of prose.
  The budget is the executable number.

## Knowledge bundle

`knowledge/` is an OKF v0.2 bundle. Read the concepts that touch the task before starting; write
them back in the same commit as the code. The `okf-knowledge-bundle` skill owns how. It is small on
purpose — a concept that restates a module docstring is not written, and `knowledge/log.md` records
which ones were refused for that reason.
Gate: `uv run pytest tests/test_okf_bundle.py`, which fails rather than skips without `okf`.

## Two rules that outlive the rebuild

**GPU or nothing.** CPU inference is forbidden and asserted four times over in `gpu.py`. A working
CPU path silently becomes the production path: it is 30× slower and nothing fails.

**The tracked tree is publishable.** This repo is public and MIT. No real company name, device
name, or absolute home path in tracked content — `NAME_BAN` guards it, and an unset `NAME_BAN` is
the failing state, not the passing one.

## Running tests

```bash
uv run pytest                    # everything that needs no GPU
uv run pytest -m gpu             # the real models, one at a time on this machine
```
