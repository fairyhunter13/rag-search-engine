---
type: Decision
resource: src/coderag/federation.py, src/coderag/server.py
title: The sweep releases what the config denies, and nothing else
description: The hourly sweep only ever added members, so 294 rows outlived the `federation.exclude` that would have refused them. It now releases the difference between two walks of the same tree -- config-driven, never filesystem-driven.
tags: [federation, registry, config, fleet]
status: stable
generated: { by: claude/opus-5, at: 2026-09-01T00:00:00Z }
---

# A claim that outlived the config that made it

`federation.sweep()` walked every direct root hourly and claimed what it found. It had no other
verb. So a member enrolled before an exclude was committed stayed enrolled forever, and the exclude
read as though it had taken effect — new links were refused while the old claims went on being
searched.

Measured on this fleet: **294 of 352 worktree rows were reachable only through a link the committed
`federation.exclude` denied.** Every engine ledger said so and none of them could act:
`claimed=[]` on every sweep, hour after hour.

# Why this release is safe where the obvious one is not

The tempting rule is "release what the walk no longer reaches" — `held - reachable`. That is the
predicate the registry refuses, for the reason
[a row leaves on a delete event and never on a scan](a-row-leaves-on-a-delete-event-and-never-on-a-scan.md)
records: a broken symlink, an unmounted volume and a deleted repository are one observation from
here, and the version that acted on it emptied a registry.

`excluded_members(root)` answers a different question. It walks the same tree twice — once with the
config's excludes applied, once with `federation_exclude` emptied — and returns the difference.

```
links(base, cfg)  ─┐
                   ├─ difference = the members this config denies
links(base, none) ─┘
```

A link that is merely gone appears in *neither* walk, so it can never land in the released set.
Nothing about a member's own existence is consulted. The release is a statement about the config,
which is a file the operator wrote, and never about the disk, which is a thing that lies.

`sweep()` therefore returns a pair, `(claimed, released)`, and releases through `registry.release`
with the root that denied the member — so a member some other root still claims, or one enrolled
`direct=True` by a session's own cwd, keeps its row and only loses that one claim. That is the same
rule `unregister` already applied; it just never ran on the automatic path.

# What it does not reach

The `direct=True` rows. A member enrolled because a session ran inside it is nobody's federation
claim, so no config denies it and this releases none of them. Their only lever is idle age, and
that stays a hand-typed decision.
