"""The header is one component now, and it still has to ablate.

The derived line that used to be the second component measured flat on docs and
on code and was deleted. The path arm stays falsifiable here because it is the
half that pays -- -0.1233 recall@1 on code without it.
"""

from __future__ import annotations

import pytest

from coderag import config
from coderag.chunk import chunk_text, scope_header

CODE = ["import os", "", "def handler(req):", "    return req", "    x = 1"]


@pytest.fixture
def arm(monkeypatch):
    def set_arm(path: bool):
        monkeypatch.setattr(config, "CHUNK_HEADER_PATH", path)

    return set_arm


def test_the_header_is_the_path_and_carries_nothing_derived(arm):
    """No `imports:`, no `in:`, on a source that would have produced both. A
    deletion that left one arm of the old dispatch behind reads as working
    against the corpus it was written for."""
    arm(True)
    assert scope_header("src/a.py") == "src/a.py"
    assert chunk_text("\n".join(CODE), rel_path="src/a.py")[0].header == "src/a.py"


def test_off_emits_no_header_at_all(arm):
    """Not an empty first line. `chunk_text` decides whether to call
    `scope_header`, and a header of "\\n" would still be a token the embedder
    sees and the FTS lane indexes."""
    arm(False)
    assert scope_header("src/a.py") == ""
    assert chunk_text("\n".join(CODE), rel_path="src/a.py")[0].header == ""


def test_the_default_is_on():
    """Asserted so a default flipped in config fails here rather than silently
    re-basing every eval arm against a different baseline."""
    assert config.CHUNK_HEADER_PATH
