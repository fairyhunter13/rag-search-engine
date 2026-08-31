# coderag — Claude Code instructions

The engine was rebuilt from zero in `src/coderag/`. The old package, its tests, its docs and its
knowledge bundle are gone. `knowledge/` is the only prose plane. The previous engine's records live
in git history at `365a235^` and are not a description of anything that exists.

## Standing rules

- **Commit to `main`.** No feature branch and no PR. Fold a stray branch in by fast-forward, then
  delete it.
- Before you commit, or touch history: read
  `knowledge/constraints/the-public-history-was-rewritten-twice.md`. `user.email` is pinned here.
- Before you add a guard or a test: read
  `knowledge/decisions/a-guard-is-placed-by-its-coupling-not-by-its-topic.md`.
- **No mocks.** A test either calls the real model on the real GPU or touches no model at all.
- Every module in `src/coderag/` under **220 executable lines** — blanks, comments and docstrings
  excluded — enforced by `tests/test_public_hygiene.py`. The largest is 198. The excluded lines
  carry the whys this repo keeps out of prose, so they are not budgeted, and a `wc` figure never
  reads as over. Physical lines are `ruff format`'s to decide and cannot be a budget: the two
  disagreed, and the formatter rewrote `tools.py` from 283 lines to 324 without adding a statement.
  Test files are not held to the ceiling, because a file covering one subject end to end is worth
  more whole than split to satisfy a count.

## Knowledge bundle

`knowledge/` is an OKF v0.2 bundle. Read the concepts that touch the task before starting. Write
them back in the same commit as the code. The `okf-knowledge-bundle` skill owns how.

It is small on purpose. A concept that restates a module docstring is not written, and
`knowledge/log.md` records which ones were refused for that reason. Gate: `.githooks/pre-push`,
which refuses the push. `uv run pytest tests/test_okf_bundle.py` fails rather than skips without
`okf` and runs in CI, but only once the change is already pushed.

## Two rules that outlive the rebuild

**GPU or nothing**. CPU inference is forbidden. A working CPU path silently becomes the production
path: it is 30× slower and nothing fails. `gpu.py` asserts it three times on *which providers the
session got*.

It asserts once, in `check_placement`, on **where the nodes actually went**. That is the only one
that can see a graph ORT quietly split. Nine shape-plumbing nodes per export are expected and
allowed. Anything else is fatal.

**The tracked tree is publishable.** This repo is public and MIT. No real company name, device
name, or absolute home path in tracked content. `NAME_BAN` guards it, and an unset `NAME_BAN` is
the failing state, not the passing one. Arm it as **`CODERAG_NAME_BAN`, comma-separated**. A clean
clone with nothing to hide sets `CODERAG_NAME_BAN=none`. The list itself is never committed, which
is why the variable has to be named here.

## Running tests

```bash
uv run pytest                    # everything that needs no GPU
uv run pytest -m gpu             # the real models, one at a time on this machine
```
