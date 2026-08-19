"""`.coderag.toml`: parsed strictly, and unioned across every claiming root.

Two decisions here are load-bearing.

Unknown keys are errors. A silently ignored typo in an exclude list is exactly
the failure that costs 70.9% of an index -- the pattern that was supposed to
drop 917,890 chunks of vendored JavaScript simply does nothing, and the only
symptom is a store three times larger than it should be.

Effective config is the union across the member's own file and *every* root
that claims it. The previous implementation took the first match in registry
order, so a project in two roots inherited one of them by whichever row was
written first: an order-dependent answer to a question that has a correct one.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import config

_INDEX_KEYS = {"exclude", "include", "respect_gitignore", "use_default_ignores"}
_FEDERATION_KEYS = {"exclude"}
_SECTIONS = {"index": _INDEX_KEYS, "federation": _FEDERATION_KEYS}

# Keys from the retired .rse-index.yaml format. Named explicitly, because
# "unknown key" on a key that used to work reads like a parser bug.
_RETIRED = {
    "patterns": "index.exclude",
    "ignore": "index.exclude",
    "exclude_patterns": "index.exclude",
    "follow_symlinks": "removed -- federation members are registered by resolved path",
    "max_file_size": "removed -- set CODERAG_MAX_FILE_BYTES",
}


class ConfigError(ValueError):
    """A `.coderag.toml` the user needs to fix, phrased so they can fix it."""


@dataclass(slots=True)
class ProjectConfig:
    exclude: tuple[str, ...] = ()
    include: tuple[str, ...] = ()
    respect_gitignore: bool = True
    use_default_ignores: bool = True
    federation_exclude: tuple[str, ...] = ()

    def signature(self) -> str:
        """Hash of everything that changes which files get indexed.

        Federation excludes are absent on purpose: they decide which members
        are discovered, not what any member contains, so folding them in would
        make every member reconcile whenever an unrelated sibling was dropped.
        """
        payload = json.dumps(
            {
                "exclude": sorted(self.exclude),
                "include": sorted(self.include),
                "respect_gitignore": self.respect_gitignore,
                "use_default_ignores": self.use_default_ignores,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _suggest(key: str, valid: set[str]) -> str:
    if key in _RETIRED:
        return f" -- retired key, now {_RETIRED[key]}"
    near = difflib.get_close_matches(key, sorted(valid), n=1, cutoff=0.6)
    return f" -- did you mean {near[0]!r}?" if near else f" -- valid keys: {sorted(valid)}"


def _strings(section: str, key: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(
            f"[{section}] {key} must be a list of strings, got {type(value).__name__}"
        )
    return tuple(value)


def _flag(section: str, key: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"[{section}] {key} must be true or false, got {type(value).__name__}")
    return value


def parse(text: str, *, source: str = "<string>") -> ProjectConfig:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source}: {exc}") from exc

    for section in raw:
        if section not in _SECTIONS:
            raise ConfigError(
                f"{source}: unknown section [{section}]{_suggest(section, set(_SECTIONS))}"
            )

    cfg = ProjectConfig()
    for section, keys in _SECTIONS.items():
        body = raw.get(section, {})
        if not isinstance(body, dict):
            raise ConfigError(f"{source}: [{section}] must be a table")
        for key, value in body.items():
            if key not in keys:
                raise ConfigError(f"{source}: unknown key [{section}] {key}{_suggest(key, keys)}")
            if section == "federation":
                cfg.federation_exclude = _strings(section, key, value)
            elif key in ("exclude", "include"):
                setattr(cfg, key, _strings(section, key, value))
            else:
                setattr(cfg, key, _flag(section, key, value))
    return cfg


def load(project: Path) -> ProjectConfig:
    """The project's own file, or defaults when it has none."""
    path = Path(project) / config.PROJECT_CONFIG_NAME
    try:
        text = path.read_text()
    except FileNotFoundError:
        return ProjectConfig()
    except OSError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return parse(text, source=str(path))


def _dedup(*groups: tuple[str, ...]) -> tuple[str, ...]:
    """Union, sorted.

    Sorted rather than insertion-ordered because the inputs arrive in registry
    order: patterns are match-any, so order carries no meaning, and leaving it
    to the caller's iteration order makes the result depend on which row was
    written first -- the exact defect the union was written to remove.
    """
    return tuple(sorted({item for group in groups for item in group}))


def effective(project: Path, roots: list[str] | tuple[str, ...] = ()) -> ProjectConfig:
    """The member's own config unioned with every root that claims it.

    Excludes union: a pattern from any claiming root applies. Scalars take the
    conservative value -- if any party wants gitignore respected, it is
    respected -- because the alternative is picking a winner, and every rule
    for picking one is arbitrary. A member with its own file still inherits its
    roots' excludes; the file adds to the union, it does not replace it.
    """
    own = load(Path(project))
    excludes = [own.exclude]
    respect = [own.respect_gitignore]
    defaults = [own.use_default_ignores]

    for root in roots:
        root_path = Path(root)
        if root_path == Path(project):
            continue
        try:
            parent = load(root_path)
        except ConfigError:
            # A root with a broken config must not make its members unindexable.
            # The root's own index pass reports the error where it belongs.
            continue
        excludes.append(parent.exclude)
        respect.append(parent.respect_gitignore)
        defaults.append(parent.use_default_ignores)

    return ProjectConfig(
        exclude=_dedup(*excludes),
        include=own.include,
        respect_gitignore=any(respect),
        use_default_ignores=any(defaults),
        federation_exclude=own.federation_exclude,
    )
