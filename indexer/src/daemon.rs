//! HTTP daemon for the indexer.
//!
//! Accepts JSON-RPC commands over HTTP, processes them, and returns JSON responses.
//! This eliminates the need for the TypeScript side to spawn one-shot CLI processes.
//!
//! Endpoints:
//!   POST /rpc - JSON-RPC: {"method": "...", "params": {...}} -> {"result": ...} or {"error": "..."}
//!   GET /ping - Health check: returns "pong"

use std::collections::{HashMap, HashSet, VecDeque};
use std::num::NonZeroUsize;
use std::path::{Path, PathBuf};
use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant};
use tokio::sync::RwLock;

use crate::links::*;
use crate::process_group::{kill_process_group, set_oom_score, setup_process_group, spawn_parent_monitor};
use crate::tui::{
    canonicalize_project_key, tui_connect_impl, tui_connections_impl, tui_disconnect_impl,
};
use crate::handlers::{handle_request, project_key, watcher_status_with_connections};
use crate::memory_watcher::*;
use crate::wait::{wait_for_any_memory_pending, wait_for_any_pending};
use crate::watcher_startup::*;
use crate::compaction::{
    compaction_check_interval, compaction_force_threshold, compaction_idle_threshold,
    compaction_ops_threshold, compaction_worker, queue_compaction,
    record_compaction_operation, release_all_memory_pressure, shutdown_compaction,
    shutdown_drain_write_queues, CompactionKey, CompactionRequest, CompactionState,
};
use crate::search::*;

use anyhow::{Context, Result};
use serde::Serialize;
use tokio::signal::unix::{signal, SignalKind};
use tokio::sync::Mutex;

// Storage cache size: controls how many LanceDB connections are held open.
// Each connection uses ~10-15 MB (Arrow/DataFusion arenas). Reduced from 20 to 10
// as default for laptop use. Override via OPENCODE_SEARCH_STORAGE_CACHE_SIZE.
pub(crate) fn max_storage_cache_size() -> usize {
    std::env::var("OPENCODE_SEARCH_STORAGE_CACHE_SIZE")
        .ok()
        .and_then(|v| v.trim().parse::<usize>().ok())
        .filter(|&n| n > 0)
        .unwrap_or(4)
}
pub(crate) const MAX_CANONICALIZED_CACHE_SIZE: usize = 200;
const COMPACTION_STATE_TTL: Duration = Duration::from_secs(86400); // 24 hours
const COMPACTION_QUEUE_SIZE: usize = 32; // Max pending compaction requests
pub(crate) const WATCHER_START_RETRY_DELAYS: [u64; 3] = [100, 200, 400]; // ms, exponential backoff
const PROJECT_INACTIVE_TTL: Duration = Duration::from_secs(3600); // 1 hour
const MAX_FILE_RETRIES: u32 = 3; // Max retries for failed files in memory watchers

// ---------------------------------------------------------------------------
// Path canonicalization cache (prevents blocking I/O in async context)
// ---------------------------------------------------------------------------

/// Cached canonicalized path (LRU evicted by LruCache)
pub(crate) struct CachedCanonicalPath {
    pub(crate) path: String,
}

/// Global cache for canonicalized paths to avoid blocking I/O.
pub(crate) fn canonicalized_paths_cache() -> &'static RwLock<lru::LruCache<String, CachedCanonicalPath>> {
    static CACHE: OnceLock<RwLock<lru::LruCache<String, CachedCanonicalPath>>> = OnceLock::new();
    CACHE.get_or_init(|| RwLock::new(lru::LruCache::new(NonZeroUsize::new(MAX_CANONICALIZED_CACHE_SIZE).unwrap())))
}

/// Global set of db paths currently being indexed.
/// Prevents the status self-heal logic from clearing progress during active indexing.
pub(crate) fn active_indexes() -> &'static Mutex<HashSet<String>> {
    static ACTIVE: OnceLock<Mutex<HashSet<String>>> = OnceLock::new();
    ACTIVE.get_or_init(|| Mutex::new(HashSet::new()))
}

/// Parent PID to monitor (if set via CLI arg or env var).
fn parent_pid() -> &'static std::sync::OnceLock<Option<i32>> {
    static PARENT_PID: std::sync::OnceLock<Option<i32>> = std::sync::OnceLock::new();
    &PARENT_PID
}



#[derive(Debug, Serialize)]
struct Response {
    id: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    result: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

/// Internal work queue item.
struct QueueItem {
    id: u64,
    method: String,
    params: serde_json::Value,
    tx: tokio::sync::oneshot::Sender<Response>,
}

/// Per-project queue: each project root gets its own serialised write queue
/// so that projects don't block each other.
struct ProjectQueue {
    queue: VecDeque<QueueItem>,
    processing: bool,
    last_activity: Instant,
}

/// Daemon state shared across connections.
pub struct DaemonState {
    /// Per-project write queues keyed by canonicalised root path.
    projects: HashMap<String, ProjectQueue>,
    /// Global shutdown token.
    pub shutdown: tokio::sync::watch::Sender<bool>,
    /// TUI connection tracking: maps project root -> set of connection IDs.
    /// Used to determine when watcher should be stopped (when all TUIs disconnect).
    pub(crate) tui_connections: HashMap<String, HashSet<String>>,
    /// Projects that have had a TUI connection.
    /// Used by the cleanup task to stop TUI-managed watchers after disconnect/crash.
    pub(crate) tui_projects: HashSet<String>,
    /// Per-database compaction state for smart deferred compaction.
    pub compaction: HashMap<CompactionKey, CompactionState>,
    /// Sender for the compaction worker queue. Stored here so RPC handlers can queue compaction.
    pub compaction_tx: Option<tokio::sync::mpsc::Sender<CompactionRequest>>,
    /// Internal watchers keyed by canonicalized project root
    pub watchers: HashMap<String, WatcherState>,
    /// Memory watchers keyed by scope (e.g., "global", "project:abc123")
    pub(crate) memory_watchers: HashMap<String, MemoryWatcherState>,
    /// Last activity timestamp for idle shutdown tracking
    pub last_activity: Instant,
}

impl DaemonState {
    /// Remove inactive projects, watchers, and compaction state that haven't been
    /// active for PROJECT_INACTIVE_TTL (1 hour). Prevents unbounded HashMap growth.
    fn cleanup_inactive(&mut self) {
        let now = Instant::now();
        let before_projects = self.projects.len();
        let before_watchers = self.watchers.len();
        let before_memory_watchers = self.memory_watchers.len();
        let before_compaction = self.compaction.len();

        // Remove inactive project queues (not processing and no recent activity)
        self.projects.retain(|_key, queue| {
            let active = now.duration_since(queue.last_activity) < PROJECT_INACTIVE_TTL;
            let keep = active || queue.processing || !queue.queue.is_empty();
            keep
        });

        // Remove inactive watchers (stopped or idle for TTL)
        self.watchers.retain(|_key, watcher| {
            now.duration_since(watcher.started_at) < PROJECT_INACTIVE_TTL
        });

        // Remove inactive memory watchers
        self.memory_watchers.retain(|_key, watcher| {
            now.duration_since(watcher.started_at) < PROJECT_INACTIVE_TTL
        });

        // Remove stale compaction state (using existing COMPACTION_STATE_TTL)
        self.compaction.retain(|_key, state| {
            now.duration_since(state.last_activity) < COMPACTION_STATE_TTL
        });

        // Clean up tui_connections with no entries
        self.tui_connections.retain(|_, connections| !connections.is_empty());

        // Clean up tui_projects that have no active watchers or connections
        let before_tui = self.tui_projects.len();
        self.tui_projects.retain(|key| {
            // Keep if there's an active watcher or TUI connection
            self.watchers.contains_key(key)
                || self.tui_connections.get(key).map(|c| !c.is_empty()).unwrap_or(false)
        });
        let cleaned_tui = before_tui.saturating_sub(self.tui_projects.len());

        let cleaned_projects = before_projects.saturating_sub(self.projects.len());
        let cleaned_watchers = before_watchers.saturating_sub(self.watchers.len());
        let cleaned_memory = before_memory_watchers.saturating_sub(self.memory_watchers.len());
        let cleaned_compaction = before_compaction.saturating_sub(self.compaction.len());

        if cleaned_projects > 0 || cleaned_watchers > 0 || cleaned_memory > 0 || cleaned_compaction > 0 || cleaned_tui > 0 {
            tracing::info!(
                "TTL cleanup: removed {} projects, {} watchers, {} memory watchers, {} compaction states, {} tui_projects",
                cleaned_projects, cleaned_watchers, cleaned_memory, cleaned_compaction, cleaned_tui
            );
        }
    }
}

/// Pending file changes buffer with separate changed/deleted tracking
pub(crate) struct PendingChanges {
    /// Files that were created or modified
    changed: HashSet<PathBuf>,
    /// Files that were deleted  
    deleted: HashSet<PathBuf>,
    /// Watch channel sender: bumps a counter on each change.
    /// Receivers call `.changed().await` — level-triggered-on-change, no stored permits.
    tx: Arc<tokio::sync::watch::Sender<u64>>,
}

impl PendingChanges {
    pub(crate) fn new() -> Self {
        let (tx, _rx) = tokio::sync::watch::channel(0u64);
        Self {
            changed: HashSet::new(),
            deleted: HashSet::new(),
            tx: Arc::new(tx),
        }
    }

    /// Add changed files and signal processor
    pub(crate) fn add_changed(&mut self, paths: Vec<PathBuf>) {
        for path in paths {
            self.deleted.remove(&path); // Changed overrides deleted
            self.changed.insert(path);
        }
        self.tx.send_modify(|v| *v = v.wrapping_add(1));
    }

    /// Add deleted files and signal processor
    pub(crate) fn add_deleted(&mut self, paths: Vec<PathBuf>) {
        for path in paths {
            self.changed.remove(&path); // Deleted overrides changed
            self.deleted.insert(path);
        }
        self.tx.send_modify(|v| *v = v.wrapping_add(1));
    }

    /// Drain all pending changes, returns (changed, deleted)
    fn drain(&mut self) -> (Vec<PathBuf>, Vec<PathBuf>) {
        let changed = self.changed.drain().collect();
        let deleted = self.deleted.drain().collect();
        (changed, deleted)
    }

    pub(crate) fn is_empty(&self) -> bool {
        self.changed.is_empty() && self.deleted.is_empty()
    }

    pub(crate) fn len(&self) -> usize {
        self.changed.len() + self.deleted.len()
    }

    /// Get a watch receiver for this pending queue.
    /// Call `.changed().await` on the receiver — wakes only on *new* sends.
    pub(crate) fn subscribe(&self) -> tokio::sync::watch::Receiver<u64> {
        self.tx.subscribe()
    }
}

/// Default backpressure limit for pending file changes (configurable via .opencode-index.yaml).
/// Reduced from 2000 to 500 to limit backlog accumulation — a smaller buffer means
/// less of a re-index storm after an embedder restart, at the cost of more frequent
/// full re-scans for events that were dropped under pressure.
pub(crate) const DEFAULT_MAX_PENDING_FILES: usize = 500;

/// Statistics for dropped watcher events (for monitoring/diagnostics)
#[derive(Debug, Default)]
pub(crate) struct DroppedEventStats {
    /// Total number of changed file events dropped since watcher start
    pub(crate) changed_files_dropped: u64,
    /// Total number of deleted file events dropped since watcher start
    pub(crate) deleted_files_dropped: u64,
    /// Number of times backpressure was triggered
    pub(crate) backpressure_events: u64,
    /// Timestamp of last drop event (for rate calculation)
    pub(crate) last_drop_time: Option<Instant>,
    /// Timestamp when stats were last logged (to avoid log spam)
    pub(crate) last_log_time: Option<Instant>,
}

/// Internal watcher state for a project (no external process)
pub struct WatcherState {
    /// Project root path
    pub(crate) root: Arc<PathBuf>,
    /// Database/storage path
    pub(crate) db_path: Arc<PathBuf>,
    /// Storage instance (cached)
    pub(crate) storage: Arc<crate::storage::Storage>,
    /// Write queue for serializing storage operations
    pub write_queue: Option<Arc<crate::storage::WriteQueue>>,
    /// Include directories being watched
    pub(crate) include_dirs: Arc<Vec<PathBuf>>,
    /// Symlink directories
    pub(crate) symlink_dirs: Arc<Vec<crate::discover::SymlinkDir>>,
    /// Pending file changes (accumulated before processing)
    pub(crate) pending: Arc<tokio::sync::Mutex<PendingChanges>>,
    /// Embedding tier
    pub(crate) tier: Arc<str>,
    /// Dimensions
    pub(crate) dimensions: u32,
    /// Operations since last compaction
    pub(crate) ops_since_compact: u64,
    /// Watcher handle (to stop watching)
    pub(crate) _watcher_handle: Option<std::thread::JoinHandle<()>>,
    /// Shutdown signal
    pub shutdown_tx: tokio::sync::watch::Sender<bool>,
    /// Configured maximum pending files (from .opencode-index.yaml watcher.max_pending_files)
    pub(crate) max_pending_files: usize,
    /// Statistics for dropped events (for diagnostics)
    pub(crate) dropped_stats: Arc<tokio::sync::Mutex<DroppedEventStats>>,
    /// Last heartbeat timestamp (updated by event collector task for zombie detection)
    pub(crate) last_heartbeat: Arc<tokio::sync::Mutex<Instant>>,
    /// Watcher start time (for uptime calculation)
    pub(crate) started_at: Instant,
}

impl WatcherState {
    /// Drain the WriteQueue, waiting for all pending writes to complete.
    /// Returns the write stats snapshot.
    pub(crate) async fn drain_write_queue(&mut self) -> Option<crate::storage::WriteQueueStatsSnapshot> {
        if let Some(wq) = self.write_queue.take() {
            // Use shutdown_shared which handles Arc properly without data loss
            Some(wq.shutdown_shared().await)
        } else {
            None
        }
    }
}

/// Memory watcher state for memories/activity directories (independent of project watchers)
pub(crate) struct MemoryWatcherState {
    /// Root directory being watched (e.g., {shared}/memories/global)
    pub(crate) root: Arc<PathBuf>,
    /// Database path (e.g., {shared}/memories/global/.lancedb)
    pub(crate) db_path: Arc<PathBuf>,
    /// Storage instance (cached)
    pub(crate) storage: Arc<crate::storage::Storage>,
    /// Write queue for serializing storage operations
    pub(crate) write_queue: Option<Arc<crate::storage::WriteQueue>>,
    /// Pending file changes (accumulated before processing)
    pub(crate) pending: Arc<tokio::sync::Mutex<PendingChanges>>,
    /// Failed files with retry count (path -> failure_count)
    pub(crate) failed_files: Arc<tokio::sync::Mutex<HashMap<PathBuf, u32>>>,
    /// Shutdown signal
    pub(crate) _shutdown_tx: tokio::sync::watch::Sender<bool>,
    /// Watcher start time (for uptime calculation)
    pub(crate) started_at: Instant,
    /// Scope identifier (e.g., "global", "project:abc123:memories")
    pub(crate) _scope: String,
}

// ---------------------------------------------------------------------------
// Storage cache: reuse LanceDB connections across requests to prevent RSS growth.
// Each Storage::open() creates a new LanceDB Connection with Arrow/DataFusion
// memory arenas (~10-15 MB each). Without caching, the daemon leaks ~10 MB/min
// during bulk indexing.
// ---------------------------------------------------------------------------

/// Cache key: (db_path, dimensions)
pub type StorageKey = (String, u32);

/// Cached storage (LRU evicted by LruCache)
pub struct CachedStorage {
    pub storage: Arc<crate::storage::Storage>,
}

pub fn storage_cache() -> &'static RwLock<lru::LruCache<StorageKey, CachedStorage>> {
    static CACHE: std::sync::OnceLock<RwLock<lru::LruCache<StorageKey, CachedStorage>>> =
        std::sync::OnceLock::new();
    CACHE.get_or_init(|| RwLock::new(lru::LruCache::new(NonZeroUsize::new(max_storage_cache_size()).unwrap())))
}



/// Get idle shutdown timeout from environment or CLI arg.
/// Default: 300 seconds (5 minutes). Set to 0 to disable.
fn idle_shutdown_timeout(cli_arg: Option<u64>) -> u64 {
    cli_arg.unwrap_or_else(|| {
        std::env::var("OPENCODE_INDEXER_IDLE_SHUTDOWN")
            .ok()
            .and_then(|v| v.parse::<u64>().ok())
            .unwrap_or(300)
    })
}











/// Get or open a cached Storage handle.
pub async fn cached_storage(path: &Path, dims: u32) -> anyhow::Result<Arc<crate::storage::Storage>> {
    let key = (path.to_string_lossy().to_string(), dims);
    let cache = storage_cache();

        // Fast path: write lock needed (LruCache.get requires &mut self)
        {
            let mut w = cache.write().await;
            if let Some(cached) = w.get(&key) {
                return Ok(cached.storage.clone());
            }
        }

        // Slow path: open and cache (LruCache handles eviction automatically)
        let storage = crate::storage::Storage::open(path, dims).await?;
        let storage = Arc::new(storage);
        {
            let mut w = cache.write().await;

            w.put(key, CachedStorage {
                storage: storage.clone(),
            });
    }
    Ok(storage)
}

/// Invalidate cached storage for a given path (used after clearing corrupted index).
pub(crate) async fn invalidate_storage_cache(path: &Path) {
    let path_str = path.to_string_lossy().to_string();
    let cache = storage_cache();
    let mut w = cache.write().await;

        // Remove all entries for this path (any dimension)
        let keys_to_remove: Vec<_> = w.iter().filter(|(p, _)| p.0 == path_str).map(|(k, _)| k.clone()).collect();

        for key in keys_to_remove {
            tracing::debug!("invalidating storage cache for {:?}", key);
            w.pop(&key);
        }
}

// Daemon server loop
// ============================================================================

/// Spawn a per-project queue processor if not already running.
fn spawn_project_processor(key: String, state: Arc<Mutex<DaemonState>>) {
    tokio::spawn(async move {
        loop {
            let item = {
                let mut s = state.lock().await;
                let pq = match s.projects.get_mut(&key) {
                    Some(pq) => pq,
                    None => break,
                };
                match pq.queue.pop_front() {
                    Some(item) => {
                        pq.last_activity = Instant::now(); // Touch on dequeue
                        item
                    }
                    None => {
                        pq.processing = false;
                        pq.last_activity = Instant::now(); // Touch on finish
                        break;
                    }
                }
            };

            let result = handle_request(&item.method, &item.params).await;

            // Track write operations for compaction
            if matches!(item.method.as_str(), "index_file" | "remove_file") {
                // Check if the operation was successful (not skipped)
                let was_write = result["success"].as_bool() == Some(true)
                    && result["skipped"].as_bool() != Some(true);

                if was_write {
                    // Extract db path and dimensions from params
                    let root = item.params["root"].as_str().unwrap_or(".");
                    let db = item.params["db"].as_str();
                    let dims = item.params["dimensions"].as_u64().unwrap_or(1024) as u32;

                    let root_path = tokio::fs::canonicalize(root)
                        .await
                        .unwrap_or_else(|_| PathBuf::from(root));
                    let storage_path = db
                        .map(PathBuf::from)
                        .unwrap_or_else(|| crate::storage::storage_path(&root_path));

                    // Record the operation for compaction tracking
                    let mut s = state.lock().await;
                    record_compaction_operation(&mut s, &storage_path, dims);
                }
            }

            let _ = item.tx.send(Response {
                id: item.id,
                result: Some(result),
                error: None,
            });
        }
    });
}

// ============================================================================
// Unified dispatcher for HTTP server
// ============================================================================

/// Opaque dispatcher passed to the HTTP server.
pub type Dispatcher = Arc<
    dyn Fn(
            String,
            serde_json::Value,
        )
            -> std::pin::Pin<Box<dyn std::future::Future<Output = serde_json::Value> + Send>>
        + Send
        + Sync,
>;

/// Route one RPC call to the correct handler, including stateful methods.
///
/// Mirrors the per-connection dispatch logic in `run()` so that both the
/// HTTP transport business logic.
pub(crate) async fn dispatch_unified(
    state: Arc<Mutex<DaemonState>>,
    method: String,
    params: serde_json::Value,
) -> serde_json::Value {
    // Graceful shutdown — drains queues, compacts, then exits the process.
    if method == "shutdown" {
        {
            let s = state.lock().await;
            let _ = s.shutdown.send(true);
        }
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        shutdown_drain_write_queues(state.clone()).await;
        shutdown_compaction(state.clone()).await;
        crate::model_client::shutdown_embedder().await;
        #[cfg(target_os = "macos")]
        {
            let _ = tokio::fs::remove_file(crate::http_server::socket_file_path()).await;
        }
        kill_process_group();
        std::process::exit(0);
    }

    if method == "startup_check" {
        let root = params["root"].as_str().unwrap_or(".");
        let db = params["db"].as_str();
        let tier = params["tier"].as_str();
        let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;
        return startup_check_impl(&state, root, db, tier, dims).await;
    }

    if method == "watcher_start" {
        let root = params["root"].as_str().unwrap_or(".");
        let db = params["db"].as_str();
        let tier = params["tier"].as_str();
        let force = params["force"].as_bool().unwrap_or(false);
        return watcher_start_internal(&state, root, db, tier, force)
            .await
            .unwrap_or_else(|e| serde_json::json!({"success": false, "error": e.to_string()}));
    }

    if method == "watcher_stop" {
        let root = params["root"].as_str().unwrap_or(".");
        let key = canonicalize_project_key(root).await;
        let can_stop = {
            let s = state.lock().await;
            s.tui_connections
                .get(&key)
                .map(|c| c.is_empty())
                .unwrap_or(true)
        };
        return if can_stop {
            watcher_stop_internal(&state, root)
                .await
                .unwrap_or_else(|e| serde_json::json!({"success": false, "error": e.to_string()}))
        } else {
            serde_json::json!({"success": false, "stopped": false, "error": "tui_connections_active"})
        };
    }

    if method == "watcher_status" {
        let root = params["root"].as_str().unwrap_or(".");
        let db = params["db"].as_str();
        let key = canonicalize_project_key(root).await;

        // Clone necessary data under outer lock, then drop it before accessing inner locks
        let watcher_data = {
            let s = state.lock().await;
            s.watchers.get(&key).map(|w| {
                (
                    w.dropped_stats.clone(),  // Arc<Mutex<_>> clone
                    w.pending.clone(),        // Arc<Mutex<_>> clone
                    w.started_at,
                    w.db_path.clone(),
                    w.max_pending_files,
                    w.last_heartbeat.clone(), // Arc<Mutex<_>> clone
                )
            })
        };
        // Outer lock dropped here

        return if let Some((dropped_stats, pending, started_at, db_path, max_pending_files, last_heartbeat)) = watcher_data {
            // Check if watcher heartbeat is alive — detects zombie watchers where
            // the inotify thread silently died but the HashMap entry remains.
            let heartbeat_alive = {
                let hb = last_heartbeat.lock().await;
                hb.elapsed() < Duration::from_secs(120)
            };

            if !heartbeat_alive {
                // Zombie watcher detected — remove it and return non-active status
                tracing::warn!("zombie watcher detected for {} (heartbeat stale), removing", key);
                let mut s = state.lock().await;
                s.watchers.remove(&key);
                let (conn_count, _) = {
                    let cc = s.tui_connections.get(&key).map(|c| c.len()).unwrap_or(0);
                    let wa = s.watchers.contains_key(&key);
                    (cc, wa)
                };
                return watcher_status_with_connections(root, db, conn_count, false);
            }

            // Now safe to acquire inner locks without holding outer lock
            let dropped = dropped_stats.lock().await;
            let uptime = started_at.elapsed().as_secs();
            let pending_count = pending.lock().await.len();

            // Re-acquire outer lock only for connection count
            let connection_count = {
                let s = state.lock().await;
                s.tui_connections.get(&key).map(|c| c.len()).unwrap_or(0)
            };

            serde_json::json!({
                "watcherActive": true,
                "internal": true,
                "watcherPid": null,
                "indexerActive": false,
                "indexerPid": null,
                "dbPath": db_path.to_string_lossy(),
                "connectionCount": connection_count,
                "metrics": {
                    "uptimeSeconds": uptime,
                    "maxPendingFiles": max_pending_files,
                    "currentPendingFiles": pending_count,
                    "droppedChangedFiles": dropped.changed_files_dropped,
                    "droppedDeletedFiles": dropped.deleted_files_dropped,
                    "backpressureEvents": dropped.backpressure_events,
                }
            })
        } else {
            let (conn_count, watcher_active) = {
                let s = state.lock().await;
                let cc = s.tui_connections.get(&key).map(|c| c.len()).unwrap_or(0);
                // Re-check watchers in the same lock: handles the race where
                // a background watcher_start completed between the two lock acquisitions.
                let wa = s.watchers.contains_key(&key);
                (cc, wa)
            };
            watcher_status_with_connections(root, db, conn_count, watcher_active)
        };
    }

    if method == "tui_disconnect" {
        let root = params["root"].as_str().unwrap_or(".");
        let key = canonicalize_project_key(root).await;
        let result = {
            let mut s = state.lock().await;
            tui_disconnect_impl(&mut s, &key, params["connectionId"].as_str().unwrap_or(""))
        };
        // Watchers are NOT auto-stopped on TUI disconnect — connection drops
        // are transient (SSE reconnect, network hiccups) and stopping the
        // watcher causes flapping with the TUI's auto-start mechanism.
        // Watchers are only stopped explicitly via watcher_stop RPC or daemon shutdown.
        return result;
    }

    if matches!(method.as_str(), "tui_connect" | "tui_connections") {
        let root = params["root"].as_str().unwrap_or(".");
        let key = canonicalize_project_key(root).await;
        let mut s = state.lock().await;
        return match method.as_str() {
            "tui_connect" => {
                tui_connect_impl(&mut s, &key, params["connectionId"].as_str().unwrap_or(""))
            }
            "tui_connections" => tui_connections_impl(&mut s, &key),
            _ => unreachable!(),
        };
    }

    // Write operations are serialized through per-project queues.
    if matches!(method.as_str(), "index_file" | "remove_file" | "run_index") {
        let key = project_key(&params).await;
        let (tx, rx) = tokio::sync::oneshot::channel();
        {
            let mut s = state.lock().await;
            let pq = s
                .projects
                .entry(key.clone())
                .or_insert_with(|| ProjectQueue {
                    queue: VecDeque::new(),
                    processing: false,
                    last_activity: Instant::now(),
                });
            pq.last_activity = Instant::now(); // Touch on every operation
            pq.queue.push_back(QueueItem {
                id: 0,
                method,
                params,
                tx,
            });
            if !pq.processing {
                pq.processing = true;
                spawn_project_processor(key, state.clone());
            }
        }
        return match rx.await {
            Ok(resp) => resp.result.unwrap_or(serde_json::json!(null)),
            Err(_) => serde_json::json!({"error": "queue dropped"}),
        };
    }

    if method == "compact" {
        let db = params["db"].as_str().map(|s| s.to_string());
        let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;
        return match db {
            Some(db_path) => {
                let path = std::path::PathBuf::from(&db_path);
                let tx = state.lock().await.compaction_tx.clone();
                match tx {
                    Some(tx) => {
                        queue_compaction(&tx, path, dims, "rpc");
                        serde_json::json!({"queued": true, "message": "compaction queued"})
                    }
                    None => serde_json::json!({"error": "compaction worker not initialized"}),
                }
            }
            None => serde_json::json!({"error": "db path required"}),
        };
    }

    if method == "compact_status" {
        let s = state.lock().await;
        let statuses: Vec<serde_json::Value> = s
            .compaction
            .iter()
            .map(|(key, cs)| {
                serde_json::json!({
                    "db": key,
                    "operations": cs.operations_since_compact,
                    "inProgress": cs.compact_in_progress,
                    "lastCompactSecs": cs.last_compact_time.map(|t| t.elapsed().as_secs()),
                })
            })
            .collect();
        return serde_json::json!({"compaction": statuses});
    }

    if method == "memory_release" {
        release_all_memory_pressure().await;
        return serde_json::json!({"ok": true, "message": "memory pressure released"});
    }

    if method == "memory_stats" {
        let mut stats = serde_json::json!({});
        {
            let _ = tikv_jemalloc_ctl::epoch::advance();
            if let Ok(allocated) = tikv_jemalloc_ctl::stats::allocated::read() {
                stats["jemalloc_allocated_bytes"] = serde_json::Value::from(allocated as u64);
            }
            if let Ok(resident) = tikv_jemalloc_ctl::stats::resident::read() {
                stats["jemalloc_resident_bytes"] = serde_json::Value::from(resident as u64);
            }
        }
        if let Ok(status) = tokio::fs::read_to_string("/proc/self/status").await {
            for line in status.lines() {
                if line.starts_with("VmRSS:") || line.starts_with("RssAnon:") || line.starts_with("RssFile:") {
                    let parts: Vec<&str> = line.split_whitespace().collect();
                    if parts.len() >= 2 {
                        stats[parts[0].trim_end_matches(':')] = parts[1].into();
                    }
                }
            }
        }
        return stats;
    }

    // All read-only stateless operations.
    handle_request(&method, &params).await
}

/// RPC methods that should NOT reset the idle-shutdown timer.
/// These are read-only status/health checks — 13+ bun servers sending periodic
/// pings should not prevent the daemon from shutting down when truly idle.
const PASSIVE_METHODS: &[&str] = &[
    "ping", "health", "status", "watcher_status", "tui_connections",
    "compact_status", "resolve_paths", "get_file_count", "get_status",
    "startup_check", "memory_stats",
];

/// Create a type-erased dispatcher that the HTTP server can call.
///
/// The returned function is `Send + Sync` so it can be shared across axum
/// handler tasks without additional synchronisation.
fn make_dispatcher(state: Arc<Mutex<DaemonState>>) -> Dispatcher {
    Arc::new(move |method, params| {
        let state = state.clone();
        Box::pin(async move {
            // Only reset idle timer for non-passive (mutating/active) methods
            if !PASSIVE_METHODS.contains(&method.as_str()) {
                let mut s = state.lock().await;
                s.last_activity = Instant::now();
            }
            dispatch_unified(state, method, params).await
        })
    })
}

/// Check if another daemon instance is already running by probing the abstract Unix socket.
/// Returns true if a responsive daemon is found.
async fn check_existing_daemon() -> bool {
    use std::os::linux::net::SocketAddrExt;
    let addr = match std::os::unix::net::SocketAddr::from_abstract_name(b"opencode-indexer") {
        Ok(a) => a,
        Err(_) => return false,
    };
    std::os::unix::net::UnixStream::connect_addr(&addr).is_ok()
}

/// Start the daemon, serving HTTP requests on an abstract Unix socket (or TCP when tcp_port > 0).
///
/// Uses abstract socket "@opencode-indexer" (kernel auto-cleans on exit).
/// When tcp_port is Some(n), binds TCP on 127.0.0.1:n (0 = OS-assigned) instead of Unix socket.
pub async fn run(idle_shutdown_arg: Option<u64>, parent_pid_arg: Option<i32>, tcp_port: Option<u16>) -> Result<()> {
    // Initialize parent PID global — CLI arg > env var > getppid()
    let ppid = if let Some(ppid) = parent_pid_arg {
        tracing::info!("will monitor parent PID {} (CLI arg)", ppid);
        Some(ppid)
    } else if let Ok(ppid_str) = std::env::var("OPENCODE_PARENT_PID") {
        ppid_str.parse::<i32>().ok().map(|ppid| {
            tracing::info!("will monitor parent PID {} (env)", ppid);
            ppid
        })
    } else {
        let detected = unsafe { libc::getppid() };
        if detected > 1 {
            tracing::info!("parent PID auto-detected: {} (getppid)", detected);
            Some(detected)
        } else {
            tracing::warn!("getppid()={} (init/container?), skip parent monitor", detected);
            None
        }
    };
    parent_pid().set(ppid).ok();

    // In TCP mode (tests) skip singleton enforcement — each test daemon binds its own random TCP port.
    // The abstract socket bind IS the singleton lock — kernel-enforced, auto-released on death.
    if tcp_port.is_none() {
        if check_existing_daemon().await {
            tracing::info!("daemon already running, exiting");
            println!("{}", serde_json::json!({"type": "already_running"}));
            return Ok(());
        }
    }

    // Set up process group for clean child termination (prevents orphaned PIDs)
    setup_process_group();

    // Tell Linux OOM-killer to prefer killing the indexer (score 500) over user
    // processes (score ~0-200) during memory pressure. Fails silently.
    set_oom_score(300);

    // Auto-start the Python embedder if needed (non-blocking background task)
    tokio::spawn(async {
        crate::model_client::ensure_embedder().await;
    });

    let (shutdown_tx, shutdown_rx) = tokio::sync::watch::channel(false);
    
    // Spawn parent monitor if parent_pid is set
    if let Some(&Some(ppid)) = parent_pid().get() {
        spawn_parent_monitor(ppid, shutdown_tx.clone()).await;
    }
    
    let state = Arc::new(Mutex::new(DaemonState {
        projects: HashMap::new(),
        shutdown: shutdown_tx,
        tui_connections: HashMap::new(),
        tui_projects: HashSet::new(),
        compaction: HashMap::new(),
        compaction_tx: None,
        watchers: HashMap::new(),
        memory_watchers: HashMap::new(),
        last_activity: Instant::now(),
    }));

    // Create compaction queue and spawn worker
    let (compaction_tx, compaction_rx) =
        tokio::sync::mpsc::channel::<CompactionRequest>(COMPACTION_QUEUE_SIZE);
    {
        let state = state.clone();
        let shutdown_rx = shutdown_rx.clone();
        tokio::spawn(compaction_worker(compaction_rx, state, shutdown_rx));
    }

    // Store compaction_tx in state so RPC handlers can access it
    {
        let mut s = state.lock().await;
        s.compaction_tx = Some(compaction_tx.clone());
    }

    // Spawn TTL cleanup task for unbounded HashMaps (projects, watchers, compaction)
    {
        let cleanup_state = state.clone();
        let mut cleanup_shutdown = shutdown_rx.clone();
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_secs(600)); // Every 10 minutes
            loop {
                tokio::select! {
                    _ = interval.tick() => {
                        {
                            let mut s = cleanup_state.lock().await;
                            s.cleanup_inactive();
                        }
                        release_all_memory_pressure().await;
                    }
                    _ = cleanup_shutdown.changed() => {
                        if *cleanup_shutdown.borrow() {
                            tracing::info!("TTL cleanup task shutting down");
                            break;
                        }
                    }
                }
            }
        });
    }

    // Start built-in memory watchers (global memories)
    {
        let state_for_builtin = state.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(500)).await;
            if let Some(shared) = dirs::data_local_dir() {
                let shared = shared.join("opencode");
                if let Err(e) = start_builtin_memory_watchers(&state_for_builtin, &shared).await {
                    tracing::debug!("failed to start built-in memory watchers: {}", e);
                }
            }
        });
    }



    // Filesystem-event-driven project directory cleanup task.
    // Watches projects/ for DELETION events only (orphan detection).
    // 24h fallback timer handles stale detection with a full deep scan.
    //
    // Fix: event filter (deletions only), 60s cooldown, 30s debounce, batched scan.
    {
        let shutdown_rx = shutdown_rx.clone();
        tokio::spawn(async move {
            use notify::event::{ModifyKind, RemoveKind};
            use notify::{EventKind, Watcher as _};

            let cfg = crate::cleaner::config();
            let base = crate::storage::shared_data_dir();

            // Cooldown: minimum seconds between consecutive cleanup runs.
            // Prevents self-triggering feedback loop (cleanup modifies projects/ → inotify fires again).
            let cooldown = Duration::from_secs(
                std::env::var("OPENCODE_INDEXER_CLEANUP_COOLDOWN_SECS")
                    .ok()
                    .and_then(|v| v.parse::<u64>().ok())
                    .unwrap_or(60),
            );
            // Debounce: how long to wait after first event before running.
            let debounce = Duration::from_secs(
                std::env::var("OPENCODE_INDEXER_CLEANUP_DEBOUNCE_SECS")
                    .ok()
                    .and_then(|v| v.parse::<u64>().ok())
                    .unwrap_or(30),
            );

            // notify → std::sync::mpsc (sync callback thread)
            // Fix 1: Only forward deletion/rename events — ignore Create, Modify::Data, Access.
            let (sync_tx, sync_rx) = std::sync::mpsc::channel::<()>();
            let mut watcher = match notify::RecommendedWatcher::new(
                {
                    let tx = sync_tx.clone();
                    move |res: notify::Result<notify::Event>| {
                        let Ok(evt) = res else { return };
                        let relevant = matches!(
                            evt.kind,
                            EventKind::Remove(RemoveKind::Folder)
                                | EventKind::Remove(RemoveKind::Any)
                                | EventKind::Modify(ModifyKind::Name(_))
                        );
                        if relevant {
                            let _ = tx.send(());
                        }
                    }
                },
                notify::Config::default(),
            ) {
                Ok(w) => w,
                Err(e) => {
                    tracing::warn!("cleanup watcher init failed: {}", e);
                    return;
                }
            };

            // Watch shared_data_dir/ for aux dir events (backups/, compaction-history/, etc.)
            if let Err(e) =
                notify::Watcher::watch(&mut watcher, &base, notify::RecursiveMode::NonRecursive)
            {
                tracing::warn!("cleanup watcher: failed to watch base dir: {}", e);
            }

            // Watch projects/ for orphan detection (deletions only) — create dir first if absent
            let projects = base.join("projects");
            if !tokio::fs::try_exists(&projects).await.unwrap_or(false) {
                let _ = tokio::fs::create_dir_all(&projects).await;
            }
            if let Err(e) =
                notify::Watcher::watch(&mut watcher, &projects, notify::RecursiveMode::NonRecursive)
            {
                tracing::warn!("cleanup watcher: failed to watch projects dir: {}", e);
            }

            // std::sync::mpsc → tokio::sync::mpsc bridge thread
            let (async_tx, mut async_rx) = tokio::sync::mpsc::channel::<()>(64);
            std::thread::spawn(move || {
                let _w = watcher; // keep watcher alive
                while sync_rx.recv().is_ok() {
                    let _ = async_tx.try_send(());
                }
            });

            // Track last cleanup time for cooldown enforcement.
            // Initialize to startup minus (cooldown - 10s) so first event fires after 10s grace.
            let mut last = tokio::time::Instant::now()
                .checked_sub(cooldown.saturating_sub(Duration::from_secs(10)))
                .unwrap_or_else(tokio::time::Instant::now);
            #[allow(unused_assignments)]
            let mut is_fallback = false;

            // Async cleanup loop: event-driven (batched) with 24h fallback (deep scan)
            let mut fallback = tokio::time::interval(crate::cleaner::interval());
            fallback.tick().await; // skip immediate first tick

            loop {
                tokio::select! {
                    msg = async_rx.recv() => {
                        if msg.is_none() {
                            break; // bridge thread exited
                        }
                        if *shutdown_rx.borrow() {
                            break;
                        }
                        // Fix 2: cooldown guard — skip if we cleaned too recently
                        if last.elapsed() < cooldown {
                            // Drain excess signals; don't schedule another run
                            while async_rx.try_recv().is_ok() {}
                            continue;
                        }
                        // Fix 3: 30s debounce to batch rapid burst events
                        tokio::time::sleep(debounce).await;
                        while async_rx.try_recv().is_ok() {
                            tokio::task::yield_now().await;
                        }
                        if *shutdown_rx.borrow() {
                            break;
                        }
                        is_fallback = false;
                    }
                    _ = fallback.tick() => {
                        if *shutdown_rx.borrow() {
                            break;
                        }
                        is_fallback = true;
                    }
                }

                last = tokio::time::Instant::now();

                if is_fallback {
                    // 24h path: full deep scan for stale detection
                    tracing::info!("running project cleanup (full 24h pass)");
                    let c = cfg.clone();
                    let b = base.clone();
                    match tokio::task::spawn_blocking(move || crate::cleaner::run(&b, &c, false))
                        .await
                    {
                        Ok(report) => {
                            if report.orphans > 0 || report.stale > 0 || report.aux_dirs > 0 {
                                tracing::info!(
                                    "cleanup complete: {} orphans, {} stale, {} aux dirs removed, {} bytes freed",
                                    report.orphans, report.stale, report.aux_dirs, report.freed
                                );
                            }
                            for err in &report.errors {
                                tracing::warn!("cleanup error: {}", err);
                            }
                        }
                        Err(e) => tracing::warn!("cleanup task failed: {}", e),
                    }
                } else {
                    // Event path: batched shallow scan (Fix 4 — at most batch_size dirs per run)
                    tracing::debug!("running project cleanup (batched event pass)");
                    let c = cfg.clone();
                    let b = base.clone();
                    match tokio::task::spawn_blocking(move || {
                        crate::cleaner::run_batch(&b, &c, false)
                    })
                    .await
                    {
                        Ok((report, complete)) => {
                            if report.orphans > 0 || report.stale > 0 {
                                tracing::info!(
                                    "cleanup batch: {} orphans, {} stale removed",
                                    report.orphans,
                                    report.stale
                                );
                            }
                            for err in &report.errors {
                                tracing::warn!("cleanup error: {}", err);
                            }
                            if complete {
                                tracing::debug!("cleanup batch: full pass complete");
                            }
                        }
                        Err(e) => tracing::warn!("cleanup batch task failed: {}", e),
                    }
                }
            }
        });
    }

    // Watcher processing task - event-driven with parallel processing.
    // CPU is physically bounded by max_blocking_threads(1): at most 1
    // spawn_blocking runs at a time, using 100% of 1 core = 4.2% of 24 cores.
    // No governor, no speed cap, no adaptive delay needed.
    {
        let state = state.clone();
        let shutdown_rx = shutdown_rx.clone();
        let compaction_tx = compaction_tx.clone();
        tokio::spawn(async move {
            loop {
                // Wait for any watcher to have pending changes OR timeout for periodic check
                // Use 60s timeout to minimize idle wakeups while still allowing periodic checks
                let had_pending = tokio::time::timeout(
                    Duration::from_secs(600), // 10-minute timeout - watcher events wake us immediately
                    wait_for_any_pending(&state),
                )
                .await
                .is_ok(); // true if we got notified, false if timeout

                // Check shutdown
                if *shutdown_rx.borrow() {
                    tracing::info!("watcher processor received shutdown");
                    break;
                }

                // If timeout expired without notification, skip processing entirely
                // This avoids expensive watcher iteration when idle
                if !had_pending {
                    continue;
                }

                // No adaptive delay — max_blocking_threads(1) physically bounds CPU
                // to 100% of 1 core (= 4.2% of 24 cores). The single blocking thread
                // runs at full speed when there's work and sits at 0% when idle.
                // No governor, no speed cap, no sleeping needed.

                // Get all watcher keys in one lock
                let keys: Vec<String> = { state.lock().await.watchers.keys().cloned().collect() };

                for key in keys {
                    // Clone watcher data under outer lock, then drop it before accessing inner pending lock
                    let watcher_data = {
                        let s = state.lock().await;
                        s.watchers.get(&key).map(|w| {
                            (
                                w.pending.clone(),       // Arc<Mutex<_>> clone
                                w.write_queue.clone(),   // Option<Arc<_>> clone
                                w.storage.clone(),       // Arc clone
                                w.root.clone(),          // Arc clone
                                w.include_dirs.clone(),  // Arc clone
                                w.symlink_dirs.clone(),  // Arc clone
                                w.tier.clone(),          // Arc clone
                                w.dimensions,
                                w.db_path.clone(),       // Arc clone
                            )
                        })
                    };
                    // Outer lock dropped here

                    let Some((pending_mutex, write_queue_opt, storage, root, include_dirs, symlink_dirs, tier, dims, db_path)) = watcher_data else {
                        continue;
                    };

                    // Now safe to acquire inner pending lock without holding outer lock
                    let (changed, deleted) = {
                        let mut pending = pending_mutex.lock().await;
                        if pending.is_empty() {
                            continue;
                        }
                        pending.drain()
                    };

                    // Skip if write_queue is drained
                    let Some(write_queue) = write_queue_opt else {
                        tracing::warn!(
                            "write_queue drained for {}, skipping pending changes",
                            key
                        );
                        continue;
                    };

                    // No per-batch cap — the single blocking thread in the runtime
                    // naturally serializes all file processing. The thread itself IS
                    // the memory and CPU bound. Process ALL pending files each iteration
                    // so the wait loop properly sleeps until new events arrive.
                    let changed: Vec<_> = changed.into_iter().collect();
                    let deleted: Vec<_> = deleted.into_iter().collect();

                    let total = changed.len() + deleted.len();
                    if total == 0 {
                        continue;
                    }

                    // Update last watched timestamp
                    if let Err(e) = storage
                        .set_last_watched_timestamp(&chrono::Utc::now().to_rfc3339())
                        .await
                    {
                        tracing::warn!(
                            "failed to update last_watched_timestamp for {}: {}",
                            key,
                            e
                        );
                    }

                    tracing::debug!(
                        "processing {} changes, {} deletions for {}",
                        changed.len(),
                        deleted.len(),
                        key
                    );

                    // Invalidate discovery cache so file counts stay fresh
                    // (used by adaptive semaphore weight for linked project indexing)
                    if let Ok(root_path) = PathBuf::from(root.as_ref()).canonicalize() {
                        crate::discover::invalidate_discovery_cache(&root_path);
                    }

                    // Check if any changed/deleted files should invalidate the links cache
                    let should_invalidate =
                        changed.iter().any(|p| should_invalidate_links_cache(p))
                            || deleted.iter().any(|p| should_invalidate_links_cache(p));
                    if should_invalidate {
                        if let Ok(root_path) = PathBuf::from(root.as_ref()).canonicalize() {
                            let root_path_owned = root_path.clone();
                            tokio::spawn(async move {
                                invalidate_links_cache(&root_path_owned).await;
                            });
                        }
                    }

                    // Process deletions through write queue
                    if !deleted.is_empty() {
                        let paths_to_delete: Vec<String> = deleted
                            .iter()
                            .map(|path| {
                                crate::discover::relative_path(path, &*root, &*include_dirs)
                            })
                            .collect();
                        if let Err(e) = write_queue.delete(paths_to_delete).await {
                            tracing::warn!("failed to queue deletions: {}", e);
                        }
                    }

                    // Process changes on a dedicated blocking thread with its own
                    // mini tokio runtime. This offloads ALL file processing CPU from
                    // the main async worker thread, keeping it at <0.5% CPU during
                    // burst indexing. The blocking thread runs at full speed during
                    // a batch, then exits — O(1) CPU and memory.
                    if !changed.is_empty() {
                        let ops = changed.len() as u64;

                        // === OFFLOADED TO BLOCKING THREAD ===
                        let _result_count = {
                            let files: Vec<_> = changed.into_iter().collect();
                            let sema = Arc::new(tokio::sync::Semaphore::new(2));
                            let embed = Arc::new(tokio::sync::Semaphore::new(2));
                            let storage = storage.clone();
                            let write_queue = write_queue.clone();
                            let root = root.clone();
                            let include_dirs = include_dirs.clone();
                            let symlink_dirs = symlink_dirs.clone();
                            let tier = tier.clone();
                            let db_path = db_path.clone();

                            // Run directly on the main async runtime — no nested spawn_blocking,
                            // no nested runtime. All I/O (tokio::fs, HTTP) is already async.
                            // Only hash_content needs spawn_blocking, and the main runtime's
                            // max_blocking_threads(1) caps it. Eliminates the ~14 extra
                            // tokio-runtime-w threads created by the nested runtime pattern.
                            let mut futs: futures::stream::FuturesUnordered<_> = files
                                        .into_iter()
                                        .map(|path| {
                                            let sema = sema.clone();
                                            let embed = embed.clone();
                                            let storage = storage.clone();
                                            let write_queue = write_queue.clone();
                                            let root = root.clone();
                                            let include_dirs = include_dirs.clone();
                                            let symlink_dirs = symlink_dirs.clone();
                                            let tier = tier.clone();
                                            let db_path = db_path.clone();

                                            async move {
                                                let _permit = sema.acquire().await.ok()?;

                                                let rel = match tokio::fs::canonicalize(&path).await {
                                                    Ok(p) => p,
                                                    Err(_) => return None,
                                                };
                                                let rel_path = crate::discover::relative_path(
                                                    &rel,
                                                    &*root,
                                                    &*include_dirs,
                                                );

                                                // Fast-path: skip unchanged files
                                                if let Ok(content) = tokio::fs::read_to_string(&rel).await {
                                                    let hash = crate::storage::hash_content(&content);
                                                    if !storage
                                                        .needs_index(&rel_path, &hash)
                                                        .await
                                                        .unwrap_or(true)
                                                    {
                                                        return None;
                                                    }
                                                }

                                                // Index the file
                                                let mut client = crate::model_client::client().await.ok()?;

                                                    match crate::cli::update_file_partial_pub(
                                                        &*root,
                                                        &*include_dirs,
                                                        &*symlink_dirs,
                                                        &*db_path,
                                                        &storage,
                                                        &mut client,
                                                        &rel,
                                                        &tier,
                                                        dims,
                                                        None,
                                                        &*embed,
                                                        false,
                                                        false,
                                                        &*write_queue,
                                                )
                                                .await
                                                {
                                                    Ok(Some(update)) => {
                                                        tracing::debug!(
                                                            "indexed {}: {} chunks",
                                                            rel_path,
                                                            update.chunks
                                                        );
                                                        Some(1u64)
                                                    }
                                                    Ok(None) => None,
                                                    Err(e) => {
                                                        tracing::warn!("failed to index {}: {}", rel_path, e);
                                                        None
                                                    }
                                                }
                                            }
                                        })
                                        .collect();

                                    use futures::StreamExt as _;
                                    let mut count = 0u64;
                                    while let Some(_) = futs.next().await {
                                        count += 1;
                                    }
                                    count
                                };
                        // === MAIN ASYNC RUNTIME RESUMES ===

                        // Fix A2: Ensure FTS index exists after each batch that wrote chunks.
                        // Skip if already confirmed this session — avoids list_indices() overhead.
                        {
                            let db = db_path.as_ref().clone();
                            let already = fts_ensured().lock().await.contains(&db);
                            if !already {
                                let s2 = storage.clone();
                                let db2 = db.clone();
                                tokio::spawn(async move {
                                    match s2.create_fts_index().await {
                                        Ok(()) => {
                                            fts_ensured().lock().await.insert(db2);
                                        }
                                        Err(e) => tracing::warn!("fts index ensure failed: {e}"),
                                    }
                                });
                            }
                        }

                        // Update last_update_timestamp after successful processing
                        let now = chrono::Utc::now().to_rfc3339();
                        if let Err(e) = storage.set_last_update_timestamp(&now).await {
                            tracing::warn!(
                                "failed to update last_update_timestamp for {}: {}",
                                key,
                                e
                            );
                        }

                        // Update ops counter and queue compaction if threshold reached
                        {
                            let mut s = state.lock().await;
                            if let Some(w) = s.watchers.get_mut(&key) {
                                w.ops_since_compact += ops;
                                if w.ops_since_compact >= 100 {
                                    // Reset counter and queue compaction (non-blocking)
                                    w.ops_since_compact = 0;
                                    queue_compaction(
                                        &compaction_tx,
                                        (*db_path).clone(),
                                        dims,
                                        "watcher",
                                    );
                                }
                            }
                        }
                    }
                }
                // Floor sleep between drain cycles to prevent residual spin
                tokio::time::sleep(Duration::from_millis(100)).await;
            }

            tracing::info!("watcher processor shutting down");
        });
    }

    // Memory watcher processing task - simpler than project watcher (no partial updates)
    {
        let state = state.clone();
        let shutdown_rx = shutdown_rx.clone();
        tokio::spawn(async move {
            // Default: 2000ms. Override via OPENCODE_INDEXER_BATCH_INTERVAL_MS.
            let min_batch_interval = Duration::from_millis(
                std::env::var("OPENCODE_INDEXER_BATCH_INTERVAL_MS")
                    .ok()
                    .and_then(|v| v.parse::<u64>().ok())
                    .unwrap_or(2000),
            );

            loop {
                // Wait for any memory watcher to have pending changes
                let had_pending = tokio::time::timeout(
                    Duration::from_secs(300),
                    wait_for_any_memory_pending(&state),
                )
                .await
                .is_ok();

                // Check shutdown
                if *shutdown_rx.borrow() {
                    tracing::info!("memory watcher processor received shutdown");
                    break;
                }

                if !had_pending {
                    continue;
                }

                // Add batching delay to coalesce rapid changes
                tokio::time::sleep(min_batch_interval).await;

                // Get all memory watcher scopes
                let scopes: Vec<String> =
                    { state.lock().await.memory_watchers.keys().cloned().collect() };

                for scope in scopes {
                    // Clone watcher data under outer lock, then drop it before accessing inner pending lock
                    let watcher_data = {
                        let s = state.lock().await;
                        s.memory_watchers.get(&scope).map(|w| {
                            (
                                w.pending.clone(),       // Arc<Mutex<_>> clone
                                w.write_queue.clone(),   // Option<Arc<_>> clone
                                w.storage.clone(),       // Arc clone
                                w.root.clone(),          // Arc clone
                                w.db_path.clone(),       // Arc clone
                                w.failed_files.clone(),  // Arc<Mutex<_>> clone
                            )
                        })
                    };
                    // Outer lock dropped here

                    let Some((pending_mutex, write_queue_opt, storage, root, db_path, failed_files)) = watcher_data else {
                        continue;
                    };

                    // Now safe to acquire inner pending lock without holding outer lock
                    let (changed, deleted) = {
                        let mut pending = pending_mutex.lock().await;
                        if pending.is_empty() {
                            continue;
                        }
                        pending.drain()
                    };

                    // Skip if write_queue is drained
                    let Some(write_queue) = write_queue_opt else {
                        tracing::warn!(
                            "write_queue drained for memory watcher {}, skipping",
                            scope
                        );
                        continue;
                    };

                    // No per-batch cap — the single blocking thread in the runtime
                    // naturally serializes all file processing. The thread itself IS
                    // the memory and CPU bound. Process ALL pending files each iteration
                    // so the wait loop properly sleeps until new events arrive.
                    let changed: Vec<_> = changed.into_iter().collect();
                    let deleted: Vec<_> = deleted.into_iter().collect();

                    let total = changed.len() + deleted.len();
                    if total == 0 {
                        continue;
                    }

                    tracing::debug!("processing {} memory changes for scope: {}", total, scope);

                    // Process deletions
                    for file_path in deleted {
                        let rel_path = file_path
                            .strip_prefix(&*root)
                            .unwrap_or(&file_path)
                            .to_string_lossy()
                            .to_string();

                        write_queue.delete_file(&rel_path).await;
                        // Remove from failed files if it was there
                        failed_files.lock().await.remove(&file_path);
                        tracing::debug!("deleted memory file from index: {}", rel_path);
                    }

                    // Process changed files - actually index them
                    let embed = tokio::sync::Semaphore::new(4);
                    for file_path in changed {
                        // Skip temporary files
                        let filename = file_path.file_name().and_then(|n| n.to_str()).unwrap_or("");
                        if filename.starts_with('.') || filename.contains(".tmp.") {
                            continue;
                        }

                        let rel_path = file_path
                            .strip_prefix(&*root)
                            .unwrap_or(&file_path)
                            .to_string_lossy()
                            .to_string();

                        tracing::debug!(
                            "indexing changed memory file: {} (scope: {})",
                            rel_path,
                            scope
                        );

                        // Read file content
                        let content = match tokio::fs::read_to_string(&file_path).await {
                            Ok(c) => c,
                            Err(e) => {
                                tracing::debug!("failed to read memory file {}: {}", rel_path, e);
                                // Track failure for retry
                                let mut ff = failed_files.lock().await;
                                let count = ff.entry(file_path.clone()).or_insert(0);
                                *count += 1;
                                continue;
                            }
                        };

                        // Check if file needs indexing (hash comparison)
                        let file_hash = crate::storage::hash_content(&content);
                        if !storage
                            .needs_index(&rel_path, &file_hash)
                            .await
                            .unwrap_or(true)
                        {
                            tracing::debug!("memory file unchanged, skipping: {}", rel_path);
                            // Clear from failed files on success
                            failed_files.lock().await.remove(&file_path);
                            continue;
                        }

                        // Get model client and index the file
                        let mut client = match crate::model_client::client().await {
                            Ok(c) => c,
                            Err(e) => {
                                tracing::warn!(
                                    "failed to get model client for memory indexing: {}",
                                    e
                                );
                                // Track failure for retry
                                let mut ff = failed_files.lock().await;
                                let count = ff.entry(file_path.clone()).or_insert(0);
                                *count += 1;
                                continue;
                            }
                        };

                        let tier = "budget";
                        let dims = 1024u32;

                        // Use partial update to index the file
                        match crate::cli::update_file_partial_pub(
                            &root,
                            &[], // include_dirs
                            &[], // symlink_dirs
                            &db_path,
                            &*storage,
                            &mut client,
                            &file_path,
                            tier,
                            dims,
                            None,   // daily_cost_limit
                            &embed,
                            false, // force
                            false, // verbose
                            &write_queue,
                        )
                        .await
                        {
                            Ok(Some(update)) => {
                                tracing::info!(
                                    "indexed memory file: {} (chunks: {}, embedded: {})",
                                    rel_path,
                                    update.chunks,
                                    update.embedded
                                );
                                // Clear from failed files on success
                                failed_files.lock().await.remove(&file_path);
                            }
                            Ok(None) => {
                                tracing::debug!("memory file skipped (no changes): {}", rel_path);
                                // Clear from failed files on success
                                failed_files.lock().await.remove(&file_path);
                            }
                            Err(e) => {
                                tracing::warn!("failed to index memory file {}: {}", rel_path, e);
                                // Track failure for retry
                                let mut ff = failed_files.lock().await;
                                let count = ff.entry(file_path.clone()).or_insert(0);
                                *count += 1;
                                if *count >= MAX_FILE_RETRIES {
                                    tracing::error!(
                                        "memory file indexing failed after {} attempts: {}",
                                        MAX_FILE_RETRIES,
                                        rel_path
                                    );
                                    // Remove while still holding the lock - no double-lock
                                    ff.remove(&file_path);
                                }
                            }
                        }
                    }
                }
                // Floor sleep between drain cycles to prevent residual spin
                tokio::time::sleep(Duration::from_millis(100)).await;
            }

            tracing::info!("memory watcher processor shutting down");
        });
    }

    // Memory indexing retry task - periodically retries failed memory file indexing
    {
        let state = state.clone();
        let shutdown_rx = shutdown_rx.clone();
        tokio::spawn(async move {
            let retry_interval = Duration::from_secs(
                std::env::var("OPENCODE_INDEXER_MEMORY_RETRY_SECS")
                    .ok()
                    .and_then(|v| v.parse::<u64>().ok())
                    .unwrap_or(300),
            );

            let mut shutdown_rx = shutdown_rx.clone();

            loop {
                // Wait for retry interval or shutdown
                tokio::select! {
                    _ = tokio::time::sleep(retry_interval) => {}
                    _ = shutdown_rx.changed() => {
                        if *shutdown_rx.borrow() {
                            tracing::info!("memory indexing retry task received shutdown");
                            break;
                        }
                    }
                }

                // Check shutdown again
                if *shutdown_rx.borrow() {
                    break;
                }

                // Get all memory watcher scopes and their failed files
                let watchers_data: Vec<(
                    String,
                    Arc<PathBuf>,
                    Arc<PathBuf>,
                    Arc<crate::storage::Storage>,
                    Arc<crate::storage::WriteQueue>,
                    Arc<tokio::sync::Mutex<HashMap<PathBuf, u32>>>,
                )> = {
                    let s = state.lock().await;
                    s.memory_watchers
                        .iter()
                        .filter_map(|(scope, w)| {
                            w.write_queue.as_ref().map(|wq| {
                                (
                                    scope.clone(),
                                    w.root.clone(),
                                    w.db_path.clone(),
                                    w.storage.clone(),
                                    wq.clone(),
                                    w.failed_files.clone(),
                                )
                            })
                        })
                        .collect()
                };

                for (scope, root, db_path, storage, write_queue, failed_files) in watchers_data {
                    // Get files to retry (failure count < 3)
                    let files_to_retry: Vec<PathBuf> = {
                        let ff = failed_files.lock().await;
                        ff.iter()
                            .filter(|(_, count)| **count < 3)
                            .map(|(path, _)| path.clone())
                            .collect()
                    };

                    if files_to_retry.is_empty() {
                        continue;
                    }

                    tracing::debug!(
                        "retrying {} failed memory files for scope: {}",
                        files_to_retry.len(),
                        scope
                    );

                    let embed = tokio::sync::Semaphore::new(4);
                    for file_path in files_to_retry {
                        // Check if file still exists
                        if !tokio::fs::try_exists(&file_path).await.unwrap_or(false) {
                            // File deleted, remove from failed files
                            failed_files.lock().await.remove(&file_path);
                            continue;
                        }

                        let rel_path = file_path
                            .strip_prefix(&*root)
                            .unwrap_or(&file_path)
                            .to_string_lossy()
                            .to_string();

                        tracing::debug!("retrying memory file indexing: {}", rel_path);

                        // Read file content
                        let content = match tokio::fs::read_to_string(&file_path).await {
                            Ok(c) => c,
                            Err(e) => {
                                tracing::debug!(
                                    "failed to read memory file for retry {}: {}",
                                    rel_path,
                                    e
                                );
                                let mut ff = failed_files.lock().await;
                                if let Some(count) = ff.get_mut(&file_path) {
                                    *count += 1;
                                }
                                continue;
                            }
                        };

                        // Check if file needs indexing
                        let file_hash = crate::storage::hash_content(&content);
                        if !storage
                            .needs_index(&rel_path, &file_hash)
                            .await
                            .unwrap_or(true)
                        {
                            // Already indexed, remove from failed files
                            failed_files.lock().await.remove(&file_path);
                            continue;
                        }

                        // Get model client
                        let mut client = match crate::model_client::client().await {
                            Ok(c) => c,
                            Err(e) => {
                                tracing::warn!("failed to get model client for retry: {}", e);
                                let mut ff = failed_files.lock().await;
                                if let Some(count) = ff.get_mut(&file_path) {
                                    *count += 1;
                                }
                                continue;
                            }
                        };

                        let tier = "budget";
                        let dims = 1024u32;

                        // Index the file
                        match crate::cli::update_file_partial_pub(
                            &root,
                            &[],
                            &[],
                            &db_path,
                            &*storage,
                            &mut client,
                            &file_path,
                            tier,
                            dims,
                            None,
                            &embed,
                            false,
                            false,
                            &write_queue,
                        )
                        .await
                        {
                            Ok(Some(update)) => {
                                tracing::info!(
                                    "retry succeeded for memory file: {} (chunks: {}, embedded: {})",
                                    rel_path, update.chunks, update.embedded
                                );
                                failed_files.lock().await.remove(&file_path);
                            }
                            Ok(None) => {
                                tracing::debug!(
                                    "retry: memory file skipped (no changes): {}",
                                    rel_path
                                );
                                failed_files.lock().await.remove(&file_path);
                            }
                            Err(e) => {
                                tracing::warn!("retry failed for memory file {}: {}", rel_path, e);
                                let mut ff = failed_files.lock().await;
                                if let Some(count) = ff.get_mut(&file_path) {
                                    *count += 1;
                                    if *count >= MAX_FILE_RETRIES {
                                        tracing::error!("memory file indexing failed after {} retry attempts: {}", MAX_FILE_RETRIES, rel_path);
                                        // Remove from failed_files to prevent unbounded growth
                                        ff.remove(&file_path);
                                    }
                                }
                            }
                        }
                    }
                }
            }

            tracing::info!("memory indexing retry task shutting down");
        });
    }

    // Background compaction checker: periodically checks all tracked databases and
    // queues compaction when idle-time or threshold conditions are met.
    // Actual compaction is performed by the compaction_worker (one at a time).
    {
        let state = state.clone();
        let mut shutdown_rx = shutdown_rx.clone();
        let compaction_tx = compaction_tx.clone();
        tokio::spawn(async move {
            let mut ticker = tokio::time::interval(compaction_check_interval());
            loop {
                tokio::select! {
                    _ = ticker.tick() => {}
                    _ = shutdown_rx.changed() => {
                        if *shutdown_rx.borrow() {
                            tracing::info!("compaction checker received shutdown signal");
                            break;
                        }
                    }
                }

                let idle_threshold = compaction_idle_threshold();
                let ops_threshold = compaction_ops_threshold();
                let force_threshold = compaction_force_threshold();

                // Collect databases that need compaction and prune stale entries
                let to_compact: Vec<(PathBuf, u32)> = {
                    let mut s = state.lock().await;

                    // Remove compaction state entries that haven't been active in 24 hours
                    let now = Instant::now();
                    let stale_keys: Vec<CompactionKey> = s
                        .compaction
                        .iter()
                        .filter(|(_, cs)| {
                            now.duration_since(cs.last_activity) >= COMPACTION_STATE_TTL
                                && cs.operations_since_compact == 0
                                && !cs.compact_in_progress
                        })
                        .map(|(k, _)| k.clone())
                        .collect();

                    for key in &stale_keys {
                        if let Some(cs) = s.compaction.remove(key) {
                            tracing::debug!(
                                "pruned stale compaction state: {} (idle for {:?})",
                                cs.db_path.display(),
                                now.duration_since(cs.last_activity)
                            );
                        }
                    }

                    // Collect databases that need compaction (skip already in-progress)
                    s.compaction
                        .iter()
                        .filter(|(_, cs)| {
                            cs.should_compact(idle_threshold, ops_threshold, force_threshold)
                        })
                        .map(|(_, cs)| (cs.db_path.clone(), cs.dimensions))
                        .collect()
                };

                // Queue compaction requests (non-blocking)
                for (db_path, dims) in to_compact {
                    queue_compaction(&compaction_tx, db_path, dims, "periodic");
                }
            }
        });
    }

    // Set up signal handlers for graceful cleanup on SIGTERM/SIGINT/SIGHUP
    let mut sigterm =
        signal(SignalKind::terminate()).context("failed to register SIGTERM handler")?;
    let mut sigint =
        signal(SignalKind::interrupt()).context("failed to register SIGINT handler")?;
    let mut sighup = signal(SignalKind::hangup()).context("failed to register SIGHUP handler")?;

    // Start HTTP server (TCP when tcp_port is Some, Unix socket otherwise)
    let dispatcher = make_dispatcher(state.clone());
    if let Some(port) = tcp_port {
        // TCP mode: used for integration tests where each daemon gets its own port.
        // serve_tcp() emits {"type":"http_ready","port":N} itself before axum::serve.
        tokio::spawn(async move {
            if let Err(e) = crate::http_server::serve_tcp(port, dispatcher).await {
                tracing::error!("HTTP server (TCP) error: {}", e);
            }
        });
    } else {
        tokio::spawn(async move {
            match crate::http_server::serve(dispatcher).await {
                Ok(()) => {}
                Err(e) => {
                    if let Some(io_err) = e.downcast_ref::<std::io::Error>() {
                        if io_err.kind() == std::io::ErrorKind::AddrInUse {
                            println!("{}", serde_json::json!({"type": "already_running"}));
                            std::process::exit(0);
                        }
                    }
                    tracing::error!("HTTP server error: {}", e);
                }
            }
        });
        // Print ready line so the TS side knows when to connect.
        // Emit only after the daemon is fully initialized (including signal handlers)
        // to avoid races in tests and callers that send signals immediately.
        println!("{}", serde_json::json!({"type": "daemon_ready"}));
    }

    // Log configuration
    {
        // Initialize the semaphore to log the configured concurrency
        let _ = federated_search_semaphore();
        // Initialize the query cache to log the configured size
        let _ = query_cache();
    }

    // Idle shutdown monitor setup
    let idle_shutdown_secs = idle_shutdown_timeout(idle_shutdown_arg);
    if idle_shutdown_secs > 0 {
        tracing::info!("idle shutdown enabled (timeout={}s)", idle_shutdown_secs);
    } else {
        tracing::info!("idle shutdown disabled");
    }

    // Wait for a termination signal or idle timeout
    let shutdown_reason = if idle_shutdown_secs > 0 {
        let state_for_idle = state.clone();
        tokio::select! {
            _ = sigterm.recv() => { "SIGTERM" }
            _ = sigint.recv() => { "SIGINT" }
            _ = sighup.recv() => { "SIGHUP" }
            _ = async {
                loop {
                    let connections = crate::http_server::ACTIVE_CONNECTIONS.load(std::sync::atomic::Ordering::SeqCst);
                    let last_activity = {
                        let s = state_for_idle.lock().await;
                        s.last_activity
                    };
                    let idle = last_activity.elapsed().as_secs();

                    // Only consider idle shutdown when zero clients are connected.
                    // When connections exist, reset the idle timer (touch last_activity).
                    if connections > 0 {
                        let mut s = state_for_idle.lock().await;
                        s.last_activity = std::time::Instant::now();
                        tracing::debug!("idle check: {} active connections, resetting idle timer", connections);
                        tokio::time::sleep(Duration::from_secs(30)).await;
                        continue;
                    }

                    tracing::debug!("idle check: no connections, idle={}s, threshold={}s", idle, idle_shutdown_secs);
                    if idle >= idle_shutdown_secs {
                        tracing::info!("idle shutdown triggered (no connections for {}s)", idle);
                        break;
                    }
                    let remaining = idle_shutdown_secs.saturating_sub(idle).max(1);
                    let sleep_duration = std::cmp::min(remaining, 30);
                    tokio::time::sleep(Duration::from_secs(sleep_duration)).await;
                }
            } => { "idle timeout" }
        }
    } else {
        tokio::select! {
            _ = sigterm.recv() => { "SIGTERM" }
            _ = sigint.recv() => { "SIGINT" }
            _ = sighup.recv() => { "SIGHUP" }
        }
    };

    tracing::info!("shutting down (reason: {})", shutdown_reason);

    // Signal received or idle timeout — drain WriteQueues, compact, and clean up
    {
        let s = state.lock().await;
        let _ = s.shutdown.send(true);
    }
    tokio::time::sleep(Duration::from_millis(100)).await;
    shutdown_drain_write_queues(state.clone()).await;
    shutdown_compaction(state.clone()).await;
    crate::model_client::shutdown_embedder().await;
    #[cfg(target_os = "macos")]
    {
        let _ = tokio::fs::remove_file(crate::http_server::socket_file_path()).await;
    }
    kill_process_group();
    tracing::info!("daemon shutdown complete");
    std::process::exit(0);
}



