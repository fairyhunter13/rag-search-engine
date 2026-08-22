"""What fraction of this machine's Claude Code sessions can coderag answer for.

The number the reach change is argued from. Re-run it rather than quoting the
prose copies in `scope.py` and the knowledge records -- those went a whole plan
without being re-derived.

    python3 scripts/reach_census.py

Reads the real registry and the real session store; writes nothing.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coderag import registry


def session_stores() -> list[Path]:
    """Every profile's `projects/` dir, deduped by realpath.

    All five `~/.claude*/projects` are symlinks to one store, so walking them
    as five directories multiplies every session by five.
    """
    seen: dict[Path, Path] = {}
    for profile in sorted(Path.home().glob(".claude*")):
        d = profile / "projects"
        if d.is_dir():
            seen.setdefault(d.resolve(), d)
    return sorted(seen)


def first_cwd(transcript: Path) -> str | None:
    try:
        with transcript.open(errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("cwd"):
                    return row["cwd"]
    except OSError:
        pass
    return None


def main() -> int:
    rows = registry.load()
    enabled = [e for e in rows.values() if e.enabled]

    counts: Counter[str] = Counter()
    unresolved: Counter[str] = Counter()
    # Splits `resolves-up`, which is what decides whether the reply's advisory is
    # true: a subdirectory's files are already in the answer, a checkout's are not.
    shape: Counter[str] = Counter()
    verdicts: dict[str, str] = {}

    for store in session_stores():
        for transcript in store.rglob("*.jsonl"):
            cwd = first_cwd(transcript)
            if not cwd:
                counts["no cwd"] += 1
                continue
            if cwd not in verdicts:
                owner = registry.enclosing(cwd)
                if owner is None:
                    verdicts[cwd] = "unresolved"
                elif owner == registry.resolve(cwd):
                    verdicts[cwd] = "direct"
                else:
                    verdicts[cwd] = "resolves-up"
            counts[verdicts[cwd]] += 1
            if verdicts[cwd] == "unresolved":
                unresolved[cwd] += 1
            elif verdicts[cwd] == "resolves-up":
                own = (Path(cwd) / ".git").exists()
                shape["own checkout" if own else "plain subdir"] += 1

    total = sum(counts[k] for k in ("direct", "resolves-up", "unresolved"))
    print(f"registry: {len(rows)} rows, {len(enabled)} enabled")
    print(f"sessions: {total} with a cwd ({counts['no cwd']} transcripts without one)")
    for name in ("direct", "resolves-up", "unresolved"):
        print(f"  {name:12s} {counts[name]:6d}  {counts[name] / total * 100:5.1f}%")
    answerable = counts["direct"] + counts["resolves-up"]
    print(f"  {'answerable':12s} {answerable:6d}  {answerable / total * 100:5.1f}%")

    up = counts["resolves-up"] or 1
    for name in ("plain subdir", "own checkout"):
        print(f"    of which {name:14s} {shape[name]:6d}  {shape[name] / up * 100:5.1f}%")

    print("\ntop unresolved cwds:")
    for path, n in unresolved.most_common(10):
        print(f"  {n:6d}  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
