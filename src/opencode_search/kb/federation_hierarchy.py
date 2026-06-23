"""Federation-global L3 community synthesis (roll-up, not re-derivation).

Reads already-enriched per-member L2 summaries (zero new per-symbol or L1 calls) and
synthesises ≤8 cross-service domain themes as level=3 rows in the root graph.db.
HR4-safe: synthesis rows only, no cross-repo edges.

Token budget: ≤ ~8 DeepSeek calls per root per enrich, reusing paid-for L2 summaries.
Deterministic with OSE_WIKI_LLM=0 / missing key (templated fallback, byte-identical).

Research grounding: arXiv 2606.02019 (federation composition invariants),
GraphRAG roll-up (root summaries cost 97% fewer tokens than source text via child reuse).
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

_L3_OFFSET = 20_000  # L1 < 10000; L2 in [10000, 20000); L3 starts here


_L3_SYSTEM = (
    "You are documenting a cross-service software architecture domain. "
    "Using ONLY the facts in the user message, write a clear 2-3 sentence overview: "
    "what this domain does and how the services collaborate. "
    "Name real sub-systems; do not invent identifiers; no preamble. "
    'Reply with JSON: {"narrative": "<2-3 sentences>"}'
)


def _l3_narrate(theme: str, child_summaries: list[str]) -> str:
    """Synthesise a 2-3 sentence cross-service narrative, or '' for templated fallback."""
    if os.environ.get("OSE_WIKI_LLM", "1") == "0":
        return ""
    try:
        from opencode_search.graph.llm import _accumulate_llm_tokens, deepseek_extract, deepseek_key
        if not deepseek_key():
            return ""
        context = "\n".join(f"- {s[:200]}" for s in child_summaries[:8])
        raw, usage = deepseek_extract(_L3_SYSTEM, f"Domain: {theme}\n\n{context}", max_tokens=300)
        _accumulate_llm_tokens(usage, "l3")
        import json
        data = json.loads(raw) if raw.strip().startswith("{") else {}
        return str(data.get("narrative", "")).strip()
    except Exception:
        return ""


def _group_by_type(member_l2_rows: list[tuple[str, str, str | None]]) -> list[tuple[str, list[str]]]:
    """Group (member_basename, title, semantic_type) rows into ≤8 themes by semantic_type.

    Returns [(theme_name, [child_summary, ...]), ...] sorted by theme name.
    Deterministic: depends only on input order of unique semantic_type values.
    """
    from collections import defaultdict
    groups: dict[str, list[str]] = defaultdict(list)
    for _member, title, stype in member_l2_rows:
        key = stype or "domain"
        groups[key].append(title)
    themes = sorted(groups.items())[:8]
    return themes


def build_federation_hierarchy(root_path: str) -> int:
    """Build federation-global L3 community rows in the root graph.db.

    Each L3 row groups member L2 domains by semantic_type into a cross-service theme.
    Idempotent: DELETE existing level>=3 rows first. HR4-safe: creates no edges.
    Returns count of L3 rows written, else 0 if not a federated root or no L2 data.
    """
    from opencode_search.core.config import project_graph_db
    from opencode_search.daemon.federation import expand_federation, federated_map
    from opencode_search.graph.store import GraphStore

    if len(expand_federation(root_path)) < 2:
        return 0

    # Freshness guard: skip L3 rebuild if root graph.db was written within 1800s.
    # Caps a member-edit storm at ≤1 L3 rebuild per 30 min per root (mirrors BPRE reuse).
    import time as _time
    _root_gdb = project_graph_db(root_path)
    if _root_gdb.exists():
        try:
            if (_time.time() - _root_gdb.stat().st_mtime) < 1800:
                _gs_tmp = GraphStore(_root_gdb)
                try:
                    _n3 = _gs_tmp._con.execute(
                        "SELECT COUNT(*) FROM communities WHERE level>=3"
                    ).fetchone()[0]
                finally:
                    _gs_tmp.close()
                if _n3 > 0:
                    log.debug("build_federation_hierarchy: L3 fresh (%d themes), skip", _n3)
                    return _n3
        except Exception:
            pass

    # Collect per-member L2 domain rows (title + semantic_type only — never reads symbols).
    def _member_l2(gs: GraphStore) -> list[tuple]:
        return gs._con.execute(
            "SELECT title, COALESCE(semantic_type, 'domain') FROM communities "
            "WHERE level=2 AND title IS NOT NULL AND title!='' AND title NOT IN ('(leaf)')"
        ).fetchall()

    per_member = federated_map(root_path, _member_l2)
    member_l2_rows: list[tuple[str, str, str | None]] = []
    for mpath, rows in per_member:
        basename = os.path.basename(mpath)
        for title, stype in rows:
            member_l2_rows.append((basename, title, stype))

    if not member_l2_rows:
        return 0

    themes = _group_by_type(member_l2_rows)

    root_gdb = project_graph_db(root_path)
    if not root_gdb.exists():
        return 0
    gs = GraphStore(root_gdb)
    try:
        gs._con.execute("DELETE FROM communities WHERE level>=3")
        gs.commit()

        written = 0
        for i, (stype, child_titles) in enumerate(themes):
            cid = _L3_OFFSET + i
            theme_label = stype.replace("_", " ").title()
            summary = _l3_narrate(theme_label, child_titles)
            if not summary:
                summary = (
                    f"Cross-service {theme_label} domain spanning {len(child_titles)} "
                    f"member-service architecture communities."
                )
            gs.upsert_community(
                cid, level=3,
                title=f"Federation: {theme_label}",
                summary=summary,
                member_count=len(child_titles),
                semantic_type="domain",
            )
            written += 1

        gs.commit()
        return written
    finally:
        gs.close()
