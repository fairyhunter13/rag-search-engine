"""Project configuration, constants, and registry management."""

import contextlib
import hashlib
import json
import logging
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema version — must match Rust's storage.rs
# ---------------------------------------------------------------------------
SCHEMA_VERSION: str = os.environ.get("OPENCODE_SCHEMA_VERSION", "2")

# ---------------------------------------------------------------------------
# Search / indexing thresholds
# ---------------------------------------------------------------------------
FTS_THRESHOLD: int = int(os.environ.get("OPENCODE_FTS_THRESHOLD", "50"))
IVF_PQ_THRESHOLD: int = int(os.environ.get("OPENCODE_IVF_PQ_THRESHOLD", "512"))
IVF_NUM_PARTITIONS_MAX: int = int(os.environ.get("OPENCODE_IVF_NUM_PARTITIONS_MAX", "256"))
IVF_NUM_SUB_VECTORS_MAX: int = int(os.environ.get("OPENCODE_IVF_NUM_SUB_VECTORS_MAX", "96"))
IVF_NPROBES: int = int(os.environ.get("OPENCODE_IVF_NPROBES", "16"))
IVF_REFINE_FACTOR: int = int(os.environ.get("OPENCODE_IVF_REFINE_FACTOR", "3"))

# ---------------------------------------------------------------------------
# Retrieval pipeline constants
# ---------------------------------------------------------------------------
STAGE1_VECTOR_K: int = int(os.environ.get("OPENCODE_STAGE1_VECTOR_K", "20"))
STAGE1_RERANK_K: int = int(os.environ.get("OPENCODE_STAGE1_RERANK_K", "15"))
GLOBAL_RERANK_MAX: int = int(os.environ.get("OPENCODE_GLOBAL_RERANK_MAX", "100"))
FINAL_TOP_K: int = int(os.environ.get("OPENCODE_FINAL_TOP_K", "10"))

# ---------------------------------------------------------------------------
# Watcher / flush timing
# ---------------------------------------------------------------------------
DEBOUNCE_DELAY_MS: int = int(os.environ.get("OPENCODE_DEBOUNCE_DELAY_MS", "1000"))
MIN_FLUSH_INTERVAL_S: int = int(os.environ.get("OPENCODE_MIN_FLUSH_INTERVAL_S", "5"))

# ---------------------------------------------------------------------------
# File size limits
# ---------------------------------------------------------------------------
DEFAULT_SOURCE_FILE_SIZE_KB: int = int(os.environ.get("OPENCODE_DEFAULT_SOURCE_FILE_SIZE_KB", "2048"))
DEFAULT_TEXT_FILE_SIZE_KB: int = int(os.environ.get("OPENCODE_DEFAULT_TEXT_FILE_SIZE_KB", "1024"))
DEFAULT_UNKNOWN_FILE_SIZE_KB: int = int(os.environ.get("OPENCODE_DEFAULT_UNKNOWN_FILE_SIZE_KB", "512"))

# ---------------------------------------------------------------------------
# Embedding batch limits
# ---------------------------------------------------------------------------
MAX_INLINE_BYTES: int = int(os.environ.get("OPENCODE_MAX_INLINE_BYTES", str(8 * 1024 * 1024)))
EMBED_PASSAGES_MAX_TEXTS: int = int(os.environ.get("OPENCODE_EMBED_PASSAGES_MAX_TEXTS", "256"))
EMBED_PASSAGES_MAX_BYTES: int = int(os.environ.get("OPENCODE_EMBED_PASSAGES_MAX_BYTES", str(24 * 1024 * 1024)))

# ---------------------------------------------------------------------------
# Registry path
# ---------------------------------------------------------------------------
REGISTRY_PATH: Path = Path(
    os.environ.get("OPENCODE_REGISTRY_PATH", os.path.expanduser("~/.local/share/opencode-search/projects.json"))
)

# ---------------------------------------------------------------------------
# Single model pair — code-specific embedding + fast reranker
# jina-v2-base-code: only code-specific ONNX model in FastEmbed (768 dims, 0.64 GB)
# jina-reranker-v1-turbo-en: distilled, 5x smaller than bge-reranker-base (0.15 GB)
# Combined VRAM: ~0.79 GB
# ---------------------------------------------------------------------------
DEFAULT_EMBED_MODEL: str = os.environ.get(
    "OPENCODE_EMBED_MODEL", "jinaai/jina-embeddings-v2-base-code"
)
DEFAULT_RERANK_MODEL: str = os.environ.get(
    "OPENCODE_RERANK_MODEL", "jinaai/jina-reranker-v1-turbo-en"
)
DEFAULT_DIMS: int = 768

# ---------------------------------------------------------------------------
# LLM enrichment defaults — claude-code (haiku-4.5) is the default since Jun 2026.
# Uses the locally installed `claude` CLI; no separate API key needed.
# Switch to codex by setting OPENCODE_LLM_PROVIDER=codex / OPENCODE_LLM_MODEL=gpt-5.4-mini.
# ---------------------------------------------------------------------------
DEFAULT_LLM_PROVIDER: str = os.environ.get("OPENCODE_LLM_PROVIDER", "ollama")
DEFAULT_LLM_MODEL: str = os.environ.get("OPENCODE_LLM_MODEL", "qwen3-enrich:1.7b")
DEFAULT_LLM_NUM_CTX: int = int(os.environ.get("OPENCODE_LLM_NUM_CTX", "4096"))
DEFAULT_LLM_TIMEOUT: int = int(os.environ.get("OPENCODE_LLM_TIMEOUT", "120"))

# Legacy tier suffix → dims mapping used only during migration
_LEGACY_TIER_DIMS: dict[str, int] = {
    "budget": 512,
    "balanced": 768,
    "premium": 768,
}


def get_index_root() -> Path:
    """Return the centralized root directory for all project indexes."""
    configured = os.environ.get("OPENCODE_INDEX_ROOT")
    if configured:
        return Path(configured).expanduser()
    return REGISTRY_PATH.parent / "indexes"


def get_project_index_dir(project_path: str | Path) -> Path:
    """Return the centralized index directory for one project."""
    resolved = Path(project_path).expanduser().resolve()
    name = resolved.name or "project"
    slug = "".join(char.lower() if char.isalnum() else "-" for char in name).strip("-")
    slug = slug or "project"
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
    return get_index_root() / f"{slug}-{digest}"


def get_project_db_path(project_path: str | Path) -> str:
    """Return the centralized LanceDB path for the project."""
    return str(get_project_index_dir(project_path) / "index")


def get_project_graph_db_path(project_path: str | Path) -> str:
    """Return the SQLite graph DB path for a project."""
    return str(get_project_index_dir(project_path) / "graph.db")


def get_project_wiki_dir(project_path: str | Path) -> Path:
    """Return the wiki directory for a project (outside project root)."""
    return get_project_index_dir(project_path) / "wiki"


def get_project_raw_dir(project_path: str | Path) -> Path:
    """Return the raw/ directory for user-supplied docs for a project."""
    return get_project_index_dir(project_path) / "raw"


def migrate_project_entry(entry: "ProjectEntry") -> bool:
    """Normalize a registry entry to the tier-free centralized index root.

    Handles two migration cases:
    1. Old per-project path (.opencode/index_<tier>) → centralized root
    2. Old tier-suffixed path (index_budget/balanced/premium) → tier-free "index"
       In this case indexed_at is nulled because dims may have changed (512→768).
    """
    canonical_db_path = get_project_db_path(entry.path)
    if entry.db_path == canonical_db_path:
        return False

    current_path = Path(entry.db_path).expanduser()
    canonical_path = Path(canonical_db_path).expanduser()

    # Detect legacy tier-suffixed paths; these are dimensionally incompatible.
    current_name = current_path.name
    is_tier_suffixed = any(
        current_name == f"index_{tier}" for tier in _LEGACY_TIER_DIMS
    )

    if current_path.exists() and not canonical_path.exists() and not is_tier_suffixed:
        # Safe to move: same dims, different location only.
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(current_path), str(canonical_path))
        except OSError as exc:
            logger.warning(
                "Failed to migrate legacy index from %s to %s: %s",
                current_path,
                canonical_path,
                exc,
            )
            return False
    elif is_tier_suffixed:
        # Dims changed (budget=512 → new=768); old vectors are incompatible.
        # Null indexed_at to force re-index; leave old data on disk for user to clean up.
        entry.indexed_at = None

    entry.db_path = canonical_db_path
    # Always ensure dims matches DEFAULT_DIMS after migration — old entries may
    # have stale dims=512 from a budget-tier run even after db_path was updated.
    if entry.dims != DEFAULT_DIMS:
        entry.dims = DEFAULT_DIMS
    return True


# ---------------------------------------------------------------------------
# Registry dataclass
# ---------------------------------------------------------------------------

@dataclass
class ProjectEntry:
    """Persisted metadata for a registered project."""

    path: str
    db_path: str
    dims: int = DEFAULT_DIMS
    indexed_at: str | None = None    # ISO-8601 timestamp or None
    file_count: int = 0
    last_active: str | None = None   # ISO-8601 timestamp or None
    watch: bool = False
    federation: list[str] = field(default_factory=list)  # paths of federated member projects

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ProjectEntry":
        # Accept extra keys gracefully so forward-compatibility is maintained.
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)


# ---------------------------------------------------------------------------
# Registry I/O
# ---------------------------------------------------------------------------

def load_registry() -> dict[str, ProjectEntry]:
    """Read the project registry from disk, returning an empty dict on missing file."""
    path = REGISTRY_PATH
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw: dict = json.load(fh)
        entries = {k: ProjectEntry.from_dict(v) for k, v in raw.items()}
        changed = False
        for entry in entries.values():
            changed = migrate_project_entry(entry) or changed
            # Also fix stale dims that weren't caught by migration (e.g., entry
            # was already at canonical db_path but still had dims=512 from an
            # old tier-based run).
            if entry.dims != DEFAULT_DIMS:
                entry.dims = DEFAULT_DIMS
                changed = True
        if changed:
            save_registry(entries)
        return entries
    except (json.JSONDecodeError, OSError, TypeError) as exc:
        logger.warning("Failed to load registry at %s: %s", path, exc)
        return {}


def save_registry(entries: dict[str, ProjectEntry]) -> None:
    """Atomically write the project registry to disk (write .tmp then rename)."""
    path = REGISTRY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {k: v.to_dict() for k, v in entries.items()}
    # Write to a sibling temp file first, then rename for atomicity.
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".registry-", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
