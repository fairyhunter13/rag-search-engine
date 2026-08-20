"""One splitter, third-party, plus the one thing no library ships: a scope header.

The splitter is `semantic-text-splitter` (Rust, MIT, no transitive deps). It
descends a boundary hierarchy largest-first -- runs of newlines, then sentence,
word, grapheme, character -- so on code it tries a blank line first and a line
break second, which is the ladder this design wanted, already written. It is
not a *semantic* chunker in the embedding-breakpoint sense: nothing here runs a
second GPU pass to decide where to cut.

No parser, no grammar, no per-language path. A controlled study over 864
configurations (arXiv:2605.04763) found structure-aware chunking does not beat
a sliding window on quality or cost, and function-level chunking was *worse* by
3.57-5.64 pp Exact Match. The old engine spent ~4,200 lines on the parser
track, including 211 lines of subprocess isolation whose only job was to
survive a parse that cannot be cancelled in-process. That is the price of the
tie.

The header is the path and nothing else. The derived line it used to carry --
imports, the enclosing declaration, the heading chain, the key path -- was
ablated on docs and on code and measured flat both times, so it went, and the
per-type dispatch it was the only reason for went with it. The path arm is what
pays: -0.1233 recall@1 on code without it. See
`knowledge/decisions/the-header-is-the-path-and-nothing-else.md`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from semantic_text_splitter import MarkdownSplitter, TextSplitter

from . import config, filters

_WS = re.compile(r"\s+")


@dataclass(slots=True)
class Chunk:
    ord: int
    start_line: int
    end_line: int
    n_chars: int
    sha256: str
    text: str
    header: str = ""

    @property
    def embed_text(self) -> str:
        """What the embedder and FTS see. Never what the store keeps as `text`.

        A header that leaks into the body shows up in every preview and inflates
        every BM25 score with the same path terms; one that never reaches the
        embedder is a no-op. Both directions are asserted.
        """
        return f"{self.header}\n{self.text}" if self.header else self.text


def nonwhitespace(text: str) -> int:
    """The size unit. Budgeting by raw length gives a deeply indented file a
    fraction of the content a flat one gets, purely from leading spaces."""
    return len(_WS.sub("", text))


def scope_header(rel_path: str) -> str:
    """The path, and nothing else.

    A derived line -- imports, the enclosing declaration, the heading chain, the
    key path -- was ablated on two corpora and measured flat both times: exactly
    tied on code recall@1 (16/16 discordant pairs) and +0.0067 on docs. It was
    not redundancy. A census put the `imports:` line at 85% tokens absent from
    the body it labels, so the embedder was being handed new text and doing
    nothing with it; on its own the derived line scored *below* no header at all.
    The path arm is the whole effect, and it is large: -0.1233 recall@1 without it.
    """
    return rel_path if config.CHUNK_HEADER_PATH else ""


def chunk_text(
    text: str,
    *,
    rel_path: str = "",
    size: int | None = None,
    overlap: int | None = None,
    header: bool | None = None,
) -> list[Chunk]:
    """Split, then locate. The offsets are the point.

    `chunk_indices` hands back each chunk with its **character** offset, which
    is what makes `start_line` a newline count rather than a re-derivation. A
    splitter that returns bare strings would force the caller to search for each
    chunk in the file, and a line range that had to be guessed at looks
    identical to one that is right.

    `trim=False` so that concatenating the chunks reconstructs the file:
    indentation is meaning in code, and a trimmed first line is a body that no
    longer matches the range it claims.
    """
    size = config.CHUNK_CHARS if size is None else size
    overlap = config.CHUNK_OVERLAP if overlap is None else overlap
    header = config.CHUNK_HEADER_PATH if header is None else header
    if overlap >= size:
        raise ValueError(f"overlap {overlap} must be smaller than size {size}")
    if not text.strip():
        return []

    # One splitter unless an arm says otherwise. `MarkdownSplitter` respects
    # fences and tables, which is a real gain and a real cost -- see
    # config.CHUNK_MD_SPLITTER -- so it is opt-in and stamped into the store.
    kind = (
        MarkdownSplitter
        if config.CHUNK_MD_SPLITTER and filters.lang_of(rel_path) in filters.DOC_LANGS
        else TextSplitter
    )
    splitter = kind.from_callback(nonwhitespace, size, overlap=overlap, trim=False)

    chunks: list[Chunk] = []
    cursor, line = 0, 1
    for offset, body in splitter.chunk_indices(text):
        # Offsets ascend, so the newline count walks forward once across the
        # whole file rather than rescanning the prefix for every chunk.
        line += text.count("\n", cursor, offset)
        cursor = offset
        end = line + body.count("\n") - (1 if body.endswith("\n") else 0)
        chunks.append(
            Chunk(
                ord=len(chunks),
                start_line=line,
                end_line=max(line, end),
                n_chars=nonwhitespace(body),
                sha256=hashlib.sha256(body.encode("utf-8", "replace")).hexdigest(),
                text=body,
                header=scope_header(rel_path) if header else "",
            )
        )
    return chunks
