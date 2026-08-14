"""Environment knobs, storage paths, project registry entry."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
_RSE_ROOT = _DATA_HOME / "rag-search"

REGISTRY_PATH = Path(os.environ.get("RSE_REGISTRY_PATH", str(_RSE_ROOT / "projects.json")))
INDEX_ROOT = Path(os.environ.get("RSE_INDEX_ROOT", str(_RSE_ROOT / "indexes")))

EMBED_MODEL = os.environ.get("RSE_EMBED_MODEL", "nomic-ai/nomic-embed-text-v1.5")
# What this pipeline prepends before embedding, per flow. nomic's card asks for these two exact
# strings and the model scores below its published numbers without them — FastEmbed supplies
# neither (`query_embed`/`passage_embed` fall through to `embed` unchanged for every text model),
# so the pipeline has to. Constants rather than env knobs, and deliberately not a model->prefix
# table: HR15 bans the table, and a knob nobody sets is the shape of the 15 removed below. They
# are the prefixes *this* configuration uses; an EMBED_MODEL override owns its own consequences.
# Changing either shifts every stored vector — `store.EMBED_PREFIX_REV` is what makes that
# invalidate the index rather than silently query a space the vectors were never embedded into.
EMBED_DOC_PREFIX = "search_document: "
EMBED_QUERY_PREFIX = "search_query: "
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
# 512 is measured, and it is a property of the *model*, not a universal optimum. The 768 that
# stood here was swept for jina-embeddings-v2-base-code (512 -> 0.9144, 768 -> 0.9498,
# 896 -> 0.9323, 1024 -> 0.8979 on the reranked gnDCG@10 curve, unimodal at 768). The nomic
# switch was A/B'd at 512 with the task prefixes above and won 8 of 8 metrics on two projects —
# dense recall@1 0.5923 -> 0.8077 on this repo, 0.1900 -> 0.3000 on a real php/js/vue project —
# so the budget ships as the arm measured it. Do not carry the old peak across the boundary: it
# was measured on a different model and a different tokenizer.
EMBED_MAX_TOKENS = int(os.environ.get("RSE_EMBED_MAX_TOKENS", "512"))
# Off by default: changing EMBED_MODEL or EMBED_MAX_TOKENS invalidates every vector
# in every project at once, so acting on that drift means reindexing the whole fleet.
# Drift is always logged; opting in is what makes the fleet migrate itself.
AUTO_MIGRATE_VECTORS = int(os.environ.get("RSE_AUTO_MIGRATE_VECTORS", "0"))
DISABLE_TENSORRT = int(os.environ.get("RSE_DISABLE_TENSORRT", "1"))
RSE_GPU_DEVICE: str | None = os.environ.get("RSE_GPU_DEVICE")  # unset = auto-pick

DAEMON_HOST = os.environ.get("RSE_MCP_DAEMON_HOST", "127.0.0.1")
DAEMON_PORT = int(os.environ.get("RSE_MCP_DAEMON_PORT", "8765"))
# RSE_MODEL_IDLE_UNLOAD_S is *not* declared here. It is a daemon-local timing knob and lives with
# its siblings (RSE_RECONCILE_INITIAL_DELAY_S, _RESYNC_S) in `daemon/server.py:12`, which is the
# only reader and what HR40 and docs/decisions/2026-07-01-idle-cpu-root-causes.md both cite. A
# second parse of the same variable here would read to every caller as the place to change it.

# Dashboard chat: claude-haiku-4-5 only. No fallback of any kind, no local generative LLM —
# stated that widely on purpose, since naming one banned fallback reads as permitting the others.
# Matches routes_chat.py:1 and HR10. There is deliberately no provider knob: HR10 makes `claude -p`
# the only path, so one would advertise a choice the invariant forbids. Only the model varies.
QUERY_LLM_MODEL = os.environ.get("RSE_QUERY_LLM_MODEL", "claude-haiku-4-5")

# Removed 2026-07-31: RSE_MCP_CLIENT_STALE_S, RSE_QUERY_LLM_NUM_CTX, RSE_QUERY_LLM_TIMEOUT,
# RSE_FINAL_TOP_K, RSE_DEBOUNCE_DELAY_MS, RSE_MIN_FLUSH_INTERVAL_S, RSE_DEFAULT_*_FILE_SIZE_KB,
# RSE_EMBED_PASSAGES_MAX_TEXTS, RSE_MAX_INLINE_BYTES, RSE_MAX_BYTES, RSE_SCHEMA_VERSION —
# plus RSE_QUERY_LLM_PROVIDER and this file's duplicate RSE_MODEL_IDLE_UNLOAD_S (see above).
# All 15 were read from the environment and then read by nothing — each outlived the call site it
# was added for. Under HR34 this file *is* the retargeting contract for a fresh clone, so a knob
# that parses and silently does nothing is worse than an absent one: it fails without an error.
# A knob comes back only together with the code that consumes it; test_sc9 in
# test_schema_consistency.py now fails the build if one is added without a consumer.

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
    # `last_active` lived here until 2026-07-31 with no reader and no writer. Registries written
    # before then still carry the key; `registry.py:_load` filters to known fields, so the stale
    # key is dropped on read rather than raising.
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
# a path nothing ever opens. The data they named outlived them by a day: deleting a
# writer does not delete what it wrote, and a census found index_dir/wiki and
# index_dir/process_graph.db still on disk across 161 index dirs — 9,016 files, 97.1 MB.
# Purged 2026-07-29. The population is closed (no writer survives), so this was a
# one-off removal and not a recurring sweep. Note what is NOT residue despite the
# tier-3-era name: index_dir/ask_cache is live on routes_chat.py and query/ask.py.
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
