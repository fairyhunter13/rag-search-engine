# The env var reached only systemd's children

**2026-08-17** · FE1, FE3, FE4b, FE6, FE8-FE12, SE6, HH6-HH9, SC11, HR29, HR34 · mechanism:
`core/config.py` `_federation_exclude_entries`, `core/index_config.py`, the federation root's
`.rse-index.yaml`

`RSE_FEDERATION_EXCLUDE=*/_worktrees/*` kept 58 worktree symlinks — 541,718 chunks, 24.8% of what
a federated query scans — out of a 193-member federation. It was in force in the daemon, FE8 proved
it, and the 58 rows came back anyway. `register_all_members()` re-derives membership from symlinks
at every start, and it also runs from the CLI, from this repo's own live suite and from operator
scripts: processes systemd never handed the variable to. Each of them saw an empty exclusion and
re-enabled every row it named.

Versioning `federation-exclude.conf` — the fix §15.3 shipped — does not touch that. It makes the
value recoverable after a host is rebuilt; it does not make a second process read it. The carrier
was the defect, not the copy count.

## The layout half moves; the host half cannot

The deployed value concatenated two unlike things:

```
/home/<user>/git/.../<repo> : */_worktrees/*
└─ host-specific absolute path  └─ repo layout
```

`test_systemd_effective_config.py` already knew — `_SUPERSET_OK` named exactly this one variable as
allowed to exceed its versioned baseline, because the versioned copy could not spell the host path.
So the split follows a line the tests had already drawn. The glob goes to `federation.exclude` in
the federation root's `.rse-index.yaml`, which every process reads and which `effective_config`
already unions from root into all 193 members. The absolute path stays in the environment, because
`test_no_absolute_home_paths` greps the whole tracked tree and would red on it (HR34) — which is why
HR34's mechanism clause is restated as "an environment read *or a project config file*, so a fresh
clone needs zero **tracked-file** edits", rather than weakened.

`_SUPERSET_OK` and the drop-in are deleted together: the exemption existed only for the line that no
longer exists, and an exemption outliving its cause is a waiver nobody reviews.

The union lands inside `_federation_exclude_entries` and nowhere else. Four consumers call
`is_federation_excluded` (`daemon/federation.py` twice, `sweeps.py`, `server.py`); joining the
sources at any call site leaves the other three reading env only, and FE5/FE7 would still pass.
The read is mtime-cached in the shape of `discover.py::_cached_effective_config`, which makes it
cheaper than the status quo — the old code re-read `os.environ`, re-split and re-`resolve()`d every
entry once per symlink in the federation walk.

## Why the tests could not simply be pointed at the file

FE9 and FE10 do not go red under this change; they go **green and vacuous**, which is worse. Each
asks whether the registry and the exclusion contradict each other, and with nothing excluded there
is nothing to contradict. They are now proven by injecting the contradiction — an enabled row the
value covers, a disabled-and-armed row it does not — and asserting each detector fires on it.

FE11 modelled "the exclusion is lost" as `_VAR=""`, which stops meaning that the moment a second
source exists. It takes an explicit `sources=()` instead — and its own docstring had already
conceded the unset-it-and-watch experiment "is not available" while the live drop-in was the only
copy, so this is the experiment it wanted, not a repair.

SE1 and SE2 are a deliberate pair: SE2 catches a drop-in vanishing off the host, SE1 catches it
ceasing to apply one restart later, and either alone leaves half the window uncovered. **SE6** is
that pair rebuilt for a file source, and it needs both halves for a sharper reason: a config file is
re-read under an mtime cache, and a project whose config stops parsing is quarantined rather than
raised, so reading the file back would only prove YAML round-trips. What a root *declares* is read
from the file; what is *in force* is read back from the running daemon through `POST /api/overview`.
SE6 also fails when no root declares anything at all, or the exclusion could be deleted outright
with the gate still green.

## Growing the config file meant fixing the loader first

`.rse-index.yaml` gained a fifth field, and the loader dropped an unknown block, a misspelled key, a
non-string list member and a quoted `"false"` in silence — `use_default_ignores: "false"` meant
`True`, because `bool("false")` is. `watcher.max_pending_files` is the standing proof: parsed,
inherited, reported in `overview(status)`, enforced by nothing, for months.

Validation is ~50 lines against a field→(block, key) table, with a `difflib` suggestion. No
`pydantic`: it resolves in the lock only transitively via `mcp`, and `pyproject.toml`'s own
seven-removal audit was written against leaning on an undeclared transitive dep.

Two rulings inside it are worth keeping:

- **A raise would be a fleet-wide outage over one typo.** `effective_config` runs on the watcher
  path and inside `iter_files`. It catches and quarantines the one project with `exclude=["*"]`, and
  `config_error(root)` carries the reason to `overview(status)` — the only surface on which a
  quarantined project is visible. `iter_files` still yields the config file itself under that
  exclusion, deliberately, or the watcher could not see the edit that lifts the quarantine.
- **`max_pending_files` is deleted, not enforced.** `daemon/watcher.py` coalesces per project into a
  set, so the backlog is already bounded by the project's file count; enforcing a cap would mean
  silently dropping index events, which is fail-soft. A `_RETIRED` table keeps the message honest
  for anyone whose file still names it — the generic unknown-key error would tell an operator to fix
  a spelling that was never wrong.

**SC11 is what makes that deletion provable rather than tidy.** SC9 fails the build when an env knob
in `config.py` has no consumer; moving a knob into a config file removes it from SC9's regex, so
file knobs had no equivalent. SC11 requires every `ProjectConfig` field to have a reader outside
`index_config.py` *and* outside `_overview.py` — excluding the reporting surface is exactly what
made it red on `max_pending_files` before the field was deleted, which is how it was proven under
R-26 rather than only ever seen green.
