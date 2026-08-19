"""The one row shape the registry stores."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path


@dataclass(slots=True)
class ProjectEntry:
    """One registry row, keyed by resolved path.

    `direct` and `roots` together record *why* the row exists, which is what
    makes a late federation join a config change to an existing project rather
    than a second row for the same code.
    """

    path: Path
    enabled: bool = True
    direct: bool = False
    roots: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    indexed_at: float | None = None
    file_count: int = 0
    chunk_count: int = 0
    last_error: str | None = None
    config_signature: str = ""

    @property
    def key(self) -> str:
        return str(self.path)

    @property
    def orphaned(self) -> bool:
        """True when nothing claims this project any more, so it can be dropped."""
        return not self.direct and not self.roots

    def to_json(self) -> dict:
        return {
            "enabled": self.enabled,
            "direct": self.direct,
            "roots": list(self.roots),
            "tags": list(self.tags),
            "indexed_at": self.indexed_at,
            "file_count": self.file_count,
            "chunk_count": self.chunk_count,
            "last_error": self.last_error,
            "config_signature": self.config_signature,
        }

    @classmethod
    def from_json(cls, path: str, row: dict) -> ProjectEntry:
        return cls(
            path=Path(path),
            enabled=bool(row.get("enabled", True)),
            direct=bool(row.get("direct", False)),
            roots=[str(r) for r in row.get("roots", [])],
            tags=[str(t) for t in row.get("tags", [])],
            indexed_at=row.get("indexed_at"),
            file_count=int(row.get("file_count", 0)),
            chunk_count=int(row.get("chunk_count", 0)),
            last_error=row.get("last_error"),
            config_signature=str(row.get("config_signature", "")),
        )

    def replace(self, **kw) -> ProjectEntry:
        return replace(self, **kw)
