"""Strict parsing, and the union that replaced an order-dependent lookup."""

from __future__ import annotations

import pytest

from coderag import config
from coderag.projcfg import ConfigError, ProjectConfig, effective, load, parse

GOOD = """
index:
  exclude: ["wiki/*", "*.min.js"]
  respect_gitignore: true

federation:
  exclude: ["**/legacy-mirror"]
"""


def test_parses_the_documented_shape():
    cfg = parse(GOOD)
    assert cfg.exclude == ("wiki/*", "*.min.js")
    assert cfg.federation_exclude == ("**/legacy-mirror",)
    assert cfg.use_default_ignores is True


def test_the_signature_did_not_move_when_the_format_did():
    """The reason the TOML-to-YAML swap reindexed nothing.

    `signature()` hashes the *parsed* values, so this literal is the digest the
    TOML parser produced for the same config. A future change that starts
    hashing anything syntactic reconciles all 256 rows, and this is what says
    so before it ships.
    """
    assert parse(GOOD).signature() == "633d0eb003bb8a5b"


def test_a_missing_file_is_defaults_not_an_error(tmp_path):
    assert load(tmp_path) == ProjectConfig()


def test_an_empty_file_is_defaults(tmp_path):
    """YAML's empty document is `None`, which is not a mapping -- so without
    this it takes the not-a-mapping refusal below and a blank file is fatal."""
    (tmp_path / config.PROJECT_CONFIG_NAME).write_text("\n# nothing yet\n")
    assert load(tmp_path) == ProjectConfig()


@pytest.mark.parametrize("text", ["hello", "- a\n- b", "42"], ids=["scalar", "list", "number"])
def test_a_document_that_is_not_a_mapping_is_refused(text):
    """TOML could not express any of these; YAML parses all three happily and
    would leave every section defaulted with nothing said."""
    with pytest.raises(ConfigError, match="must be a mapping of sections"):
        parse(text)


def test_a_section_that_is_not_a_mapping_is_refused():
    with pytest.raises(ConfigError, match="index: must be a mapping"):
        parse("index: [a, b]\n")


def test_a_leftover_toml_config_is_refused_rather_than_ignored(tmp_path):
    """Ignoring it takes its excludes with it, and the only symptom is a store
    that grew. `tools` turns this into a `last_error` on the row."""
    (tmp_path / config.RETIRED_CONFIG_NAME).write_text('[index]\nexclude = ["wiki/*"]\n')
    with pytest.raises(ConfigError, match="the config is YAML now"):
        load(tmp_path)


def test_a_migrated_project_is_done_even_if_it_kept_the_old_file(tmp_path):
    """The refusal is for a project that has *only* the old file."""
    (tmp_path / config.RETIRED_CONFIG_NAME).write_text('[index]\nexclude = ["old/*"]\n')
    (tmp_path / config.PROJECT_CONFIG_NAME).write_text('index:\n  exclude: ["new/*"]\n')
    assert load(tmp_path).exclude == ("new/*",)


def test_a_typo_names_the_nearest_key():
    """A silently ignored exclude typo is the 70.9% failure."""
    with pytest.raises(ConfigError, match="did you mean 'exclude'"):
        parse('index:\n  exlcude: ["a"]\n')


def test_a_retired_key_says_so_rather_than_guessing():
    with pytest.raises(ConfigError, match=r"retired key, now index\.exclude"):
        parse('index:\n  patterns: ["a"]\n')


def test_an_unknown_section_is_rejected():
    with pytest.raises(ConfigError, match=r"unknown section \[indexing\]"):
        parse('indexing:\n  exclude: ["a"]\n')


@pytest.mark.parametrize(
    "text,match",
    [
        ('index:\n  exclude: "wiki/*"\n', "must be a list of strings"),
        ("index:\n  exclude: [1, 2]\n", "must be a list of strings"),
        ('index:\n  respect_gitignore: "yes"\n', "must be true or false"),
        ("index:\n  exclude: [\n", "source"),
        # YAML 1.1's boolean words. An unquoted `no` is `False`, so a pattern
        # that happens to be one arrives as a bool -- refused here rather than
        # silently excluding nothing.
        ("index:\n  exclude: [no, off]\n", "must be a list of strings"),
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
    _write(root, 'index:\n  exclude: ["public/assets/plugins/*"]\n')
    member = tmp_path / "member"
    member.mkdir()

    assert effective(member, [str(root)]).exclude == ("public/assets/plugins/*",)


def test_two_roots_union_rather_than_first_one_winning(tmp_path):
    """The old code took the first registry match, so the answer depended on
    which row happened to be written first. Both orders must agree."""
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a, 'index:\n  exclude: ["from_a/*"]\n')
    _write(b, 'index:\n  exclude: ["from_b/*"]\n')
    member = tmp_path / "member"
    member.mkdir()

    forward = effective(member, [str(a), str(b)]).exclude
    backward = effective(member, [str(b), str(a)]).exclude
    assert set(forward) == {"from_a/*", "from_b/*"}
    assert forward == backward, "the union must not depend on registry order"


def test_a_members_own_file_adds_to_the_union_rather_than_replacing_it(tmp_path):
    root = tmp_path / "root"
    _write(root, 'index:\n  exclude: ["from_root/*"]\n')
    member = tmp_path / "member"
    _write(member, 'index:\n  exclude: ["from_member/*"]\n')

    assert set(effective(member, [str(root)]).exclude) == {"from_root/*", "from_member/*"}


def test_scalars_take_the_conservative_value(tmp_path):
    root = tmp_path / "root"
    _write(root, "index:\n  respect_gitignore: true\n")
    member = tmp_path / "member"
    _write(member, "index:\n  respect_gitignore: false\n")

    assert effective(member, [str(root)]).respect_gitignore is True


def test_a_broken_root_config_does_not_make_its_members_unindexable(tmp_path):
    root = tmp_path / "root"
    _write(root, "index:\n  exclude: [\n")
    member = tmp_path / "member"
    _write(member, 'index:\n  exclude: ["mine/*"]\n')

    assert effective(member, [str(root)]).exclude == ("mine/*",)


def test_a_root_never_inherits_from_itself(tmp_path):
    root = tmp_path / "root"
    _write(root, 'index:\n  exclude: ["x/*"]\n')
    assert effective(root, [str(root)]).exclude == ("x/*",)


def test_the_signature_moves_with_the_excludes_and_not_with_federation():
    base = parse('index:\n  exclude: ["a"]\n').signature()
    assert parse('index:\n  exclude: ["a", "b"]\n').signature() != base
    assert parse('index:\n  exclude: ["a"]\nfederation:\n  exclude: ["z"]\n').signature() == base


def test_the_signature_ignores_pattern_order():
    """Order churn would reconcile every project on an unrelated edit."""
    assert parse('index:\n  exclude: ["a","b"]\n').signature() == (
        parse('index:\n  exclude: ["b","a"]\n').signature()
    )
