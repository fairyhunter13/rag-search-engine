---
type: Defect
resource: src/coderag/scope.py, src/coderag/tools.py
title: A rootless call with no pin resolved to the daemon's cwd, which is $HOME
description: "`scope.default_root` fell back to `Path.cwd()` when the client sent no roots. The daemon's cwd is the operator's home directory, so a real Claude Code session's rootless `search` came back as \"$HOME is not indexed -- call index(root=$HOME) first\" -- advice that, taken, indexes everything on the machine. It now refuses and names the fix."
tags: [scope, tools, mcp-roots]
status: stable
generated: { by: claude/opus-5, at: 2026-08-20T18:10:00Z }
---

# How it surfaced

`tests/test_live_agent.py::test_the_locations_the_session_was_handed_resolve` failed with no
parseable results. Widening the failure message to dump the payload showed the tool result was an
error envelope, not an empty one:

```
{"error":"$HOME is not indexed -- call index(root=$HOME) first","results":[]}
```

The unit shipped `CODERAG_REQUIRE_CLIENT_ROOTS=0` during the rollout (it no longer does), and the
journal showed `workspace pin: 0 root(s)` on every call — so every rootless call took the fallback.

# Why the error blamed the wrong thing

`enforce` never saw it. `registry.resolve(root or default_root(pinned))` produced `$HOME`, and the
*indexed* gate refused first, so the refusal read as a missing index rather than a missing root.
`$HOME` is in `FORBIDDEN_ROOTS`, so the advice could never have worked either — the caller is sent
to a call the engine would refuse.

# The fix

`default_root` raises `ScopeError` when the pin is empty. Both tools already catch `ScopeError` and
return it as an error envelope, so the reply names the actual fix (`pass root=<project path>`)
rather than a path. `index_project`'s `resolve` call had to move inside its existing `try`, which is
where the raise would otherwise have escaped as a transport error.

Guard: `tests/test_scope.py::test_a_rootless_call_with_no_pin_names_the_fix_rather_than_home` runs
both tools and asserts the home path is absent from the message, not merely that an error occurred.
