"""Rich, navigable wiki bundle generated from the graph store (DeepWiki-style).

Layout: a sectioned `index.md`, one `community_{id}.md` per L1 community, one `domain_{id}.md`
per L2 architecture domain, and — for federated roots — a `federation.md` aggregating members.

Community pages, citations and diagrams are FULLY DETERMINISTIC: they reuse the cached community
summary as prose and draw call-graphs from real `edges`, so a `Sources:[file:line]()` always
resolves on disk and every mermaid node maps to a real symbol/community. Only the L2-domain
narrative calls the cloud LLM (DeepSeek); the kill-switch ``OSE_WIKI_LLM=0`` and any
missing-key/error path fall back to templated prose, so the wiki always builds with zero local
GPU. Citations are project-root-relative so the absolute device path never leaks (public repo).
"""
from __future__ import annotations

import os
from pathlib import Path

from opencode_search.graph.store import GraphStore

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
_LEAF_TITLES = ("(leaf)",)  # placeholder L2 communities stamped by _enrich_project


def _wiki_llm_on() -> bool:
    return os.environ.get("OSE_WIKI_LLM", "1") != "0"


def _narrate(context: str, kind: str) -> str:
    """DeepSeek-synthesized narrative for `context`, or '' to signal the caller to template.

    Returns '' (→ deterministic templated prose) when the kill-switch is set, no key is present,
    or any error occurs. The wiki must always build without the local LLM.
    """
    if not _wiki_llm_on():
        return ""
    try:
        from opencode_search.graph.llm import deepseek_chat, deepseek_key
        if not deepseek_key():
            return ""
        prompt = (
            f"You are documenting a software {kind}. Using ONLY the facts below, write a clear "
            f"2-4 sentence overview: its purpose and how its parts fit together. Name real "
            f"sub-systems; do not invent identifiers; no preamble.\n\n{context}"
        )
        return deepseek_chat(prompt, max_tokens=400).strip()
    except Exception:
        return ""


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


def _mermaid_domain(store: GraphStore, parent_cid: int) -> str:
    """Architecture diagram: inter-community call edges among the L1 children of an L2 domain."""
    kids = {r[0]: (r[1] or f"Community {r[0]}") for r in store._con.execute(
        "SELECT id, title FROM communities WHERE parent_id=?", (parent_cid,)).fetchall()}
    if len(kids) < 2:
        return ""
    ph = ",".join("?" * len(kids))
    rows = store._con.execute(
        f"SELECT s1.community_id, s2.community_id FROM edges e "
        f"JOIN symbols s1 ON e.caller_sid=s1.sid JOIN symbols s2 ON e.callee_sid=s2.sid "
        f"WHERE s1.community_id IN ({ph}) AND s2.community_id IN ({ph}) "
        f"AND s1.community_id!=s2.community_id",
        tuple(kids) + tuple(kids)).fetchall()
    seen: set[tuple[int, int]] = set()
    edges: list[tuple[str, str]] = []
    for a, b in rows:
        if (a, b) not in seen:
            seen.add((a, b))
            edges.append((kids[a], kids[b]))
    return _render_mermaid(edges)


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


def _template_domain(title: str, summary: str, children: list) -> str:
    names = ", ".join(t for _, t, _ in children[:6] if t)
    base = summary or f"The {title} domain."
    return f"{base} It groups {len(children)} sub-communities: {names}." if names else base


def _render_domain(store: GraphStore, cid: int, title: str, summary: str) -> str:
    children = store._con.execute(
        "SELECT id, title, semantic_type FROM communities WHERE parent_id=? ORDER BY id", (cid,)
    ).fetchall()
    # Phase 2c derived view: reuse stored summary (from enrich_community_l2) — zero new tokens.
    # Only fall through to _narrate() when no summary is stored yet.
    if not summary:
        ctx = []
        for kid, ktitle, kstype in children:
            ks = store._con.execute("SELECT summary FROM communities WHERE id=?", (kid,)).fetchone()
            ksum = ks[0] if ks and ks[0] else ""
            ctx.append(f"- {ktitle} [{kstype or 'community'}]: {ksum[:160]}")
        context = f"Domain: {title}\nSub-communities:\n" + "\n".join(ctx)
        summary = _narrate(context, "architecture domain") or _template_domain(title, summary, children)
    narrative = summary
    parts = [f"# {title}", "", "**Architecture Domain**", "", narrative, ""]
    if children:
        parts += ["## Sub-communities", ""]
        parts += [f"- [{kt}](community_{kid}.md) — {_TYPE_LABEL.get(ks or '', 'community')}"
                  for kid, kt, ks in children]
        parts.append("")
    diagram = _mermaid_domain(store, cid)
    if diagram:
        parts += ["## Architecture", "", diagram, ""]
    parts += ["---", "", "[← Index](index.md)"]
    return "\n".join(parts) + "\n"


def _render_index(l1: list, l2: list) -> str:
    n_types = len({(r[3] or "feature") for r in l1})
    parts = ["# Project Wiki", "",
             f"{len(l1)} code communities across {n_types} semantic types.", ""]
    if l2:
        parts += ["## Architecture Domains", ""]
        parts += [f"- [{title}](domain_{cid}.md)" for cid, title, _s, _m in l2]
        parts.append("")
    by_type: dict[str, list] = {}
    for cid, title, _summary, stype, _mc, _parent in l1:
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
    """Write a rich wiki bundle (index + per-community + per-domain pages). Returns page count.

    Community pages are deterministic (reuse cached summaries, cite real sources, draw real
    edges); domain pages add a DeepSeek narrative (templated fallback). Signature/return are
    unchanged so existing callers (sweeps, /api/build_hierarchy, cli) keep working.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    root = _project_root(store)
    l1 = store._con.execute(
        "SELECT id, title, summary, semantic_type, member_count, parent_id FROM communities "
        "WHERE level=1 AND title IS NOT NULL AND title!='' AND summary IS NOT NULL AND summary!='' "
        "ORDER BY id").fetchall()
    ph = ",".join("?" * len(_LEAF_TITLES))
    l2 = store._con.execute(
        f"SELECT id, title, summary, member_count FROM communities "
        f"WHERE level>=2 AND title IS NOT NULL AND title!='' AND title NOT IN ({ph}) ORDER BY id",
        _LEAF_TITLES).fetchall()
    if not l1 and not l2:
        return 0
    count = 0
    (output_dir / "index.md").write_text(_render_index(l1, l2), encoding="utf-8")
    count += 1
    for cid, title, summary, stype, mc, _parent in l1:
        (output_dir / f"community_{cid}.md").write_text(
            _render_community(store, root, cid, title, summary, stype, mc or 0), encoding="utf-8")
        count += 1
    for cid, title, summary, _mc in l2:
        (output_dir / f"domain_{cid}.md").write_text(
            _render_domain(store, cid, title, summary or ""), encoding="utf-8")
        count += 1
    return count


# ── federated index ──────────────────────────────────────────────────────────

def _federation_member_summary(gs: GraphStore) -> dict:
    """Per-member rollup for federation.md: type counts, top L2 domains, key business communities."""
    types = dict(gs._con.execute(
        "SELECT COALESCE(NULLIF(semantic_type,''),'unclassified'), COUNT(*) FROM communities "
        "WHERE level=1 GROUP BY COALESCE(NULLIF(semantic_type,''),'unclassified')").fetchall())
    domains = [r[0] for r in gs._con.execute(
        "SELECT title FROM communities WHERE level>=2 AND title IS NOT NULL AND title!='' "
        "AND title NOT IN ('(leaf)') ORDER BY member_count DESC LIMIT 8").fetchall()]
    top = [r[0] for r in gs._con.execute(
        "SELECT title FROM communities WHERE level=1 AND title IS NOT NULL "
        "AND semantic_type IN ('business_process','business_rule') "
        "ORDER BY member_count DESC LIMIT 8").fetchall()]
    return {"types": types, "domains": domains, "top": top}


def _render_federation(root_path: str, per_member: list) -> str:
    rollup: dict[str, int] = {}
    parts = ["# Federation Overview", "",
             f"Logical entity spanning {len(per_member)} members "
             f"(root: `{os.path.basename(root_path)}`).", ""]
    for path, data in per_member:
        parts += [f"## {os.path.basename(path)}" + ("  _(root)_" if path == root_path else ""), ""]
        if data["domains"]:
            parts.append("**Domains:** " + ", ".join(data["domains"]))
        if data["top"]:
            parts += ["", "**Key business logic:** " + ", ".join(data["top"])]
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
    from opencode_search.core.config import project_wiki_dir
    from opencode_search.daemon.federation import expand_federation, federated_map

    if len(expand_federation(root_path)) < 2:
        return 0
    per_member = federated_map(root_path, _federation_member_summary)
    if len(per_member) < 2:
        return 0
    wiki_dir = project_wiki_dir(root_path)
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / "federation.md").write_text(_render_federation(root_path, per_member), encoding="utf-8")
    return 1
