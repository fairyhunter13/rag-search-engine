"""The chunker's boundaries, and the two-column lexical contract."""

from __future__ import annotations

import itertools

import pytest

from coderag.chunk import chunk_text
from coderag.lexical import fts_query, identifier_tokens, split_identifier


@pytest.mark.parametrize(
    "word,parts",
    [
        ("parseUserConfig", ["parse", "user", "config"]),
        ("parse_user_config", ["parse", "user", "config"]),
        ("ParseUserConfig", ["parse", "user", "config"]),
        ("HTTPServer", ["http", "server"]),
        ("parseHTTPResponse", ["parse", "http", "response"]),
        ("pkg.Method", ["pkg", "method"]),
        ("$userName", ["user", "name"]),
        ("user2Config", ["user2", "config"]),
    ],
)
def test_identifiers_split_into_their_parts(word, parts):
    assert split_identifier(word) == parts


@pytest.mark.parametrize("word", ["user", "config", "x", "CONFIG"])
def test_a_single_word_yields_no_parts(word):
    """Repeating it would double its term frequency and bias BM25 toward
    chunks dense in short names."""
    assert split_identifier(word) == []


def test_identifier_tokens_dedupes_repeats():
    text = "getUserName(); getUserName(); getUserName();"
    assert identifier_tokens(text).split() == ["get", "user", "name"]


def test_fts_query_carries_both_the_whole_term_and_its_parts():
    q = fts_query("parseUserConfig")
    assert '"parseuserconfig"' in q
    assert '"user"' in q and '"config"' in q


def test_fts_query_ors_rather_than_ands():
    """One unmatched word in a five-word question should cost rank, not
    eliminate the chunk."""
    assert " OR " in fts_query("read the user config")
    assert " AND " not in fts_query("read the user config")


@pytest.mark.parametrize("hostile", ['a" OR "b', "NOT x", "a AND b", "col:val", "a*", "^x", "-"])
def test_fts_syntax_in_a_user_query_is_neutralised(hostile):
    """Unquoted, FTS5 treats these as syntax: a colon raises and a bare NOT
    silently returns the wrong rows. A user must never need to know FTS5
    exists."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE f USING fts5(text, tokens)")
    conn.execute("INSERT INTO f VALUES('alpha beta', 'alpha beta')")
    q = fts_query(hostile)
    if q:
        conn.execute("SELECT COUNT(*) FROM f WHERE f MATCH ?", (q,)).fetchone()


def test_an_empty_query_is_empty_not_broken():
    assert fts_query("   ") == ""
    assert fts_query("!!!") == ""


# ------------------------------------------------------------------ chunking


def _lines(n, width=40):
    return "\n".join(f"x{i:03d}" + "y" * width for i in range(n))


def test_the_chunks_reconstruct_the_file_and_their_ranges_resolve():
    """Two claims, and the second is the one the engine's output rests on.

    `trim=False` means concatenation is lossless, so nothing is dropped between
    chunks. And every `lines` range this returns is opened by an agent, so a
    range that does not resolve to its own body is a result that reads exactly
    like a working one.
    """
    text = _lines(200)
    chunks = chunk_text(text, size=300, overlap=0)
    lines = text.splitlines()

    assert "".join(c.text for c in chunks) == text
    assert chunks[0].start_line == 1 and chunks[-1].end_line == 200
    for c in chunks:
        assert c.text.strip("\n") == "\n".join(lines[c.start_line - 1 : c.end_line])


def test_windows_overlap_and_always_advance():
    chunks = chunk_text(_lines(200), size=300, overlap=60)
    starts = [c.start_line for c in chunks]

    assert starts == sorted(starts) and len(set(starts)) == len(starts), "windows must advance"
    assert any(a.end_line >= b.start_line for a, b in itertools.pairwise(chunks))


def test_a_line_heavier_than_the_whole_budget_still_terminates():
    """Minified survivors and long string literals hit this constantly; the
    naive step-back loops forever."""
    text = "short\n" + "z" * 5000 + "\nshort\n"
    chunks = chunk_text(text, size=100, overlap=30)

    assert chunks and chunks[-1].end_line == 3


def test_weight_ignores_indentation():
    """Otherwise the same function chunks differently depending on nesting."""
    flat = chunk_text("\n".join("abcd" for _ in range(50)), size=40, overlap=10)
    nested = chunk_text("\n".join(" " * 24 + "abcd" for _ in range(50)), size=40, overlap=10)
    assert len(flat) == len(nested)


def test_empty_and_whitespace_only_input_yield_no_chunks():
    """A blank file has nothing to retrieve, and an empty chunk still costs a
    row, a vector and a slot in every candidate set it lands in."""
    assert chunk_text("") == []
    assert chunk_text("\n\n\n", size=100) == []
    assert chunk_text("   \t\n  \n") == []


def test_overlap_must_be_smaller_than_the_window():
    with pytest.raises(ValueError, match="must be smaller"):
        chunk_text("a\nb\n", size=10, overlap=10)


def test_line_numbers_survive_non_ascii():
    """The gate that would otherwise ship silently wrong.

    Character offsets and byte offsets are identical on ASCII, so an all-ASCII
    fixture passes with the two units confused. The symptom downstream is line
    numbers that drift by a few lines, and only in files with accents in them.
    """
    text = "# hello\n# héllo wörld \U0001f600\n\n\ndef target():\n    return 'café'\n"
    chunks = chunk_text(text, size=12, overlap=0, header=False)

    assert "".join(c.text for c in chunks) == text
    hit = next(c for c in chunks if "def target" in c.text)
    assert hit.start_line == 5, "the emoji line must count as one line, not four bytes' worth"


# ------------------------------------------------------------- the scope header


SOURCE = (
    "import jwt\n"
    "from .models import User\n"
    "\n\n"
    "class SessionStore:\n"
    "    def get(self, key):\n"
    "        return self._redis.get(key)\n"
    "\n\n"
    "    def put(self, key, value):\n"
    "        return self._redis.set(key, value)\n"
)


def test_the_header_carries_path_imports_and_the_enclosing_declaration():
    """Finding 8's whole claim: a chunk taken from the middle of a class knows
    which class it is in, without a parser."""
    chunks = chunk_text(SOURCE, rel_path="src/auth/session.py", size=20)
    tail = chunks[-1]

    assert "src/auth/session.py" in tail.header
    assert "import jwt" in tail.header and "from .models import User" in tail.header
    assert "in: class SessionStore" in tail.header


def test_the_header_reaches_the_embedder_and_never_the_stored_body():
    """Both directions, or it is decoration. A header that leaks into the body
    shows up in every preview and inflates every BM25 score with the same path
    terms; one that never reaches the embedder is a no-op nothing would notice.
    """
    chunk = chunk_text(SOURCE, rel_path="src/auth/session.py", size=20)[-1]

    assert chunk.embed_text.startswith("src/auth/session.py")
    assert chunk.text in chunk.embed_text
    assert "src/auth/session.py" not in chunk.text


def test_the_header_is_switchable_off():
    chunk = chunk_text(SOURCE, rel_path="src/auth/session.py", size=20, header=False)[-1]
    assert chunk.header == "" and chunk.embed_text == chunk.text


def test_a_file_with_no_declarations_still_gets_a_header():
    """The path alone is the floor. A config file has no enclosing scope, and a
    header that collapses to nothing loses the one signal that always exists."""
    chunk = chunk_text("a = 1\nb = 2\n", rel_path="settings.py")[0]
    assert chunk.header == "settings.py"


def test_chunk_hashes_are_content_addressed():
    a = chunk_text(_lines(50), size=200, overlap=40)
    b = chunk_text(_lines(50), size=200, overlap=40)
    assert [c.sha256 for c in a] == [c.sha256 for c in b]
