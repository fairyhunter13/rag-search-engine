"""The knowledge bundle is gated by the only gate this repo has.

Fail-closed on a missing checker, deliberately. There is no CI here and no git
hook: if a missing `okf` binary made this skip, the bundle's two possible
outcomes would be pass and silence, which is the same thing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BUNDLE = REPO / "knowledge"

# Pinned: `@latest` lets the verdict change with no commit in this repo.
INSTALL = "go install github.com/fairyhunter13/okf/cmd/okf@v0.1.0"


def test_the_checker_is_installed():
    """Named separately so a missing binary reads as a missing tool, not as a
    broken bundle."""
    assert shutil.which("okf"), f"okf is not on PATH; the bundle is ungated. {INSTALL}"


def test_the_bundle_passes_okf_check():
    okf = shutil.which("okf")
    if okf is None:
        pytest.fail(f"okf is not on PATH; the bundle is ungated. {INSTALL}")
    out = subprocess.run([okf, "check", str(BUNDLE)], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stdout + out.stderr


def test_every_link_between_concepts_resolves():
    """`okf check` treats a dangling link as a warning -- a forward reference to
    a concept not yet written is legal. That is right for the format and wrong
    for a bundle being reported as finished, so the strict half lives here."""
    broken = []
    for concept in sorted(BUNDLE.rglob("*.md")):
        for line in concept.read_text().splitlines():
            for target in _links(line):
                if not (concept.parent / target).resolve().exists():
                    broken.append(f"{concept.relative_to(REPO)} -> {target}")
    assert broken == [], broken


def test_every_resource_a_concept_names_exists():
    """A concept whose `resource:` points at a deleted module is the failure
    mode that deleted the last bundle: 31 files describing code that was gone."""
    missing = []
    for concept in sorted(BUNDLE.rglob("*.md")):
        for line in concept.read_text().splitlines():
            if line.startswith("resource:"):
                for target in line.split(":", 1)[1].strip().split(","):
                    if target.strip() and not (REPO / target.strip()).exists():
                        missing.append(f"{concept.relative_to(REPO)}: {target.strip()}")
    assert missing == [], missing


def test_the_checker_actually_rejects_something(tmp_path):
    """Without this, the three tests above pass just as well against an `okf`
    that exits 0 on everything and a bundle directory that is empty."""
    okf = shutil.which("okf")
    if okf is None:
        pytest.fail(f"okf is not on PATH; the bundle is ungated. {INSTALL}")
    bad = tmp_path / "knowledge"
    bad.mkdir()
    (bad / "index.md").write_text("no frontmatter, no okf_version\n")
    (bad / "broken.md").write_text("---\ntitle: no type key\n---\n\nbody\n")
    out = subprocess.run([okf, "check", str(bad)], capture_output=True, text=True, check=False)
    assert out.returncode != 0, "okf accepted a bundle with no type key"


def _links(line: str) -> list[str]:
    """Markdown link targets that point at a file in the bundle."""
    out = []
    rest = line
    while "](" in rest:
        rest = rest.split("](", 1)[1]
        target = rest.split(")", 1)[0]
        if target.endswith(".md") and not target.startswith(("http", "#", "/")):
            out.append(target)
    return out
