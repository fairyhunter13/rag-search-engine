---
type: Decision
resource: src/coderag/tools.py, src/coderag/cli.py
title: Two MCP tools, and everything an operator needs is on the CLI
description: The old engine exposed 4 tools, 16 HTTP routes and 20 CLI commands. The rebuild exposes two actions to the agent, and the refusals — no wait, no fleet fan-out, no auto-index — are the load-bearing part.
tags: [mcp, interface, scope]
status: stable
generated: { by: claude/opus-5, at: 2026-08-19T09:50:00Z }
---

# Decision

The agent-facing surface is exactly two actions, and the unit of both is **the current root
together with its federated members**:

- `index` — flag the root and its members as indexed. Returns immediately. The work is background.
- `search` — search the root and its members.

Everything else the old engine exposed as a tool parameter was an operator concern. Operator
concerns are CLI subcommands: `serve`, `doctor`, `reindex --full`, `list`, `bridge-stdio`,
`install-systemd`. An agent has no reason to call any of them. A tool an agent can call is a tool
an agent will call in the middle of answering a question about something else.

# The refusals are the design

Each of these is a thing the tool could plausibly do and deliberately does not.

**No `wait` on `index`, and no synchronous path behind it**. A tool call that blocks on an index
build is a session that stalls for an hour. `index` returns the current state — queue depth, the
project in flight, counts, `last_error` — so calling it again *is* the status check. That is why
there is no third action.

**`search` names one root, never a list, and never widens**. Fleet-wide fan-out across 148
projects measured **164.78 s against 7.01 s** scoped. It answered a question about one repo with a
member's vendored JavaScript. If the cwd is not a flagged root, `search` returns an error naming what to
call. It does not silently widen to the fleet and it does not auto-index. An unasked-for index of
whatever directory the session occurred to start in is a background job nobody ordered.

**An unknown `mode` or `lang` is an error naming the valid set.** A silently widened corpus reads
like an engine defect. The caller sees plausible results from the wrong scope and has no way to
tell.

**Unflagging never deletes an index directory.** Both fleet-wide index wipes in this engine's
history came from something that deleted store directories on a calculated set. `enabled=False`
removes rows from the registry and arms nothing. The bytes stay.

# What was dropped, so it is not re-proposed

The `graph` tool and its ~4,200 lines of symbol extraction (it exists to serve tree-sitter, which
[the chunker decision](one-chunker-and-it-is-third-party.md) removes), `overview`, `ask`, the chat
route, and the dashboard. Of 16 HTTP routes, `/healthz` and `/mcp` survive. Journald, `doctor` and a
repeat `index` call answer what the other fourteen answered — and one of them, `/api/gpu/release`,
answered 200 without doing anything.

# `search` takes many questions, because a round trip is the unit of cost

A fleet reading on 2026-08-31 measured output at 0.6% of spend. So the bill is the number of API
round trips times the resident context each one carries, and the size of an answer is a rounding
error beside it. That reading also measured tool calls per assistant message at exactly 1.00 over
172,494 calls, which is the arithmetic floor. Every independent question paid a whole round trip.

`search` now takes `queries`, a list of up to `config.MAX_QUERIES` questions, in place of `query`.
Three questions in one call cost one round trip rather than three. The cap keeps one call from
becoming a fleet scan.

The shared work inside the call is real but partial. One `federation.unit` expansion, one
`conns.session()` and one embedder load are paid once for the whole call. Retrieval and reranking
stay per question, because `embed.get_reranker().score` takes one query. So the saving is the round
trip, and not the search.

Three rules hold the contract.

* **The single-query reply shape does not move.** A caller that passes `query` gets exactly what it
  got before. `queries` returns a second shape, with an `answers` list in question order.
* **The batch envelope hoists what does not vary.** `mode` and `searched` are the same for every
  question in one call, so they sit once at the top rather than once per answer.
* **One ledger row per search, never per call.** A batched question stays countable beside a single
  one, so [the search row](a-search-writes-one-row-and-the-log-level-stays.md) still answers the
  same questions.

An over-cap batch is refused whole. A silently truncated batch would answer a question nobody asked,
and the caller could not tell which one went missing.
