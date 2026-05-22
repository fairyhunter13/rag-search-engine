"""Project configuration, constants, and registry management."""

import json
import logging
import os
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
SKIP_STAGE1_RERANK_N: int = int(os.environ.get("OPENCODE_SKIP_STAGE1_RERANK_N", "5"))

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
    os.environ.get("OPENCODE_REGISTRY_PATH", os.path.expanduser("~/.opencode/projects.json"))
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
        return {k: ProjectEntry.from_dict(v) for k, v in raw.items()}
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
