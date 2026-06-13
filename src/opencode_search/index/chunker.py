"""Code chunking: chonkie CodeChunker for code, line-based fallback."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Chunk:
    path: str
    start_line: int
    end_line: int
    language: str
    content: str


def _line_chunks(
    text: str, path: str, lang: str,
    size: int = 100, overlap: int = 10,
) -> list[Chunk]:
    lines = text.splitlines()
    chunks, i = [], 0
    while i < len(lines):
        block = lines[i : i + size]
        chunks.append(Chunk(
            path=path, start_line=i + 1, end_line=i + len(block),
            language=lang, content="\n".join(block),
        ))
        i += size - overlap
    return chunks


def chunk_file(path: Path, content: str, language: str) -> list[Chunk]:
    """Chunk one file. Falls back to line-based if chonkie fails."""
    if not content.strip():
        return []
    try:
        from chonkie import CodeChunker
        chunker = CodeChunker(chunk_size=512, chunk_overlap=64)
        raw = chunker.chunk(content)
        if raw:
            return [
                Chunk(
                    path=str(path),
                    start_line=getattr(c, "start_index", 0),
                    end_line=getattr(c, "end_index", 0),
                    language=language,
                    content=c.text,
                )
                for c in raw
            ]
    except Exception:
        pass
    return _line_chunks(content, str(path), language)
