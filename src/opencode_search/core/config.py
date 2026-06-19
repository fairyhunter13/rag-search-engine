"""Environment knobs, storage paths, project registry entry."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
_OCS_ROOT = _DATA_HOME / "opencode-search"

REGISTRY_PATH = Path(os.environ.get("OPENCODE_REGISTRY_PATH", str(_OCS_ROOT / "projects.json")))
INDEX_ROOT = Path(os.environ.get("OPENCODE_INDEX_ROOT", str(_OCS_ROOT / "indexes")))

EMBED_MODEL = os.environ.get("OPENCODE_EMBED_MODEL", "jinaai/jina-embeddings-v2-base-code")
RERANK_MODEL = os.environ.get("OPENCODE_RERANK_MODEL", "jinaai/jina-reranker-v1-turbo-en")
EMBED_DEVICE = os.environ.get("OPENCODE_EMBED_DEVICE", "cuda")  # "cpu" is forbidden
THERMAL_MAX_C = int(os.environ.get("OPENCODE_GPU_TEMP_MAX", "80"))
DISABLE_TENSORRT = int(os.environ.get("OPENCODE_DISABLE_TENSORRT", "1"))

DAEMON_HOST = os.environ.get("OPENCODE_MCP_DAEMON_HOST", "127.0.0.1")
DAEMON_PORT = int(os.environ.get("OPENCODE_MCP_DAEMON_PORT", "8765"))
IDLE_SHUTDOWN_S = int(os.environ.get("OPENCODE_MCP_IDLE_SHUTDOWN_S", "900"))
CLIENT_STALE_S = int(os.environ.get("OPENCODE_MCP_CLIENT_STALE_S", "60"))
MODEL_IDLE_UNLOAD_S = int(os.environ.get("OPENCODE_MODEL_IDLE_UNLOAD_S", "300"))

LLM_PROVIDER = os.environ.get("OPENCODE_LLM_PROVIDER", "ollama")
LLM_MODEL = os.environ.get("OPENCODE_LLM_MODEL", "qwen3-enrich:1.7b")
LLM_BASE_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
LLM_NUM_CTX = int(os.environ.get("OPENCODE_LLM_NUM_CTX", "4096"))
LLM_TIMEOUT = int(os.environ.get("OPENCODE_LLM_TIMEOUT", "120"))
LLM_CONCURRENCY = int(os.environ.get("OPENCODE_LLM_CONCURRENCY", os.environ.get("OLLAMA_NUM_PARALLEL", "3")))

# Dashboard chat ONLY — forbidden everywhere else. Claude Code (haiku) only; codex removed.
QUERY_LLM_PROVIDER = os.environ.get("OPENCODE_QUERY_LLM_PROVIDER", "claude")
QUERY_LLM_MODEL = os.environ.get("OPENCODE_QUERY_LLM_MODEL", "claude-haiku-4-5")
QUERY_LLM_FALLBACK_MODEL = "claude-haiku-4-5"
QUERY_LLM_NUM_CTX = int(os.environ.get("OPENCODE_QUERY_LLM_NUM_CTX", "4096"))
QUERY_LLM_TIMEOUT = int(os.environ.get("OPENCODE_QUERY_LLM_TIMEOUT", "180"))

FINAL_TOP_K = int(os.environ.get("OPENCODE_FINAL_TOP_K", "10"))

DEBOUNCE_DELAY_MS = int(os.environ.get("OPENCODE_DEBOUNCE_DELAY_MS", "1000"))
MIN_FLUSH_INTERVAL_S = int(os.environ.get("OPENCODE_MIN_FLUSH_INTERVAL_S", "5"))
DEFAULT_SOURCE_FILE_SIZE_KB = int(os.environ.get("OPENCODE_DEFAULT_SOURCE_FILE_SIZE_KB", "2048"))
DEFAULT_TEXT_FILE_SIZE_KB = int(os.environ.get("OPENCODE_DEFAULT_TEXT_FILE_SIZE_KB", "1024"))
DEFAULT_UNKNOWN_FILE_SIZE_KB = int(os.environ.get("OPENCODE_DEFAULT_UNKNOWN_FILE_SIZE_KB", "512"))
EMBED_PASSAGES_MAX_TEXTS = int(os.environ.get("OPENCODE_EMBED_PASSAGES_MAX_TEXTS", "256"))
MAX_INLINE_BYTES = int(os.environ.get("OPENCODE_MAX_INLINE_BYTES", str(8 * 1024 * 1024)))
MAX_BYTES = int(os.environ.get("OPENCODE_MAX_BYTES", str(24 * 1024 * 1024)))

SCHEMA_VERSION = os.environ.get("OPENCODE_SCHEMA_VERSION", "2")

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


def project_wiki_dir(project_path: str) -> Path:
    return index_dir(project_path) / "wiki"


def root_process_db(root_path: str) -> Path:
    """Root-level BPRE process graph (cross-service edges, processes). HR4: never in per-member graph.db."""
    return index_dir(root_path) / "process_graph.db"


def embed_batch_size() -> int:
    try:
        from opencode_search.core.gpu import vram_free_mb
        return 8 if vram_free_mb() >= 7_000 else 6
    except Exception:
        return 8
