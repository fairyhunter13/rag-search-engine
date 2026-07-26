"""Code chunking: chonkie CodeChunker for code, line-based fallback."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from rag_search.core.config import EMBED_MAX_TOKENS, EMBED_MODEL

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Chunk:
    path: str
    start_line: int
    end_line: int
    language: str
    content: str


@lru_cache(maxsize=1)
def _tokenizer():
    """The embed model's own tokenizer, so a chunk budget counts the same units the embedder does."""
    from tokenizers import Tokenizer
    return Tokenizer.from_pretrained(EMBED_MODEL)


def _line_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Convert chonkie's *character* offsets into 1-based inclusive line numbers."""
    return text.count("\n", 0, start) + 1, text.count("\n", 0, end) + 1


def _fit_budget(header: str, text: str, path: str, lang: str, start: int) -> list[Chunk]:
    """One chonkie chunk → chunks that actually fit the embed budget.

    chonkie treats chunk_size as a target, not a cap: an indivisible AST node
    overshoots it (measured up to 1210 against a 1024 budget), and the header adds
    a few tokens more. The excess would be truncated at embed time rather than
    stored, so split it by lines instead of losing the tail — the same silent loss
    this whole change exists to remove, just smaller.
    """
    tok = _tokenizer()
    body = header + text
    if len(tok.encode(body).ids) <= EMBED_MAX_TOKENS:
        return [Chunk(path, start, start + text.count("\n"), lang, body)]
    parts = _line_chunks(
        text, path, lang, max_tokens=EMBED_MAX_TOKENS - len(tok.encode(header).ids),
    )
    for p in parts:
        p.start_line += start - 1
        p.end_line += start - 1
        p.content = header + p.content
    return parts


def _line_chunks(
    text: str, path: str, lang: str,
    *, max_tokens: int = EMBED_MAX_TOKENS, overlap: int = 10,
) -> list[Chunk]:
    """Sliding-window fallback, bounded by TOKENS rather than a fixed line count.

    A fixed 100-line window overflowed the embedder's budget on 78% of chunks,
    so their tails were silently truncated away before ever being embedded.
    Growing the window by measured token cost keeps every chunk inside the
    window that will embed it.
    """
    lines = text.splitlines()
    if not lines:
        return []
    try:
        counts = [len(e.ids) for e in _tokenizer().encode_batch(lines)]
    except Exception:  # tokenizer unavailable (offline first run) — estimate instead
        counts = [max(1, len(ln) // 4) for ln in lines]
    chunks, i = [], 0
    while i < len(lines):
        total = n = 0
        while i + n < len(lines) and (n == 0 or total + counts[i + n] <= max_tokens):
            total += counts[i + n]
            n += 1
        block = lines[i : i + n]
        chunks.append(Chunk(
            path=path, start_line=i + 1, end_line=i + len(block),
            language=lang, content="\n".join(block),
        ))
        # Stop once a window reaches the last line. Without this, a tail shorter
        # than `overlap` makes the stride collapse to 1 and emits one near-duplicate
        # chunk per remaining line — a fixed stride could never underflow this way.
        if i + n >= len(lines):
            break
        i += max(n - overlap, 1)
    return chunks


def chunk_file(
    path: Path, content: str, language: str,
    *, project_root: Path | None = None,
) -> list[Chunk]:
    """Chunk one file. Falls back to line-based if chonkie fails.

    Prepends a deterministic structural-path header to every chunk so the
    embedder knows the repo context without extra tokens (cAST, arXiv 2506.15655).
    """
    if not content.strip():
        return []
    try:
        rel = str(path.relative_to(project_root)) if project_root else path.name
    except ValueError:
        rel = path.name
    header = f"# {rel}\n"
    # "auto" is retried because chonkie raises on any language name it has no
    # grammar for (e.g. "text"), and that would otherwise cost us AST chunking
    # on every file of that language.
    for lang in dict.fromkeys((language, "auto")):
        try:
            from chonkie import CodeChunker
            raw = CodeChunker(
                tokenizer=_tokenizer(), chunk_size=EMBED_MAX_TOKENS, language=lang,
            ).chunk(content)
        except Exception as exc:
            log.debug("CodeChunker(language=%r) failed on %s: %s: %s",
                      lang, rel, type(exc).__name__, exc)
            continue
        if raw:
            out: list[Chunk] = []
            for c in raw:
                start, _ = _line_span(content, c.start_index, c.end_index)
                out.extend(_fit_budget(header, c.text, str(path), language, start))
            return out
    log.warning("chonkie unusable for %s (language=%r) — line-window fallback", rel, language)
    chunks = _line_chunks(content, str(path), language)
    for c in chunks:
        c.content = header + c.content
    return chunks
