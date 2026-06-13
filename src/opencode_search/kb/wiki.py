"""Generate wiki pages from community summaries."""
from __future__ import annotations

from pathlib import Path

from opencode_search.graph.store import GraphStore


def build_wiki(store: GraphStore, output_dir: Path) -> int:
    """Write one .md page per enriched community to output_dir. Returns page count."""
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = store._con.execute(
        "SELECT id, level, title, summary FROM communities "
        "WHERE title IS NOT NULL AND title != '' AND summary IS NOT NULL AND summary != '' "
        "ORDER BY level, id"
    ).fetchall()
    count = 0
    for cid, _level, title, summary in rows:
        (output_dir / f"community_{cid}.md").write_text(
            f"# {title}\n\n{summary}\n", encoding="utf-8"
        )
        count += 1
    return count
