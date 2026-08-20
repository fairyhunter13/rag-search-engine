# coderag — Claude Code instructions

The engine was rebuilt from zero in `src/coderag/`. The old package, its tests, its docs and its
knowledge bundle are gone. `knowledge/` is the only prose plane; the previous engine's records live
in git history at `365a235^` and are not a description of anything that exists.

## Standing rules

- **No mocks.** A test either calls the real model on the real GPU or touches no model at all.
- Every file in `src/coderag/` under 300 lines; the largest is 291. The package is 3,928 lines by
  `wc -l` and **2,197 executable** — blanks, comments and docstrings excluded — the rest carrying
  the whys this repo keeps out of prose. The budget is the executable number, so a `wc` figure alone
  never reads as over. Test files are not held to the ceiling: four are over it, each because it
  covers one subject end to end, and splitting a subject to satisfy a line count buys nothing.

## Knowledge bundle

`knowledge/` is an OKF v0.2 bundle. Read the concepts that touch the task before starting; write
them back in the same commit as the code. The `okf-knowledge-bundle` skill owns how. It is small on
purpose — a concept that restates a module docstring is not written, and `knowledge/log.md` records
which ones were refused for that reason.
Gate: `uv run pytest tests/test_okf_bundle.py`, which fails rather than skips without `okf`.

## Two rules that outlive the rebuild

**GPU or nothing.** CPU inference is forbidden. A working CPU path silently becomes the production
path: it is 30× slower and nothing fails. `gpu.py` asserts it three times on *which providers the
session got* and once — `check_placement` — on **where the nodes actually went**, which is the only
one that can see a graph ORT quietly split. Nine shape-plumbing nodes per export are expected and
allowed; anything else is fatal.

**The tracked tree is publishable.** This repo is public and MIT. No real company name, device
name, or absolute home path in tracked content — `NAME_BAN` guards it, and an unset `NAME_BAN` is
the failing state, not the passing one. Arm it as **`CODERAG_NAME_BAN`, comma-separated**; a clean
clone with nothing to hide sets `CODERAG_NAME_BAN=none`. The list itself is never committed, which
is why the variable has to be named here.

## Running tests

```bash
uv run pytest                    # everything that needs no GPU
uv run pytest -m gpu             # the real models, one at a time on this machine
```
