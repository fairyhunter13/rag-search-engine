"""OKF generator tests — GPU-free, RSE-free, no daemon required."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from okf.generate import OKF_VERSION, generate


@pytest.fixture
def synth_repo(tmp_path):
    root = tmp_path / "myproject"
    src = root / "src"
    src.mkdir(parents=True)
    (src / "main.py").write_text("def main(): pass\n")
    (src / "utils.py").write_text("def helper(): pass\n")
    lib = root / "lib"
    lib.mkdir()
    (lib / "models.py").write_text("class Model: pass\n")
    return root


class TestOKFBundle:
    def test_index_written(self, synth_repo, tmp_path):
        result = generate(synth_repo, out_dir=tmp_path / "okf")
        assert any("index.md" in p for p in result["written"])

    def test_log_written(self, synth_repo, tmp_path):
        result = generate(synth_repo, out_dir=tmp_path / "okf")
        assert any("log.md" in p for p in result["written"])

    def test_version_in_result(self, synth_repo, tmp_path):
        result = generate(synth_repo, out_dir=tmp_path / "okf")
        assert result["version"] == OKF_VERSION

    def test_project_name_in_result(self, synth_repo, tmp_path):
        result = generate(synth_repo, out_dir=tmp_path / "okf")
        assert result["project"] == "myproject"

    def test_index_frontmatter(self, synth_repo, tmp_path):
        out = tmp_path / "okf"
        generate(synth_repo, out_dir=out)
        content = (out / "index.md").read_text()
        assert f'okf_version: "{OKF_VERSION}"' in content
        assert "generated: true" in content

    def test_fragment_per_module(self, synth_repo, tmp_path):
        out = tmp_path / "okf"
        generate(synth_repo, out_dir=out)
        frags = list(out.glob("fragment_*.md"))
        assert len(frags) >= 2  # src + lib

    def test_fragment_frontmatter(self, synth_repo, tmp_path):
        out = tmp_path / "okf"
        generate(synth_repo, out_dir=out)
        for frag in out.glob("fragment_*.md"):
            content = frag.read_text()
            assert f'okf_version: "{OKF_VERSION}"' in content
            assert "type: component" in content

    def test_no_absolute_paths_in_output(self, synth_repo, tmp_path):
        out = tmp_path / "okf"
        generate(synth_repo, out_dir=out)
        home = str(Path.home())
        for md in out.glob("*.md"):
            assert home not in md.read_text(), f"Device path leaked in {md.name}"

    def test_idempotent(self, synth_repo, tmp_path):
        out = tmp_path / "okf"
        r1 = generate(synth_repo, out_dir=out)
        r2 = generate(synth_repo, out_dir=out)
        assert len(r2["written"]) == 0, "second run should produce no new writes"
        assert len(r2["skipped"]) == len(r1["written"])

    def test_no_errors_key(self, synth_repo, tmp_path):
        result = generate(synth_repo, out_dir=tmp_path / "okf")
        assert "written" in result and "skipped" in result and "version" in result

    def test_default_out_dir(self, synth_repo):
        """Default out_dir is <project>/docs/okf/."""
        result = generate(synth_repo)
        out = synth_repo / "docs" / "okf"
        assert out.exists()
        assert (out / "index.md").exists()
        assert len(result["written"]) > 0
