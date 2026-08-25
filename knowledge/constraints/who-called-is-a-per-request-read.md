---
type: Constraint
resource: src/coderag/scope.py, src/coderag/peers.py, src/coderag/pinledger.py
title: On 2026-07-28 the caller identifies itself per request, so nothing about it may be read from a session
description: The handshake is gone and this daemon is stateless_http=True. So `client_params` is a fallback and not a source. clientInfo, capabilities and the protocol version all arrive in `params._meta` on every call. Three defects in a row came from reading a session that was never there, or from writing to a context the resolver boundary throws away.
tags: [mcp, scope, observability, protocol]
status: stable
generated: { by: claude/opus-5, at: 2026-08-20T23:55:00Z }
---

# The session is not a place to read from

MCP 2026-07-28 removed the `initialize` handshake on streamable HTTP. Every request self-describes
through `params._meta`: `io.modelcontextprotocol/protocolVersion`, `…/clientCapabilities`
(required) and `…/clientInfo` (optional). The SDK hands that to a tool as
`ctx.request_context.meta`, an open `RequestParamsMeta` TypedDict.

`ctx.session.client_params` is synthesized by `Connection.from_envelope` only when clientInfo
*and* capabilities both validate. So the caps-less caller, the one an attribution most needs to
name, is exactly the one it cannot name. And the legacy stateless path builds every connection
`from_envelope(version, None, None)`, so `client_capabilities` there is unconditionally `None`:
"advertises no roots capability" would blame a client setting for a protocol revision. That branch
is `legacy-path`, and no client setting changes it.

Validate the raw `_meta` dict with `Implementation.model_validate(raw, by_name=False)` — the wire is
camelCase, and `clientInfo` is a serialization alias, never a pydantic attribute. Reading
`params.clientInfo` returns `None` on every path and named nobody in 197 of 197 log lines.

A legacy-era caller therefore cannot be named at all here: it sends its `clientInfo` in the
handshake, and the legacy *stateless* path throws that away. Every `branch=legacy-path` row in the
ledger reads `client=unidentified` and always will. The peer pid is the only identity those calls
have, which is the second reason it is collected.

# A resolver's ContextVar writes do not reach the tool

A **sync** (`def`) resolver runs through `anyio.to_thread.run_sync`, which executes it in a fresh
`contextvars.copy_context()` and discards it. A `ContextVar` set in the resolver and read in the
tool body is therefore always the default. That is how the journal came to say `not asked, no
resolver ran` on a call where a root had plainly arrived.

The channel the SDK does offer. Resolvers are memoized per `tools/call` by resolver identity. So
one shared `Resolve` dependency named by both the resolver and the tool signature yields the same
object on both sides. `scope.Verdicted` is that dependency.

# What a caller cannot write

The client names itself and nothing checks it. On loopback the kernel already knows: the peer source
port on `ctx.request_context.request.scope["client"]` resolves through `/proc/net/tcp{,6}` to a
socket inode and thence to the owning pid. `peers.of` does that, cached per connection, and returns
`unknown` on any miss — it decides a log field, not a permission.

This is what made the rollout's census evidence rather than self-report. See [the pin rollout needed
a reason, not a count](../decisions/the-pin-rollout-needed-a-reason-not-a-count.md).
