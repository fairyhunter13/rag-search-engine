---
type: Defect
resource: tests/test_public_hygiene.py, src/coderag/config.py
title: The publishability guard read a variable nothing set, then a list nothing could match
description: "Two independent failures in one guard: the installer wrote a differently-named variable, and once that was fixed it wrote a colon-joined value into a comma-split reader. The first is loud, the second is green."
tags: [hygiene, guards, resolved]
status: resolved
generated: { by: claude/opus-5, at: 2026-08-21T04:10:00Z }
---

# Two failures, and only one of them announces itself

`config.NAME_BAN` reads `CODERAG_NAME_BAN`. The private companion repo that owns the list installed
it into three scopes under the engine's *previous* name. Nothing on the machine set the variable the
guard reads, so `terms()` failed — correctly, and that red is the whole point of an unset ban being
fatal. It was read as a stale test rather than as the finding it was.

Fixing the name exposed the second failure. The installer joined the tokens with `os.pathsep`; the
guard splits on `,`. A colon-joined list survives the split as **one token that appears in nothing**,
so all seven tests passed over an empty search. That green is indistinguishable from a clean tree,
and it is the one this repo's charter cannot afford: a public MIT tree whose only check that it is
publishable is a substring sweep.

While the guard was inert, three tracked `knowledge/` files accumulated a device-scale count, a
corpus name and a `pytest-of-<user>` path. All three were caught by the first armed run.

# Why unset-is-fatal was not enough

The rule as written covers absence. It does not cover a value that is present, well-formed as a
string, and semantically empty — which is the shape every mis-configured guard on this machine has
taken. `terms()` now refuses a token containing `:` or `;`: a company or device name does not carry
a separator, so a token that does is a list joined with the wrong one. Whitespace is deliberately
allowed, because a real name can hold a space.

The generalisation: **fail-closed on missing input is half a guard. The other half is refusing an
input that cannot match anything.**
