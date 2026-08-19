# coderag — Claude Code instructions

The engine is being rebuilt from zero in `src/coderag/`. The old package, its tests, its docs and
its knowledge bundle are gone; `docs/archive/` is the only survivor and it describes code that no
longer exists. **Do not take an architectural claim from `docs/archive/` as current** — it is a
record of what was measured, not of what is built.

## While the rebuild is in progress

- Build order: `config` → `gpu` → `registry` → `projcfg` → `federation` → `store` → `chunk` →
  `discover` → `embed` → `index` → `search` → `watch` → `server` → `tools` → `cli` → `systemd`.
  Each module lands with its tests.
- **No mocks.** A test either calls the real model on the real GPU or touches no model at all.
- Every file under 300 lines; the package under 2,700.
- `knowledge/` is deliberately absent. It is rebuilt against the shipped engine, one concept per
  commit that earns one, not ahead of the code.

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
