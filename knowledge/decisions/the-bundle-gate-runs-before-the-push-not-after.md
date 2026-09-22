---
type: Decision
resource: .githooks/pre-push
title: The bundle gate runs before the push, not after
description: CI was this bundle's only enforcement, so every ungated change was already published by the time anything read it. A pre-push runs the same check at the moment the change is made, and four tests assert the hook itself is live. A second arm scans for banned names, which CI cannot do because a public runner holds no private list.
tags: [okf, knowledge, gate, hooks, ci]
generated:
  by: claude/opus-5
  at: 2026-08-21T09:10:00Z
---

# The choice

`.githooks/pre-push` runs `okfrules check -Werror knowledge` and `core.hooksPath` points at it.
CI keeps its own step. This is a second measurement at an earlier moment, not a replacement.

Since 2026-09-22 a second block runs `tests/test_public_hygiene.py` first. The two blocks are
public hygiene, then the bundle, which is the order and the shape graphrag's hook already used.

# Why the hygiene arm is here and not left to CI

CI runs that file with `CODERAG_NAME_BAN=none`. That is correct rather than a skip, because a
public runner cannot hold the private list. It is also why CI cannot be the gate for it: with the
list empty, all three scans pass over any tree. The real list exists only in a developer's
environment, so the only moment it can grade a change is on that developer's machine, before the
push. Nothing ran it there.

The cost of the gap is measured in this repo's own history: two `filter-repo` rewrites, the second
finding 170 (path, term) hits across about 150 files and 12 of the 15 banned terms. See
[the public history was rewritten
twice](../constraints/the-public-history-was-rewritten-twice.md).

This arm survives the six-rule ruling of 2026-08-29 on the same footing as the bundle check. What
that ruling removed from this hook were two gates *on the gate* — a refusal probe and an index-mode
arm. A scan of the tracked tree for banned names is a primary content gate, it runs in both public
engines, it grades the same thing in each, and it reaches into no other team's tree.

It is not a complete guard, and the limit is worth stating. A ban list holds the names somebody
thought to add. It does not hold an identifier borrowed out of a private tree, which is a separate
rule a reviewer keeps rather than a test.

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
