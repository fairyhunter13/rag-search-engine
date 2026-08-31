---
type: Decision
resource: tests/test_public_hygiene.py, src/coderag/config.py
title: A guard is placed by its coupling, not by its topic
description: A guard that needs this device, a private project or a local path lives in the private companion repo. Everything device-neutral stays in the public tree. The split is per assertion, so one feature's guards routinely land in both.
tags: [hygiene, guards, testing, publishing]
status: stable
generated: { by: claude/opus-5, at: 2026-08-31T12:00:00Z }
---

# The rule

Split the requirement before you place the test. A guard that needs **this device, a private
project, or a local path** belongs in the private companion repo. Anything **universal and
device-neutral** stays in this public tree. The split is per assertion, not per topic.

# Why topic is the wrong axis

The reflex is that a feature touching private data sends all of its tests private. That moves
universal requirements into a hand-run repo where nothing notices them breaking. The public suite
runs on every push and from the shell every day. The private one runs when somebody remembers.

# The ban list is the worked example

"An operator must declare a ban list" is universal, so it is `tests/test_public_hygiene.py`, with
`CODERAG_NAME_BAN=none` as the sentinel a stranger's clone can satisfy. "The list holds *these*
names" needs the values, so it is private, and the list itself is never committed here. A positive
control proving the mechanism fires carries no names, so it is universal and public.
