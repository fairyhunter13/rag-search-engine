"""Handler package — re-exports all public names for backward compatibility."""
from opencode_search.handlers._autopipeline import (
    auto_pipeline_enabled,
    get_pipeline_events,
    handle_auto_pipeline,
    schedule_auto_pipeline,
    schedule_incremental_enrichment,
)
from opencode_search.handlers._business import (
    handle_ask_business,
    handle_business_rules,
    handle_feature_map,
    handle_process_flows,
)
from opencode_search.handlers._chat_router import handle_chat_auto, handle_chat_auto_stream
from opencode_search.handlers._common import (
    _indexing_lock,
    _indexing_status,
    _now_iso,
    _touch_projects_last_active,
    resolve_indexed_project_path,
)
from opencode_search.handlers._debug_trace import handle_debug_trace
from opencode_search.handlers._enrichment import (
    handle_enrich_hierarchy,
    handle_enrich_project,
    handle_get_symbol_intent,
)
from opencode_search.handlers._feature import handle_ask_feature
from opencode_search.handlers._federation import (
    _expand_with_federation,
    handle_add_federation_member,
    handle_discover_federation,
    handle_index_federation,
    handle_list_federation,
    handle_remove_federation_member,
)
from opencode_search.handlers._global_search import handle_global_synthesis
from opencode_search.handlers._graph import (
    handle_callflow_html,
    handle_dedup_nodes,
    handle_detect_impact,
    handle_detect_patterns,
    handle_get_callees,
    handle_get_callers,
    handle_get_communities,
    handle_get_symbol,
    handle_global_search,
    handle_graph_diff,
    handle_graph_export,
    handle_import_cycles,
    handle_project_structure,
    handle_suggest_questions,
    handle_trace_path,
)
from opencode_search.handlers._index import (
    _build_incremental_on_change,
    handle_index_project,
)
from opencode_search.handlers._kb_chat import handle_kb_chat
from opencode_search.handlers._patterns import handle_analyze_patterns_llm
from opencode_search.handlers._pipeline import handle_pipeline
from opencode_search.handlers._pr_impact import handle_pr_impact
from opencode_search.handlers._query import (
    handle_list_indexed_projects,
    handle_project_status,
    handle_search_code,
)
from opencode_search.handlers._service_mesh import handle_detect_service_mesh
from opencode_search.handlers._tree_html import handle_tree_html
from opencode_search.handlers._vacuum import handle_vacuum
from opencode_search.handlers._watch import (
    handle_ensure_project_watching,
    handle_release_project_watch,
    handle_stop_watching,
)
from opencode_search.handlers._wiki import (
    handle_wiki_ingest,
    handle_wiki_lint,
    handle_wiki_query,
)

__all__ = [
    "_build_incremental_on_change",
    "_expand_with_federation",
    "_indexing_lock",
    "_indexing_status",
    "_now_iso",
    "_touch_projects_last_active",
    "auto_pipeline_enabled",
    "get_pipeline_events",
    "handle_add_federation_member",
    "handle_analyze_patterns_llm",
    "handle_ask_feature",
    "handle_auto_pipeline",
    "handle_callflow_html",
    "handle_chat_auto",
    "handle_chat_auto_stream",
    "handle_dedup_nodes",
    "handle_detect_impact",
    "handle_detect_patterns",
    "handle_detect_service_mesh",
    "handle_discover_federation",
    "handle_enrich_hierarchy",
    "handle_enrich_project",
    "handle_ensure_project_watching",
    "handle_get_callees",
    "handle_get_callers",
    "handle_get_communities",
    "handle_get_symbol",
    "handle_get_symbol_intent",
    "handle_global_search",
    "handle_global_synthesis",
    "handle_graph_diff",
    "handle_graph_export",
    "handle_import_cycles",
    "handle_index_federation",
    "handle_index_project",
    "handle_list_federation",
    "handle_list_indexed_projects",
    "handle_pipeline",
    "handle_pr_impact",
    "handle_project_status",
    "handle_project_structure",
    "handle_release_project_watch",
    "handle_remove_federation_member",
    "handle_search_code",
    "handle_stop_watching",
    "handle_suggest_questions",
    "handle_trace_path",
    "handle_tree_html",
    "handle_vacuum",
    "handle_wiki_ingest",
    "handle_wiki_lint",
    "handle_wiki_query",
    "resolve_indexed_project_path",
    "schedule_auto_pipeline",
    "schedule_incremental_enrichment",
]
