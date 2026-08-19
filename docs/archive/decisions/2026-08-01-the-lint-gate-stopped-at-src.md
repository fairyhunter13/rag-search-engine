# The lint gate stopped at `src/`, and naming the directory would not have fixed it

**2026-08-01** · P5 · `ruff.toml` (new), `ci.yml`, `CLAUDE.md`, two skills, six files under `scripts/`

`ruff check src/rag_search src/tests` is the lint gate — in CI (`ci.yml`), in `CLAUDE.md`, and in the
`pre-deploy` and `issue-sweep` skills. All four name the same two paths, and **`scripts/` is in none
of them**. Ten files, 2,234 lines, including the executable conformance checker the world-model layer
depends on, had never been linted.

## The part that makes it a trap rather than an oversight

The obvious fix — append `scripts` to the command — **lints it under the wrong rules**, silently.

ruff resolves configuration by walking up from each file until it finds one. `[tool.ruff]` lives in
`src/pyproject.toml`, beside the package it governs, and **there is no `pyproject.toml` at the repo
root**. So `src/rag_search/*.py` finds the repo's config one directory up, while `scripts/*.py` walks
past the root and falls back to ruff's *built-in defaults* — `E4`/`E7`/`E9`/`F` instead of the
declared `E, F, I, N, W, UP, B, C4, SIM, RUF`.

The two rulesets do not disagree quietly:

| invocation | config actually used | findings |
|---|---|---:|
| `ruff check scripts` (before) | ruff defaults | 3 |
| `ruff check --config src/pyproject.toml scripts` | the repo's own rules | **15** |

A gate reporting 3 where the declared rules find 15 is worse than no gate, because the 3 look like a
complete answer. The fix is a two-line root `ruff.toml` holding `extend = "src/pyproject.toml"`, so
*any* invocation from the root — including a bare `ruff check .`, and including whatever path a future
change appends — is governed by the config the repo declares. Then the path is added to all four
call sites, which is the cheap half.

## What the 15 were

Nine mechanical (`I001` import ordering ×5, `E401` multiple-imports ×3, plus a `SIM114` branch merge)
and six by hand: `C401`/`C416` redundant comprehensions, two `B007` unused loop variables, a `SIM108`
if/else that is a ternary, and a `SIM105` `try/except/pass` that is `contextlib.suppress`. No bugs
among them — which is the expected result for a first lint of code that has been read and re-read, and
is not an argument against the gate. The point of a gate is that the *next* file added to `scripts/`
is checked before anyone reads it.

`check_world_model.py` is the one edited file that is itself a gate, and the `SIM114` fix rewrote its
path-matching branch, so it was re-run both ways (`--all` and working-tree) and still reports
**CONFORMS**. Every edited script was smoke-run. Fast live suite: **867 passed, 0 failed**.

## Transferable

- **A gate's scope is data, and nobody diffs it.** Four call sites agreed with each other perfectly
  and had agreed on the wrong set of paths since the directory was created. Agreement between copies
  of a command says nothing about whether the command covers what it should.
- **Config discovery is per-file, so "the linter passed" is a per-file claim.** Any tool that walks up
  from the file for its settings can apply two different rulesets in one invocation and report one
  exit code. Check *which* config a path resolves to before trusting a clean run over it.
- **Measure a gap under the rules you actually declare.** The first count of this gap — 3 — was taken
  with the default ruleset and reported as if it were the repo's. It was off by 5×, and it made the
  gap look too small to be worth closing.
