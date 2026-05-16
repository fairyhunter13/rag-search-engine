//! Memory watcher management — watches memory/activity directories for file changes
//! and feeds them into the embedding pipeline via storage write queues.
//!
//! Extracted from daemon.rs for better organization.

use std::collections::HashMap;
use std::path::Path;
use std::sync::Arc;
use std::time::Instant;

use tokio::sync::Mutex;

use crate::daemon::{DaemonState, MemoryWatcherState, PendingChanges};
use crate::watcher::{self, WatchEvent};

use anyhow::{Context, Result};

/// Start a memory watcher for a specific directory
pub(crate) async fn start_memory_watcher(
    state: &Arc<Mutex<DaemonState>>,
    root: &Path,
    db_path: &Path,
    scope: &str,
    _tier: &str,
    dimensions: u32,
) -> Result<()> {
    // Check if watcher already exists for this scope
    {
        let s = state.lock().await;
        if s.memory_watchers.contains_key(scope) {
            tracing::debug!("memory watcher already running for scope: {}", scope);
            return Ok(());
        }
    }

    tracing::info!(
        "starting memory watcher for scope: {}, path: {}",
        scope,
        root.display()
    );

    // Create directories if they don't exist
    tokio::fs::create_dir_all(root)
        .await
        .context("failed to create memory directory")?;
    tokio::fs::create_dir_all(db_path.parent().unwrap_or(db_path))
        .await
        .context("failed to create db directory")?;

    // Open or create storage
    let storage = Arc::new(crate::storage::Storage::open(db_path, dimensions).await?);

    // Create write queue for serializing storage operations
    let write_queue = Arc::new(crate::storage::WriteQueue::new(storage.clone(), 32));

    // Setup pending changes buffer
    let pending: Arc<tokio::sync::Mutex<PendingChanges>> =
        Arc::new(tokio::sync::Mutex::new(PendingChanges::new()));

    // Create shutdown channel
    let (shutdown_tx, shutdown_rx) = tokio::sync::watch::channel(false);

    // Start the file watcher (watch for .md files in memory/activity directories)
    let watcher_rx = watcher::watch(
        root,
        &[],
        &[],
        Arc::new(move |path: &std::path::Path| {
            // Only watch .md files in memory/activity directories
            path.extension().map(|e| e == "md").unwrap_or(false)
        }),
    )?;

    // Spawn task to collect events into pending
    let pending_for_collector = pending.clone();
    let scope_for_collector = scope.to_string();
    let mut shutdown_rx_clone = shutdown_rx.clone();
    tokio::spawn(async move {
        let mut watcher_rx = watcher_rx;
        loop {
            tokio::select! {
                event = watcher_rx.recv() => {
                    match event {
                        Some(WatchEvent::Changed(paths)) => {
                            let mut p = pending_for_collector.lock().await;
                            p.add_changed(paths);
                        }
                        Some(WatchEvent::Deleted(paths)) => {
                            let mut p = pending_for_collector.lock().await;
                            p.add_deleted(paths);
                        }
                        None => {
                            tracing::debug!("memory watcher channel closed for {}", scope_for_collector);
                            break;
                        }
                    }
                }
                _ = shutdown_rx_clone.changed() => {
                    if *shutdown_rx_clone.borrow() {
                        tracing::debug!("memory watcher shutdown for {}", scope_for_collector);
                        break;
                    }
                }
            }
        }
    });

    // Store in state
    {
        let mut s = state.lock().await;
        s.memory_watchers.insert(
            scope.to_string(),
            MemoryWatcherState {
                root: Arc::new(root.to_path_buf()),
                db_path: Arc::new(db_path.to_path_buf()),
                storage,
                write_queue: Some(write_queue),
                pending,
                failed_files: Arc::new(tokio::sync::Mutex::new(HashMap::new())),
                _shutdown_tx: shutdown_tx,
                started_at: Instant::now(),
                _scope: scope.to_string(),
            },
        );
    }

    tracing::info!("memory watcher started for scope: {}", scope);
    Ok(())
}

/// Start built-in memory watchers (global memories) on daemon startup
pub(crate) async fn start_builtin_memory_watchers(
    state: &Arc<Mutex<DaemonState>>,
    shared_path: &Path,
) -> Result<()> {
    tracing::debug!("starting built-in memory watchers");

    // Watch global memories
    let global_memory_dir = shared_path.join("memories").join("global");
    let global_db = global_memory_dir.join(".lancedb");

    if let Err(e) = start_memory_watcher(
        state,
        &global_memory_dir,
        &global_db,
        "global",
        "budget",
        1024,
    )
    .await
    {
        tracing::warn!("failed to start global memory watcher: {}", e);
    }

    tracing::info!("built-in memory watchers started");
    Ok(())
}

/// Start project-specific memory and activity watchers
pub(crate) async fn start_project_memory_watchers(
    state: &Arc<Mutex<DaemonState>>,
    shared_path: &Path,
    project_id: &str,
    tier: &str,
    dimensions: u32,
) -> Result<()> {
    tracing::debug!(
        "starting project memory watchers for project_id: {}",
        project_id
    );

    let project_dir = shared_path.join("projects").join(project_id);

    // Watch project memories
    let memory_dir = project_dir.join("memories");
    let memory_db = memory_dir.join(".lancedb");
    let memory_scope = format!("project:{}:memories", project_id);

    if let Err(e) = start_memory_watcher(
        state,
        &memory_dir,
        &memory_db,
        &memory_scope,
        tier,
        dimensions,
    )
    .await
    {
        tracing::warn!("failed to start project memory watcher: {}", e);
    }

    // Watch project activity
    let activity_dir = project_dir.join("activity");
    let activity_db = activity_dir.join(".lancedb");
    let activity_scope = format!("project:{}:activity", project_id);

    if let Err(e) = start_memory_watcher(
        state,
        &activity_dir,
        &activity_db,
        &activity_scope,
        tier,
        dimensions,
    )
    .await
    {
        tracing::warn!("failed to start project activity watcher: {}", e);
    }

    tracing::info!(
        "project memory watchers started for project_id: {}",
        project_id
    );
    Ok(())
}
