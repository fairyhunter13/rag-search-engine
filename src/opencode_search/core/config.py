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
EMBED_DEVICE = os.environ.get("OPENCODE_EMBED_DEVICE", "cuda")  # "cpu" is forbidden
ONNX_ARENA_MB = int(os.environ.get("OPENCODE_ONNX_ARENA_MB", "4096"))
THERMAL_MAX_C = int(os.environ.get("OPENCODE_THERMAL_MAX_C", "80"))

DAEMON_HOST = os.environ.get("OPENCODE_DAEMON_HOST", "127.0.0.1")
DAEMON_PORT = int(os.environ.get("OPENCODE_DAEMON_PORT", "8765"))

LLM_PROVIDER = os.environ.get("OPENCODE_LLM_PROVIDER", "ollama")
LLM_MODEL = os.environ.get("OPENCODE_LLM_MODEL", "qwen3-enrich:1.7b")
LLM_BASE_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")

# Dashboard chat ONLY — forbidden everywhere else
QUERY_LLM_PROVIDER = os.environ.get("OPENCODE_QUERY_LLM_PROVIDER", "codex")
QUERY_LLM_MODEL = os.environ.get("OPENCODE_QUERY_LLM_MODEL", "gpt-5.4-mini")

FINAL_TOP_K = int(os.environ.get("OPENCODE_FINAL_TOP_K", "10"))
FTS_THRESHOLD = float(os.environ.get("OPENCODE_FTS_THRESHOLD", "0.15"))

IGNORED_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".ruff_cache",
    "node_modules", ".venv", "venv", ".env", "dist", "build", "target",
    ".tox", ".pytest_cache", "coverage", ".coverage",
})


@dataclass
class ProjectEntry:
    path: str
    enabled: bool = True
    indexed_at: str | None = None
    file_count: int = 0
    chunk_count: int = 0
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


def embed_batch_size() -> int:
    try:
        from opencode_search.core.gpu import vram_free_mb
        return 8 if vram_free_mb() >= 7_000 else 6
    except Exception:
        return 8
