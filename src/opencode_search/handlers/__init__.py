"""Handler package — re-exports all public names for backward compatibility."""
from opencode_search.handlers._common import (
    _indexing_lock,
    _indexing_status,
    _now_iso,
    _touch_projects_last_active,
    resolve_indexed_project_path,
)
from opencode_search.handlers._enrichment import (
    handle_enrich_project,
    handle_get_symbol_intent,
)
from opencode_search.handlers._autopipeline import (
    auto_pipeline_enabled,
    handle_auto_pipeline,
    schedule_auto_pipeline,
)
from opencode_search.handlers._patterns import handle_analyze_patterns_llm
from opencode_search.handlers._pipeline import handle_pipeline
from opencode_search.handlers._federation import (
    _expand_with_federation,
    handle_add_federation_member,
    handle_discover_federation,
    handle_index_federation,
    handle_list_federation,
    handle_remove_federation_member,
)
from opencode_search.handlers._graph import (
    handle_detect_impact,
    handle_detect_patterns,
    handle_get_callers,
    handle_get_callees,
    handle_get_communities,
    handle_get_symbol,
    handle_global_search,
    handle_graph_export,
    handle_project_structure,
    handle_trace_path,
)
from opencode_search.handlers._index import (
    _build_incremental_on_change,
    handle_index_project,
)
from opencode_search.handlers._query import (
    handle_list_indexed_projects,
    handle_project_status,
    handle_search_code,
)
from opencode_search.handlers._watch import (
    handle_ensure_project_watching,
    handle_release_project_watch,
    handle_stop_watching,
)
from opencode_search.handlers._wiki import (
    handle_wiki_generate,
    handle_wiki_ingest,
    handle_wiki_lint,
    handle_wiki_query,
    handle_wiki_reindex,
)

__all__ = [
    "_build_incremental_on_change",
    "_expand_with_federation",
    "_indexing_lock",
    "_indexing_status",
    "_now_iso",
    "_touch_projects_last_active",
    "handle_add_federation_member",
    "auto_pipeline_enabled",
    "handle_analyze_patterns_llm",
    "handle_auto_pipeline",
    "handle_detect_impact",
    "schedule_auto_pipeline",
    "handle_detect_patterns",
    "handle_discover_federation",
    "handle_enrich_project",
    "handle_ensure_project_watching",
    "handle_get_callers",
    "handle_get_callees",
    "handle_get_communities",
    "handle_get_symbol",
    "handle_get_symbol_intent",
    "handle_global_search",
    "handle_graph_export",
    "handle_index_federation",
    "handle_project_structure",
    "handle_index_project",
    "handle_list_federation",
    "handle_list_indexed_projects",
    "handle_pipeline",
    "handle_project_status",
    "handle_release_project_watch",
    "handle_remove_federation_member",
    "handle_search_code",
    "handle_stop_watching",
    "handle_trace_path",
    "handle_wiki_generate",
    "handle_wiki_ingest",
    "handle_wiki_lint",
    "handle_wiki_query",
    "handle_wiki_reindex",
    "resolve_indexed_project_path",
]
