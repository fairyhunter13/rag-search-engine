---
type: Decision
resource: .githooks/pre-push
title: The bundle gate runs before the push, not after
description: CI was this bundle's only enforcement, so every ungated change was already published by the time anything read it. A pre-push runs the same check at the moment the change is made, and four tests assert the hook itself is live.
tags: [okf, knowledge, gate, hooks, ci]
generated:
  by: claude/opus-5
  at: 2026-08-21T09:10:00Z
---

# The choice

`.githooks/pre-push` runs `okfrules check -Werror knowledge` and `core.hooksPath` points at it.
CI keeps its own step. This is a second measurement at an earlier moment, not a replacement.

# What it replaced, and why that failed

Nothing. This repo had no git hook at all — `core.hooksPath` was unset, `.git/hooks` held only
samples, and there was no `.githooks/`. CLAUDE.md named `uv run pytest tests/test_okf_bundle.py`
as the gate, and nothing ran it on commit or on push.

CI does run it, but a CI gate answers after the change is on the remote. The whole purpose of a
bundle is to be read by the next session. So "the push is already public and the bundle is wrong"
is the state the gate exists to prevent, not to report.

# Why the hook asserts things about itself

A hook is the one gate that can stop running without anything turning red. Three of its arms are
about the hook rather than the bundle:

- **A missing checker fails closed.** `okfrules` absent means the bundle is unchecked, not fine.
- **The index mode is 100755.** A hook chmod -x'd in the *index* is planted non-executable in
  every future clone, and git skips a hook it cannot execute without printing anything.
- **The checker refuses something.** A type-less concept goes in first. If that is accepted, the
  checker on PATH gates nothing and every other arm passes anyway. This was verified against a
  stub `okfrules` that exits 0 — the hook blocks it.

`tests/test_okf_bundle.py` carries the mirror of the first three, so a checkout that pushes
ungated fails the suite rather than passing quietly.

# Why -Werror

A broken link is a *warning*: plain `check` prints it and exits 0. This bundle is warning-free, so
a warning here can only be a link that just broke or an orphan that just appeared.
