"""Strict parsing, and the union that replaced an order-dependent lookup."""

from __future__ import annotations

import pytest

from coderag import config
from coderag.projcfg import ConfigError, ProjectConfig, effective, load, parse

GOOD = """
[index]
exclude = ["wiki/*", "*.min.js"]
respect_gitignore = true

[federation]
exclude = ["**/legacy-mirror"]
"""


def test_parses_the_documented_shape():
    cfg = parse(GOOD)
    assert cfg.exclude == ("wiki/*", "*.min.js")
    assert cfg.federation_exclude == ("**/legacy-mirror",)
    assert cfg.use_default_ignores is True


def test_a_missing_file_is_defaults_not_an_error(tmp_path):
    assert load(tmp_path) == ProjectConfig()


def test_a_typo_names_the_nearest_key():
    """A silently ignored exclude typo is the 70.9% failure."""
    with pytest.raises(ConfigError, match="did you mean 'exclude'"):
        parse('[index]\nexlcude = ["a"]\n')


def test_a_retired_key_says_so_rather_than_guessing():
    with pytest.raises(ConfigError, match=r"retired key, now index\.exclude"):
        parse('[index]\npatterns = ["a"]\n')


def test_an_unknown_section_is_rejected():
    with pytest.raises(ConfigError, match=r"unknown section \[indexing\]"):
        parse('[indexing]\nexclude = ["a"]\n')


@pytest.mark.parametrize(
    "text,match",
    [
        ('[index]\nexclude = "wiki/*"\n', "must be a list of strings"),
        ("[index]\nexclude = [1, 2]\n", "must be a list of strings"),
        ('[index]\nrespect_gitignore = "yes"\n', "must be true or false"),
        ("[index]\nexclude = [\n", "source"),
    ],
)
def test_wrong_types_are_rejected_with_the_reason(text, match):
    with pytest.raises(ConfigError, match=match):
        parse(text, source="source")


def _write(path, text):
    path.mkdir(parents=True, exist_ok=True)
    (path / config.PROJECT_CONFIG_NAME).write_text(text)


def test_a_member_with_no_file_inherits_its_root(tmp_path):
    """Asserted on a pattern the root owns, so dropping the union fails it."""
    root = tmp_path / "root"
    _write(root, '[index]\nexclude = ["public/assets/plugins/*"]\n')
    member = tmp_path / "member"
    member.mkdir()

    assert effective(member, [str(root)]).exclude == ("public/assets/plugins/*",)


def test_two_roots_union_rather_than_first_one_winning(tmp_path):
    """The old code took the first registry match, so the answer depended on
    which row happened to be written first. Both orders must agree."""
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a, '[index]\nexclude = ["from_a/*"]\n')
    _write(b, '[index]\nexclude = ["from_b/*"]\n')
    member = tmp_path / "member"
    member.mkdir()

    forward = effective(member, [str(a), str(b)]).exclude
    backward = effective(member, [str(b), str(a)]).exclude
    assert set(forward) == {"from_a/*", "from_b/*"}
    assert forward == backward, "the union must not depend on registry order"


def test_a_members_own_file_adds_to_the_union_rather_than_replacing_it(tmp_path):
    root = tmp_path / "root"
    _write(root, '[index]\nexclude = ["from_root/*"]\n')
    member = tmp_path / "member"
    _write(member, '[index]\nexclude = ["from_member/*"]\n')

    assert set(effective(member, [str(root)]).exclude) == {"from_root/*", "from_member/*"}


def test_scalars_take_the_conservative_value(tmp_path):
    root = tmp_path / "root"
    _write(root, "[index]\nrespect_gitignore = true\n")
    member = tmp_path / "member"
    _write(member, "[index]\nrespect_gitignore = false\n")

    assert effective(member, [str(root)]).respect_gitignore is True


def test_a_broken_root_config_does_not_make_its_members_unindexable(tmp_path):
    root = tmp_path / "root"
    _write(root, "[index]\nexclude = [\n")
    member = tmp_path / "member"
    _write(member, '[index]\nexclude = ["mine/*"]\n')

    assert effective(member, [str(root)]).exclude == ("mine/*",)


def test_a_root_never_inherits_from_itself(tmp_path):
    root = tmp_path / "root"
    _write(root, '[index]\nexclude = ["x/*"]\n')
    assert effective(root, [str(root)]).exclude == ("x/*",)


def test_the_signature_moves_with_the_excludes_and_not_with_federation():
    base = parse('[index]\nexclude = ["a"]\n').signature()
    assert parse('[index]\nexclude = ["a", "b"]\n').signature() != base
    assert parse('[index]\nexclude = ["a"]\n[federation]\nexclude = ["z"]\n').signature() == base


def test_the_signature_ignores_pattern_order():
    """Order churn would reconcile every project on an unrelated edit."""
    assert parse('[index]\nexclude = ["a","b"]\n').signature() == (
        parse('[index]\nexclude = ["b","a"]\n').signature()
    )
