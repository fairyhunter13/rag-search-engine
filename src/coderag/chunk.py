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
3.96-5.64 pp Exact Match. The old engine spent ~4,200 lines on the parser
track, including 211 lines of subprocess isolation whose only job was to
survive a parse that cannot be cancelled in-process. That is the price of the
tie.

**The header is where the measured gain is.** The three-way code comparison
that put a tree-sitter chunker first credits its scope context headers --
enclosing class, imports, path -- not its AST boundaries; cAST has the same
parser, lacks the header, and loses. So the header is built from what a regex
already knows, and the grammar stays uninstalled.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from semantic_text_splitter import TextSplitter

from . import config

_WS = re.compile(r"\s+")

# Import-ish lines across the languages in this corpus. Deliberately shallow:
# a wrong guess costs one header line, and a parser costs a wheel per language.
_IMPORT = re.compile(
    r"^\s{0,3}(?:import\s|from\s+\S+\s+import\s|#include\s|use\s|using\s|require\s*\(|"
    r"require\s+['\"]|package\s|@import\s)"
)

# A column-0 declaration. The generic C-family arm catches `function foo(...)`,
# `int main(...)` and the method-ish lines the keyword list does not name.
_DECL = re.compile(
    r"^(?:(?:export|public|private|protected|static|final|abstract|async|pub|declare)\s+)*"
    r"(?:def|class|func|function|type|struct|interface|impl|trait|enum|module|namespace)\s+\S"
    r"|^\w[\w\s*&<>,:\[\]\.]*\([^;{}]*\)\s*\{?\s*$"
)

MAX_IMPORTS = 5


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


def _imports(lines: list[str]) -> str:
    found = [ln.strip() for ln in lines[:200] if _IMPORT.match(ln)]
    return ", ".join(found[:MAX_IMPORTS])


def _decl_at(lines: list[str], line_no: int) -> str:
    """The nearest column-0 declaration at or above a 1-based line."""
    for i in range(min(line_no, len(lines)) - 1, -1, -1):
        line = lines[i]
        if line[:1].strip() and _DECL.match(line) and not _IMPORT.match(line):
            return line.rstrip().rstrip("{").strip()
    return ""


def scope_header(rel_path: str, lines: list[str], start_line: int) -> str:
    parts = [rel_path]
    if imports := _imports(lines):
        parts.append(f"imports: {imports}")
    if decl := _decl_at(lines, start_line):
        parts.append(f"in: {decl}")
    return "\n".join(parts)


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
    header = config.CHUNK_HEADER if header is None else header
    if overlap >= size:
        raise ValueError(f"overlap {overlap} must be smaller than size {size}")
    if not text.strip():
        return []

    splitter = TextSplitter.from_callback(nonwhitespace, size, overlap=overlap, trim=False)
    lines = text.splitlines()

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
                header=scope_header(rel_path, lines, line) if header else "",
            )
        )
    return chunks
