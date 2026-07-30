"""RB1-RB6 — malformed input degrades to a *recorded* outcome, never a silent zero or a hang.

Part 4 of W1-A's audit apparatus. Every case here is a real shape from the fleet or a real
incident, not an invented adversarial input:

- RB1 wrong-language bytes — a Katalon project ships 2,439 `.rs`/`.ts` files that are XML.
- RB2 truncation — a file read mid-write, or a git checkout interrupted.
- RB3 deep nesting — `process()` **segfaults** (exitcode -11) on a 10,000-deep javascript
  expression, measured 2026-07-30; 5,000 is fine and raw `parse()` survives 10,000, so it is
  `process()` alone. HR39 contained it, the pool respawned, the next file extracted normally.
- RB4 minified — one line, no whitespace: the shape of every shipped bundle.
- RB5 NUL bytes — 16 chunks in the fleet carry them.
- RB6 mixed encodings — latin-1 bytes in a file the pipeline decodes as utf-8.

The assertion is deliberately **not** "no exception raised". A rung named `error` is a pass here,
because the property under test is that the outcome is *recorded* — the whole ladder exists so
that "0 symbols" stops being one undifferentiated symptom. What fails is an outcome the record
cannot name, or a claim of extraction that did not happen.

These call the extractor in-process rather than through `run_bounded`. The bounded-parse worker's
own containment — kill, respawn, `parse_crash_count` — is `test_bounded_parse.py` BP1-BP4's job;
duplicating it here would run a spawn-context pool per case under a 1-core quota for no new
information.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from rag_search.graph.extractor import EXTRACTION_RUNGS, extract_symbols_with_stats

pytestmark = pytest.mark.live

_XML = ('<?xml version="1.0" encoding="UTF-8"?>\n<WebElementEntity>\n'
        "  <name>btn</name>\n</WebElementEntity>\n")
_PY = "def top(a):\n    return helper(a)\n\n\ndef helper(b):\n    return b\n"


def _run(name: str, src: str, lang: str):
    """Extract, and assert the outcome is one the record can name. Returns `(symbols, stats)`."""
    syms, st = extract_symbols_with_stats(Path(name), src, lang)
    assert st.rung in EXTRACTION_RUNGS, f"{name}: unrecordable rung {st.rung!r}"
    assert st.symbol_count == len(syms), f"{name}: count {st.symbol_count} != {len(syms)}"
    assert st.language == lang or st.language == "unknown", f"{name}: language {st.language!r}"
    return syms, st


@pytest.mark.parametrize("lang", ["rust", "typescript", "go", "python", "java"])
def test_rb1_wrong_language_bytes_are_recorded_as_a_mismatch(lang: str) -> None:
    """RB1 — XML bytes under five different claimed languages, all recorded, none pretending.

    Parametrised across languages because the threshold must not be tuned to one grammar's error
    recovery: measured 2026-07-30, XML scores 0.916-1.000 as rust/typescript/python/go, and a
    grammar whose recovery happened to be gentler would fall through to `generic` and read as an
    empty file again — the exact defect this rung closes.
    """
    syms, st = _run(f"o.{lang[:2]}", _XML, lang)
    assert st.rung == "language_mismatch", f"{lang}: XML recorded as {st.rung}"
    assert not syms, f"{lang}: invented {len(syms)} symbols from XML"


@pytest.mark.parametrize("cut", [0.1, 0.5, 0.9])
def test_rb2_truncation_never_invents_a_symbol(cut: float) -> None:
    """RB2 — a file cut mid-token yields a subset of the whole file's symbols, never more.

    Subset, not equality: a definition the cut removed is legitimately gone. The direction that
    would be a defect is the other one — a truncated file producing a name the complete file does
    not, which is what a walk reading past a partial node looks like.
    """
    whole = {s.name for s in _run("t.py", _PY, "python")[0]}
    part = {s.name for s in _run("t.py", _PY[: int(len(_PY) * cut)], "python")[0]}
    assert part <= whole, f"truncation at {cut} invented {part - whole}"


def test_rb3_deep_nesting_extracts_without_a_recursion_error() -> None:
    """RB3 — D6. Every AST walk in the extractor is an explicit stack, so depth is bounded by the
    source rather than by CPython's frame limit.

    This was not theoretical: the `RecursionError` surfaced *out of the bounded-parse worker* as
    `PARSE_TIMEOUT`, so a file too deep to walk was indistinguishable from one too slow to parse,
    and `parse_timeout_count` corroborated the wrong story. 5,000 rather than 10,000 because
    `process()` itself segfaults at 10,000 — that is HR39's containment case (BP4), not this one,
    and conflating them is how the two were confused in the first place.
    """
    src = "function outer(){ return " + "f(" * 5000 + "1" + ")" * 5000 + " }\n"
    syms, st = _run("deep.js", src, "javascript")
    assert "outer" in {s.name for s in syms}, f"deep nesting lost the definition (rung={st.rung})"


def test_rb4_minified_source_still_extracts() -> None:
    """RB4 — one line, no whitespace: the shape of every shipped bundle.

    Also pins the negative half of X2 from the other side. Minified javascript scores an error
    ratio of 0.000, so it must never be called a language mismatch — a threshold that caught it
    would mislabel a large share of the fleet's `dist/` output.
    """
    src = "function a(b){return b*2}function c(d){return a(d)+1}var e=c(3);"
    syms, st = _run("m.js", src, "javascript")
    assert {"a", "c"} <= {s.name for s in syms}, [s.name for s in syms]
    assert st.rung != "language_mismatch", "minified javascript called a language mismatch"


def test_rb5_nul_bytes_do_not_stop_extraction() -> None:
    """RB5 — 16 chunks in the fleet carry NUL bytes; a file holding one must still extract.

    Measured error ratio 0.120 — well under the mismatch threshold — so the recorded outcome is
    an ordinary rung, and the definitions on either side of the NUL run survive.
    """
    syms, st = _run("n.py", "def before():\n    return 1\n\x00\x00\x00\ndef after():\n    return 2\n",
                    "python")
    assert {"before", "after"} <= {s.name for s in syms}, (
        f"NUL bytes cost a definition (rung={st.rung}): {[s.name for s in syms]}")


def test_rb6_non_utf8_bytes_survive_the_replace_decode() -> None:
    """RB6 — latin-1 bytes in a file the pipeline decodes as utf-8.

    `sweeps._extract_graph` reads with `errors="replace"`, so the extractor is handed U+FFFD where
    the undecodable bytes were. The property is that a replacement character inside a *string
    literal or comment* costs nothing: it is not part of any identifier, and a walk that measured
    byte offsets against the decoded text rather than the encoded bytes would drift from it here
    and mis-slice every name after the first replacement.
    """
    raw = "def café_handler():\n    s = 'éèê'  # accented\n    return s\n"
    src = raw.encode("latin-1").decode("utf-8", errors="replace")
    syms, st = _run("e.py", src, "python")
    assert len(syms) == 1, f"expected one definition, got {[s.name for s in syms]} (rung={st.rung})"
    assert syms[0].name.endswith("_handler"), syms[0].name
