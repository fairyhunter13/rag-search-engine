"""The scope header dispatches on file type, and each arm has to earn its line.

Written as four arms plus one guard-mutation check, because the failure this
file exists to catch is not "the header is empty" -- it is the header being
*confidently wrong*, which is the hardest kind of distractor to retrieve past.
"""

import pytest

from coderag import filters
from coderag.chunk import chunk_text

MARKDOWN = """# coderag

A retrieval engine.

## Per-project config

Each root may carry a `.coderag.toml`.

### Excludes

An exclude reaches every member of the federation.
"""

YAML = """name: CI
on:
  push:
    branches: ["main"]
jobs:
  quality:
    runs-on: ubuntu-latest
"""

TOML = """[tool.ruff]
line-length = 100

[tool.ruff.lint]
select = ["E", "F"]
"""


def _in(chunk):
    """The `in:` value, or None. The header is newline-joined, path first."""
    for line in chunk.header.splitlines():
        if line.startswith("in: "):
            return line[4:]
    return None


def test_a_prose_chunk_names_its_enclosing_heading_chain():
    """The doc analogue of `in: class SessionStore`: which section am I in."""
    tail = chunk_text(MARKDOWN, rel_path="README.md", size=40)[-1]
    assert _in(tail) == "coderag > Per-project config > Excludes"


def test_the_heading_chain_keeps_ancestors_and_drops_siblings():
    """A walk that kept every heading above the chunk would read
    `Excludes > Per-project config > coderag` on the *second* section too."""
    chunks = chunk_text(MARKDOWN, rel_path="README.md", size=40)
    mid = next(c for c in chunks if "`.coderag.toml`" in c.text)
    assert _in(mid) == "coderag > Per-project config"


def test_a_prose_header_is_never_built_from_the_declaration_regex():
    """The guard, and the reason the dispatch exists rather than a patch.

    `_DECL`'s generic C-family arm matches any line ending in a parenthetical,
    so this heading used to be inherited as `in:` by chunks in a later,
    unrelated section. Asserting a heading chain is not enough on its own --
    the wrong answer is also heading-shaped here -- so the chunk that must not
    borrow it is the one taken from the section *after* it.
    """
    text = "# Five profiles (shared, work, red, blue, ops)\n\nx\n\n# Storage\n\ny\n"
    tail = chunk_text(text, rel_path="docs/design.md", size=4)[-1]
    assert _in(tail) == "Storage"
    assert "profiles" not in tail.header


def test_a_yaml_chunk_names_its_enclosing_key_path():
    tail = chunk_text(YAML, rel_path=".github/workflows/ci.yml", size=20)[-1]
    assert _in(tail) == "jobs.quality.runs-on"


def test_a_toml_key_is_qualified_by_its_table():
    """TOML is the one dialect where a column-0 key is not the top. Stopping the
    indent walk at column 0 -- correct for YAML and JSON -- reported
    `in: line-length`, which is true of half the config files on the machine.
    """
    hit = next(c for c in chunk_text(TOML, rel_path="pyproject.toml", size=8) if "100" in c.text)
    assert _in(hit) == "tool.ruff.line-length"


def test_the_code_arm_is_unchanged_and_still_gets_imports():
    """The dispatch is additive. A regression here means prose handling was
    bought with code recall, which is 75% of this corpus."""
    src = "import os\n\n\ndef target(a):\n    return a\n"
    tail = chunk_text(src, rel_path="src/x.py", size=6)[-1]
    assert "imports: import os" in tail.header
    assert _in(tail) == "def target(a):"


@pytest.mark.parametrize("rel", ["notes.zig", "main.mojo", "Makefile"])
def test_an_unlabeled_extension_falls_to_the_code_arm(rel):
    """`indexable()` is a denylist, so a language nobody has heard of is indexed
    today with no change. It gets the generic declaration regex for free, and
    degrades to the path -- which is why this asserts the floor, not a value."""
    assert filters.lang_of(rel) not in filters.DOC_LANGS | filters.DATA_LANGS
    chunk = chunk_text("fn main() {\n    ok();\n}\n", rel_path=rel, size=6)[-1]
    assert chunk.header.startswith(rel)


def test_every_dispatch_arm_still_names_the_path_first():
    """The path is the one signal that always exists. An arm that returns an
    empty chain must collapse to the path, never to an empty header."""
    for rel, text in [("a.md", "no headings here\n"), ("b.yaml", "- 1\n"), ("c.py", "pass\n")]:
        assert chunk_text(text, rel_path=rel, size=200)[0].header == rel
