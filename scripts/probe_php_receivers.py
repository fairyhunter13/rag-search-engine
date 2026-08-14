#!/usr/bin/env python3
"""How many dropped call edges would a receiver type and an inheritance walk recover?

    python scripts/probe_php_receivers.py
    python scripts/probe_php_receivers.py --max-files 800

Read-only: parses source with tree-sitter, reads `graph.db` for the symbol population, opens no
vector store, needs no daemon and no GPU, writes nothing.

Fleet-wide, 105 roots / 22,842 files / 585,970 call sites: 63,917 dropped, 13,394 recovered (21.0%
of dropped, +12.0% resolved call sites) — laravel 25.7%, other 39.6%, ci3 16.1%. The gate was ~20%.

It drives the *shipped* resolver — `graph.php_receivers`, the same module `sweeps` calls — rather
than a copy of it, so a number here is a number the sweep reproduces. All this file adds is the
replay of today's name-only resolution and, on each non-recovery, the reason: `narrow` returns ""
for all of them, and only the probe cares which "" it was.

`sweeps._extract_graph` builds a callee pool by *name* alone, prefers the caller's own file, and
drops the edge when more than `_MAX_CALLEE_FANOUT` candidates survive — precision 1.000 at recall
0.633. Those drops are not wrong edges; they are edges no evidence could choose between. This
replays that resolution and asks, of each *dropped* site, whether a receiver type narrows the pool
to exactly one: (1) the receiver's type from the same file — a typed property, a typed parameter,
`$v = new X()`, `$this`, `self`/`static`/`parent`; (2) that name to an FQN via the file's own
`namespace` + `use` clauses and the FQN to a file via `composer.json`'s PSR-4 map
(`_ImportResolver._read_psr4`, reused rather than re-derived); (3) failing that, upward through
`extends` and trait `use` until exactly one class on the chain declares the callee.

Every recovery narrows to **exactly one** candidate — the standard the cap already enforces — so it
raises recall without touching precision, and the cap stays 1.

The tier was struck once on a CodeIgniter 3 store, which predates namespaces, `use`, and type
declarations and so was guaranteed to read zero. The dialect table is the self-check, and it passed:
CI3 recovers 510 of 40,576 drops through the type hops (1.3%) against Laravel's 3,490, and is
carried instead by the chain walk onto `CI_Controller`/`MY_Model`.

HR34: counts and rates only — a store appears as an index and a dialect label, never as a name
or a path.

**Kept by decision, not by inertia.** The plan that produced it said "promoted or deleted once it
answers"; it answered, and it is kept, because R1's headline is only reproducible through it — no
other artifact re-derives the 21.0% from source. It is also the worked example of the rule the same
execution earned twice: *a probe that re-derives its own view of the code is measuring a different
program.* An earlier draft re-read the tree-sitter grammar itself, silently mismeasured the call
population by 45%, and looked healthy throughout. Any successor oracle must join on the shipped
extractor's own output — `_callee_node`/`_is_call_node`/`_unwrap_callee`, imported, never re-read —
and must run a positive control, a case the join *must* match, before its first real number.
"""
from __future__ import annotations

import argparse
import collections
import sqlite3
import sys
from pathlib import Path

from rag_search.core.config import project_graph_db
from rag_search.core.registry import list_projects
from rag_search.daemon.sweeps import _ImportResolver
from rag_search.graph.php_receivers import FileFacts, Resolver, parse_facts
from rag_search.index.discover import detect_language, iter_files


def _php_symbols(db: Path) -> dict[str, dict[str, int]]:
    """Callee name -> {defining file: how many symbols of that name it holds}.

    Counted per *symbol*, not per file, because that is what the shipped resolution counts: a file
    declaring `handle()` on two classes leaves a pool of two and is dropped, and folding it to one
    file would score it as unambiguous when nothing about it is.
    """
    by_name: dict[str, dict[str, int]] = collections.defaultdict(collections.Counter)
    if not db.is_file():
        return by_name
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT name, file FROM symbols WHERE file LIKE '%.php'").fetchall()
    except sqlite3.Error:
        return by_name
    finally:
        con.close()
    for name, fstr in rows:
        if name:
            by_name[name][fstr] += 1
    return by_name


def _dialect(root: Path) -> str:
    """`laravel`, `ci3`, or `other`, off declared facts — a report label only.

    Nothing in the recovery path branches on it. It exists because the tier this re-opens was
    struck on a CI3 measurement, and the two dialects are expected to read very differently.
    """
    cj = root / "composer.json"
    text = ""
    if cj.is_file():
        try:
            text = cj.read_text(errors="replace")
        except OSError:
            text = ""
    if '"laravel/framework"' in text:
        return "laravel"
    if '"codeigniter' in text or (root / "system" / "core" / "CodeIgniter.php").is_file():
        return "ci3"
    return "other"


def _parse_root(root: Path, max_files: int) -> tuple[dict[str, FileFacts], bool]:
    facts: dict[str, FileFacts] = {}
    truncated, seen = False, 0
    for fpath in iter_files(root, federation_mode=True):
        if detect_language(fpath) != "php":
            continue
        if max_files and seen >= max_files:
            truncated = True
            break
        try:
            src = fpath.read_bytes()
        except OSError:
            continue
        seen += 1
        f = parse_facts(src)
        if f is not None:
            facts[str(fpath)] = f
    return facts, truncated


def _why(res: Resolver, f: FileFacts, hint: str, cands: dict[str, int],
         t: collections.Counter) -> None:
    """Attribute one non-recovery. Only the probe needs this; the sweep just sees ""."""
    recv = res.receiver_class(f, hint)
    if not recv:
        t["no_receiver_type"] += 1
    elif len({s for step in res.chain(recv) if cands.get(s := res.file_of(step)) == 1}) > 1:
        t["chain_ambiguous"] += 1
    elif recv not in res.by_fqn and not res.file_of(recv):
        t["class_not_indexed"] += 1
    else:
        t["no_match"] += 1


def probe(root: Path, max_files: int) -> dict | None:
    """Replay today's resolution over one root and price the three hops, or None if no PHP."""
    by_name = _php_symbols(project_graph_db(str(root)))
    if not by_name:
        return None
    facts, truncated = _parse_root(root, max_files)
    if not facts:
        return None
    res = Resolver(_ImportResolver(root)._read_psr4(), facts)
    t: collections.Counter = collections.Counter()
    for path, f in facts.items():
        for callee, _line, hint in f.calls:
            cands = by_name.get(callee, {})
            if not cands:
                t["no_candidate"] += 1
                continue
            # Same file is the preferred scope, exactly as `_extract_graph` treats it.
            pool = {path: cands[path]} if path in cands else cands
            if sum(pool.values()) <= 1:
                t["resolved"] += 1
                continue
            t["dropped"] += 1
            won = res.narrow(f, hint, cands)
            if not won:
                _why(res, f, hint, cands, t)
                continue
            t["recovered"] += 1
            # Which hop paid is a report column, not a decision: `narrow` already chose.
            t["recovered_direct" if res.file_of(res.receiver_class(f, hint)) == won
              else "recovered_chain"] += 1
    return {"dialect": _dialect(root), "php_files": len(facts),
            "truncated": truncated, "tally": t}


_HEAD = (f"{'group':<12}{'files':>7}{'sites':>9}{'resolvd':>9}{'dropped':>9}{'recov':>7}"
         f"{'recov%':>8}{'direct':>8}{'chain':>7}{'noRecv%':>9}{'edges+%':>9}")


def _row(name: str, files: int, t: collections.Counter) -> None:
    sites = t["resolved"] + t["no_candidate"] + t["dropped"]
    dropped, resolved = t["dropped"] or 1, t["resolved"] or 1
    print(f"{name:<12}{files:>7}{sites:>9}{t['resolved']:>9}{t['dropped']:>9}{t['recovered']:>7}"
          f"{100 * t['recovered'] / dropped:>7.1f}%{t['recovered_direct']:>8}"
          f"{t['recovered_chain']:>7}{100 * t['no_receiver_type'] / dropped:>8.1f}%"
          f"{100 * t['recovered'] / resolved:>8.1f}%")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-files", type=int, default=0,
                    help="parse at most this many PHP files per root (0 = every one)")
    args = ap.parse_args()

    rows: list[dict] = []
    for p in list_projects():
        if not p.enabled or not Path(p.path).is_dir():
            continue
        try:
            r = probe(Path(p.path), args.max_files)
        except OSError:
            continue
        if r:
            rows.append(r)
            print(f"  ... {len(rows)} php root(s) probed", file=sys.stderr)
    if not rows:
        print("no PHP root with an indexed symbol population")
        return 1

    # Both tables from one parse. The per-store rows decide *where* the tiers ship — an aggregate
    # that clears the gate can be a few large roots carrying many that do not.
    for label, key in (("per store", lambda i, r: f'{r["dialect"]}#{i}'),
                       ("by dialect", lambda i, r: r["dialect"])):
        groups: dict[str, list[dict]] = collections.defaultdict(list)
        for i, r in enumerate(rows):
            groups[key(i, r)].append(r)
        print(f"\n== {label} ==\n{_HEAD}")
        total: collections.Counter = collections.Counter()
        for name, group in sorted(groups.items()):
            t: collections.Counter = collections.Counter()
            for r in group:
                t.update(r["tally"])
            total.update(t)
            _row(name, sum(r["php_files"] for r in group), t)
        _row("ALL", sum(r["php_files"] for r in rows), total)
    truncated = sum(r["truncated"] for r in rows)
    if truncated:
        print(f"note: {truncated} root(s) hit --max-files and were parsed only in part")
    return 0


if __name__ == "__main__":
    sys.exit(main())
