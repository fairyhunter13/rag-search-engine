"""LLM enrichment: symbol intent (batch 20/call) + community summary."""
from __future__ import annotations

import json

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
"semantic_type": "<feature|utility|infrastructure|domain|test>"}}"""


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
