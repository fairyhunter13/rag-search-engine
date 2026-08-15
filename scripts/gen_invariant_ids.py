#!/usr/bin/env python3
"""Generate `docs/reference/invariant-ids.md` — where every `AU5`/`RL2`/`CB3` is defined.

Run: .venv/bin/python scripts/gen_invariant_ids.py   (`--check` diffs instead of writing)

The codebase argues in short ids and refers to them from anywhere: "guarded by AU5 and AU6",
"RL1/RL2 in test_daemon.py", "F4d". `HR*` has §13b to define it; the other ~30 prefixes had
nothing, so resolving one meant a grep. Measured 2026-08-15: 448 distinct ids in `src/`, 304 of
them in no document at all.

Generated for the reason `gen_grammar_capability_matrix.py` is: 448 entries maintained by hand
describe the day they were written. `test_invariant_id_index_is_current` re-runs this and diffs.

Sections split by what a reader can do with the answer. **defined**: one declaration site — a test
name, a docstring opener, a §13b row, or a source comment — and the claim there is the meaning.
**ambiguous**: two sites in two files, worse than undefined, since `SC1` could be the
suite-concurrency gate or the surface-consistency check and both exist. **cross-file, unanchored**:
the real backlog. **file-local**: listed but not a backlog — one file, which is its definition.
"""
from __future__ import annotations

import ast
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "docs" / "reference" / "invariant-ids.md"
SPEC = REPO / "docs" / "architecture" / "federation-ops-and-invariants.md"

ID = re.compile(r"\b([A-Z]{1,4}\d{1,2}[a-z]?)\b")
# Two ways a guard claims an id, and the repo uses both. The name is authoritative because it is
# what `pytest -k cb3` selects and what a failure line prints; the docstring opener is the older
# convention and survives where a test predates the naming one.
NAMED = re.compile(r"^test_([a-z]{1,4})(\d{1,2})([a-z]?)_")
DECLARES = re.compile(r"\s*([A-Z]{1,4}\d{1,2}[a-z]?)\b\s*[:—–-]")
# Tokens of the id shape that are not ids. Deliberately short: a long list here is the
# hand-maintained allowlist this generator exists to replace.
NOT_IDS = {"UTF8", "SHA256", "FTS5", "BM25", "P100", "X86", "F32", "INT8", "ISO8601", "RFC3339"}


def _sources() -> list[Path]:
    return sorted(
        p for root in ("src/rag_search", "src/tests") for p in (REPO / root).rglob("*.py")
    )


def _declarations() -> tuple[dict[str, list[tuple[str, str, str]]], list[tuple[str, str, str, str]]]:
    """(id -> [(path, guard, claim)], [(path, guard, name id, docstring id)] where the two differ)."""
    out: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    mismatched: list[tuple[str, str, str, str]] = []
    for path in _sources():
        rel = str(path.relative_to(REPO))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # a fixture corpus may hold deliberate junk
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            doc = ast.get_docstring(node) or ""
            first = " ".join(doc.split("\n", 1)[0].split())
            named = NAMED.match(node.name)
            # Only the prefix uppercases: the trailing letter of SC4b is part of the id, not a
            # word boundary, so upper()ing the whole match invents `SC4B` and reports a mismatch
            # against the very docstring that spells it correctly.
            name_id = named[1].upper() + named[2] + named[3] if named else None
            doc_m = DECLARES.match(doc)
            doc_id = doc_m[1] if doc_m and doc_m[1] not in NOT_IDS else None
            declared = name_id or doc_id
            if name_id and doc_id and name_id != doc_id:
                if _key(name_id)[:2] == _key(doc_id)[:2]:
                    # Same rule, and the docstring carries the finer sub-id the name dropped
                    # (`test_sc8_…` opening `SC8a`). Not a disagreement — a name too coarse to
                    # distinguish its own siblings, so take the specific one.
                    declared = doc_id
                else:
                    mismatched.append((rel, node.name, name_id, doc_id))
            if declared and declared not in NOT_IDS:
                claim = first[len(doc_id):].lstrip(" :—–-") if doc_id else first
                out[declared].append((rel, node.name, claim or "—"))
    return out, mismatched


def _declared_in_spec() -> dict[str, str]:
    """id -> the §13b row's opening claim. HR ids are defined in prose, not by a test."""
    body = SPEC.read_text(encoding="utf-8").split("13b.", 1)[-1].split("\n## ", 1)[0]
    out: dict[str, str] = {}
    for line in body.splitlines():
        m = re.match(r"\|\s*\*\*(HR\d+)\*\*\s*\|\s*(.+?)\s*\|\s*$", line)
        if m:
            claim = re.sub(r"[*`]", "", m.group(2)).split(". ")[0]
            out[m.group(1)] = claim.strip().rstrip(".")
    return out


def _comment_declarations() -> dict[str, tuple[str, int, str]]:
    """id -> (path, line, claim) for the ids a source comment introduces.

    Source uses the same opener a test docstring does — `# H1: StructureKind → our canonical kind`,
    `# S8: the host language of a file, cached`. Not scanning for it reported those ids as
    undefined while their definition sat one grep away, which is the confusion this file exists
    to remove. Only the first site counts: a repeated `# S8:` restates one rule, it does not
    declare a second.
    """
    out: dict[str, tuple[str, int, str]] = {}
    for path in _sources():
        rel = str(path.relative_to(REPO))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            m = re.match(r'\s*(?:#|""")\s*([A-Z]{1,4}\d{1,2}[a-z]?)\b\s*[:—–-]\s*(.+)', line)
            if m and m[1] not in NOT_IDS and m[1] not in out:
                out[m[1]] = (rel, n, m[2].strip().rstrip('"').strip() or "—")
    return out


def _referenced() -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for path in _sources():
        rel = str(path.relative_to(REPO))
        for tok in set(ID.findall(path.read_text(encoding="utf-8"))):
            if tok not in NOT_IDS:
                out[tok].add(rel)
    return out


def _key(name: str) -> tuple[str, int, str]:
    """Sort and group by (prefix, number, letter) so CB9 precedes CB10 and SC4b follows SC4."""
    m = re.match(r"([A-Z]+)(\d+)([a-z]?)", name)
    return (m[1], int(m[2]), m[3]) if m else (name, 0, "")


def render() -> str:
    (tests, mismatched), spec, refs = _declarations(), _declared_in_spec(), _referenced()
    comments = {k: v for k, v in _comment_declarations().items() if k not in tests and k not in spec}
    defined = {k: v[0] for k, v in tests.items() if len(v) == 1}
    # Several guards under one id is the normal case — the id names the rule and each guard proves
    # a facet of it. It is only ambiguous when the sites are in different files, because then the
    # two are different rules that happen to have collided on a prefix.
    multi = {k: v for k, v in tests.items() if len(v) > 1}
    ambiguous = {k: v for k, v in multi.items() if len({s[0] for s in v}) > 1}
    facets = {k: v for k, v in multi.items() if k not in ambiguous}
    loose = [k for k in refs if k not in tests and k not in spec and k not in comments]
    # An id used in one file is defined by that file for every reader who will ever meet it, and a
    # global entry for it would be prose restating a comment. Only a cross-file id can strand
    # someone, so that is the count worth acting on.
    crossfile = sorted((k for k in loose if len(refs[k]) > 1), key=_key)
    filelocal = sorted((k for k in loose if len(refs[k]) == 1), key=_key)

    L = [
        "# Invariant ids",
        "",
        "Generated by `scripts/gen_invariant_ids.py` — do not edit. Gated by "
        "`test_invariant_id_index_is_current`.",
        "",
        "The short ids this codebase argues in (`AU5`, `RL2`, `CB3`, `F4d`) resolve here. A guard "
        "declares one by carrying it in its name (`test_cb3_…`) or by opening its docstring with "
        "it; `HR*` is declared by a §13b row instead. The test is the rule's definition, so the "
        "claim column is the meaning, not a pointer to it.",
        "",
        f"{len(defined)} defined by one guard · {len(facets)} by several · {len(comments)} by a "
        f"source comment · {len(ambiguous)} ambiguous · {len(crossfile)} cross-file and unanchored "
        f"· {len(filelocal)} file-local shorthand · {len(mismatched)} where the name and the "
        "docstring disagree.",
        "",
        "## Hard requirements (HR)",
        "",
        "Defined by §13b of `docs/architecture/federation-ops-and-invariants.md` and mapped to "
        "their guards by §14. Repeated here only so an `HR` lookup lands somewhere.",
        "",
        "| id | requirement |",
        "|---|---|",
    ]
    L += [f"| `{n}` | {spec[n]} |" for n in sorted(spec, key=_key)]
    L += ["", "## Defined by a guard", ""]

    by_prefix: dict[str, list[str]] = defaultdict(list)
    for name in defined:
        by_prefix[_key(name)[0]].append(name)
    for prefix in sorted(by_prefix):
        L += [f"### {prefix}", "", "| id | guard | file | claim |", "|---|---|---|---|"]
        for name in sorted(by_prefix[prefix], key=_key):
            path, test, claim = defined[name]
            L.append(f"| `{name}` | `{test}` | `{path}` | {claim} |")
        L.append("")

    L += [
        "## Proved by several guards",
        "",
        "One rule, one file, more than one test. Not a defect — the id names the rule and each "
        "guard pins a facet of it — but a reference to one of these resolves to a set, so the "
        "reader needs all of them.",
        "",
        "| id | guards | file |",
        "|---|---|---|",
    ]
    for name in sorted(facets, key=_key):
        sites = facets[name]
        L.append(
            f"| `{name}` | " + "<br>".join(f"`{t}`" for _, t, _ in sites) + f" | `{sites[0][0]}` |"
        )

    L += [
        "",
        "## Ambiguous — one id, two rules",
        "",
        "Declared in more than one file, so the id names two unrelated rules and a reference to it "
        "does not resolve. These are defects: two topics abbreviated to the same prefix and then "
        "numbered from one independently.",
        "",
        "| id | sites |",
        "|---|---|",
    ]
    for name in sorted(ambiguous, key=_key):
        sites = "<br>".join(f"`{t}` — `{p}`" for p, t, _ in ambiguous[name])
        L.append(f"| `{name}` | {sites} |")

    L += [
        "",
        "## The name and the docstring disagree",
        "",
        "The guard's name claims one id and its first docstring line another. The name wins here, "
        "because it is what `pytest -k` selects and what a failure line prints — but a reader who "
        "trusts the prose resolves to the wrong rule, and where the docstring's id also exists "
        "elsewhere they land on a real, unrelated guard.",
        "",
        "| guard | name says | docstring says | file |",
        "|---|---|---|---|",
    ]
    for rel, guard, name_id, doc_id in sorted(mismatched, key=lambda r: (r[0], r[1])):
        L.append(f"| `{guard}` | `{name_id}` | `{doc_id}` | `{rel}` |")

    L += [
        "",
        "## Declared by a source comment",
        "",
        "No guard carries these; a comment introduces them instead. The claim is that comment's "
        "own first line, cut where it wraps — the site column is where to read the rest.",
        "",
        "| id | claim | site |",
        "|---|---|---|",
    ]
    for name in sorted(comments, key=_key):
        rel, line, claim = comments[name]
        L.append(f"| `{name}` | {claim} | `{rel}:{line}` |")

    L += [
        "",
        "## Cross-file, unanchored",
        "",
        "Used in more than one file and introduced in none of them. This is the list that costs a "
        "reader something: the id crosses a file boundary, so no single file explains it, and "
        "resolving one means reading every site and inferring the rule they share.",
        "",
        "| id | mentioned in |",
        "|---|---|",
    ]
    L += [f"| `{name}` | {_files(refs[name])} |" for name in crossfile]

    L += [
        "",
        "## File-local shorthand",
        "",
        "Used in exactly one file and introduced nowhere. Listed for completeness, not as a "
        "backlog: the file is the definition for everyone who will ever meet the id, and a global "
        "entry would restate what its only reader already has open.",
        "",
        "| id | file |",
        "|---|---|",
    ]
    L += [f"| `{name}` | {_files(refs[name])} |" for name in filelocal]
    return "\n".join(L) + "\n"


def _files(paths: set[str]) -> str:
    names = sorted(paths)
    shown = ", ".join(f"`{Path(f).name}`" for f in names[:3])
    return shown + (f", +{len(names) - 3} more" if len(names) > 3 else "")


def main() -> int:
    text = render()
    if "--check" in sys.argv:
        if OUT.exists() and OUT.read_text(encoding="utf-8") == text:
            return 0
        print(f"{OUT.relative_to(REPO)} is stale — re-run scripts/gen_invariant_ids.py",
              file=sys.stderr)
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
