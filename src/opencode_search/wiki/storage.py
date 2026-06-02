"""Wiki filesystem storage: wiki pages, raw docs, index, and log."""
from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path


class WikiStorage:
    """Manages wiki/ and raw/ directories for a project."""

    def __init__(self, wiki_dir: Path, raw_dir: Path) -> None:
        self.wiki_dir = wiki_dir
        self.raw_dir = raw_dir
        wiki_dir.mkdir(parents=True, exist_ok=True)
        raw_dir.mkdir(parents=True, exist_ok=True)

    def wiki_path(self, name: str) -> Path:
        return self.wiki_dir / f"{name}.md"

    def raw_path(self, name: str) -> Path:
        return self.raw_dir / name

    def index_path(self) -> Path:
        return self.wiki_dir / "index.md"

    def log_path(self) -> Path:
        return self.wiki_dir / "log.md"

    def write_wiki_page(self, name: str, content: str) -> None:
        self.wiki_path(name).write_text(content, encoding="utf-8")

    def read_wiki_page(self, name: str) -> str | None:
        path = self.wiki_path(name)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def list_wiki_pages(self) -> list[str]:
        return sorted(
            p.stem for p in self.wiki_dir.glob("*.md")
            if p.name not in ("index.md", "log.md")
        )

    def append_log(self, entry: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self.log_path().open("a", encoding="utf-8") as f:
            f.write(f"[{now}] {entry}\n")

    def write_index(self, content: str) -> None:
        self.index_path().write_text(content, encoding="utf-8")

    def register_raw_source(self, source_path: str) -> str:
        """Copy source to raw/ dir. Returns the name (filename)."""
        src = Path(source_path)
        name = src.name
        dest = self.raw_path(name)
        shutil.copy2(str(src), str(dest))
        return name

    def list_raw_sources(self) -> list[str]:
        return sorted(p.name for p in self.raw_dir.iterdir() if p.is_file())
