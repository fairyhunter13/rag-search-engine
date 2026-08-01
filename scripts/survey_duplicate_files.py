#!/usr/bin/env python3
"""How much of one extension's indexed corpus is byte-identical copies, split by the seam that matters.

    python scripts/survey_duplicate_files.py            # .html, the largest known offender
    python scripts/survey_duplicate_files.py --ext .css

Exists because the case for de-duplicating a corpus keeps being made from a guess about *why* the
copies are there — "one purchased theme vendored across sibling repos" — and the shape of the
duplication decides the design, not the story about it. Measured rather than assumed, `.html` came
back 6,494 files with 794 distinct contents: the mass is not one theme, it is library example
directories vendored per repo (110 groups at exactly 25 copies).

**The split is the point.** A within-project rule needs no state beyond the project it is indexing.
A cross-fleet rule makes one federation member's results depend on what another member indexed,
which is the independence P3 buys, so it has to earn the coupling. This reports both reclaims so
that trade is priced instead of argued.

Read-only: hashes file bodies, opens no store, needs no daemon and no GPU. Deliberately no
normalisation — "byte-copy of a file in another repo" is the claim, and anything cleverer (stripping
whitespace, parsing) measures a different one. Reports counts and ranks only, never a path or a
project name, so its output can be pasted into a public repo (P18/HR34).
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import hashlib
import sys
from pathlib import Path

from rag_search.core.registry import list_projects
from rag_search.index.discover import iter_files


def survey(ext: str) -> tuple[int, dict[str, list[str]]]:
    """(files scanned, content hash -> the project name of each copy)."""
    by_hash: dict[str, list[str]] = collections.defaultdict(list)
    scanned = 0
    for p in list_projects():
        if not p.enabled:
            continue
        root = Path(p.path)
        try:
            files = list(iter_files(root))
        except OSError:
            continue
        for f in files:
            if Path(f).suffix.lower() != ext:
                continue
            scanned += 1
            with contextlib.suppress(OSError):
                by_hash[hashlib.sha256(Path(f).read_bytes()).hexdigest()].append(root.name)
    return scanned, by_hash


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ext", default=".html", help="file extension to survey (default: .html)")
    args = ap.parse_args()
    ext = args.ext.lower()
    if not ext.startswith("."):
        ext = "." + ext

    scanned, by_hash = survey(ext)
    if not scanned:
        print(f"no {ext} files in any enabled project", file=sys.stderr)
        return 1

    within = cross = 0
    per_project: collections.Counter = collections.Counter()
    for copies in by_hash.values():
        # Keep one per project: the reclaim available without any cross-member state.
        for proj, n in collections.Counter(copies).items():
            if n > 1:
                within += n - 1
                per_project[proj] += n - 1
        # Keep one fleet-wide: strictly larger, and the difference is what coupling members costs.
        cross += len(copies) - 1

    print(f"{ext} files scanned          : {scanned}")
    print(f"distinct contents           : {len(by_hash)}")
    print(f"within-project reclaim      : {within}  ({within / scanned:.1%})")
    print(f"cross-fleet reclaim         : {cross}  ({cross / scanned:.1%})")
    print(f"extra bought by going cross : {cross - within}")
    print(f"projects with within-dupes  : {len(per_project)}")
    print("\nwithin-project reclaim by project (ranked; names withheld — P18):")
    for i, (_proj, n) in enumerate(per_project.most_common(10), start=1):
        print(f"  #{i:<3d} {n:6d}")
    print("\ngroup-size histogram (copies per identical content):")
    hist = collections.Counter(len(v) for v in by_hash.values() if len(v) > 1)
    for size in sorted(hist):
        print(f"  {size:5d} copies : {hist[size]} groups")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
