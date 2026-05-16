//! TUI connection tracking.
//!
//! Tracks TUI connections per project so the daemon can start/stop watchers.
//! Extracted from daemon.rs for better organization.

use std::time::Instant;

use crate::daemon::{
    canonicalized_paths_cache, CachedCanonicalPath, DaemonState, MAX_CANONICALIZED_CACHE_SIZE,
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
    // Fast path: check cache and update access time
    {
        let r = canonicalized_paths_cache().read().await;
        if let Some(cached) = r.get(root) {
            let path = cached.path.clone();
            drop(r);

            // Update last_access with write lock
            let mut w = canonicalized_paths_cache().write().await;
            if let Some(entry) = w.get_mut(root) {
                entry.last_access = Instant::now();
            }
            return path;
        }
    }

    // Slow path: async canonicalize and cache with LRU eviction
    let canonical = tokio::fs::canonicalize(root)
        .await
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|_| root.to_string());

    {
        let mut cache = canonicalized_paths_cache().write().await;

        // LRU eviction: remove oldest entry if cache is full
        if cache.len() >= MAX_CANONICALIZED_CACHE_SIZE && !cache.contains_key(root) {
            if let Some(oldest_key) = cache
                .iter()
                .min_by_key(|(_, cached)| cached.last_access)
                .map(|(k, _)| k.clone())
            {
                tracing::debug!(
                    "canonicalized path cache full ({}), evicting oldest entry: {}",
                    MAX_CANONICALIZED_CACHE_SIZE,
                    oldest_key
                );
                cache.remove(&oldest_key);
            }
        }

        cache.insert(root.to_string(), CachedCanonicalPath {
            path: canonical.clone(),
            last_access: Instant::now(),
        });
    }

    canonical
}
