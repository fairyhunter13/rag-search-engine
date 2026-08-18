---
type: Constraint
resource: src/tests/live/test_public_hygiene.py
title: The tracked tree is publishable and device-neutral
description: HR34 — no absolute home path in any tracked file, every machine-specific value arrives from the environment or a project config, and the banned-name list lives outside this repo because publishing it would publish the names.
tags: [public-repo, hygiene, name-ban, hr34]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# The tracked tree is publishable and device-neutral

This is a public repo. A fresh clone must need **zero edits to tracked files** to retarget at
another machine, and no tracked file may name a person, a device, a company or a home directory.

## The carrier is chosen by what the value is

Both carriers exist, and picking the wrong one is what breaks the row:

| Value | Carrier | Why |
|---|---|---|
| repo layout, exclusions | `.rse-index.yaml` | every process reads a file; only systemd's children read the environment |
| a host path, a GPU index, a daemon port | `os.environ.get(...)` | `test_no_absolute_home_paths` greps the whole tracked tree, so a tracked config carrying `/home/<user>/` reds this row directly |

`RSE_FEDERATION_EXCLUDE` carried both until it was split along exactly that line. The corollary bit
this repo the other way round too:
[the env var reached only systemd's children](../../docs/decisions/2026-08-17-the-env-var-reached-only-systemds-children.md).

Banned absolute forms: `/home/<user>/`, `/root/`, `/Users/<user>/`, `C:\Users\<user>\`.

## The name ban is a mechanism here and a list elsewhere

Device-specific names — company, codename, device id — never ship in this repo. The **mechanism**
is `test_no_banned_device_names`; the **values** arrive through `RSE_NAME_BAN` and live in a private
audit repo, because a list of the names you must not publish publishes them. That was not a
hypothetical: see
[the name guard would have published the list](../../docs/decisions/2026-08-05-the-name-guard-would-have-published-the-list.md).

A clone with nothing to ban must declare `RSE_NAME_BAN=none`. **An unset variable fails**, because a
guard that stands down when its input is missing reports the same green as a clean tree.

The pre-RSE brand tokens are permanently banned outside a narrow external-product allowlist, and the
patterns live in `test_no_legacy_ose_opencode_tokens_reappear` — the one place allowed to spell
them.

## The tree is governed; the history is not

The 2026-08-04 sweep's own pre-sweep text stays reachable through `git log -p`. Rewriting roughly
92 % of commits to remove strings no longer present in the tree was measured and declined.

**This has a live cost that a contributor must know about.** The private audit repo counts
rag-search-engine commits containing a banned name against a fixed budget, and that budget currently
has no headroom — the lever that shrank it is spent. Every new commit here, message and diff alike,
must be clean, or the only remedy left is the history rewrite that was already declined.

## Usability is part of the row

Whether a stranger can *use* the repo is checked too: the declared license and the shipped `LICENSE`
must agree, and the shipped MCP configs must advertise exactly the registered tools — otherwise a
clone's editor integration offers a tool the server does not serve.

A separate ceiling in the same file caps tracked markdown line count. It exists to stop documentation
from growing without anyone deciding to grow it, and it is raised only in a commit that says so.

## Sources

Row HR34 in [§13b](../../docs/architecture/federation-ops-and-invariants.md).

| Claim | Guard | File |
|---|---|---|
| no absolute home paths | `test_no_absolute_home_paths` | `test_public_hygiene.py` |
| brand tokens stay out | `test_no_legacy_ose_opencode_tokens_reappear` | `test_public_hygiene.py` |
| device names stay out | `test_no_banned_device_names` | `test_public_hygiene.py` |
| the ban variable is declared | `test_nb1_name_ban_variable_is_declared` | `test_public_hygiene.py` |
| runtime config is env-driven | `test_runtime_config_is_env_driven` | `test_public_hygiene.py` |
| license metadata agrees | `test_the_repo_ships_the_license_its_metadata_declares` | `test_public_hygiene.py` |
| shipped configs match the surface | `test_e8b_shipped_mcp_configs_advertise_the_registered_tools` | `test_server.py` |

Record: [public release hardening](../../docs/decisions/2026-07-09-public-release-hardening.md).
