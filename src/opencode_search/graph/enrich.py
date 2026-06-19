"""LLM enrichment: symbol intent (batch 20/call) + community summary + semantic type backfill."""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from opencode_search.graph.llm import chat
from opencode_search.graph.store import GraphStore

_INTENT_PROMPT = """\
For each symbol below, reply with ONLY a JSON array of short intent strings (≤8 words each).
Format: ["intent for 1", "intent for 2", ...]
Symbols:
{symbols}"""

_L2_COMMUNITY_PROMPT = """\
Summarize this architecture domain in 2 sentences. Cover: purpose, main sub-systems.
Sub-communities: {children}
Reply ONLY with JSON: {{"title": "<short domain name>", "summary": "<2 sentences>"}}"""

_COMMUNITY_PROMPT = """\
Summarize this code community in 2 sentences. Cover: purpose, main patterns.
Members: {members}
Reply ONLY with JSON: {{"title": "<short title>", "summary": "<2 sentences>", \
"semantic_type": "<business_process|business_rule|feature|utility|infrastructure|domain|test>"}}
Hint: if orchestrating multi-step workflows -> business_process; if enforcing constraints/validation -> business_rule."""

# ---------------------------------------------------------------------------
# Semantic type backfill -- 3-stage pipeline (keyword -> batch LLM -> commit)
# ---------------------------------------------------------------------------

# test must be checked first -- mocks must not become business_process/rule
_KEYWORD_ORDER = ["test", "business_process", "business_rule", "infrastructure", "domain", "utility"]

_KEYWORD_PATTERNS: dict[str, re.Pattern] = {
    "test": re.compile(r"\b(test|mock|stub|fake|fixture|assert|bench|suite)\b", re.I),
    "business_process": re.compile(
        r"\b(management|orchestration|process|pipeline|workflow|scheduling|"
        r"dispatch|coordinator|fulfillment|lifecycle|queue)\b", re.I
    ),
    "business_rule": re.compile(
        r"\b(validation|rule|guard|check|constraint|eligibility|clash|"
        r"enforcement|policy|compliance|criteria|allowance)\b", re.I
    ),
    "infrastructure": re.compile(
        r"\b(database|cache|http|grpc|client|repository|store|gateway|"
        r"connection|pool|config|logging|tracing|metric|middleware)\b", re.I
    ),
    "domain": re.compile(r"\b(model|entity|proto|message|struct|enum|schema|type|dto|vo)\b", re.I),
    "utility": re.compile(
        r"\b(util|helper|format|convert|parse|encode|decode|transform|serializ)\b", re.I
    ),
}

_VALID_TYPES = frozenset(
    ("business_process", "business_rule", "feature", "utility", "infrastructure", "domain", "test")
)


def classify_by_keyword(title: str) -> str | None:
    """Return semantic_type from title keywords alone, or None if ambiguous."""
    for type_name in _KEYWORD_ORDER:
        if _KEYWORD_PATTERNS[type_name].search(title):
            return type_name
    return None


_BACKFILL_BATCH_PROMPT = """\
Classify each community into exactly ONE semantic_type.
Types: business_process|business_rule|feature|utility|infrastructure|domain|test
Examples: "Clash Detection"->business_rule, "Management System"->business_process, "Mock Service"->test
Communities: {items}
Reply ONLY with JSON array: [{{"id":<N>,"semantic_type":"<type>"}}]"""


def _classify_batch(batch: list[tuple[int, str, str]]) -> list[tuple[int, str]]:
    """Send one LLM call to classify <=10 communities. Returns [(cid, semantic_type)]."""
    items_str = json.dumps([
        {"id": cid, "title": title, "summary": (summary or "")[:120]}
        for cid, title, summary in batch
    ], ensure_ascii=False)
    try:
        raw = chat(_BACKFILL_BATCH_PROMPT.format(items=items_str))
        parsed = json.loads(raw.strip())
        return [
            (int(item["id"]),
             item.get("semantic_type", "feature")
             if item.get("semantic_type", "") in _VALID_TYPES else "feature")
            for item in parsed
        ]
    except Exception:
        return [(cid, "feature") for cid, _, _ in batch]


def upgrade_to_business_types(store: GraphStore) -> int:
    """Keyword-only upgrade: reclassify existing (old) types → business_process/rule where title matches.

    Targets communities already classified with the pre-expansion vocabulary (test/domain/utility
    /feature/infra) that should be business_process or business_rule. Idempotent: returns 0 on 2nd run.
    """
    rows = store._con.execute(
        "SELECT id, title FROM communities WHERE level=1 "
        "AND semantic_type NOT IN ('business_process', 'business_rule', 'unknown')"
        "AND semantic_type IS NOT NULL AND title IS NOT NULL"
    ).fetchall()
    count = 0
    for cid, title in rows:
        new_type = classify_by_keyword(title or "")
        if new_type in ("business_process", "business_rule"):
            store._con.execute("UPDATE communities SET semantic_type=? WHERE id=?", (new_type, cid))
            count += 1
    if count:
        store.commit()
    return count


def backfill_semantic_types(store: GraphStore, thermal_guard_fn=None) -> int:
    """Classify L1 communities with summary but no semantic_type. Idempotent: returns 0 on 2nd run."""
    import time
    rows = store._con.execute(
        "SELECT id, title, summary, member_count FROM communities "
        "WHERE level=1 AND summary IS NOT NULL AND summary!='' AND semantic_type IS NULL"
    ).fetchall()
    if not rows:
        return 0
    keyword_updates: list[tuple[int, str]] = []
    llm_pending: list[tuple[int, str, str]] = []
    for cid, title, summary, mc in rows:
        title = title or ""
        if (mc is not None and mc < 3) or len(title) < 10:
            keyword_updates.append((cid, "unknown"))
        elif (t := classify_by_keyword(title)) is not None:
            keyword_updates.append((cid, t))
        else:
            llm_pending.append((cid, title, summary or ""))
    for cid, stype in keyword_updates:
        store._con.execute("UPDATE communities SET semantic_type=? WHERE id=?", (stype, cid))
    batches = [llm_pending[i : i + 10] for i in range(0, len(llm_pending), 10)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_classify_batch, b) for b in batches]
        for future in as_completed(futures):
            if thermal_guard_fn is not None and thermal_guard_fn():
                time.sleep(3)
            try:
                for cid, stype in future.result():
                    store._con.execute(
                        "UPDATE communities SET semantic_type=? WHERE id=?", (stype, cid))
            except Exception:
                pass
    store.commit()
    return len(rows)


def enrich_community_l2(store: GraphStore, community_id: int) -> None:
    """Assign title+summary to one L2 community from its enriched L1 child summaries."""
    rows = store._con.execute(
        "SELECT title, summary FROM communities "
        "WHERE parent_id=? AND summary IS NOT NULL AND summary!=''",
        (community_id,),
    ).fetchall()
    if not rows:
        return
    children = "; ".join(f"{r[0]}: {r[1][:100]}" for r in rows if r[0])
    try:
        raw = chat(_L2_COMMUNITY_PROMPT.format(children=children[:2000]))
        data = json.loads(raw.strip())
        store.upsert_community(
            community_id, level=2,
            title=data.get("title", "")[:200],
            summary=data.get("summary", "")[:2000],
            member_count=len(rows),
        )
        store.commit()
    except Exception:
        pass


def enrich_symbols(store: GraphStore, batch_size: int = 20) -> int:
    """Assign LLM-generated intent to symbols that lack one. Returns count enriched."""
    symbols = [s for s in store.list_symbols() if not s.get("intent")]
    enriched = 0
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i : i + batch_size]
        lines = "\n".join(
            f"{j + 1}. [{s['kind']}] {s['name']} in {s['file']}"
            for j, s in enumerate(batch)
        )
        try:
            raw = chat(_INTENT_PROMPT.format(symbols=lines))
            intents = json.loads(raw.strip())
            if isinstance(intents, list):
                for sym, intent in zip(batch, intents, strict=False):
                    store.set_intent(sym["sid"], str(intent)[:120])
                    enriched += 1
        except Exception:
            pass
    store.commit()
    return enriched


def enrich_community(store: GraphStore, community_id: int) -> None:
    """Assign title + summary + semantic_type to one community via LLM."""
    rows = store._con.execute(
        "SELECT name,kind,file FROM symbols WHERE community_id=? LIMIT 30",
        (community_id,),
    ).fetchall()
    if not rows:
        return
    members = "; ".join(f"{r[1]} {r[0]}" for r in rows)
    try:
        raw = chat(_COMMUNITY_PROMPT.format(members=members[:2000]))
        data = json.loads(raw.strip())
        store.upsert_community(
            community_id, level=1,
            title=data.get("title", "")[:200],
            summary=data.get("summary", "")[:2000],
            member_count=len(rows),
            semantic_type=data.get("semantic_type", "")[:50],
        )
        store.commit()
    except Exception:
        pass
