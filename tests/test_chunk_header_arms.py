"""The header's two components ablate independently.

`CHUNK_HEADER=0` used to be the only arm, and it removed the path and the
derived line together. On the docs corpus 48.6% of eval queries have their
heading echoed in the positive's filename, so that arm scored the derived line
and a filename identity shortcut as one number -- and the derived line is the
half with no published result behind it.
"""

from __future__ import annotations

import pytest

from coderag import config
from coderag.chunk import chunk_text, scope_header

CODE = ["import os", "", "def handler(req):", "    return req", "    x = 1"]
DOC = ["# Federation", "", "## Excludes", "", "Inherited from the root."]


@pytest.fixture
def arm(monkeypatch):
    def set_arm(path: bool, derived: bool):
        monkeypatch.setattr(config, "CHUNK_HEADER_PATH", path)
        monkeypatch.setattr(config, "CHUNK_HEADER_DERIVED", derived)

    return set_arm


def test_path_only_drops_every_derived_line(arm):
    """Every arm of the dispatch, not just the code one: a doc file's heading
    chain is derived too, and a suppression that only covered `_decl_at` would
    read as working on the corpus it was written against."""
    arm(True, False)
    assert scope_header("src/a.py", CODE, 3) == "src/a.py"
    assert scope_header("docs/a.md", DOC, 5) == "docs/a.md"


def test_derived_only_drops_the_path(arm):
    """The path is the identity shortcut. Removing it while keeping the derived
    line is the cell that says what the derived line is worth on its own."""
    arm(False, True)
    assert scope_header("src/a.py", CODE, 3) == "imports: import os\nin: def handler(req):"
    assert scope_header("docs/a.md", DOC, 5) == "in: Federation > Excludes"


def test_both_off_emits_no_header_at_all(arm):
    """Not an empty first line. `chunk_text` decides whether to call
    `scope_header`, and a header of "\\n" would still be a token the embedder
    sees and the FTS lane indexes."""
    arm(False, False)
    assert scope_header("src/a.py", CODE, 3) == ""
    assert chunk_text("\n".join(CODE), rel_path="src/a.py")[0].header == ""


def test_the_default_is_both_components(arm):
    """The 2x2's fourth cell, asserted so a default flipped in config fails here
    rather than silently re-basing every arm against a different baseline."""
    arm(True, True)
    assert (
        scope_header("src/a.py", CODE, 3) == "src/a.py\nimports: import os\nin: def handler(req):"
    )
    assert config.CHUNK_HEADER_PATH and config.CHUNK_HEADER_DERIVED
