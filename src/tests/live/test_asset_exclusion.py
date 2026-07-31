"""Live tests: text-encoded images never reach the index.

Provenance (measured 2026-07-31, whole fleet): 9,173 `.svg` and `.drawio` files held **21,059
chunks — 4.96 % of the corpus**, the largest bucket after HTML. Their content is geometry: path
data, transform matrices, base64 blobs. Nothing in it answers a question about code, and every
chunk of it competes for a place in a `search` result with a chunk that could.

They reached the index because the rule that drops every *other* image cannot see them.
`_has_text_bytes` screens for a NUL byte, which is git's own binary test, and SVG really is
text — so it passes, a grammar parses it, and it is chunked and embedded like source.

ASSET1 — is_image_path() truth table: the XML image formats match, source never does.
ASSET2 — is_ignored_path() drops them, so watcher, indexer and drift gate agree by construction
         (they share `_should_drop`; testing the shared entry point is what makes that true for
         all three rather than for whichever one this test happened to call).
ASSET3 — an explicit RSE `include` still wins, matching where is_generated_path and
         is_secret_path sit in the decision order. Asserted so the precedence is on record.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def test_asset1_is_image_path_truth_table():
    """ASSET1: text-encoded images match; hand-written source never does."""
    from rag_search.index.discover import is_image_path

    images = [
        "logo.svg",
        "public/assets/icons/arrow-left.svg",
        "docs/ARCHITECTURE.SVG",          # case is a spelling, not a format
        "docs/diagrams/flow.drawio",
        "docs/diagrams/flow.drawio.xml",
    ]
    ordinary = [
        "src/svg.py",                     # 'svg' as the whole stem, not the extension
        "src/components/SvgIcon.tsx",
        "docs/drawio.md",                 # a document *about* the format
        "public/logo.png",                # a real binary image; _has_text_bytes owns this one
        "src/main.go",
    ]
    for p in images:
        assert is_image_path(p), f"{p} should be treated as a text-encoded image"
    for p in ordinary:
        assert not is_image_path(p), f"{p} should NOT be treated as a text-encoded image"


def test_asset2_is_ignored_path_drops_text_encoded_images(safe_tmp_path):
    """ASSET2: discovery's shared decision order drops them, so all three consumers agree."""
    from rag_search.index.discover import is_ignored_path

    root = safe_tmp_path
    (root / "public").mkdir()
    dropped = root / "public" / "logo.svg"
    dropped.write_text('<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h24v24H0z"/></svg>\n')
    kept = root / "public" / "app.js"
    kept.write_text("export const x = 1;\n")

    assert is_ignored_path(dropped, root), (
        f"{dropped} reached the index — see the module docstring for the 21,059-chunk "
        "measurement. `_has_text_bytes` cannot catch this: SVG is genuinely text."
    )
    assert not is_ignored_path(kept, root), (
        "the exclusion took an ordinary source file with it — it must match the XML image "
        "formats only"
    )


def test_asset3_explicit_include_still_wins(safe_tmp_path):
    """ASSET3: an explicit RSE `include` overrides the drop, as it does for the other two.

    Pins the precedence rather than the convenience. All three subtractive rules — generated,
    secret, image — sit below `cfg.include` on one argument: an include is the user naming a file
    in their own repo, and silently ignoring that is the more surprising failure. If any of them
    ever needs to become absolute, it should change here first, deliberately.
    """
    from rag_search.core.index_config import ProjectConfig
    from rag_search.index.discover import is_ignored_path

    root = safe_tmp_path
    (root / "docs").mkdir()
    target = root / "docs" / "architecture.svg"
    target.write_text('<svg xmlns="http://www.w3.org/2000/svg"><title>arch</title></svg>\n')

    cfg = ProjectConfig(include=["docs/architecture.svg"])
    assert not is_ignored_path(target, root, cfg), (
        "an explicit include did not override the image drop — the decision order no longer "
        "matches is_generated_path's and is_secret_path's, which sit directly above it"
    )
