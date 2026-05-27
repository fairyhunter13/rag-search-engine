"""Handler package — re-exports all public names for backward compatibility."""
from opencode_search.handlers._common import (
    _indexing_lock,
    _indexing_status,
    _now_iso,
    _touch_projects_last_active,
    resolve_indexed_project_path,
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

__all__ = [
    "_build_incremental_on_change",
    "_indexing_lock",
    "_indexing_status",
    "_now_iso",
    "_touch_projects_last_active",
    "handle_ensure_project_watching",
    "handle_index_project",
    "handle_list_indexed_projects",
    "handle_project_status",
    "handle_release_project_watch",
    "handle_search_code",
    "handle_stop_watching",
    "resolve_indexed_project_path",
]
