---
type: Defect
resource: src/coderag/tools.py, tests/test_server_tools.py
title: The index reply graded a project by one row of 143
description: "Every action takes the unit to be the root together with its members. The status the same tool returns did not: `indexed` was the root's own row, 33,053 chunks of the 185,453 the project answers from."
tags: [federation, tools, registry, status]
status: stable
generated: { by: claude/opus-5, at: 2026-08-25T04:40:00Z }
---

# The actions were collective and the answer was not

`tools.py` opens by saying it: the unit of both tools is the root together with its
members. `federation.register` claims every member. `index_project` submits every member.
`unregister` re-walks the survivors. `watch._roots` arms every enabled row, and `search`
runs against `federation.expand(root)`. Each of those is the whole unit.

`_status` read one row. One root federates on this machine, and its own row holds
**33,053 chunks of 185,453** across 143 projects. So `index` answered *is my project
built* with **17.8%** of the project.

The other fields failed the same way. `watching` covered the root alone. A member is
armed in a later pass than its root, because the sweep claims it later. `last_error` was
the root's. A member dropped from the watch set for an unparseable `.coderag.yaml` was
reported nowhere the caller could see.

`queue_depth` is the same shape from the other side. It counts the fleet, so a caller
polling for its own unit read the work of 164 rows. Nothing told it which of that work
was its own.

# What the reply carries now

`indexed` sums `files` and `chunks` over the enabled rows of `[root, *members]`. It also
names how many projects it summed. `root_indexed` keeps the root's own pair. The store,
the watcher and the queue all work at that grain, and one stuck member is invisible in a
total. `pending` counts the whole-project walks queued **for this unit**.
`members_watching` and `member_errors` answer for the members, which hold 82.2% of the
corpus.

Two fields stay per project on purpose. `watching` is still `watch.armed(target)`. A
member dropped for a broken config never joins the armed set, so an all-of-them predicate
reads false forever. The live fixtures poll that field before they write, and they would
hang out their timeout rather than fail. `suppressed_by_inherited_excludes` is not summed
either. It walks the candidate set twice per project, and 143 of those walks is a
fleet-sized cost charged to a status call.

`_pending` lives in `tools.py` rather than beside the queue it reads. `index.py` is at the
300-line ceiling `CLAUDE.md` sets and `tests/test_public_hygiene.py` enforces.
