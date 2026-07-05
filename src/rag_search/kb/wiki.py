"""Rich, navigable wiki bundle generated from the graph store (DeepWiki-style).

Layout: a sectioned `index.md`, one `community_{id}.md` per L1 community, and — for federated
roots — a `federation.md` aggregating members. Community pages, citations and diagrams are FULLY
DETERMINISTIC: they reuse the cached community summary as prose and draw call-graphs from real
`edges`, so a `Sources:[file:line]()` always resolves on disk. Citations are project-root-relative
so the absolute device path never leaks (public repo).
"""
from __future__ import annotations

import os
from pathlib import Path

from rag_search.graph.store import GraphStore

_TYPE_LABEL = {
    "business_process": "Business Process",
    "business_rule": "Business Rule",
    "feature": "Feature",
    "utility": "Utility",
    "infrastructure": "Infrastructure",
    "domain": "Domain",
    "test": "Test",
}
# Index section order — business-first, plumbing last.
_TYPE_ORDER = ["business_process", "business_rule", "feature", "domain",
               "infrastructure", "utility", "test"]
_MERMAID_CAP = 40  # max nodes and max edges per diagram (hygiene + readability)


# ── path / text helpers ──────────────────────────────────────────────────────

def _project_root(store: GraphStore) -> str:
    """Common ancestor of all symbol files — for project-root-relative citations (one root/db)."""
    files = [r[0] for r in store._con.execute(
        "SELECT DISTINCT file FROM symbols WHERE file IS NOT NULL AND file!=''").fetchall()]
    if not files:
        return ""
    if len(files) == 1:
        return os.path.dirname(files[0])
    try:
        return os.path.commonpath(files)
    except ValueError:
        return ""


def _rel(path: str, root: str) -> str:
    """Project-root-relative path; falls back to the basename for out-of-tree files."""
    if not path:
        return ""
    if root:
        try:
            r = os.path.relpath(path, root)
            if not r.startswith(".."):
                return r
        except ValueError:
            pass
    return os.path.basename(path)


def _label(text: str, max_words: int = 4) -> str:
    """A short mermaid-safe label: ≤max_words, no quotes/newlines/brackets."""
    words = (text or "").replace('"', "").replace("\n", " ").split()
    out = " ".join(words[:max_words]) or "node"
    return out.replace("[", "(").replace("]", ")")


# ── mermaid (deterministic, from real edges) ─────────────────────────────────

def _render_mermaid(edges: list[tuple[str, str]]) -> str:
    """A ```mermaid graph TD``` block from (caller, callee) name pairs; '' if no edges.

    Node ids are alnum (n0, n1, …), labels quoted+truncated; node and edge counts capped.
    """
    if not edges:
        return ""
    ids: dict[str, str] = {}
    lines: list[str] = []
    for a, b in edges:
        if len(lines) >= _MERMAID_CAP:
            break
        new_nodes = [n for n in (a, b) if n not in ids]
        if len(ids) + len(new_nodes) > _MERMAID_CAP:
            break
        for n in (a, b):
            ids.setdefault(n, f"n{len(ids)}")
        lines.append(f'    {ids[a]}["{_label(a)}"] --> {ids[b]}["{_label(b)}"]')
    if not lines:
        return ""
    return "```mermaid\ngraph TD\n" + "\n".join(lines) + "\n```"


def _mermaid_callgraph(store: GraphStore, cid: int) -> str:
    """Member-to-member call edges within community cid (often empty — coarse edge extraction)."""
    rows = store._con.execute(
        "SELECT s1.name, s2.name FROM edges e "
        "JOIN symbols s1 ON e.caller_sid=s1.sid JOIN symbols s2 ON e.callee_sid=s2.sid "
        "WHERE s1.community_id=? AND s2.community_id=? AND e.caller_sid!=e.callee_sid LIMIT ?",
        (cid, cid, _MERMAID_CAP * 2)).fetchall()
    return _render_mermaid([(a, b) for a, b in rows])



# ── page renderers ───────────────────────────────────────────────────────────

def _members_table(store: GraphStore, cid: int, root: str) -> str:
    """Markdown table of community members with project-root-relative source citations."""
    rows = store._con.execute(
        "SELECT name, kind, file, start_line FROM symbols "
        "WHERE community_id=? ORDER BY name LIMIT 25",
        (cid,)).fetchall()
    if not rows:
        return ""
    out = ["| Symbol | Kind | Source |", "|---|---|---|"]
    for name, kind, file, line in rows:
        rel = _rel(file or "", root)
        src = f"[{rel}:{line}]({rel})" if rel and line else ""
        out.append(f"| `{name}` | {kind or ''} | {src} |")
    return "\n".join(out)


def _render_community(store: GraphStore, root: str, cid: int, title: str,
                      summary: str, stype: str | None, mc: int) -> str:
    badge = _TYPE_LABEL.get(stype or "", "Community")
    parts = [f"# {title}", "", f"**Type:** {badge} · **Members:** {mc}", "", summary, ""]
    table = _members_table(store, cid, root)
    if table:
        parts += ["## Members", "", table, ""]
    diagram = _mermaid_callgraph(store, cid)
    if diagram:
        parts += ["## Call graph", "", diagram, ""]
    parts += ["---", "", "[← Index](index.md)"]
    return "\n".join(parts) + "\n"


def _render_index(l1: list) -> str:
    n_types = len({(r[3] or "feature") for r in l1})
    parts = ["# Project Wiki", "",
             f"{len(l1)} code communities across {n_types} semantic types.", ""]
    by_type: dict[str, list] = {}
    for cid, title, _summary, stype, _mc in l1:
        by_type.setdefault(stype or "feature", []).append((cid, title))
    parts += ["## Communities by Type", ""]
    for st in _TYPE_ORDER:
        items = by_type.get(st)
        if not items:
            continue
        parts += [f"### {_TYPE_LABEL.get(st, st)} ({len(items)})", ""]
        parts += [f"- [{title}](community_{cid}.md)" for cid, title in items]
        parts.append("")
    return "\n".join(parts) + "\n"


# ── public API ───────────────────────────────────────────────────────────────

def build_wiki(store: GraphStore, output_dir: Path) -> int:
    """Write a rich wiki bundle (index + per-community pages). Returns page count.

    Community pages are deterministic (reuse cached summaries, cite real sources, draw real
    edges). Signature/return unchanged so existing callers keep working.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    root = _project_root(store)
    l1 = store._con.execute(
        "SELECT id, title, summary, semantic_type, member_count FROM communities "
        "WHERE level=1 AND title IS NOT NULL AND title!='' AND summary IS NOT NULL AND summary!='' "
        "ORDER BY id").fetchall()
    if not l1:
        return 0
    count = 0
    (output_dir / "index.md").write_text(_render_index(l1), encoding="utf-8")
    count += 1
    for cid, title, summary, stype, mc in l1:
        (output_dir / f"community_{cid}.md").write_text(
            _render_community(store, root, cid, title, summary, stype, mc or 0), encoding="utf-8")
        count += 1
    return count


# ── federated index ──────────────────────────────────────────────────────────

def _federation_member_summary(gs: GraphStore) -> dict:
    """Per-member rollup for federation.md: type counts + key business communities."""
    types = dict(gs._con.execute(
        "SELECT COALESCE(NULLIF(semantic_type,''),'unclassified'), COUNT(*) FROM communities "
        "WHERE level=1 GROUP BY COALESCE(NULLIF(semantic_type,''),'unclassified')").fetchall())
    top = [r[0] for r in gs._con.execute(
        "SELECT title FROM communities WHERE level=1 AND title IS NOT NULL "
        "AND semantic_type IN ('business_process','business_rule') "
        "ORDER BY member_count DESC LIMIT 8").fetchall()]
    return {"types": types, "top": top}


def _render_federation(root_path: str, per_member: list) -> str:
    rollup: dict[str, int] = {}
    parts = ["# Federation Overview", "",
             f"Logical entity spanning {len(per_member)} members "
             f"(root: `{os.path.basename(root_path)}`).", ""]
    for path, data in per_member:
        parts += [f"## {os.path.basename(path)}" + ("  _(root)_" if path == root_path else ""), ""]
        if data["top"]:
            parts.append("**Key business logic:** " + ", ".join(data["top"]))
        parts.append("")
        for st, n in data["types"].items():
            rollup[st] = rollup.get(st, 0) + n
    parts += ["## Semantic Type Rollup", "", "| Type | Communities |", "|---|---|"]
    parts += [f"| {_TYPE_LABEL.get(st, st.title())} | {rollup[st]} |"
              for st in [*_TYPE_ORDER, "unclassified"] if st in rollup]
    return "\n".join(parts) + "\n"


def build_federated_index(root_path: str) -> int:
    """Write `federation.md` into the root's wiki dir when it has >1 federation member.

    Presentation-only aggregation of per-member data; creates/reads NO cross-repo edges (HR4
    preserved). Returns 1 if written, else 0 (standalone projects have no federation page).
    """
    from rag_search.core.config import project_wiki_dir
    from rag_search.daemon.federation import expand_federation, federated_map

    if len(expand_federation(root_path)) < 2:
        return 0
    per_member = federated_map(root_path, _federation_member_summary)
    if len(per_member) < 2:
        return 0
    wiki_dir = project_wiki_dir(root_path)
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / "federation.md").write_text(_render_federation(root_path, per_member), encoding="utf-8")
    return 1
