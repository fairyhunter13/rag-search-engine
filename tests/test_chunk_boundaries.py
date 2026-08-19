"""Three claims the chunker's docstring makes and no test checked.

`chunk.py` describes a boundary ladder, a character-offset contract and a
2,000-unit budget. The first is the splitter's behaviour and was taken on
trust; the second was covered only by concatenation, which passes with the
offsets uniformly wrong; the third was never exercised at its real value --
every existing test passes a small `size=` so the chunker splits at all.
"""

from __future__ import annotations

from coderag import config
from coderag.chunk import chunk_text, nonwhitespace

# Six three-line paragraphs, blank-line separated, and a budget with room for a
# fourth line. That gap is the whole fixture: a splitter that only knew about
# line breaks would spend the slack and cut inside the next paragraph.
PARAGRAPHS = ["".join(f"paragraph {i} line {j}\n" for j in range(3)) for i in range(6)]
PROSE = "\n".join(PARAGRAPHS)
BUDGET = nonwhitespace(PARAGRAPHS[0]) * 4 // 3 + 2


def test_a_blank_line_outranks_a_line_break_even_with_budget_left_over():
    """The ladder's top rung, asserted rather than described.

    The obvious version of this test does not discriminate: with a budget of
    two whole paragraphs, cutting on line breaks lands on a paragraph boundary
    anyway and the assertion passes against a splitter that never knew blank
    lines existed. So the budget here deliberately fits one paragraph plus a
    line, and the claim is that the splitter leaves that line on the table.
    """
    chunks = chunk_text(PROSE, size=BUDGET, overlap=0, header=False)

    assert nonwhitespace(PARAGRAPHS[0]) < BUDGET, "no slack means nothing is being chosen"
    assert [c.text.strip() for c in chunks] == [p.strip() for p in PARAGRAPHS]


def test_every_chunk_sits_at_the_character_offset_its_line_number_claims():
    """The assertion the non-ASCII gate was named for.

    Concatenating the chunks reconstructs the file whether or not the offsets
    are right -- that check passes with `start_line` uniformly off by any
    amount. This one walks the file to the offset each chunk's line number
    implies and demands the chunk be there, which is what the emoji breaks if
    character and byte offsets are confused.
    """
    text = "# hello\n# héllo wörld \U0001f600\n\n\ndef target():\n    return 'café'\n\n\ntail = 1\n"
    chunks = chunk_text(text, size=12, overlap=0, header=False)

    offset = 0
    for chunk in chunks:
        assert text[offset:].startswith(chunk.text)
        assert text.count("\n", 0, offset) + 1 == chunk.start_line
        offset += len(chunk.text)
    assert offset == len(text)


def test_the_shipped_budget_is_two_thousand_and_it_is_what_chunks_a_real_file():
    """Every other test here passes a small `size=`, so the default was a
    number in a config file rather than a value anything ran under. The unit is
    non-whitespace characters, not bytes and not tokens."""
    assert config.CHUNK_CHARS == 2000

    text = "".join(f"def f{i}():\n    return {i}\n\n\n" for i in range(400))
    chunks = chunk_text(text, header=False)

    assert len(chunks) > 1
    assert max(nonwhitespace(c.text) for c in chunks) <= config.CHUNK_CHARS
