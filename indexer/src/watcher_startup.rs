//! Watcher startup, repair, and lifecycle management.
//!
//! Extracted from daemon.rs to keep the HTTP daemon module focused on request dispatch.
//! These functions handle starting/stopping internal file watchers, corruption recovery,
//! and background re-indexing.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::Result;
use tokio::sync::Mutex;

use crate::daemon::{
    active_indexes, dispatch_unified, invalidate_storage_cache, DaemonState, DroppedEventStats,
    PendingChanges, WatcherState, DEFAULT_MAX_PENDING_FILES, WATCHER_START_RETRY_DELAYS,
};
use crate::handlers::status_impl;
use crate::links::*;
use crate::memory_watcher::*;
use crate::search::embed_concurrency;
use crate::tui::canonicalize_project_key;
use crate::watcher::{self, WatchEvent};
use crate::{cli, config, discover, storage};

/// Guard that deregisters an active index on drop.
struct StartupGuard(String);
impl Drop for StartupGuard {
    fn drop(&mut self) {
        let k = self.0.clone();
        tokio::spawn(async move {
            active_indexes().lock().await.remove(&k);
        });
    }
}

/// Spawn background rebuild: run full index then start watcher (with retry).
/// Shared between the error path and corruption path.
fn spawn_background_rebuild(
    state: &Arc<Mutex<DaemonState>>,
    root_str: &str,
    db_str: Option<&str>,
    tier_str: &str,
    dims: u32,
    db_path_for_guard: &Path,
) {
    let state_clone = state.clone();
    let root_str = root_str.to_string();
    let db_str = db_str.map(|s| s.to_string());
    let tier_str = tier_str.to_string();
    let guard_key = db_path_for_guard.to_string_lossy().to_string();

    tokio::spawn(async move {
        let db_path = db_str
            .clone()
            .map(PathBuf::from)
            .unwrap_or_else(|| storage::storage_path(&PathBuf::from(&root_str)));
        active_indexes().lock().await.insert(guard_key.clone());
        let _guard = StartupGuard(guard_key.clone());

        tracing::info!("startup_check: starting background rebuild for {}", root_str);

        if let Err(e) = cli::run_indexing_pub(
            &PathBuf::from(&root_str),
            &db_path,
            &tier_str,
            dims,
            true,  // force
            None,  // daily_cost_limit
            false, // verbose
            &[],   // exclude
            &[],   // include
            embed_concurrency(),
            None,  // scan_concurrency
            true,  // quiet
            false, // json_lines
        )
        .await
        {
            tracing::warn!("startup_check: background indexing failed: {}", e);
            return;
        }

        tracing::info!("startup_check: background indexing completed for {}", root_str);

        // Then start watcher (with retry)
        let db_ref = db_str.as_deref();
        let delays = WATCHER_START_RETRY_DELAYS;
        let mut started = false;
        for (attempt, &delay) in delays.iter().enumerate() {
            match watcher_start_internal(&state_clone, &root_str, db_ref, Some(&tier_str), false).await {
                Ok(_) => {
                    started = true;
                    break;
                }
                Err(e) => {
                    tracing::warn!(
                        "startup_check: background watcher start failed (attempt {}/{}): {}",
                        attempt + 1,
                        delays.len(),
                        e
                    );
                    tokio::time::sleep(Duration::from_millis(delay)).await;
                }
            }
        }
        if !started {
            tracing::error!(
                "startup_check: background watcher start failed after {} retries for {}",
                delays.len(),
                root_str
            );
        }
    });
}

// ============================================================================
// startup_check — auto-fix corruption and/or start watcher
// ============================================================================

/// Startup check: auto-fix corruption and/or start watcher.
/// All decision logic is here in the daemon - TUI just displays results.
///
/// Returns JSON with:
/// - action: "none" | "rebuilt" | "rebuilding" | "watcher_started" | "error"
/// - message: Human-readable description
/// - corrupted: bool (was index corrupted?)
/// - indexed: bool (is index present?)
/// - watching: bool (is watcher running after this call?)
pub(crate) async fn startup_check_impl(
    state: &Arc<Mutex<DaemonState>>,
    root: &str,
    db: Option<&str>,
    tier: Option<&str>,
    dims: u32,
) -> serde_json::Value {
    let root_path = match tokio::fs::canonicalize(root).await {
        Ok(p) => p,
        Err(e) => {
            return serde_json::json!({
                "action": "error",
                "message": format!("Invalid root path: {}", e),
                "corrupted": false,
                "indexed": false,
                "watching": false,
            })
        }
    };

    let db_path = db
        .map(PathBuf::from)
        .unwrap_or_else(|| storage::storage_path(&root_path));

    // Check if db exists
    if !tokio::fs::try_exists(&db_path).await.unwrap_or(false) {
        return serde_json::json!({
            "action": "none",
            "message": "No index exists yet",
            "corrupted": false,
            "indexed": false,
            "watching": false,
        });
    }

    let tier = tier.unwrap_or("budget");

    // Get status to check for corruption
    let status = match status_impl(Some(root), Some(db_path.to_str().unwrap_or("")), dims).await {
        Ok(s) => s,
        Err(e) => {
            // Any error reading the index when the database directory exists means
            // the index can't be used — treat it as recoverable corruption.
            // This catches LanceDB version incompatibilities, IO errors during
            // table scans, and other patterns that `is_corruption_error()` does
            // not recognise but are still unrecoverable without a rebuild.
            let is_known_corruption = storage::is_corruption_error(&e);
            if is_known_corruption {
                tracing::warn!(
                    "startup_check: status check failed due to corruption for {}: {}",
                    root_path.display(),
                    e
                );
            } else {
                tracing::warn!(
                    "startup_check: status check failed (not a recognised corruption \
                     pattern — treating as recoverable) for {}: {}",
                    root_path.display(),
                    e
                );
            }

            // Clear the corrupted/unreadable index
            if let Err(clear_err) = storage::clear_corrupted_index(&db_path) {
                return serde_json::json!({
                    "action": "error",
                    "message": format!("Failed to clear corrupted index: {}", clear_err),
                    "corrupted": true,
                    "indexed": false,
                    "watching": false,
                    "corruptionErrors": [e.to_string()],
                });
            }

            // Invalidate storage cache
            invalidate_storage_cache(&db_path).await;

            // Spawn background rebuild: run full index then start watcher (non-blocking)
            spawn_background_rebuild(state, root, db, tier, dims, &db_path);

            return serde_json::json!({
                "action": "rebuilding",
                "message": format!(
                    "Detected unreadable index ({}), cleared and started rebuild in background",
                    e
                ),
                "corrupted": is_known_corruption,
                "indexed": false,
                "watching": false,
                "corruptionErrors": [e.to_string()],
            });
        }
    };

    let corrupted = status["corrupted"].as_bool().unwrap_or(false);
    let indexed = status["indexed"].as_bool().unwrap_or(false);
    let exists = status["exists"].as_bool().unwrap_or(false);
    let corruption_errors: Vec<String> = status["corruptionErrors"]
        .as_array()
        .map(|arr: &Vec<serde_json::Value>| {
            arr.iter()
                .filter_map(|v: &serde_json::Value| v.as_str().map(String::from))
                .collect()
        })
        .unwrap_or_default();

    // Check if already watching
    let key = root_path.to_string_lossy().to_string();
    let already_watching = {
        let s = state.lock().await;
        s.watchers.contains_key(&key)
    };

    // Case 1: Index is corrupted - clear and rebuild
    if corrupted {
        tracing::info!(
            "startup_check: detected corrupted index for {}, errors: {:?}",
            root_path.display(),
            corruption_errors
        );

        // Clear the corrupted index
        if let Err(e) = storage::clear_corrupted_index(&db_path) {
            return serde_json::json!({
                "action": "error",
                "message": format!("Failed to clear corrupted index: {}", e),
                "corrupted": true,
                "indexed": false,
                "watching": false,
                "corruptionErrors": corruption_errors,
            });
        }

        // Invalidate storage cache
        invalidate_storage_cache(&db_path).await;

            // Spawn background rebuild: run full index then start watcher (non-blocking)
            spawn_background_rebuild(state, root, db, tier, dims, &db_path);
            return serde_json::json!({
            "action": "rebuilding",
            "message": format!("Cleared corrupted index and started rebuild in background. Errors were: {}", corruption_errors.join(", ")),
            "corrupted": true,
            "indexed": false,
            "watching": false, // Not watching yet, will start after rebuild
            "corruptionErrors": corruption_errors,
        });
    }

    // Case 2: Index exists, not corrupted, not watching - start watcher in background
    if exists && !already_watching {
        // Spawn watcher in background (non-blocking)
        let state_clone = state.clone();
        let root_str = root.to_string();
        let db_str = db.map(|s| s.to_string());
        let tier_str = tier.to_string();

        tokio::spawn(async move {
            let db_ref = db_str.as_deref();
            let delays = WATCHER_START_RETRY_DELAYS;
            let mut started = false;
            for (attempt, &delay) in delays.iter().enumerate() {
                match watcher_start_internal(
                    &state_clone,
                    &root_str,
                    db_ref,
                    Some(&tier_str),
                    false,
                )
                .await
                {
                    Ok(_) => {
                        started = true;
                        break;
                    }
                    Err(e) => {
                        tracing::warn!(
                            "startup_check: background watcher_start failed (attempt {}/{}): {}",
                            attempt + 1,
                            delays.len(),
                            e
                        );
                        tokio::time::sleep(Duration::from_millis(delay)).await;
                    }
                }
            }
            if !started {
                tracing::error!(
                    "startup_check: background watcher_start failed after {} retries for {}",
                    delays.len(),
                    root_str
                );
            }
        });

        return serde_json::json!({
            "action": "watcher_starting",
            "message": "Starting watcher in background",
            "corrupted": false,
            "indexed": true,
            // The watcher task has been spawned — watching is in progress.
            // Return true so callers don't redundantly re-trigger startup_check.
            "watching": true,
        });
    }

    // Case 3: Everything is fine, nothing to do
    serde_json::json!({
        "action": "none",
        "message": if already_watching { "Index healthy, watcher already running" } else { "Index healthy" },
        "corrupted": false,
        "indexed": indexed,
        "watching": already_watching,
    })
}

// ============================================================================
// watcher_start_internal — internal watcher start
// ============================================================================

/// Start an internal watcher for a project (runs within daemon, no external process)
pub(crate) fn watcher_start_internal<'a>(
    state: &'a Arc<Mutex<DaemonState>>,
    root: &'a str,
    db: Option<&'a str>,
    tier: Option<&'a str>,
    _force: bool,
) -> std::pin::Pin<
    Box<dyn std::future::Future<Output = Result<serde_json::Value, anyhow::Error>> + Send + 'a>,
> {
    Box::pin(async move {
        // Use consistent fallback pattern for canonicalization (matches watcher_stop, watcher_status, etc.)
        // This ensures HashMap keys are consistent even when path can't be canonicalized.
        let root_path = tokio::fs::canonicalize(root)
            .await
            .unwrap_or_else(|_| PathBuf::from(root));
        let key = root_path.to_string_lossy().to_string();

        // Check if already watching
        {
            let s = state.lock().await;
            if s.watchers.contains_key(&key) {
                return Ok(serde_json::json!({
                    "success": true,
                    "started": false,
                    "internal": true,
                    "message": "already_watching"
                }));
            }
        }

        // Resolve paths
        let db_path = if let Some(db) = db {
            PathBuf::from(db)
        } else {
            storage::storage_path(&root_path)
        };

        // tier resolution is deferred until after storage is opened below (to avoid
        // a second open here); keep the Option for now.
        // === Check if project is indexed ===
        // Watcher should NOT auto-trigger full indexing. If the project is not indexed,
        // reject the request. Users must explicitly run /index first.
        // Check both directory existence AND actual data — Storage::open() creates
        // the chunks.lance directory even with 0 rows, so a directory-only check
        // can falsely report an index as present after a failed initial run.
        let chunks_dir = db_path.join("chunks.lance");
        let index_exists = tokio::fs::try_exists(&db_path).await.unwrap_or(false);
        let chunks_exist = tokio::fs::try_exists(&chunks_dir).await.unwrap_or(false)
            && tokio::fs::metadata(&chunks_dir)
                .await
                .map(|m| m.is_dir())
                .unwrap_or(false);

        if !index_exists || !chunks_exist {
            tracing::info!(
                "watcher_start rejected: project is not indexed (db_exists={}, chunks_exist={})",
                index_exists,
                chunks_exist
            );
            return Ok(serde_json::json!({
                "success": false,
                "started": false,
                "error": "project is not indexed - run /index first"
            }));
        }

        // Even if chunks.lance dir exists, verify it actually has data.
        // Storage::open creates the directory structure even when 0 files are indexed.
        {
            let probe = storage::Storage::open(&db_path, 1024).await;
            if let Ok(store) = probe {
                let count = store.count_chunks().await.unwrap_or(0);
                if count == 0 {
                    let files = store.get_file_count().await.unwrap_or(0);
                    if files == 0 {
                        tracing::info!(
                            "watcher_start rejected: index exists but has 0 files/chunks at {}",
                            db_path.display()
                        );
                        return Ok(serde_json::json!({
                            "success": false,
                            "started": false,
                            "error": "project index is empty (0 files) - run /index first"
                        }));
                    }
                }
            }
        }

        tracing::info!("index exists at {}, starting watcher", db_path.display());

        // === PHASE 2: Start incremental watcher ===
        // Read dimensions from index metadata.
        let temp_storage = storage::Storage::open(&db_path, 1024).await?;
        let dimensions = temp_storage.get_dimensions().await?.unwrap_or(1024);
        let tier = tier.unwrap_or("budget");
        drop(temp_storage);

        // Open storage with correct dimensions
        let storage = Arc::new(storage::Storage::open(&db_path, dimensions).await?);

        // Create write queue for serializing storage operations
        let write_queue = Arc::new(storage::WriteQueue::new(storage.clone(), 100));

        // Load config for filtering and watcher settings
        let project_config = config::load(&root_path);
        let index_cfg = config::effective(&project_config, None, None);
        let max_pending_files = project_config.watcher.max_pending_files;

        tracing::info!(
            "watcher config: max_pending_files={} (default={})",
            max_pending_files,
            DEFAULT_MAX_PENDING_FILES
        );

        // Setup pending changes buffer (with separate changed/deleted tracking)
        let pending: Arc<tokio::sync::Mutex<PendingChanges>> =
            Arc::new(tokio::sync::Mutex::new(PendingChanges::new()));

        // Setup dropped event statistics for monitoring
        let dropped_stats: Arc<tokio::sync::Mutex<DroppedEventStats>> =
            Arc::new(tokio::sync::Mutex::new(DroppedEventStats::default()));

        // Create shutdown channel
        let (shutdown_tx, shutdown_rx) = tokio::sync::watch::channel(false);

        // Start the notify watcher with config-based filter.
        // Pass exclude patterns so the watcher skips heavy dirs (target/, node_modules/, etc.)
        // at setup time rather than exhausting inotify watch limits.
        let root_clone = root_path.clone();
        let cfg_clone = index_cfg.clone();
        let watcher_rx = watcher::watch(
            &root_path,
            &[],
            &index_cfg.exclude,
            Arc::new(move |path| discover::should_index(path, &root_clone, &cfg_clone)),
        )?;

        // Spawn task to collect events into pending with configurable backpressure
        let pending_for_collector = pending.clone();
        let dropped_stats_for_collector = dropped_stats.clone();
        let root_for_collector = root_path.to_string_lossy().to_string();
        let mut shutdown_rx_clone = shutdown_rx.clone();
        tokio::spawn(async move {
            let mut watcher_rx = watcher_rx;
            // Log dropped event stats every 30 seconds if there were drops
            const STATS_LOG_INTERVAL: Duration = Duration::from_secs(30);

            loop {
                tokio::select! {
                    event = watcher_rx.recv() => {
                        match event {
                            Some(WatchEvent::Changed(paths)) => {
                                let mut p = pending_for_collector.lock().await;
                                // Apply backpressure: skip if buffer too large
                                if p.len() < max_pending_files {
                                    p.add_changed(paths);
                                } else {
                                    let dropped_count = paths.len() as u64;
                                    drop(p); // Release pending lock before acquiring stats lock

                                    let mut stats = dropped_stats_for_collector.lock().await;
                                    stats.changed_files_dropped += dropped_count;
                                    stats.backpressure_events += 1;
                                    let now = Instant::now();
                                    stats.last_drop_time = Some(now);

                                    // Log with rate limiting to avoid spam
                                    let should_log = stats.last_log_time
                                        .map(|t| now.duration_since(t) >= STATS_LOG_INTERVAL)
                                        .unwrap_or(true);

                                    if should_log {
                                        tracing::warn!(
                                            project = %root_for_collector,
                                            dropped = dropped_count,
                                            total_changed_dropped = stats.changed_files_dropped,
                                            total_deleted_dropped = stats.deleted_files_dropped,
                                            backpressure_events = stats.backpressure_events,
                                            buffer_limit = max_pending_files,
                                            "pending buffer full - dropping changed files (consider increasing watcher.max_pending_files in .opencode-index.yaml)"
                                        );
                                        stats.last_log_time = Some(now);
                                    }
                                }
                            }
                            Some(WatchEvent::Deleted(paths)) => {
                                let mut p = pending_for_collector.lock().await;
                                if p.len() < max_pending_files {
                                    p.add_deleted(paths);
                                } else {
                                    let dropped_count = paths.len() as u64;
                                    drop(p); // Release pending lock before acquiring stats lock

                                    let mut stats = dropped_stats_for_collector.lock().await;
                                    stats.deleted_files_dropped += dropped_count;
                                    stats.backpressure_events += 1;
                                    let now = Instant::now();
                                    stats.last_drop_time = Some(now);

                                    // Log with rate limiting to avoid spam
                                    let should_log = stats.last_log_time
                                        .map(|t| now.duration_since(t) >= STATS_LOG_INTERVAL)
                                        .unwrap_or(true);

                                    if should_log {
                                        tracing::warn!(
                                            project = %root_for_collector,
                                            dropped = dropped_count,
                                            total_changed_dropped = stats.changed_files_dropped,
                                            total_deleted_dropped = stats.deleted_files_dropped,
                                            backpressure_events = stats.backpressure_events,
                                            buffer_limit = max_pending_files,
                                            "pending buffer full - dropping deleted files (consider increasing watcher.max_pending_files in .opencode-index.yaml)"
                                        );
                                        stats.last_log_time = Some(now);
                                    }
                                }
                            }
                            None => break,
                        }
                    }
                    _ = shutdown_rx_clone.changed() => {
                        if *shutdown_rx_clone.borrow() {
                            // Log final stats on shutdown if any drops occurred
                            let stats = dropped_stats_for_collector.lock().await;
                            if stats.backpressure_events > 0 {
                                tracing::info!(
                                    project = %root_for_collector,
                                    total_changed_dropped = stats.changed_files_dropped,
                                    total_deleted_dropped = stats.deleted_files_dropped,
                                    backpressure_events = stats.backpressure_events,
                                    "watcher shutdown - final dropped event statistics"
                                );
                            }
                            break;
                        }
                    }
                }
            }
        });

        // Create watcher state
        let watcher_state = WatcherState {
            root: Arc::new(root_path.clone()),
            db_path: Arc::new(db_path.clone()),
            storage,
            write_queue: Some(write_queue),
            include_dirs: Arc::new(vec![]),
            symlink_dirs: Arc::new(vec![]),
            pending,
            tier: Arc::from(tier),
            dimensions,
            ops_since_compact: 0,
            _watcher_handle: None,
            shutdown_tx,
            max_pending_files,
            dropped_stats,
            started_at: Instant::now(),
        };

        // Insert into state
        {
            let mut s = state.lock().await;
            s.watchers.insert(key.clone(), watcher_state);
        }

        tracing::info!("started internal watcher for {}", root);

        // Trigger linked project indexing in background after watcher starts.
        // Only indexes linked projects whose index is missing or empty.
        // Gate: OPENCODE_INDEXER_DISABLE_LINK_CASCADE=1 disables entirely.
        if std::env::var("OPENCODE_INDEXER_DISABLE_LINK_CASCADE").as_deref() != Ok("1") {
            let root_for_links = root.to_string();
            let tier_for_links = tier.to_string();
            let dims_for_links = dimensions;
            tokio::spawn(async move {
                // Small delay to let the watcher fully initialize
                tokio::time::sleep(Duration::from_secs(2)).await;
                let links = tokio::task::spawn_blocking({
                    let root = root_for_links.clone();
                    move || cached_discover_links(&root)
                })
                .await
                .unwrap_or_default();
                // Filter to only links that need indexing (missing or empty index)
                let mut stale = Vec::new();
                for link in links {
                    if needs_initial_index(&link.db).await {
                        stale.push(link);
                    }
                }
                if stale.is_empty() {
                    tracing::debug!("link cascade T+2s: all linked indexes up-to-date, skipping");
                    return;
                }
                tracing::info!(
                    "auto-indexing {} linked projects after watcher start",
                    stale.len()
                );
                for link in stale {
                    ensure_link_index(link, &tier_for_links, dims_for_links, &root_for_links).await;
                }
            });
        }
        // After linked project indexing, start watchers for linked projects via dispatch_unified.
        // Only starts watchers for projects whose index already exists (current or just built).
        // Gate: OPENCODE_INDEXER_DISABLE_LINK_CASCADE=1 disables entirely.
        if std::env::var("OPENCODE_INDEXER_DISABLE_LINK_CASCADE").as_deref() != Ok("1") {
            let root_for_links = root.to_string();
            let tier_for_links = tier.to_string();
            let state_for_links = state.clone();
            tokio::spawn(async move {
                // Retry up to 5 times with increasing delays to handle slow indexing.
                // Delays: 10s, 40s, 70s, 100s, 130s (total wait up to ~5.8 minutes).
                // Tracks already-started repos to avoid duplicate watcher starts.
                let mut already_watching: std::collections::HashSet<String> =
                    std::collections::HashSet::new();
                for attempt in 0u64..5 {
                    let delay = Duration::from_secs(10 + attempt * 30);
                    tokio::time::sleep(delay).await;

                    // Discover all linked repos (regardless of index readiness) to track total.
                    // Also filter the ready ones for immediate watcher start.
                    let already_watching_snap = already_watching.clone();
                    let (all_repos, pairs): (Vec<String>, Vec<(String, String)>) =
                        tokio::task::spawn_blocking({
                            let root = root_for_links.clone();
                            move || {
                                let links = cached_discover_links(&root);
                                let all_repos: Vec<String> = links
                                    .iter()
                                    .map(|l| l.repo.to_string_lossy().to_string())
                                    .collect();
                                let pairs: Vec<(String, String)> = links
                                    .into_iter()
                                    .filter(|l| l.db.join("chunks.lance").exists())
                                    .filter(|l| {
                                        !already_watching_snap
                                            .contains(&l.repo.to_string_lossy().to_string())
                                    })
                                    .map(|l| {
                                        (
                                            l.repo.to_string_lossy().to_string(),
                                            l.db.to_string_lossy().to_string(),
                                        )
                                    })
                                    .collect();
                                (all_repos, pairs)
                            }
                        })
                        .await
                        .unwrap_or_default();

                    for (repo, db) in pairs {
                        already_watching.insert(repo.clone());
                        let result = dispatch_unified(
                            state_for_links.clone(),
                            "watcher_start".to_string(),
                            serde_json::json!({ "root": repo, "db": db, "tier": tier_for_links }),
                        )
                        .await;
                        if result["started"].as_bool().unwrap_or(false) {
                            tracing::info!(
                                "started watcher for linked project (attempt {}): {}",
                                attempt + 1,
                                db
                            );
                        } else if result["message"].as_str() == Some("already_watching") {
                            tracing::debug!("watcher already running for linked project: {}", db);
                        } else if result["success"].as_bool() == Some(false) {
                            tracing::debug!(
                                "watcher_start for linked project {}: {:?}",
                                db,
                                result
                            );
                        }
                    }

                    // Only break early when every discovered link has had a watcher start
                    // dispatched. If some repos have no index yet, keep retrying so we
                    // pick them up once their indexing completes.
                    if all_repos.iter().all(|r| already_watching.contains(r)) {
                        tracing::debug!(
                            "link cascade: all {} linked projects have watchers, stopping retries",
                            all_repos.len()
                        );
                        break;
                    }
                }
            });
        }

        // Periodic check task: re-discover linked projects every 60 seconds while the
        // watcher is active. This handles the case where a new symlink or submodule is
        // added after the initial cascade, or where a link's index becomes ready long
        // after startup.
        if std::env::var("OPENCODE_INDEXER_DISABLE_LINK_CASCADE").as_deref() != Ok("1") {
            let root_for_periodic = root.to_string();
            let tier_for_periodic = tier.to_string();
            let state_for_periodic = state.clone();
            let dims_for_periodic = dimensions;
            // Subscribe to the per-watcher shutdown channel so the task exits when the
            // watcher is stopped.
            let mut periodic_shutdown_rx = {
                state
                    .lock()
                    .await
                    .watchers
                    .get(&root.to_string())
                    .map(|w| w.shutdown_tx.subscribe())
            };
            tokio::spawn(async move {
                let interval = Duration::from_secs(
                    std::env::var("OPENCODE_INDEXER_LINK_POLL_SECS")
                        .ok()
                        .and_then(|v| v.parse::<u64>().ok())
                        .unwrap_or(60),
                );
                // Track repos we have already tried to start watchers for, so we don't
                // spam on every tick.
                let mut known_repos: std::collections::HashSet<String> =
                    std::collections::HashSet::new();
                loop {
                    // Sleep or exit on shutdown
                    if let Some(ref mut rx) = periodic_shutdown_rx {
                        tokio::select! {
                            _ = tokio::time::sleep(interval) => {}
                            _ = rx.changed() => {
                                if *rx.borrow() {
                                    tracing::debug!(
                                        "link periodic check: watcher shutdown, exiting"
                                    );
                                    break;
                                }
                            }
                        }
                    } else {
                        tokio::time::sleep(interval).await;
                    }

                    // Re-discover links to detect newly added symlinks/submodules
                    let known_snap = known_repos.clone();
                    let root_snap = root_for_periodic.clone();
                    let tier_snap = tier_for_periodic.clone();
                    let (new_unindexed, new_indexed): (Vec<_>, Vec<(String, String)>) =
                        tokio::task::spawn_blocking(move || {
                            let links = cached_discover_links(&root_snap);
                            let new_unindexed: Vec<Link> = links
                                .iter()
                                .filter(|l| {
                                    !known_snap
                                        .contains(&l.repo.to_string_lossy().to_string())
                                        && !l.db.join("chunks.lance").exists()
                                })
                                .cloned()
                                .collect();
                            let new_indexed: Vec<(String, String)> = links
                                .into_iter()
                                .filter(|l| {
                                    !known_snap
                                        .contains(&l.repo.to_string_lossy().to_string())
                                        && l.db.join("chunks.lance").exists()
                                })
                                .map(|l| {
                                    (
                                        l.repo.to_string_lossy().to_string(),
                                        l.db.to_string_lossy().to_string(),
                                    )
                                })
                                .collect();
                            (new_unindexed, new_indexed)
                        })
                        .await
                        .unwrap_or_default();

                    // Trigger indexing for newly discovered unindexed links
                    for link in new_unindexed {
                        let repo_str = link.repo.to_string_lossy().to_string();
                        tracing::info!(
                            "link periodic check: new unindexed linked project discovered: {}",
                            repo_str
                        );
                        known_repos.insert(repo_str);
                        ensure_link_index(
                            link,
                            &tier_snap,
                            dims_for_periodic,
                            &root_for_periodic,
                        )
                        .await;
                    }

                    // Start watchers for newly indexed links
                    for (repo, db) in new_indexed {
                        tracing::info!(
                            "link periodic check: starting watcher for newly indexed link: {}",
                            db
                        );
                        known_repos.insert(repo.clone());
                        let result = dispatch_unified(
                            state_for_periodic.clone(),
                            "watcher_start".to_string(),
                            serde_json::json!({ "root": repo, "db": db, "tier": tier_snap }),
                        )
                        .await;
                        if result["started"].as_bool().unwrap_or(false) {
                            tracing::info!("link periodic check: watcher started for: {}", db);
                        } else if result["success"].as_bool() == Some(false) {
                            tracing::debug!(
                                "link periodic check: watcher_start failed for {}: {:?}",
                                db,
                                result
                            );
                        }
                    }
                }
            });
        }

        // Start project memory and activity watchers in background
        // Extract project_id from root path for memory/activity directories
        let project_id = storage::git_project_id(&root_path);
        if let Some(shared) = dirs::data_local_dir() {
            let shared = shared.join("opencode");
            let state_for_mem = state.clone();
            let tier_for_mem = tier.to_string();
            tokio::spawn(async move {
                tokio::time::sleep(Duration::from_millis(100)).await;
                if let Err(e) = start_project_memory_watchers(
                    &state_for_mem,
                    &shared,
                    &project_id,
                    &tier_for_mem,
                    dimensions,
                )
                .await
                {
                    tracing::debug!(
                        "failed to start project memory watchers for {}: {}",
                        project_id,
                        e
                    );
                }
            });
        }

        Ok(serde_json::json!({
            "success": true,
            "started": true,
            "internal": true,
            "dbPath": db_path.to_string_lossy(),
        }))
    })
}

// ============================================================================
// watcher_stop_internal — internal watcher stop
// ============================================================================

/// Stop an internal watcher for a project
pub(crate) async fn watcher_stop_internal(
    state: &Arc<Mutex<DaemonState>>,
    root: &str,
) -> Result<serde_json::Value, anyhow::Error> {
    const DRAIN_TIMEOUT: Duration = Duration::from_secs(30);

    // Use canonicalize_project_key for consistent HashMap lookups
    let key = canonicalize_project_key(root).await;

    // Signal shutdown first
    {
        let s = state.lock().await;
        if let Some(watcher) = s.watchers.get(&key) {
            let _ = watcher.shutdown_tx.send(true);
        } else {
            return Ok(serde_json::json!({
                "success": false,
                "stopped": false,
                "error": "no_internal_watcher",
            }));
        }
    }

    // Wait briefly for processing to settle
    tokio::time::sleep(Duration::from_millis(100)).await;

    // Drain WriteQueue before removing watcher
    {
        let mut s = state.lock().await;
        if let Some(watcher) = s.watchers.get_mut(&key) {
            match tokio::time::timeout(DRAIN_TIMEOUT, watcher.drain_write_queue()).await {
                Ok(Some(stats)) => {
                    tracing::info!(
                        "Watcher {}: drained WriteQueue ({} batches, {} chunks written)",
                        root,
                        stats.batches_written,
                        stats.chunks_written
                    );
                }
                Ok(None) => {
                    tracing::warn!(
                        "Watcher {}: WriteQueue already drained or has other references",
                        root
                    );
                }
                Err(_) => {
                    tracing::warn!(
                        "Watcher {}: WriteQueue drain timed out after {:?}",
                        root,
                        DRAIN_TIMEOUT
                    );
                }
            }
        }
    }

    // Now remove the watcher
    let mut s = state.lock().await;
    let removed = s.watchers.remove(&key).is_some();

    if removed {
        tracing::info!("stopped internal watcher for {}", root);
        Ok(serde_json::json!({
            "success": true,
            "stopped": true,
            "internal": true,
        }))
    } else {
        Ok(serde_json::json!({
            "success": false,
            "stopped": false,
            "error": "watcher_removed_during_drain",
        }))
    }
}
