---
type: Decision
resource: src/coderag/tools.py, src/coderag/cli.py
title: Two MCP tools, and everything an operator needs is on the CLI
description: The old engine exposed 4 tools, 16 HTTP routes and 20 CLI commands; the rebuild exposes two actions to the agent, and the refusals — no wait, no fleet fan-out, no auto-index — are the load-bearing part.
tags: [mcp, interface, scope]
status: stable
generated: { by: claude/opus-5, at: 2026-08-19T09:50:00Z }
---

# Decision

The agent-facing surface is exactly two actions, and the unit of both is **the current root
together with its federated members**:

- `index` — flag the root and its members as indexed. Returns immediately; the work is background.
- `search` — search the root and its members.

Everything else the old engine exposed as a tool parameter was an operator concern, and operator
concerns are CLI subcommands: `serve`, `doctor`, `reindex --full`, `list`, `bridge-stdio`,
`install-systemd`. An agent has no reason to call any of them, and a tool an agent can call is a
tool an agent will call in the middle of answering a question about something else.

# The refusals are the design

Each of these is a thing the tool could plausibly do and deliberately does not.

**No `wait` on `index`, and no synchronous path behind it.** A tool call that blocks on an index
build is a session that stalls for an hour. `index` returns the current state — queue depth, the
project in flight, counts, `last_error` — so calling it again *is* the status check. That is why
there is no third action.

**`search` names one root, never a list, and never widens.** Fleet-wide fan-out across 148 projects
measured **164.78 s against 7.01 s** scoped, and answered a question about one repo with a member's
vendored JavaScript. If the cwd is not a flagged root, `search` returns an error naming what to
call. It does not silently widen to the fleet and it does not auto-index — an unasked-for index of
whatever directory the session happened to start in is a background job nobody ordered.

**An unknown `mode` or `lang` is an error naming the valid set.** A silently widened corpus reads
like an engine defect: the caller sees plausible results from the wrong scope and has no way to tell.

**Unflagging never deletes an index directory.** Both fleet-wide index wipes in this engine's
history came from something that deleted store directories on a computed set. `enabled=False`
removes rows from the registry and arms nothing; the bytes stay.

# What was dropped, so it is not re-proposed

The `graph` tool and its ~4,200 lines of symbol extraction (it exists to serve tree-sitter, which
[the chunker decision](one-chunker-and-it-is-third-party.md) removes), `overview`, `ask`, the chat
route, and the dashboard. Of 16 HTTP routes, `/healthz` and `/mcp` survive; journald, `doctor` and a
repeat `index` call answer what the other fourteen answered — and one of them, `/api/gpu/release`,
answered 200 without doing anything.
