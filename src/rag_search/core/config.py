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

# Under HR34 this file *is* the retargeting contract a fresh clone reads, so a knob that parses
# and then feeds nothing is worse than an absent one: it fails without an error. 15 such knobs
# were removed 2026-07-31; test_sc9 in test_schema_consistency.py fails the build on any knob
# added without a consumer.

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
    # Last *full* build. Incremental passes never restamp it, so it is completeness, not
    # freshness — index age is the store's own `meta.source_mtime` (`_vectors_baseline`).
    indexed_at: str | None = None
    file_count: int = 0
    chunk_count: int = 0
    dims: int = 768
    # Registries on disk may carry keys this dataclass no longer declares; `registry.py:_load`
    # filters to known fields, so a removed field is dropped on read rather than raising.
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


# Deleting a path builder does not delete what it wrote: retiring the `wiki`/`process_graph.db`
# writers left 9,016 orphaned files (97.1 MB) across 161 index dirs, purged separately on
# 2026-07-29 — the size is here because the files are gone, so nothing can re-measure it. Not
# residue, despite the era: `index_dir/ask_cache` is live on routes_chat.py and query/ask.py.
# A federation root's `.rse-index.yaml` carries the layout patterns; the env var keeps the
# host-specific absolute paths HR34 forbids a tracked file from holding. Env alone reached only
# processes systemd handed the variable to, so the CLI, the live suite and every script ran with
# it unset and re-enabled the excluded rows — 58 of them, measured 2026-07-30, with the drop-in
# installed and in force.
FEDERATION_EXCLUDE_SOURCES: tuple[str, ...] = ("env", "config")

_CFG_EXCLUDE_CACHE: tuple[tuple[tuple[str, float], ...], tuple[str, ...]] | None = None
_ROOT_CFG_CACHE: tuple[float, list[Path]] | None = None


def _federation_root_configs() -> list[Path]:
    """The `.rse-index.yaml` of every registered federation root.

    Roots only: a member's own config governs what is indexed inside it, never which projects
    exist. The import is deferred because `core.registry` imports this module at module scope.

    Cached on the registry's mtime because `_cached_effective_config` calls this to build its
    stamp, on the walker's per-file path — and `list_projects()` reads and migrates the whole
    236-row JSON every time, so an uncached call here would make a cache hit cost more than the
    miss it replaced. Bound worth knowing: a root config *created* while the daemon runs stays
    invisible until the registry is next written, which `register_all_members()` does at every
    start. Edits to an already-seen file are caught — `_config_exclude_raw` stamps its mtime.
    """
    global _ROOT_CFG_CACHE
    try:
        stamp = REGISTRY_PATH.stat().st_mtime
    except OSError:
        stamp = 0.0
    if _ROOT_CFG_CACHE is not None and _ROOT_CFG_CACHE[0] == stamp:
        return _ROOT_CFG_CACHE[1]
    try:
        from rag_search.core.registry import list_projects
        entries = list_projects()
    except Exception:
        return []
    out = [
        p for e in entries if e.federation
        for n in (".rse-index.yaml", ".rse-index.yml")
        if (p := Path(e.path) / n).is_file()
    ]
    _ROOT_CFG_CACHE = (stamp, out)
    return out


def _config_exclude_raw() -> tuple[str, ...]:
    """`federation.exclude` unioned across every federation root, cached on the files' mtimes.

    The uncached read this stands beside re-hit os.environ, re-split and re-resolve()d every
    entry on every call — once per symlink inside the federation walk — so caching a YAML read
    here is cheaper than the status quo, not an added cost.
    """
    global _CFG_EXCLUDE_CACHE
    from rag_search.core.index_config import load_project_config

    paths = _federation_root_configs()
    try:
        stamps = tuple(sorted((str(p), p.stat().st_mtime) for p in paths))
    except OSError:
        stamps = ()
    if _CFG_EXCLUDE_CACHE is not None and _CFG_EXCLUDE_CACHE[0] == stamps:
        return _CFG_EXCLUDE_CACHE[1]
    out: list[str] = []
    for p in paths:
        for pat in load_project_config(p.parent).federation_exclude:
            if pat not in out:
                out.append(pat)
    _CFG_EXCLUDE_CACHE = (stamps, tuple(out))
    return tuple(out)


def _federation_exclude_entries(
    sources: tuple[str, ...] = FEDERATION_EXCLUDE_SOURCES,
) -> tuple[frozenset[str], tuple[str, ...]]:
    """Split the configured exclusion into (exact_or_prefix_set, glob_tuple).

    Entries containing * ? [ are treated as fnmatch globs (expanduser, not resolved).
    All other entries are resolved to absolute paths for exact/prefix matching.

    `sources` exists so a caller can ask what happens with an exclusion source *absent*. With
    two sources an empty RSE_FEDERATION_EXCLUDE no longer means "nothing excluded", and FE11 —
    which proves the exclusion is load-bearing by running discovery with it and without — needs
    a way to say "neither" that the filesystem cannot quietly contradict.
    """
    raw: list[str] = []
    if "env" in sources:
        raw += os.environ.get("RSE_FEDERATION_EXCLUDE", "").split(os.pathsep)
    if "config" in sources:
        raw += _config_exclude_raw()
    exact: set[str] = set()
    globs: list[str] = []
    for p in raw:
        p = p.strip()
        if not p:
            continue
        if any(c in p for c in ("*", "?", "[")):
            globs.append(os.path.expanduser(p))
        else:
            exact.add(str(Path(p).expanduser().resolve()))
    return frozenset(exact), tuple(globs)


def is_federation_excluded(
    candidate: str, sources: tuple[str, ...] = FEDERATION_EXCLUDE_SOURCES,
) -> bool:
    """True if candidate matches any configured exclusion entry.

    Plain entries match by exact path or prefix (subtree).
    Entries with glob chars (* ? [) are matched with fnmatch against the resolved candidate.
    """
    import fnmatch
    try:
        resolved = Path(candidate).resolve()
    except OSError:
        return False
    exact_or_prefix, globs = _federation_exclude_entries(sources)
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
