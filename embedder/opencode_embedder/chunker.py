"""Unified Chunking Module

Routes files to the best chunker per file type:
- Code (165+ langs)  → Chonkie CodeChunker (AST via tree-sitter)
- JS Components      → LangChain JSFrameworkTextSplitter (React/Vue/Svelte/Astro)
- Markdown           → LangChain MarkdownHeaderTextSplitter + size control
- HTML               → LangChain HTMLSemanticPreservingSplitter
- JSON               → LangChain RecursiveJsonSplitter (structure-aware)
- YAML/TOML          → Parse to dict → RecursiveJsonSplitter
- LaTeX/RST          → LangChain RecursiveCharacterTextSplitter(Language.*)
- XML                → RecursiveCharacterTextSplitter with HTML separators
- Prose/Text         → Chonkie SemanticChunker (embedding similarity)
- Fallback           → Chonkie TokenChunker

Recommendations:
- Target 750 tokens per chunk (~3 000 chars) to fit within ONNX memory budget
- Max 1 500 estimated tokens per chunk (hard safety limit)
"""

import contextvars
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from opencode_embedder import tokenizer as tok


def _detect_language(path: Path) -> str:
    """Detect language from file extension / special names.

    This is intentionally local to the model server path.
    """
    ext = path.suffix.lstrip(".").lower()
    name = path.name.lower()

    match ext:
        case "rs":
            return "rust"
        case "go":
            return "go"
        case "ts":
            return "typescript"
        case "tsx":
            return "tsx"
        case "js" | "mjs" | "cjs":
            return "javascript"
        case "jsx":
            return "jsx"
        case "py" | "pyi" | "pyw":
            return "python"
        case "java":
            return "java"
        case "kt" | "kts":
            return "kotlin"
        case "scala":
            return "scala"
        case "c" | "h":
            return "c"
        case "cpp" | "cc" | "hpp" | "cxx" | "hxx":
            return "cpp"
        case "cs":
            return "csharp"
        case "rb":
            return "ruby"
        case "php":
            return "php"
        case "swift":
            return "swift"
        case "md" | "mdx" | "markdown" | "mdown" | "mkd":
            return "markdown"
        case "yaml" | "yml":
            return "yaml"
        case "json" | "jsonc" | "json5" | "jsonl":
            return "json"
        case "toml":
            return "toml"
        case "html" | "htm" | "xhtml":
            return "html"
        case "xml" | "xsl" | "xslt" | "plist":
            return "xml"
        case "tex" | "latex" | "ltx":
            return "latex"
        case "rst":
            return "rst"
        case "txt":
            return "text"

    if name == "dockerfile":
        return "dockerfile"
    if name in {"makefile", "gnumakefile"}:
        return "makefile"
    if name == "cmakelists.txt":
        return "cmake"
    if name in {"gemfile", "rakefile"}:
        return "ruby"

    return "unknown"


log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_TOKENS_PER_CHUNK = 1_500
MIN_TOKENS_PER_CHUNK = 50
TARGET_TOKENS_PER_CHUNK = 750
CHARS_PER_TOKEN = 4  # conservative estimate for code

# Character targets (libraries use char counts, not tokens)
# Smaller chunks keep ONNX inference memory bounded: 3000 chars ≈ 750-1350
# real tokens, fitting within the 1024-token truncation safety net set on
# the embedder.  Previous value of 16 000 chars produced ~6 000 tokens whose
# O(n²) attention workspace consumed 10+ GB of RAM.
TARGET_CHARS = TARGET_TOKENS_PER_CHUNK * CHARS_PER_TOKEN  # 3 000
MAX_CHARS = MAX_TOKENS_PER_CHUNK * CHARS_PER_TOKEN  # 6 000

# Files larger than this skip structure-aware chunkers (too slow to parse)
LARGE_FILE_CHARS = 1_000_000  # ~1MB / ~250K tokens

# Current tier for token counting (thread-safe via contextvars)
_tier_var: contextvars.ContextVar[str] = contextvars.ContextVar("tier", default="premium")


def set_tier(tier: str) -> None:
    """Set the current tier for token counting"""
    _tier_var.set(tier)


def get_tier() -> str:
    """Get the current tier for token counting"""
    return _tier_var.get()


@dataclass
class Chunk:
    """A chunk of text with metadata"""

    content: str
    start_line: int
    end_line: int
    chunk_type: str
    name: Optional[str] = None
    language: str = "unknown"


def count_tokens(text: str) -> int:
    """Count tokens using current tier's tokenizer"""
    return tok.count_tokens_for_tier(text, get_tier())


# ---------------------------------------------------------------------------
# Language → tree-sitter name mapping
# ---------------------------------------------------------------------------
_LANG_TO_TREESITTER: dict[str, str] = {
    "rust": "rust",
    "go": "go",
    "typescript": "typescript",
    "tsx": "tsx",
    "javascript": "javascript",
    "jsx": "javascript",
    "python": "python",
    "java": "java",
    "c": "c",
    "cpp": "cpp",
    "ruby": "ruby",
    "php": "php",
    "swift": "swift",
    "kotlin": "kotlin",
    "scala": "scala",
    "csharp": "csharp",
    "lua": "lua",
    "r": "r",
    "perl": "perl",
    "elixir": "elixir",
    "erlang": "erlang",
    "haskell": "haskell",
    "elm": "elm",
    "clojure": "clojure",
    "clojurescript": "clojure",
    "lisp": "commonlisp",
    "scheme": "scheme",
    "racket": "racket",
    "ocaml": "ocaml",
    "fsharp": "fsharp",
    "nim": "nim",
    "zig": "zig",
    "v": "v",
    "d": "d",
    "dart": "dart",
    "julia": "julia",
    "sql": "sql",
    "bash": "bash",
    "zsh": "bash",
    "fish": "fish",
    "powershell": "powershell",
    "protobuf": "proto",
    "graphql": "graphql",
    "css": "css",
    "scss": "scss",
    "sass": "scss",
    "vue": "vue",
    "svelte": "svelte",
    "astro": "astro",
    "dockerfile": "dockerfile",
    "makefile": "make",
    "cmake": "cmake",
    "gradle": "groovy",
    "latex": "latex",
}

# Languages that are document/data formats (not code, handled separately)
_DOC_LANGUAGES = frozenset(
    {
        "markdown",
        "json",
        "yaml",
        "toml",
        "html",
        "xml",
        "latex",
        "rst",
        "text",
        "unknown",
    }
)

# ---------------------------------------------------------------------------
# Cached chunker singletons (lazy-loaded)
# ---------------------------------------------------------------------------
_MAX_CODE_CHUNKERS = 10
_code_chunkers: OrderedDict[str, object] = OrderedDict()
_semantic_chunker: object = None
_token_chunker: object = None


def _get_code_chunker(ts_lang: str):
    """Get or create a CodeChunker for the given tree-sitter language (LRU, cap=10)."""
    if ts_lang in _code_chunkers:
        _code_chunkers.move_to_end(ts_lang)
        return _code_chunkers[ts_lang]

    from chonkie import CodeChunker

    chunker = CodeChunker(
        language=ts_lang,
        tokenizer="character",
        chunk_size=TARGET_CHARS,
    )
    _code_chunkers[ts_lang] = chunker
    if len(_code_chunkers) > _MAX_CODE_CHUNKERS:
        _code_chunkers.popitem(last=False)  # evict oldest
    return chunker


def _get_semantic_chunker():
    """Get or create the SemanticChunker (uses local 32M model, free & fast).

    GPU NOTE: model2vec's StaticModel (potion-base-32M) intentionally runs on CPU.
    It is NOT an ONNX/transformer model — it performs static lookup + linear projection
    via numpy, which has no GPU execution path. This is acceptable because:
      1. Inference is sub-millisecond on CPU (no attention, no O(n²) ops)
      2. It is only used for plain-text/prose files (rare in code repos)
      3. The resulting text chunks are embedded by FastEmbed, which DOES use GPU
    Attempting to pass ONNX providers would raise a TypeError.
    """
    global _semantic_chunker
    if _semantic_chunker is None:
        from chonkie import SemanticChunker

        log.debug(
            "Loading SemanticChunker with potion-base-32M (CPU-only static model — intentional)"
        )
        _semantic_chunker = SemanticChunker(
            embedding_model="minishlab/potion-base-32M",
            threshold=0.7,
            chunk_size=TARGET_CHARS,
            similarity_window=3,
        )
        log.debug("SemanticChunker ready (CPU inference, static model, ~150MB)")
    return _semantic_chunker


def _get_token_chunker():
    """Get or create the fallback TokenChunker."""
    global _token_chunker
    if _token_chunker is None:
        from chonkie import TokenChunker

        _token_chunker = TokenChunker(
            tokenizer="character",
            chunk_size=TARGET_CHARS,
            chunk_overlap=0,
        )
    return _token_chunker


def cleanup_chunkers() -> None:
    """Release all cached chunker singletons to free memory.

    Call this after a batch of chunking is complete. The SemanticChunker
    alone holds a ~150MB model (potion-base-32M) that otherwise never unloads.
    """
    global _semantic_chunker, _token_chunker
    _code_chunkers.clear()
    _semantic_chunker = None
    _token_chunker = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chunk(text: str, chunk_type: str, language: str, name: Optional[str] = None) -> Chunk:
    """Create a Chunk from text."""
    end_line = max(0, text.count("\n"))
    return Chunk(
        content=text,
        start_line=0,
        end_line=end_line,
        chunk_type=chunk_type,
        name=name,
        language=language,
    )


def _chonkie_to_chunks(results, chunk_type: str, language: str) -> list[Chunk]:
    """Convert Chonkie chunk results to our Chunk dataclass."""
    return [
        Chunk(
            content=r.text,
            start_line=getattr(r, "start_index", 0),
            end_line=getattr(r, "end_index", 0),
            chunk_type=chunk_type,
            language=language,
        )
        for r in results
    ]


def _enforce_token_limit(chunks: list[Chunk]) -> list[Chunk]:
    """Split any chunk that exceeds the token limit."""
    result: list[Chunk] = []
    for chunk in chunks:
        if count_tokens(chunk.content) <= MAX_TOKENS_PER_CHUNK:
            result.append(chunk)
        else:
            result.extend(split_by_tokens(chunk.content, chunk.language))
    return result


def _merge_tiny(chunks: list[Chunk]) -> list[Chunk]:
    """Merge tiny chunks with their neighbors."""
    if len(chunks) < 2:
        return chunks

    min_chars = MIN_TOKENS_PER_CHUNK * CHARS_PER_TOKEN
    result: list[Chunk] = []
    i = 0
    while i < len(chunks):
        current = chunks[i]
        while (
            i + 1 < len(chunks)
            and len(current.content) < min_chars
            and len(current.content) + len(chunks[i + 1].content) < MAX_CHARS
        ):
            nxt = chunks[i + 1]
            current = Chunk(
                content=f"{current.content}\n{nxt.content}",
                start_line=current.start_line,
                end_line=nxt.end_line,
                chunk_type=current.chunk_type,
                name=current.name,
                language=current.language,
            )
            i += 1
        result.append(current)
        i += 1
    return result


# ---------------------------------------------------------------------------
# File-type specific chunkers
# ---------------------------------------------------------------------------


def _chunk_markdown(content: str) -> list[Chunk]:
    """Markdown: header-aware splitting with size control."""
    from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

    headers = [("#", "h1"), ("##", "h2"), ("###", "h3"), ("####", "h4")]
    md_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=headers,
        strip_headers=False,
    )
    sections = md_splitter.split_text(content)

    # Constrain large sections
    size_splitter = RecursiveCharacterTextSplitter(
        chunk_size=TARGET_CHARS,
        chunk_overlap=0,
    )
    splits = size_splitter.split_documents(sections)

    return [_make_chunk(doc.page_content, "markdown", "markdown") for doc in splits]


def _make_json_splitter():
    """Create a RecursiveJsonSplitter with best available options."""
    from langchain_text_splitters import RecursiveJsonSplitter

    try:
        return RecursiveJsonSplitter(max_chunk_size=TARGET_CHARS, convert_lists=True)
    except TypeError:
        return RecursiveJsonSplitter(max_chunk_size=TARGET_CHARS)


def _chunk_json(content: str) -> list[Chunk]:
    """JSON: structure-aware depth-first splitting."""
    data = json.loads(content)
    splitter = _make_json_splitter()
    texts = splitter.split_text(json_data=data)

    return [_make_chunk(t, "json", "json") for t in texts]


def _chunk_jsonl(content: str) -> list[Chunk]:
    """JSONL: group line-delimited JSON objects into chunks."""
    chunks: list[Chunk] = []
    buffer = ""

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        candidate = f"{buffer}\n{stripped}" if buffer else stripped
        if len(candidate) > TARGET_CHARS and buffer:
            chunks.append(_make_chunk(buffer, "json", "json"))
            buffer = stripped
        else:
            buffer = candidate

    if buffer:
        chunks.append(_make_chunk(buffer, "json", "json"))

    return chunks or [_make_chunk(content, "json", "json")]


def _chunk_yaml(content: str) -> list[Chunk]:
    """YAML: parse to dict, split via JSON splitter."""
    import yaml

    docs = list(yaml.safe_load_all(content))
    # Filter None docs (empty YAML documents produce None)
    docs = [d for d in docs if d is not None]

    if not docs:
        return [_make_chunk(content, "yaml", "yaml")]

    data = docs[0] if len(docs) == 1 else docs
    if not isinstance(data, (dict, list)):
        return [_make_chunk(content, "yaml", "yaml")]

    splitter = _make_json_splitter()
    texts = splitter.split_text(json_data=data)

    return [_make_chunk(t, "yaml", "yaml") for t in texts]


def _chunk_toml(content: str) -> list[Chunk]:
    """TOML: parse to dict, split via JSON splitter."""
    import tomllib

    data = tomllib.loads(content)
    splitter = _make_json_splitter()
    texts = splitter.split_text(json_data=data)

    return [_make_chunk(t, "toml", "toml") for t in texts]


def _chunk_html(content: str) -> list[Chunk]:
    """HTML: semantic-preserving split (tables, lists, code stay intact)."""
    from langchain_text_splitters import HTMLSemanticPreservingSplitter

    headers = [("h1", "Header 1"), ("h2", "Header 2"), ("h3", "Header 3")]
    splitter = HTMLSemanticPreservingSplitter(
        headers_to_split_on=headers,
        max_chunk_size=TARGET_CHARS,
        elements_to_preserve=["table", "ul", "ol", "code", "pre"],
        separators=["\n\n", "\n", ". ", "! ", "? "],
    )
    docs = splitter.split_text(content)

    return [_make_chunk(doc.page_content, "html", "html") for doc in docs]


def _chunk_xml(content: str) -> list[Chunk]:
    """XML: split with HTML-style separators (XML ≈ HTML structure)."""
    from langchain_text_splitters import Language, RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter.from_language(
        language=Language.HTML,
        chunk_size=TARGET_CHARS,
        chunk_overlap=0,
    )
    texts = splitter.split_text(content)

    return [_make_chunk(t, "xml", "xml") for t in texts]


def _chunk_latex(content: str) -> list[Chunk]:
    """LaTeX: structure-aware splitting (\\section, \\subsection, etc.)."""
    from langchain_text_splitters import Language, RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter.from_language(
        language=Language.LATEX,
        chunk_size=TARGET_CHARS,
        chunk_overlap=0,
    )
    texts = splitter.split_text(content)

    return [_make_chunk(t, "latex", "latex") for t in texts]


def _chunk_rst(content: str) -> list[Chunk]:
    """reStructuredText: structure-aware splitting."""
    from langchain_text_splitters import Language, RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter.from_language(
        language=Language.RST,
        chunk_size=TARGET_CHARS,
        chunk_overlap=0,
    )
    texts = splitter.split_text(content)

    return [_make_chunk(t, "rst", "rst") for t in texts]


def _chunk_js_framework(content: str, language: str) -> list[Chunk]:
    """JS framework components: understands template/script/style sections.

    Handles React (.jsx/.tsx), Vue (.vue), Svelte (.svelte), and Astro (.astro)
    component files with framework-aware separators that respect component
    boundaries (e.g. <template>, <script>, <style> sections in Vue/Svelte).
    Falls back to tree-sitter CodeChunker if the framework splitter fails.
    """
    from langchain_text_splitters import JSFrameworkTextSplitter

    framework_map = {
        "jsx": "jsx",
        "tsx": "tsx",
        "vue": "vue",
        "svelte": "svelte",
        "astro": "astro",
    }
    framework = framework_map.get(language, "jsx")

    try:
        splitter = JSFrameworkTextSplitter(
            framework=framework,
            chunk_size=TARGET_CHARS,
            chunk_overlap=0,
        )
        texts = splitter.split_text(content)
        chunks = [_make_chunk(t, "component", language) for t in texts]
        return chunks if chunks else _chunk_code(content, language)
    except Exception:
        return _chunk_code(content, language)


def _chunk_code(content: str, language: str) -> list[Chunk]:
    """Code: AST-based splitting via tree-sitter (165+ languages)."""
    ts_lang = _LANG_TO_TREESITTER.get(language)
    if not ts_lang:
        return _chunk_fallback(content, language)

    chunker = _get_code_chunker(ts_lang)
    results = chunker.chunk(content)
    chunks = _chonkie_to_chunks(results, "code", language)

    return chunks if chunks else _chunk_fallback(content, language)


def _chunk_prose(content: str) -> list[Chunk]:
    """Prose/text: semantic chunking groups related content together."""
    chunker = _get_semantic_chunker()
    results = chunker.chunk(content)
    chunks = _chonkie_to_chunks(results, "semantic", "text")

    return chunks if chunks else [_make_chunk(content, "text", "text")]


def _chunk_fallback(content: str, language: str) -> list[Chunk]:
    """Fallback: fixed-size token chunking."""
    chunker = _get_token_chunker()
    results = chunker.chunk(content)
    chunks = _chonkie_to_chunks(results, "block", language)

    return chunks if chunks else [_make_chunk(content, "block", language)]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def chunk_file(content: str, path: Path) -> list[Chunk]:
    """Chunk a file using the best strategy for its type.

    Routing:
     1. Markdown        → MarkdownHeaderTextSplitter + RecursiveCharacterTextSplitter
     2. JSON/JSONC/JSON5 → RecursiveJsonSplitter (structure-aware)
     3. JSONL           → Line-based grouping
     4. YAML            → Parse to dict → RecursiveJsonSplitter
     5. TOML            → Parse to dict → RecursiveJsonSplitter
     6. HTML            → HTMLSemanticPreservingSplitter
     7. XML/XSL/XSLT    → RecursiveCharacterTextSplitter(Language.HTML)
     8. LaTeX           → RecursiveCharacterTextSplitter(Language.LATEX)
     9. RST             → RecursiveCharacterTextSplitter(Language.RST)
    10. Code (165+ langs) → Chonkie CodeChunker (tree-sitter AST)
    11. Prose/Text       → Chonkie SemanticChunker
    12. Fallback         → Chonkie TokenChunker
    """
    # Early return for empty files
    if not content or not content.strip():
        return []

    language = _detect_language(path)
    ext = path.suffix.lstrip(".").lower()

    # Small files: return as single chunk
    tokens = count_tokens(content)
    if tokens <= MIN_TOKENS_PER_CHUNK:
        return [_make_chunk(content, "block", language)]

    # Files that fit in one chunk: skip splitting overhead
    if tokens <= TARGET_TOKENS_PER_CHUNK:
        return [_make_chunk(content, "block", language)]

    # Large files: skip structure-aware parsing (too slow), use fast fallback
    if len(content) > LARGE_FILE_CHARS:
        log.info("Large file %s (%d chars) — using fast fallback", path, len(content))
        chunks = _chunk_fallback(content, language)
    else:
        try:
            chunks = _route(content, ext, language)
        except (MemoryError, RecursionError, SystemError) as e:
            # These should not be silently swallowed
            log.error("Critical error in chunker: %s", e)
            raise
        except Exception as exc:
            log.warning("Chunker failed for %s (%s): %s — using fallback", path, language, exc)
            chunks = _chunk_fallback(content, language)

    # Safety: enforce token limit on every chunk
    chunks = _enforce_token_limit(chunks)

    # Merge tiny fragments
    chunks = _merge_tiny(chunks)

    return chunks or [_make_chunk(content, "block", language)]


def _route(content: str, ext: str, language: str) -> list[Chunk]:
    """Route to the best chunker based on file type / extension."""
    match ext:
        case "md" | "mdx" | "markdown" | "mdown" | "mkd":
            return _chunk_markdown(content)
        case "json" | "jsonc" | "json5":
            return _chunk_json(content)
        case "jsonl":
            return _chunk_jsonl(content)
        case "yaml" | "yml":
            return _chunk_yaml(content)
        case "toml":
            return _chunk_toml(content)
        case "html" | "htm" | "xhtml":
            return _chunk_html(content)
        case "xml" | "xsl" | "xslt" | "plist":
            return _chunk_xml(content)
        case "tex" | "latex" | "ltx":
            return _chunk_latex(content)
        case "rst":
            return _chunk_rst(content)

    # JS framework components (template/script/style aware)
    if language in {"jsx", "tsx", "vue", "svelte", "astro"}:
        return _chunk_js_framework(content, language)

    # Code (anything with a tree-sitter mapping)
    if language not in _DOC_LANGUAGES:
        return _chunk_code(content, language)

    # Prose / text / unknown
    if language in {"text", "unknown"}:
        return _chunk_prose(content)

    # Shouldn't reach here, but handle gracefully
    return _chunk_fallback(content, language)


# ---------------------------------------------------------------------------
# Token-based splitting (used by _enforce_token_limit)
# ---------------------------------------------------------------------------


def split_by_tokens(text: str, language: str) -> list[Chunk]:
    """Split by token count — last-resort fallback for oversized chunks."""
    if not text:
        return []

    chunks: list[Chunk] = []
    estimated_chars = MAX_TOKENS_PER_CHUNK * CHARS_PER_TOKEN
    start = 0

    while start < len(text):
        end = min(start + estimated_chars, len(text))
        segment = text[start:end]

        tokens = count_tokens(segment)
        while tokens > MAX_TOKENS_PER_CHUNK and end > start + 100:
            end = start + (end - start) * 3 // 4
            segment = text[start:end]
            tokens = count_tokens(segment)

        # Try to break at a natural boundary
        if end < len(text):
            search_start = max(start, end - 100)
            for i in range(end - 1, search_start - 1, -1):
                if text[i] in "\n ":
                    end = i + 1
                    segment = text[start:end]
                    break

        if segment.strip():
            chunks.append(_make_chunk(segment, "block", language))

        start = end

    return chunks
