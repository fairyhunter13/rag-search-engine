# coderag

Code retrieval for agents, over the current repo and the repos it federates. Two MCP tools, one
SQLite file per project, GPU-only inference.

```
index    flag this root and its federated projects as indexed; returns immediately
search   ranked locations -- path, line range, preview -- across the same set
```

Everything else is a CLI subcommand, because everything else is an operator's job.

## Requirements

An NVIDIA GPU. **CPU inference is forbidden and asserted four times over** — a working CPU path is
30× slower and fails nothing, so it silently becomes the production path. If the GPU is not
available the daemon refuses to start.

Python 3.12+, `uv`, and the CUDA 13 runtime wheels that `onnxruntime-gpu==1.29.*` links against.
The ORT pin is exact on purpose: a floating range moved the linked CUDA major once already.

## Install

```bash
uv sync
uv run coderag doctor            # GPU, registered projects, orphan rows
uv run coderag install-systemd   # a --user unit on 127.0.0.1:8765
```

Register the MCP server with any client that speaks streamable HTTP:

```json
{"mcpServers": {"coderag": {"type": "http", "url": "http://127.0.0.1:8765/mcp"}}}
```

For a client that only speaks stdio, `coderag bridge-stdio` forwards to the same daemon.

## The two tools

**`index(root="", enabled=True)`** — registers the root, discovers the repos it symlinks, registers
each under its **resolved** path, arms the watcher, and queues a content-hash pass. It returns
before any of that finishes: the return value is state, not a result — `members`, `queue_depth`,
`current`, `indexed`, `roots`, `suppressed_by_inherited_excludes`, `last_error`, `watching`.
Calling it again is how you ask for status; there is no `wait`. `enabled=False` unflags the root
and its members and **never deletes an index directory**.

**`search(query, root="", k=10, mode="hybrid", …)`** — returns ranked *locations*: `path`,
`lines`, `lang`, per-lane `scores`, and a short preview. Read the ranges you want; `include_body`
is off by default. `mode="lexical"` is the one to use for an identifier, a signature or an error
string, `mode="semantic"` for a question in English, and the default fuses both with RRF and a
cross-encoder. An unknown `mode` or `lang` is an error naming the valid set, never a widened
corpus. `root` names one root; the federation expansion is the engine's job.

## Per-project config

`.coderag.toml` at a project root, parsed with `tomllib`. **Unknown keys are errors** with a
"did you mean" — a silently ignored exclude typo is how an index ends up three times too large.

```toml
[index]
exclude = ["wiki/*", "*.min.js", "public/assets/plugins/*"]
respect_gitignore = true
use_default_ignores = true

[federation]
exclude = ["*/legacy-mirror"]
```

A member inherits the excludes of **every** root that claims it, unioned with its own. Narrowing or
widening that set makes the next index pass a reconcile rather than a no-op — see
[knowledge/](knowledge/index.md).

## CLI

```
coderag serve | index [--full] | search <query> | list | doctor | release
coderag bridge-stdio | install-systemd
```

## Development

```bash
uv run pytest -m "not gpu and not live"   # no model is loaded
uv run pytest -m gpu                      # the real models, one run at a time on a machine
```

No mocks: a test either calls the real model on the real GPU or touches no model at all. The public
hygiene guard needs `CODERAG_NAME_BAN` set — `=none` for a clean clone, because a guard that stands
down when its input is missing reports the same green as a clean tree.

`docs/archive/` is the previous engine's incident record: unmaintained, and describing code that
was deleted in `365a235`. Do not read an architectural claim there as current.

## License

MIT. See [LICENSE](LICENSE).
