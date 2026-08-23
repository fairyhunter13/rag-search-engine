# coderag

Code retrieval for agents, over the current repo and the repos it federates. Two MCP tools, one
SQLite file per project, GPU-only inference.

```
index    flag this root and its federated projects as indexed; returns immediately
search   ranked locations -- path, line range, preview -- across the same set
```

Everything else is a CLI subcommand, because everything else is an operator's job.

## Requirements

An NVIDIA GPU. **CPU inference is forbidden**, by three assertions inside the daemon and one
placement gate outside it — a working CPU path is 30× slower and fails nothing, so it silently
becomes the production path. If the GPU is not available the daemon refuses to start.

Python 3.12+, `uv`, and the CUDA 13 runtime wheels that `onnxruntime-gpu==1.29.*` links against.
The ORT pin is exact on purpose: a floating range moved the linked CUDA major once already.

## Install

```bash
uv sync
uv run coderag doctor            # GPU, registered projects, orphan rows and stores
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
and its members and **never deletes an index directory**; a member left with no root claiming it
loses its row, and its store then shows up as unclaimed in `doctor`.

**`search(query, root="", k=10, mode="hybrid", …)`** — returns ranked *locations*: `path`,
`lines`, `lang`, per-lane `scores`, and a short preview. Read the ranges you want; `include_body`
is off by default. `mode="lexical"` is the one to use for an identifier, a signature or an error
string, `mode="semantic"` for a question in English, and the default fuses both with RRF and a
cross-encoder. An unknown `mode` or `lang` is an error naming the valid set, never a widened
corpus. `root` names one root; the federation expansion is the engine's job.

## Per-project config

`.coderag.yaml` at a project root, parsed with `yaml.safe_load`. **Unknown keys are errors** with a
"did you mean" — a silently ignored exclude typo is how an index ends up three times too large.
Quote patterns that YAML would read as something else: bare `no`, `on` and `off` are booleans, and
arrive as a type error rather than as a pattern.

```yaml
index:
  exclude: ["wiki/*", "*.min.js", "public/assets/plugins/*"]
  respect_gitignore: true
  use_default_ignores: true

federation:
  exclude: ["*/legacy-mirror"]
```

A leftover `.coderag.toml` is **refused**, not ignored: ignoring it would drop its excludes and the
only symptom would be a store that grew. Rename it and convert the sections to mappings.

A member inherits the excludes of **every** root that claims it, unioned with its own. Narrowing or
widening that set makes the next index pass a reconcile rather than a no-op — see
[knowledge/](knowledge/index.md).

## CLI

```
coderag serve [--host H] [--port P]        run the daemon in the foreground
coderag index [root] [--full]              index synchronously; --full rebuilds from nothing
coderag search <query> [root] [-k N] [--mode hybrid|lexical|semantic]
coderag list                               every registered project
coderag doctor [--prune]                   GPU, missing projects, orphan rows and stores
coderag release                            unload the models now
coderag health [--url U]                   ask the daemon whether the fleet is indexing
coderag bridge-stdio [--url U] [--idle S]  forward stdio JSON-RPC to the daemon
coderag install-systemd [--no-enable]
```

`install-systemd` also writes `coderag-health.timer`, which runs `coderag health` hourly and pages
through the existing alert unit. `health` exits non-zero only for a project that was failing at the
previous check as well: `last_error` clears on the hourly sweep, so a checker keyed on one sample
pages for every transient failure.

`doctor --prune` is the only destructive subcommand: it deletes store directories no registry row
claims, holding the registry lock so nothing can claim one while it looks, and keeps any store
written to within `CODERAG_PRUNE_MIN_IDLE_S` (60s) because that is a row-less job still finishing.
It exits non-zero if anything was kept, and never touches a disabled row's store.

## Development

```bash
uv run pytest -m "not gpu and not live and not restart" \
  --ignore=tests/test_okf_bundle.py     # no model is loaded
uv run pytest tests/test_okf_bundle.py  # needs the Go okf validator on PATH
uv run pytest -m gpu          # the real models, one run at a time on a machine
uv run pytest -m live         # needs a daemon already running; never starts one
uv run pytest -m restart      # starts and kills daemons of its own, on a private state dir
```

No mocks: a test either calls the real model on the real GPU or touches no model at all. The public
hygiene guard needs `CODERAG_NAME_BAN` set — `=none` for a clean clone, because a guard that stands
down when its input is missing reports the same green as a clean tree.

## License

MIT. See [LICENSE](LICENSE).
