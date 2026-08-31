---
type: Decision
resource: src/coderag/federation.py, src/coderag/server.py
title: The sweep releases what the config denies, and nothing else
description: The hourly sweep only ever added members, so a claim outlived the `federation.exclude` that would have refused it. It now releases the difference between two walks of the same tree -- config-driven, never filesystem-driven. The 294-row figure that motivated it was a misread pattern; re-measured, the release frees nothing on this fleet.
tags: [federation, registry, config, fleet]
status: stable
generated: { by: claude/opus-5, at: 2026-09-01T00:00:00Z }
---

# A claim that outlived the config that made it

`federation.sweep()` walked every direct root hourly and claimed what it found. It had no other
verb. So a member enrolled before an exclude was committed stayed enrolled forever, and the exclude
read as though it had taken effect — new links were refused while the old claims went on being
searched.

The motivating figure was wrong, and the fix outlived it. The count that justified this — 294 of 352
worktree rows said to be reachable only through a denied link — read `repositories/worktrees/*` as
matching the *target*. It does not. Re-measured on 2026-09-01 with the release shipped:
`excluded_members` returns **59** container links, **none** of them held by any row, so the sweep
releases nothing here. The 292 links under `repositories/prod/<BU>/<svc>-<ref>` that resolve into
`_worktrees/<svc>/<ref>` are admitted on purpose — the config comment says so, and says the earlier
`*/_worktrees/*` was narrowed precisely to keep them.

So this closes a real hole and reclaims nothing today. Both halves are the record.

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
