"""Project configuration, constants, and registry management."""

import hashlib
import json
import logging
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
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
# Tier model definitions  (must match embeddings.py TIER_MODELS)
# ---------------------------------------------------------------------------
_TIER_MODELS: dict[str, tuple[str, str]] = {
    "premium": (
        "jinaai/jina-embeddings-v2-base-code",
        "jinaai/jina-reranker-v2-base-multilingual",
    ),
    "balanced": (
        "jinaai/jina-embeddings-v2-base-en",
        "jinaai/jina-reranker-v1-turbo-en",
    ),
    "budget": (
        "jinaai/jina-embeddings-v2-small-en",
        "Xenova/ms-marco-MiniLM-L-6-v2",
    ),
}

_TIER_DIMS: dict[str, int] = {
    "premium": 768,
    "balanced": 768,
    "budget": 512,
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


def get_project_db_path(project_path: str | Path, tier: str) -> str:
    """Return the centralized LanceDB path for the project+tier pair."""
    return str(get_project_index_dir(project_path) / f"index_{tier}")


def get_legacy_project_db_path(project_path: str | Path, tier: str) -> str:
    """Return the historical per-project LanceDB path for compatibility checks."""
    resolved = Path(project_path).expanduser().resolve()
    return str(resolved / ".opencode" / f"index_{tier}")


def get_tier_models(tier: str) -> tuple[str, str]:
    """Return (embed_model, rerank_model) for the given tier name.

    Raises ValueError for unknown tiers.
    """
    try:
        return _TIER_MODELS[tier]
    except KeyError:
        raise ValueError(
            f"Unknown tier {tier!r}. Valid tiers: {list(_TIER_MODELS)}"
        )


def get_tier_dims(tier: str) -> int:
    """Return embedding dimensionality for the given tier name.

    Raises ValueError for unknown tiers.
    """
    try:
        return _TIER_DIMS[tier]
    except KeyError:
        raise ValueError(
            f"Unknown tier {tier!r}. Valid tiers: {list(_TIER_DIMS)}"
        )


def migrate_project_entry(entry: "ProjectEntry") -> bool:
    """Normalize a registry entry to the centralized index root.

    If the entry still points at the legacy per-project path and that directory
    exists, move it into the centralized root so indexed data is preserved.
    """
    canonical_db_path = get_project_db_path(entry.path, entry.tier)
    if entry.db_path == canonical_db_path:
        return False

    current_path = Path(entry.db_path).expanduser()
    canonical_path = Path(canonical_db_path).expanduser()
    if current_path.exists() and not canonical_path.exists():
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

    entry.db_path = canonical_db_path
    return True


# ---------------------------------------------------------------------------
# Registry dataclass
# ---------------------------------------------------------------------------

@dataclass
class ProjectEntry:
    """Persisted metadata for a registered project."""

    path: str
    db_path: str
    tier: str
    dims: int
    indexed_at: str | None = None    # ISO-8601 timestamp or None
    file_count: int = 0
    last_active: str | None = None   # ISO-8601 timestamp or None
    watch: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ProjectEntry":
        # Accept extra keys gracefully so forward-compatibility is maintained.
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
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
        # Clean up the temp file if something went wrong.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
