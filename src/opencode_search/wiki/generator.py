"""Wiki page generation using LLM and code graph."""
from __future__ import annotations

import logging
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opencode_search.enricher.client import LLMClient
    from opencode_search.graph.storage import GraphStorage

    from .storage import WikiStorage

log = logging.getLogger(__name__)


class WikiGenerator:
    """Generate and manage wiki pages for a project."""

    def __init__(
        self,
        llm: LLMClient,
        wiki: WikiStorage,
        graph: GraphStorage,
    ) -> None:
        self.llm = llm
        self.wiki = wiki
        self.graph = graph

    async def generate_module_page(self, module: str, project_path: str) -> str:
        """Generate wiki page for a module/package."""
        import asyncio

        nodes = [
            n for n in self.graph.all_nodes()
            if n.qualified_name.startswith(module) or n.file.endswith(module)
        ]
        symbols = [n.qualified_name for n in nodes if n.kind != "file"]
        imports = [
            e.to_id for e in self.graph.all_edges()
            if e.kind == "IMPORTS" and any(n.id == e.from_id for n in nodes)
        ]

        content = await asyncio.to_thread(
            self.llm.module_wiki_page, module, symbols, imports
        )
        self.wiki.write_wiki_page(f"module_{_safe_name(module)}", content)
        self.wiki.append_log(f"Generated module page: {module}")
        return content

    async def generate_community_page(self, community_id: int) -> str:
        """Generate wiki page for a Leiden community cluster."""
        import asyncio

        from opencode_search.handlers._enrichment import _sample_community_code

        nodes = self.graph.get_community_nodes(community_id)
        if not nodes:
            log.debug("generate_community_page: community %d has no nodes, skipping", community_id)
            return ""

        summaries = [
            f"{n.qualified_name} ({n.kind})"
            + (f": {n.docstring[:80]}" if n.docstring else "")
            for n in nodes[:30]
        ]
        code_samples = _sample_community_code(nodes[:5])

        title, summary = await asyncio.to_thread(
            self.llm.community_summary, summaries, code_samples
        )
        content = f"# {title}\n\n{summary}\n\n## Members\n\n"
        content += "\n".join(f"- `{n.qualified_name}` ({n.kind})" for n in nodes)

        page_name = f"community_{community_id}"
        self.wiki.write_wiki_page(page_name, content)
        self.wiki.append_log(f"Generated community page: {community_id} — {title}")

        # Update community title/summary in graph DB
        communities = self.graph.get_communities()
        for c in communities:
            if c.id == community_id:
                from datetime import datetime
                c.title = title
                c.summary = summary
                c.generated_at = datetime.now(UTC).isoformat()
                self.graph.upsert_community(c)
                break

        return content

    async def generate_index(self) -> str:
        """Regenerate wiki/index.md from current page list."""
        pages = self.wiki.list_wiki_pages()
        lines = ["# Wiki Index\n"]
        for name in sorted(pages):
            lines.append(f"- [{name}]({name}.md)")
        content = "\n".join(lines)
        self.wiki.write_index(content)
        return content

    async def ingest_raw_source(
        self, source_path: str, project_path: str
    ) -> list[str]:
        """Process a raw doc into wiki pages. Returns list of created page names."""
        import asyncio

        src = Path(source_path)
        if not src.exists():
            raise FileNotFoundError(f"Source not found: {source_path}")

        self.wiki.register_raw_source(source_path)
        content = src.read_text(encoding="utf-8", errors="replace")

        wiki_content = await asyncio.to_thread(
            self.llm.raw_doc_to_wiki, content, src.name
        )
        page_name = _safe_name(src.stem)
        self.wiki.write_wiki_page(page_name, wiki_content)
        self.wiki.append_log(f"Ingested raw source: {src.name} → {page_name}.md")

        # Regenerate index
        await self.generate_index()
        return [page_name]

    async def lint(self) -> dict[str, object]:
        """Health check: orphaned pages, stale pages, empty pages."""
        pages = self.wiki.list_wiki_pages()
        issues: list[str] = []

        # Check index references
        index_content = ""
        index_path = self.wiki.index_path()
        if index_path.exists():
            index_content = index_path.read_text(encoding="utf-8")

        orphans = []
        empty_pages = []
        for name in pages:
            if name not in index_content and name != "index":
                orphans.append(name)
            content = self.wiki.read_wiki_page(name) or ""
            if not content.strip():
                empty_pages.append(name)

        if orphans:
            issues.append(f"Orphaned pages: {orphans}")
        if empty_pages:
            issues.append(f"Empty pages: {empty_pages}")

        return {
            "healthy": len(issues) == 0,
            "total_pages": len(pages),
            "orphans": orphans,
            "empty_pages": empty_pages,
            "issues": issues,
        }


def _safe_name(name: str) -> str:
    """Convert a name to a safe filesystem/page name."""
    return "".join(c if (c.isalnum() or c in "_-") else "_" for c in name).strip("_")
