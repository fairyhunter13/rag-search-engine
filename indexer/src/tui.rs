//! TUI connection tracking.
//!
//! Tracks TUI connections per project so the daemon can start/stop watchers.
//! Extracted from daemon.rs for better organization.

use crate::daemon::{
    canonicalized_paths_cache, CachedCanonicalPath, DaemonState,
};

/// Register a TUI connection for a project.
/// Returns the current connection count for that project.
pub(crate) fn tui_connect_impl(state: &mut DaemonState, key: &str, connection_id: &str) -> serde_json::Value {
    state.tui_projects.insert(key.to_string());
    let connections = state.tui_connections.entry(key.to_string()).or_default();
    connections.insert(connection_id.to_string());
    let count = connections.len();
    tracing::info!(
        "TUI connected: {} (project: {}, total: {})",
        connection_id,
        key,
        count
    );
    serde_json::json!({
        "success": true,
        "connectionId": connection_id,
        "project": key,
        "connectionCount": count,
    })
}

/// Unregister a TUI connection for a project.
/// Returns the remaining connection count and whether the watcher should be stopped.
pub(crate) fn tui_disconnect_impl(
    state: &mut DaemonState,
    key: &str,
    connection_id: &str,
) -> serde_json::Value {
    let mut count = 0;
    let mut should_stop_watcher = false;

    if let Some(connections) = state.tui_connections.get_mut(key) {
        connections.remove(connection_id);
        count = connections.len();
        if count == 0 {
            state.tui_connections.remove(key);
            should_stop_watcher = true;
        }
    }

    tracing::info!(
        "TUI disconnected: {} (project: {}, remaining: {}, stop_watcher: {})",
        connection_id,
        key,
        count,
        should_stop_watcher
    );

    serde_json::json!({
        "success": true,
        "connectionId": connection_id,
        "project": key,
        "connectionCount": count,
        "shouldStopWatcher": should_stop_watcher,
    })
}

/// Get current TUI connection status for a project.
pub(crate) fn tui_connections_impl(state: &mut DaemonState, key: &str) -> serde_json::Value {
    let count = state.tui_connections.get(key).map(|c| c.len()).unwrap_or(0);
    let connections: Vec<&String> = state
        .tui_connections
        .get(key)
        .map(|c| c.iter().collect())
        .unwrap_or_default();

    serde_json::json!({
        "project": key,
        "connectionCount": count,
        "connections": connections,
    })
}

/// Canonicalize project key for consistent lookups (async with caching).
pub(crate) async fn canonicalize_project_key(root: &str) -> String {
    // Fast path: check cache (LruCache needs write lock for get)
    {
        let mut w = canonicalized_paths_cache().write().await;
        if let Some(cached) = w.get(root) {
            return cached.path.clone();
        }
    }

    // Slow path: async canonicalize and cache (LruCache handles eviction automatically)
    let canonical = tokio::fs::canonicalize(root)
        .await
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|_| root.to_string());

    {
        let mut cache = canonicalized_paths_cache().write().await;
        cache.put(root.to_string(), CachedCanonicalPath {
            path: canonical.clone(),
        });
    }

    canonical
}
