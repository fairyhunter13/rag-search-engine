"""Constants, paths, and the environment switches.

Nothing here imports another coderag module: everything else depends on this,
so a cycle would be unresolvable.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

APP = "coderag"

# ---------------------------------------------------------------- environment


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"CODERAG_{name}", default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"CODERAG_{name}={raw!r} is not an integer") from exc


def _env_flag(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    return raw in ("1", "true", "yes", "on") if raw else default


# ---------------------------------------------------------------------- paths

STATE_DIR = Path(_env("STATE_DIR") or (Path.home() / ".local" / "share" / APP)).expanduser()
REGISTRY_PATH = STATE_DIR / "projects.json"
REGISTRY_LOCK = STATE_DIR / "projects.lock"
BACKUP_DIR = STATE_DIR / "backups"
INDEX_DIR = STATE_DIR / "indexes"
# A store nothing has written to for this long is not being indexed right now.
# The registry lock cannot answer that question: a job queued before its row was
# dropped still creates its directory and indexes into it, with no row to hold.
PRUNE_MIN_IDLE_S = _env_int("PRUNE_MIN_IDLE_S", 60)
PROJECT_CONFIG_NAME = ".coderag.yaml"
# One spelling, not two: `.yml` would be a second name to get right in every
# repo and a second branch in every test that writes one.
RETIRED_CONFIG_NAME = ".coderag.toml"

# The registry has been destroyed twice; rotation is cheap and is the only
# thing that made the second recovery possible.
BACKUP_KEEP = 20


def index_path(project: Path | str) -> Path:
    """Where a project's store lives.

    Keyed by a hash of the resolved path, never written inside the project
    itself: the engine indexes read-only trees, and a store inside a repo is a
    file the watcher would then see change.
    """
    resolved = str(Path(project).resolve())
    digest = hashlib.sha256(resolved.encode()).hexdigest()[:16]
    return INDEX_DIR / f"{Path(resolved).name}-{digest}" / "index.db"


# -------------------------------------------------------------------- serving

HOST = _env("HOST", "127.0.0.1")
PORT = _env_int("PORT", 8765)
MCP_URL = f"http://{HOST}:{PORT}/mcp"

# 0 disables the timer. Unprompted, an idle daemon holds 12.2 GB: the ONNX BFC
# arena never shrinks, so only an explicit unload returns the VRAM.
MODEL_IDLE_UNLOAD_S = _env_int("MODEL_IDLE_UNLOAD_S", 900)
BRIDGE_IDLE_S = _env_int("BRIDGE_IDLE_S", 0)
SCHEDULER_TICK_S = _env_int("SCHEDULER_TICK_S", 60)

# An absolute deadline on stopping, because nothing downstream of the advisory
# 5 s joins is bounded: a tool call runs in a shielded anyio thread the session
# manager's cancel cannot reach, and its task group then waits for that thread.
# 15 s is above the 5+5 joins and below any TimeoutStopSec worth setting.
SHUTDOWN_DEADLINE_S = _env_int("SHUTDOWN_DEADLINE_S", 15)

# On by default: a daemon back after a week has to catch up, and this is the
# only reconcile there is. Off is for when starting the daemon and starting a
# fleet-wide index are two different intentions -- serving during a bake-off,
# or a live suite on a card that has to stay free. The tick still picks up
# anything submitted; only the sweep of everything enabled is skipped.
RECONCILE_ON_START = _env_flag("RECONCILE_ON_START", True)
WATCH_DEBOUNCE_MS = _env_int("WATCH_DEBOUNCE_MS", 1500)

# Fail-closed: a call that arrives with no workspace pin is refused. The unit
# carries no override -- the rollout that needed one ended with the census in
# `scope`.
REQUIRE_CLIENT_ROOTS = _env_flag("REQUIRE_CLIENT_ROOTS", True)

# How often the watcher loop surfaces from Rust to look at the re-arm flag. It
# is not a re-arm interval: surfacing is free, re-arming is not (see `watch`),
# so this only bounds how long a newly registered project waits to be watched.
WATCH_POLL_MS = _env_int("WATCH_POLL_MS", 5000)

# --------------------------------------------------------------------- models

# Settled by a pre-committed tie-break, not by a win: gte and bge differ by
# 0.023 recall@10 at p=0.39, and the first criterion was "an fp16 export
# exists". See `knowledge/decisions/the-embedder-is-settled-by-a-tie-break.md`.
# The pair below must stay consistent with the store's `meta`, which forces a
# rebuild on a mismatch rather than mixing two vector spaces in one table.
EMBED_MODEL = _env("EMBED_MODEL", "Alibaba-NLP/gte-modernbert-base")
EMBED_DIMS = _env_int("EMBED_DIMS", 768)
EMBED_MAX_TOKENS = _env_int("EMBED_MAX_TOKENS", 768)

# How the token states become one vector. Third in the list of things a model
# carries with it, after its prefixes and its context limit -- and the one that
# is invisible when wrong, because every pooling produces a plausible unit
# vector. bge and gte-modernbert are trained on CLS; nomic and jina on the mean.
EMBED_POOLING = _env("EMBED_POOLING", "cls")

RERANK_MODEL = _env("RERANK_MODEL", "Alibaba-NLP/gte-reranker-modernbert-base")
RERANK_MAX_TOKENS = _env_int("RERANK_MAX_TOKENS", 512)

# Which export to pull. A lighter model is first a lighter file of the same
# model -- no re-index, no second vector space to keep coherent. fp16 is the
# tie-break's first criterion and it is the default here because of that; it
# scored identically to fp32 on nomic, and `tests/test_embed_gpu.py` is where
# that is checked for gte rather than assumed.
EMBED_ONNX_FILE = _env("EMBED_ONNX_FILE", "onnx/model_fp16.onnx")
RERANK_ONNX_FILE = _env("RERANK_ONNX_FILE", "onnx/model.onnx")

# Matryoshka truncation. nomic-embed-text-v1.5 is trained so a prefix of the
# vector is itself a vector; below its trained rungs (512/256/128/64) this is
# just throwing away dimensions, which is why it is a per-arm knob and not a
# default. EMBED_DIMS stays the width the store is built against either way.
EMBED_TRUNCATE_DIMS = _env_int("EMBED_TRUNCATE_DIMS", 0)

# Blank, because gte-modernbert is not trained with any. They are still a
# precondition wherever a model has them -- +0.008 unprefixed against +0.062
# prefixed on nomic, the whole margin -- so the mechanism stays and only the
# strings are empty. `side` is load-bearing regardless: see embed.DOCUMENT.
DOCUMENT_PREFIX = _env("DOCUMENT_PREFIX", "")
QUERY_PREFIX = _env("QUERY_PREFIX", "")

PROGRESS_PATH = STATE_DIR / "progress.json"
PROGRESS_WRITE_S = _env_int("PROGRESS_WRITE_S", 2)
# 0 means adapt to free VRAM at load time.
EMBED_BATCH = _env_int("EMBED_BATCH", 0)
RERANK_BATCH = _env_int("RERANK_BATCH", 16)
DISABLE_TENSORRT = _env_flag("DISABLE_TENSORRT", True)

# ------------------------------------------------------------------- chunking

# Non-whitespace characters, so that indented and dense code get comparable
# amounts of content per chunk -- the unit cAST defines (arXiv:2506.15655).
# 2,000 is a round number in a flat region, not an optimum: the 864-config study
# (arXiv:2605.04763) reports that "chunk size has a weaker, non-monotonic
# effect" and names no default. An earlier comment here quoted that paper
# calling 2,000 "a robust default"; it says no such thing.
CHUNK_CHARS = _env_int("CHUNK_CHARS", 2000)

# Zero, and the same study is why: overlap is negligible (<=0.5 pp) at sizes
# >=2,000, and at 1,000 it *degrades* EM by 1.2 pp by cutting new content per
# chunk. It also manufactures the near-duplicates that rank.diversify then
# has to clean up. Kept as a knob because Phase 3 measures 0 against 300.
CHUNK_OVERLAP = _env_int("CHUNK_OVERLAP", 0)

# Prepended to the embedded and FTS text, never to the stored body. One
# component now: the derived line it used to pair with measured flat on docs and
# on code and is gone. Caveat on the docs number only: 48.6% of those queries
# have their heading echoed in the positive's filename, so part of that arm is a
# filename shortcut. The code corpus has no such echo and still pays -0.1233.
CHUNK_HEADER_PATH = _env_flag("CHUNK_HEADER_PATH", True)

# Use MarkdownSplitter (same wheel) for doc-langs. Off by default and a bake-off
# arm, not an edit: it removed every broken boundary in the 50-file sample -- 4
# unbalanced code fences and 2 mid-table starts, both to 0 -- for 30% more,
# smaller chunks. It also has a failure the sample missed: on a code-heavy doc it
# emits a ~9-char chunk holding only a fence opener, and strips the fence markers
# from the body. `--corpus docs` is what settles the trade.
CHUNK_MD_SPLITTER = _env_flag("CHUNK_MD_SPLITTER", False)

# Bump on any change to boundaries or to the header. Neither is covered by
# `ProjectConfig.signature()`, which versions excludes, so before this existed a
# header change rewrote what gets embedded while every store on disk still read
# as current -- stale in the one direction nothing reports. 3: the derived
# header line was deleted, so every earlier store embedded a different string.
CHUNK_ALGO = 3

# ------------------------------------------------------------------ retrieval

RRF_K = _env_int("RRF_K", 60)
CANDIDATES = _env_int("CANDIDATES", 60)
MAX_K = 50
MODES = ("hybrid", "lexical", "semantic")

# ------------------------------------------------------------------ discovery

MAX_FILE_BYTES = _env_int("MAX_FILE_BYTES", 1_500_000)
BINARY_SNIFF_BYTES = 8192

# Refusing these is not taste. A walk rooted at / or ~ enumerates the whole
# machine, and the caches hold copies of source already indexed under its real
# path.
FORBIDDEN_ROOTS = frozenset(
    {
        Path("/"),
        Path("/tmp"),
        Path.home(),
    }
)

# These are refused for their descendants too: nothing under any of them is a
# project, and an exact-match check never fires because a caller names a
# subdirectory, not the tree. `/` and `~` are exact-only above for the same
# reason inverted -- every project on this machine is under both. `/tmp` is
# exact-only on purpose: the live suite indexes throwaway repos there through
# the daemon, and disable-never-prune is what keeps those rows honest.
FORBIDDEN_TREES = frozenset(
    {
        Path("/var"),
        Path("/usr"),
        Path("/etc"),
        Path("/proc"),
        Path("/sys"),
        Path("/dev"),
        Path.home() / ".cache",
        Path.home() / ".local",
        Path.home() / ".config",
    }
)

# -------------------------------------------------------------------- hygiene

# The repo is MIT and public. An unset ban is the failing state, not the
# passing one: a guard that stands down when its input is missing reports the
# same green as a clean tree. A clean clone declares itself with "none".
NAME_BAN = _env("NAME_BAN")
