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

**The header is unevidenced, and this docstring used to claim otherwise.** It
credited cAST's rival with scope headers and said cAST "lacks the header, and
loses". cAST prepends no header at all: its +4.3 Recall@5 is from AST
boundaries, and its own limitations section says it works "without explicit
contextual awareness" -- naming the header as future work, not as its result.
Every *measured* gain for prepending context comes from an LLM-generated blurb
(one model call per chunk) or from context trained into the embedder. No
published result isolates a regex scope header. So the header stays for now
because it is nearly free, `CHUNK_HEADER=0` is a real bake-off arm, and
whichever way that arm falls is what ships.

The header is also the one place per-type knowledge enters this pipeline, which
is why it dispatches on language rather than the splitter doing so.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from semantic_text_splitter import MarkdownSplitter, TextSplitter

from . import config, filters

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

# A markdown ATX heading. The prose analogue of a declaration: the chain of
# enclosing headings is what tells a reader which section a paragraph is in.
_HEADING = re.compile(r"^(#{1,6})\s+(\S.*?)\s*#*$")

# A mapping key in YAML, JSON or a TOML `key = ...` line, with its indentation.
# Quotes are optional so the same pattern reads JSON's `"key":` form.
_KEY = re.compile(r"""^(\s*)(?:-\s+)?["']?([\w.\-/]+)["']?\s*[:=](?:\s|$)""")

# A TOML table header, which states its own full path and ends the walk.
_TOML_TABLE = re.compile(r"^\s*\[+\s*([^\]]+?)\s*\]+\s*$")


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


def _heading_chain(lines: list[str], line_no: int) -> str:
    """The enclosing markdown headings, outermost first.

    Walk up keeping only headings that are strictly shallower than the last one
    kept, which is what makes `### C` under `## B` under `# A` come back as the
    path rather than as every heading above the chunk.
    """
    chain, want = [], 7
    for i in range(min(line_no, len(lines)) - 1, -1, -1):
        if (match := _HEADING.match(lines[i])) and (level := len(match.group(1))) < want:
            chain.append(match.group(2))
            want = level
            if level == 1:
                break
    return " > ".join(reversed(chain))


def _key_path(lines: list[str], line_no: int, *, toml: bool) -> str:
    """The enclosing key path in YAML, JSON or TOML, by indentation.

    Same shape as the heading walk, with indent standing in for heading level.
    TOML is the one dialect where a column-0 key is not the top: `[tool.ruff]`
    above it is, so only a table header ends that walk. Everywhere else a
    column-0 key does, which also keeps the table regex away from the YAML flow
    sequences it would otherwise misread.
    """
    path, want = [], None
    for i in range(min(line_no, len(lines)) - 1, -1, -1):
        line = lines[i]
        if toml and (match := _TOML_TABLE.match(line)):
            path.append(match.group(1))
            break
        if (match := _KEY.match(line)) and (want is None or len(match.group(1)) < want):
            path.append(match.group(2))
            want = len(match.group(1))
            if want == 0 and not toml:
                break
    return ".".join(reversed(path))


def scope_header(rel_path: str, lines: list[str], start_line: int) -> str:
    """Path, plus whatever "where am I" means for this file type.

    Dispatching here rather than in the splitter is deliberate. The boundary
    evidence rates format-aware splitting a tie on code and this corpus is a
    quarter prose and data, so the cheap, reversible place to hold per-type
    knowledge is the one string we prepend -- not a second chunker, which costs
    a full re-index to compare and doubles the eval matrix.

    An unlabeled extension falls to the code arm on purpose: a new language gets
    the generic declaration regex for free, and the arm degrades to the path.
    """
    parts = [rel_path]
    lang = filters.lang_of(rel_path)
    if lang in filters.DOC_LANGS:
        # Never `_decl_at` here. The generic C-family arm matches any prose line
        # ending in a parenthetical, so a section heading became an `in:` line
        # borrowed from an unrelated part of the file -- and an adjacent-but-wrong
        # header is the hardest kind of distractor to retrieve past.
        if chain := _heading_chain(lines, start_line):
            parts.append(f"in: {chain}")
    elif lang in filters.DATA_LANGS:
        if keys := _key_path(lines, start_line, toml=lang == "toml"):
            parts.append(f"in: {keys}")
    else:
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

    # One splitter unless an arm says otherwise. `MarkdownSplitter` respects
    # fences and tables, which is a real gain and a real cost -- see
    # config.CHUNK_MD_SPLITTER -- so it is opt-in and stamped into the store.
    kind = (
        MarkdownSplitter
        if config.CHUNK_MD_SPLITTER and filters.lang_of(rel_path) in filters.DOC_LANGS
        else TextSplitter
    )
    splitter = kind.from_callback(nonwhitespace, size, overlap=overlap, trim=False)
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
