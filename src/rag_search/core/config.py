"""Environment knobs, storage paths, project registry entry."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
_RSE_ROOT = _DATA_HOME / "rag-search"

REGISTRY_PATH = Path(os.environ.get("RSE_REGISTRY_PATH", str(_RSE_ROOT / "projects.json")))
INDEX_ROOT = Path(os.environ.get("RSE_INDEX_ROOT", str(_RSE_ROOT / "indexes")))

EMBED_MODEL = os.environ.get("RSE_EMBED_MODEL", "jinaai/jina-embeddings-v2-base-code")
# Measured against the 40-query golden set, A/B'd alone so the result is attributable and
# re-run once bit-identical: recall@1 0.725 -> 0.800, MRR 0.783 -> 0.825, gnDCG@10 0.789 -> 0.824,
# recall@10 flat at 0.850 (a reranker reorders the candidate set; it cannot add to it). The cost
# is real and on the hot path: 179 ms per 20-chunk rerank against jina-turbo's 66 ms. Revert with
# RSE_RERANK_MODEL=jinaai/jina-reranker-v1-turbo-en — no reindex either way, the reranker never
# touches a stored vector. Served via _CUSTOM_RERANKERS; fastembed ships no description for it.
RERANK_MODEL = os.environ.get("RSE_RERANK_MODEL", "Alibaba-NLP/gte-reranker-modernbert-base")
EMBED_DEVICE = os.environ.get("RSE_EMBED_DEVICE", "cuda")  # "cpu" is forbidden
# Token budget shared by the chunker and the embedder, so a chunk can never be
# larger than the window that embeds it.  Was effectively 512 with 100-line
# chunks, which silently truncated ~51% of all indexed code away.
#
# 768 is measured, not chosen: swept against the 40-query golden set on
# claude-code-workflows (deterministic — 768 reproduced bit-identically), the
# reranked gnDCG@10 curve is unimodal and peaks here, so bigger is NOT better.
#     512 -> 0.9144   768 -> 0.9498   896 -> 0.9323   1024 -> 0.8979
# vs 0.9160 for the old truncating pipeline. Past the peak, a chunk covers so
# much unrelated code that any single concept in it is diluted out of the
# embedding — the opposite failure from truncation, and just as costly.
EMBED_MAX_TOKENS = int(os.environ.get("RSE_EMBED_MAX_TOKENS", "768"))
# Off by default: changing EMBED_MODEL or EMBED_MAX_TOKENS invalidates every vector
# in every project at once, so acting on that drift means reindexing the whole fleet.
# Drift is always logged; opting in is what makes the fleet migrate itself.
AUTO_MIGRATE_VECTORS = int(os.environ.get("RSE_AUTO_MIGRATE_VECTORS", "0"))
DISABLE_TENSORRT = int(os.environ.get("RSE_DISABLE_TENSORRT", "1"))
RSE_GPU_DEVICE: str | None = os.environ.get("RSE_GPU_DEVICE")  # unset = auto-pick

DAEMON_HOST = os.environ.get("RSE_MCP_DAEMON_HOST", "127.0.0.1")
DAEMON_PORT = int(os.environ.get("RSE_MCP_DAEMON_PORT", "8765"))
CLIENT_STALE_S = int(os.environ.get("RSE_MCP_CLIENT_STALE_S", "60"))
MODEL_IDLE_UNLOAD_S = int(os.environ.get("RSE_MODEL_IDLE_UNLOAD_S", "300"))

# Dashboard chat: claude-haiku-4-5 only. No fallback of any kind, no local generative LLM.
# ("No DeepSeek fallback" until 2026-07-28 — there is no DeepSeek left to fall back to, and the
# narrower wording would read as permitting some other one. Matches routes_chat.py:1 and HR10.)
QUERY_LLM_PROVIDER = os.environ.get("RSE_QUERY_LLM_PROVIDER", "claude")
QUERY_LLM_MODEL = os.environ.get("RSE_QUERY_LLM_MODEL", "claude-haiku-4-5")
QUERY_LLM_NUM_CTX = int(os.environ.get("RSE_QUERY_LLM_NUM_CTX", "4096"))
QUERY_LLM_TIMEOUT = int(os.environ.get("RSE_QUERY_LLM_TIMEOUT", "180"))

FINAL_TOP_K = int(os.environ.get("RSE_FINAL_TOP_K", "10"))

DEBOUNCE_DELAY_MS = int(os.environ.get("RSE_DEBOUNCE_DELAY_MS", "1000"))
MIN_FLUSH_INTERVAL_S = int(os.environ.get("RSE_MIN_FLUSH_INTERVAL_S", "5"))
DEFAULT_SOURCE_FILE_SIZE_KB = int(os.environ.get("RSE_DEFAULT_SOURCE_FILE_SIZE_KB", "2048"))
DEFAULT_TEXT_FILE_SIZE_KB = int(os.environ.get("RSE_DEFAULT_TEXT_FILE_SIZE_KB", "1024"))
DEFAULT_UNKNOWN_FILE_SIZE_KB = int(os.environ.get("RSE_DEFAULT_UNKNOWN_FILE_SIZE_KB", "512"))
EMBED_PASSAGES_MAX_TEXTS = int(os.environ.get("RSE_EMBED_PASSAGES_MAX_TEXTS", "256"))
MAX_INLINE_BYTES = int(os.environ.get("RSE_MAX_INLINE_BYTES", str(8 * 1024 * 1024)))
MAX_BYTES = int(os.environ.get("RSE_MAX_BYTES", str(24 * 1024 * 1024)))

SCHEMA_VERSION = os.environ.get("RSE_SCHEMA_VERSION", "2")

IGNORED_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".ruff_cache",
    "node_modules", ".venv", "venv", ".env", "dist", "build", "target",
    ".tox", ".pytest_cache", "coverage", ".coverage",
    ".next", ".nuxt", "vendor", "bower_components", ".idea", ".vscode",
    ".nyc_output", ".cache", "tmp", "temp", "logs",
    # Browser/OS data dirs — binary SQLite/cache blobs tokenize to 8192 tokens
    # and cause FusedMatMul to request 24 GB workspace, OOMing the 16 GB GPU.
    ".playwright-profile", ".chromium", ".chrome-profile", ".playwright",
    "playwright-cache", "chrome-profile", "chromium-profile",
    # Frontend/tool build-cache dirs — regenerated continuously by dev servers
    # (vite/astro/svelte-kit watch mode), misread as source drift if not excluded.
    ".svelte-kit", ".playwright-mcp", ".astro", ".turbo", ".parcel-cache",
    ".vite", ".output", ".vitest",
})


@dataclass
class ProjectEntry:
    path: str
    enabled: bool = True
    indexed_at: str | None = None
    file_count: int = 0
    chunk_count: int = 0
    dims: int = 768
    last_active: str | None = None
    last_change_seen: str | None = None
    federation: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


def index_dir(project_path: str) -> Path:
    import hashlib
    import re
    slug = re.sub(r"[^a-z0-9]", "-", Path(project_path).name.lower())[:40]
    h = hashlib.sha256(project_path.encode()).hexdigest()[:16]
    return INDEX_ROOT / f"{slug}-{h}"


def project_vector_db(project_path: str) -> Path:
    return index_dir(project_path) / "vectors.db"


def project_graph_db(project_path: str) -> Path:
    return index_dir(project_path) / "graph.db"


# project_wiki_dir and root_process_db stood here and left with tier 3. Both were
# pure path builders, so neither had a test of its own — their only callers were
# kb/wiki.py and kb/bpre.py, and with those deleted a surviving helper would just be
# a path nothing ever opens. The directories they named (index_dir/wiki,
# index_dir/process_graph.db) are stale on disk for already-indexed projects; nothing
# reads them and reconcile does not walk them, so they are inert rather than a leak.
def federation_exclude_paths() -> frozenset[str]:
    """Resolved absolute paths excluded from federation discovery + reconcile indexing.

    Configured via RSE_FEDERATION_EXCLUDE (os.pathsep-separated list of paths).
    Paths are expanded (~ allowed) and resolved before comparison. Empty by default.
    """
    raw = os.environ.get("RSE_FEDERATION_EXCLUDE", "")
    return frozenset(
        str(Path(p).expanduser().resolve()) for p in raw.split(os.pathsep) if p.strip()
    )


def _federation_exclude_entries() -> tuple[frozenset[str], tuple[str, ...]]:
    """Split RSE_FEDERATION_EXCLUDE into (exact_or_prefix_set, glob_tuple).

    Entries containing * ? [ are treated as fnmatch globs (expanduser, not resolved).
    All other entries are resolved to absolute paths for exact/prefix matching.
    """
    raw = os.environ.get("RSE_FEDERATION_EXCLUDE", "")
    exact: set[str] = set()
    globs: list[str] = []
    for p in raw.split(os.pathsep):
        p = p.strip()
        if not p:
            continue
        if any(c in p for c in ("*", "?", "[")):
            globs.append(os.path.expanduser(p))
        else:
            exact.add(str(Path(p).expanduser().resolve()))
    return frozenset(exact), tuple(globs)


def is_federation_excluded(candidate: str) -> bool:
    """True if candidate matches any entry in RSE_FEDERATION_EXCLUDE.

    Plain entries match by exact path or prefix (subtree).
    Entries with glob chars (* ? [) are matched with fnmatch against the resolved candidate.
    """
    import fnmatch
    try:
        resolved = Path(candidate).resolve()
    except OSError:
        return False
    exact_or_prefix, globs = _federation_exclude_entries()
    for entry in exact_or_prefix:
        entry_p = Path(entry)
        if resolved == entry_p or resolved.is_relative_to(entry_p):
            return True
    resolved_str = str(resolved)
    return any(fnmatch.fnmatch(resolved_str, pat) for pat in globs)


def embed_batch_size() -> int:
    """Embed batch size, from RSE_EMBED_BATCH or scaled to free VRAM.

    Was a flat 8 on a 16 GB card. That is the batch *count* multiplier for the
    per-batch cost that dominates bulk indexing: fastembed's numpy pooling pays fixed
    per-call overhead once per batch, and Python↔ONNX dispatch likewise. A 128k-chunk
    project at 8 pays it ~16,000 times; at 32, ~4,000. Capped well under fastembed's
    own default of 256 because attention is quadratic in the 768-token sequence —
    L5 walks the ladder and keeps the winner.
    """
    override = os.environ.get("RSE_EMBED_BATCH")
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            pass
    try:
        from rag_search.core.gpu import vram_free_mb
        free = vram_free_mb()
    except Exception:
        return 8
    # Reserve headroom for the reranker, which must stay co-resident (v2 OOM'd here).
    for threshold, size in ((9_000, 32), (7_000, 16), (4_000, 8)):
        if free >= threshold:
            return size
    return 6
