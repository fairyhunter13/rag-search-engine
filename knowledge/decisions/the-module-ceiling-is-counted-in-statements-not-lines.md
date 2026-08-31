---
type: Decision
resource: tests/test_public_hygiene.py, CLAUDE.md, pyproject.toml
title: The module ceiling is counted in statements, not lines
description: A 300-line physical ceiling and `ruff format` are the same authority claimed twice, and CI stayed red because the formatter rewrote `tools.py` from 283 lines to 324 without adding a statement. The ceiling now counts executable lines, which the formatter cannot move.
tags: [hygiene, formatting, ci, ceiling]
status: stable
generated:
  by: claude/opus-5
  at: 2026-09-01T00:00:00Z
---

# The conflict

`tests/test_public_hygiene.py` capped every module at 300 physical lines. `ruff format` decides
where a physical line ends. So the cap and the formatter both owned the same number, and the
package was written to satisfy the cap: imports packed onto one line, keyword arguments packed
three to a line, dict literals collapsed. Running the formatter unpacks all of it —
`src/coderag/tools.py` goes from 283 lines to 324, with the same statements in the same order.

That left `ruff format --check` red on 10 files and no way to make it green, because doing so broke
the ceiling test instead. CI had been red on it since before the disk work, and the block was real
rather than a stale config.

# The ruling

The ceiling counts **executable lines**: neither blank, comment, nor docstring. 220 per module,
against a largest of 198 today.

Two properties fall out. The count is invariant under `ruff format`, so the conflict is not
resolved once but made unreachable. And the lines it excludes are exactly the ones this package
uses to carry its reasons — CLAUDE.md already called the executable number the budget, and a
physical cap taxed a docstring at the same rate as a statement, which is a standing incentive to
delete the why.

# What was rejected

**Splitting `tools.py`.** It would have bought a green run and nothing else: the file is one
subject, the split would be at a line count rather than at a seam, and every other module the
formatter unpacks would arrive at the same question later.

**Raising the physical cap to 340.** Same defect one notch up. The number would still be the
formatter's to move, and the next `ruff` release that changes a wrapping rule reopens it.

**Turning the formatter off.** The check is cheap, it is already in CI, and hand-packing to fit a
line count is what produced the shape being unpacked here.

# What would reverse it

A module that passes at 220 executable lines and is unreadable in one sitting. The number is a
proxy and the counter is exact; if the proxy stops tracking, move the number rather than the
counter.
