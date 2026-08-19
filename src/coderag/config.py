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
PROJECT_CONFIG_NAME = ".coderag.toml"

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
WATCH_DEBOUNCE_MS = _env_int("WATCH_DEBOUNCE_MS", 1500)

# --------------------------------------------------------------------- models

# Provisional until the bake-off replaces it. Whatever wins, the pair below
# must stay consistent with the store's `meta`, which forces a rebuild on a
# mismatch rather than mixing two vector spaces in one table.
EMBED_MODEL = _env("EMBED_MODEL", "nomic-ai/nomic-embed-text-v1.5")
EMBED_DIMS = _env_int("EMBED_DIMS", 768)
EMBED_MAX_TOKENS = _env_int("EMBED_MAX_TOKENS", 768)

# How the token states become one vector. Third in the list of things a model
# carries with it, after its prefixes and its context limit -- and the one that
# is invisible when wrong, because every pooling produces a plausible unit
# vector. bge and gte-modernbert are trained on CLS; nomic and jina on the mean.
EMBED_POOLING = _env("EMBED_POOLING", "mean")

RERANK_MODEL = _env("RERANK_MODEL", "Alibaba-NLP/gte-reranker-modernbert-base")
RERANK_MAX_TOKENS = _env_int("RERANK_MAX_TOKENS", 512)

# Which export to pull. Every model in the bake-off publishes fp16 and int8
# siblings beside `model.onnx`, so "a lighter model" is first a lighter file of
# the same model -- no re-index, no second vector space to keep coherent.
EMBED_ONNX_FILE = _env("EMBED_ONNX_FILE", "onnx/model.onnx")
RERANK_ONNX_FILE = _env("RERANK_ONNX_FILE", "onnx/model.onnx")

# Matryoshka truncation. nomic-embed-text-v1.5 is trained so a prefix of the
# vector is itself a vector; below its trained rungs (512/256/128/64) this is
# just throwing away dimensions, which is why it is a per-arm knob and not a
# default. EMBED_DIMS stays the width the store is built against either way.
EMBED_TRUNCATE_DIMS = _env_int("EMBED_TRUNCATE_DIMS", 0)

# The prefixes are a precondition, not a tuning knob: the measured margin
# between two embedders on this corpus was entirely the prefixes -- +0.008
# unprefixed, a tie, against +0.062 prefixed.
DOCUMENT_PREFIX = _env("DOCUMENT_PREFIX", "search_document: ")
QUERY_PREFIX = _env("QUERY_PREFIX", "search_query: ")

# Thermal governor for the index path only. 0 disables it. The default is off
# because a desktop card that never reaches 84 C would only pay for the poll;
# on the laptop this was written for, the card sits three degrees past its own
# throttle point and the poll is free by comparison.
INDEX_TEMP_C = _env_int("INDEX_TEMP_C", 0)
INDEX_TEMP_POLL_S = _env_int("INDEX_TEMP_POLL_S", 5)
INDEX_TEMP_WAIT_S = _env_int("INDEX_TEMP_WAIT_S", 120)

# 0 means adapt to free VRAM at load time.
EMBED_BATCH = _env_int("EMBED_BATCH", 0)
RERANK_BATCH = _env_int("RERANK_BATCH", 16)
DISABLE_TENSORRT = _env_flag("DISABLE_TENSORRT", True)

# ------------------------------------------------------------------- chunking

# Non-whitespace characters, so that indented and dense code get comparable
# amounts of content per chunk. 2,000 is measured, not chosen: "across both
# benchmarks, 2,000 non-whitespace characters is a robust default"
# (arXiv:2605.04763), which is also the unit cAST defines.
CHUNK_CHARS = _env_int("CHUNK_CHARS", 2000)

# Zero, and the same study is why: overlap is negligible (<=0.5 pp) at sizes
# >=2,000, and at 1,000 it *degrades* EM by 1.2 pp by cutting new content per
# chunk. It also manufactures the near-duplicates that search._diversify then
# has to clean up. Kept as a knob because Phase 3 measures 0 against 300.
CHUNK_OVERLAP = _env_int("CHUNK_OVERLAP", 0)

# The scope header prepended to the embedded and FTS text (never the stored
# body). Off is the other arm of the Phase 3 chunker measurement.
CHUNK_HEADER = _env_flag("CHUNK_HEADER", True)

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
        Path("/var"),
        Path("/usr"),
        Path("/etc"),
        Path("/proc"),
        Path("/sys"),
        Path("/dev"),
        Path.home(),
        Path.home() / ".cache",
        Path.home() / ".local",
        Path.home() / ".config",
    }
)

DEFAULT_IGNORES = (
    ".git/*",
    ".hg/*",
    ".svn/*",
    "node_modules/*",
    "vendor/*",
    ".venv/*",
    "venv/*",
    "__pycache__/*",
    ".mypy_cache/*",
    ".pytest_cache/*",
    ".ruff_cache/*",
    "target/*",
    "dist/*",
    "build/*",
    ".next/*",
    ".nuxt/*",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.lock",
    "package-lock.json",
    "yarn.lock",
    "composer.lock",
    "poetry.lock",
    "uv.lock",
)

# -------------------------------------------------------------------- hygiene

# The repo is MIT and public. An unset ban is the failing state, not the
# passing one: a guard that stands down when its input is missing reports the
# same green as a clean tree. A clean clone declares itself with "none".
NAME_BAN = _env("NAME_BAN")
