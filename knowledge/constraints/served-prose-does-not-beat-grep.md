---
type: Constraint
resource: src/coderag/tools.py, tests/test_live_agent.py
title: No amount of served prose makes a client prefer this server over its own grep
description: "Three escalations of the MCP instructions and the tool description all lost to grep on a literal-string question in a navigable repo — so the agent layer asks the one question the client's own tools cannot answer, and asserts on the transcript."
tags: [mcp, agent, instructions, negative-result]
status: stable
generated: { by: claude/opus-5, at: 2026-08-20T02:00:00Z }
---

# The measurement

A scripted `claude -p` session, Sonnet, isolated `CLAUDE_CONFIG_DIR`, this server the only MCP
server configured. Asked a question whose answer was a literal string in this repo, it answered with
`grep` — correctly — and went on doing so through **three escalations**: two rewrites of the served
`instructions` (up to and including "reach for `search` before grep") and one of the `search` tool
description.

Everything upstream of the model was verified working. The daemon serves the instructions; the
client injects them (a probe session quoted them back verbatim); `search` returned the right hits
for the same query. The model read the instruction and made a different call.

Two confounds in the question, both real, both recorded rather than tuned away:

- a literal string is grep's own ground, and nothing here wins there;
- in a repo the model can navigate, it guesses the filename from the layout and searches nothing at
  all.

# What follows for the tests

The agent layer now stands the session in a **root whose only interesting content lives in a
federated member reached through a directory symlink**. `grep -r` does not follow a symlinked
directory, so that is a question the client's own tools cannot answer from the working tree — and it
is the capability this engine exists for, which makes it the fair contest rather than a rigged one.
The session calls the server on the first attempt.

Two more things the layer learned, both cheap to lose again:

- `--allowedTools` **pre-approves, it does not restrict.** A session given only
  `mcp__coderag__search` still used `Bash` and `Read`. It cannot be used to force the contest.
- A `tool_result` block's `content` arrives as a **JSON string**, not as the protocol's list of
  content blocks. Reading only the list form finds every result block and parses none of them,
  which looks exactly like a server that returned nothing.

# What follows for the product

The instructions are worth writing well and are not a control surface. What competes with grep is
naming the thing grep cannot do — one working tree versus all of them — in the *tool description*,
which is the text the model weighs at decision time. That is where the surviving sentence lives.

The unqualified escalations ("always call this first") were tried and did not work; they are also
the kind of prose that degrades a host prompt for every other tool. The shipped wording is
de-escalated on purpose: reach for it when the question is about behaviour rather than a literal
string, fall back on an error, and quote the error.
