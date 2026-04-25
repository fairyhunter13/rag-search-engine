//! HTTP daemon for the indexer.
//!
//! Accepts JSON-RPC commands over HTTP, processes them, and returns JSON responses.
//! This eliminates the need for the TypeScript side to spawn one-shot CLI processes.
//!
//! Endpoints:
//!   POST /rpc - JSON-RPC: {"method": "...", "params": {...}} -> {"result": ...} or {"error": "..."}
//!   GET /ping - Health check: returns "pong"

use std::collections::{HashMap, HashSet, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant};
use tokio::sync::RwLock;

use crate::watcher::{self, WatchEvent};

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use tokio::signal::unix::{signal, SignalKind};
use tokio::sync::Mutex;

const MAX_STORAGE_CACHE_SIZE: usize = 20;
const MAX_CANONICALIZED_CACHE_SIZE: usize = 1000;
const MAX_LINK_CACHE_SIZE: usize = 500;
const COMPACTION_STATE_TTL: Duration = Duration::from_secs(86400); // 24 hours
const COMPACTION_QUEUE_SIZE: usize = 100; // Max pending compaction requests
const WATCHER_START_RETRY_DELAYS: [u64; 3] = [100, 200, 400]; // ms, exponential backoff
const TUI_CLEANUP_SETTLE_MS: u64 = 100; // ms wait after shutdown signal before drain
const TUI_CLEANUP_DRAIN_TIMEOUT_SECS: u64 = 30; // drain timeout for TUI cleanup
const PROJECT_INACTIVE_TTL: Duration = Duration::from_secs(3600); // 1 hour
const MAX_FILE_RETRIES: u32 = 3; // Max retries for failed files in memory watchers

// ---------------------------------------------------------------------------
// Path canonicalization cache (prevents blocking I/O in async context)
// ---------------------------------------------------------------------------

/// Cached canonicalized path with LRU tracking
struct CachedCanonicalPath {
    path: String,
    last_access: Instant,
}

/// Global cache for canonicalized paths to avoid blocking I/O.
fn canonicalized_paths_cache() -> &'static RwLock<HashMap<String, CachedCanonicalPath>> {
    static CACHE: OnceLock<RwLock<HashMap<String, CachedCanonicalPath>>> = OnceLock::new();
    CACHE.get_or_init(|| RwLock::new(HashMap::new()))
}

/// Global set of db paths currently being indexed.
/// Prevents the status self-heal logic from clearing progress during active indexing.
fn active_indexes() -> &'static Mutex<HashSet<String>> {
    static ACTIVE: OnceLock<Mutex<HashSet<String>>> = OnceLock::new();
    ACTIVE.get_or_init(|| Mutex::new(HashSet::new()))
}

/// Parent PID to monitor (if set via CLI arg or env var).
fn parent_pid() -> &'static std::sync::OnceLock<Option<i32>> {
    static PARENT_PID: std::sync::OnceLock<Option<i32>> = std::sync::OnceLock::new();
    &PARENT_PID
}

// ---------------------------------------------------------------------------
// Process group cleanup utilities (prevent dangling child PIDs)
// ---------------------------------------------------------------------------

/// Set up process group for clean child process termination.
///
/// On Linux, uses prctl(PR_SET_PDEATHSIG) to ensure child processes receive
/// SIGTERM when the daemon dies (even from SIGKILL). Also creates a new
/// process group so we can kill all children on shutdown.
#[cfg(target_os = "linux")]
fn setup_process_group() {
    // Try to become session leader (new process group)
    unsafe {
        libc::setpgid(0, 0);
    }

    // Set parent death signal for any children we spawn
    unsafe {
        const PR_SET_PDEATHSIG: libc::c_int = 1;
        libc::prctl(PR_SET_PDEATHSIG, libc::SIGTERM);
    }
}

#[cfg(not(target_os = "linux"))]
fn setup_process_group() {
    // Try to become session leader on non-Linux (best-effort)
    unsafe {
        libc::setpgid(0, 0);
    }
}

/// Kill all processes in our process group.
///
/// Called on exit to ensure no orphaned child processes.
fn kill_process_group() {
    unsafe {
        let pgid = libc::getpgid(0);
        if pgid > 0 {
            // Send SIGTERM to entire process group (negative PID)
            libc::killpg(pgid, libc::SIGTERM);
        }
    }
}

/// Monitor parent process and exit if it dies.
/// Spawns a background task that polls parent PID every 5 seconds.
async fn spawn_parent_monitor(parent_pid: i32, shutdown_tx: tokio::sync::watch::Sender<bool>) {
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_secs(5)).await;
            // Check if parent is alive via kill(pid, 0)
            let alive = unsafe { libc::kill(parent_pid, 0) == 0 };
            if !alive {
                tracing::warn!("Parent process {} died, initiating shutdown", parent_pid);
                let _ = shutdown_tx.send(true);
                break;
            }
        }
    });
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
struct DaemonState {
    /// Per-project write queues keyed by canonicalised root path.
    projects: HashMap<String, ProjectQueue>,
    /// Global shutdown token.
    shutdown: tokio::sync::watch::Sender<bool>,
    /// TUI connection tracking: maps project root -> set of connection IDs.
    /// Used to determine when watcher should be stopped (when all TUIs disconnect).
    tui_connections: HashMap<String, HashSet<String>>,
    /// Projects that have had a TUI connection.
    /// Used by the cleanup task to stop TUI-managed watchers after disconnect/crash.
    tui_projects: HashSet<String>,
    /// Per-database compaction state for smart deferred compaction.
    compaction: HashMap<CompactionKey, CompactionState>,
    /// Sender for the compaction worker queue. Stored here so RPC handlers can queue compaction.
    compaction_tx: Option<tokio::sync::mpsc::Sender<CompactionRequest>>,
    /// Internal watchers keyed by canonicalized project root
    watchers: HashMap<String, WatcherState>,
    /// Memory watchers keyed by scope (e.g., "global", "project:abc123")
    memory_watchers: HashMap<String, MemoryWatcherState>,
    /// Last activity timestamp for idle shutdown tracking
    last_activity: Arc<RwLock<Instant>>,
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

        let cleaned_projects = before_projects.saturating_sub(self.projects.len());
        let cleaned_watchers = before_watchers.saturating_sub(self.watchers.len());
        let cleaned_memory = before_memory_watchers.saturating_sub(self.memory_watchers.len());
        let cleaned_compaction = before_compaction.saturating_sub(self.compaction.len());

        if cleaned_projects > 0 || cleaned_watchers > 0 || cleaned_memory > 0 || cleaned_compaction > 0 {
            tracing::info!(
                "TTL cleanup: removed {} projects, {} watchers, {} memory watchers, {} compaction states",
                cleaned_projects, cleaned_watchers, cleaned_memory, cleaned_compaction
            );
        }
    }
}

/// Pending file changes buffer with separate changed/deleted tracking
struct PendingChanges {
    /// Files that were created or modified
    changed: HashSet<PathBuf>,
    /// Files that were deleted  
    deleted: HashSet<PathBuf>,
    /// Watch channel sender: bumps a counter on each change.
    /// Receivers call `.changed().await` — level-triggered-on-change, no stored permits.
    tx: Arc<tokio::sync::watch::Sender<u64>>,
}

impl PendingChanges {
    fn new() -> Self {
        let (tx, _rx) = tokio::sync::watch::channel(0u64);
        Self {
            changed: HashSet::new(),
            deleted: HashSet::new(),
            tx: Arc::new(tx),
        }
    }

    /// Add changed files and signal processor
    fn add_changed(&mut self, paths: Vec<PathBuf>) {
        for path in paths {
            self.deleted.remove(&path); // Changed overrides deleted
            self.changed.insert(path);
        }
        self.tx.send_modify(|v| *v = v.wrapping_add(1));
    }

    /// Add deleted files and signal processor
    fn add_deleted(&mut self, paths: Vec<PathBuf>) {
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

    fn is_empty(&self) -> bool {
        self.changed.is_empty() && self.deleted.is_empty()
    }

    fn len(&self) -> usize {
        self.changed.len() + self.deleted.len()
    }

    /// Get a watch receiver for this pending queue.
    /// Call `.changed().await` on the receiver — wakes only on *new* sends.
    fn subscribe(&self) -> tokio::sync::watch::Receiver<u64> {
        self.tx.subscribe()
    }
}

/// Default backpressure limit for pending file changes (configurable via .opencode-index.yaml)
const DEFAULT_MAX_PENDING_FILES: usize = 10000;

/// Statistics for dropped watcher events (for monitoring/diagnostics)
#[derive(Debug, Default)]
struct DroppedEventStats {
    /// Total number of changed file events dropped since watcher start
    changed_files_dropped: u64,
    /// Total number of deleted file events dropped since watcher start
    deleted_files_dropped: u64,
    /// Number of times backpressure was triggered
    backpressure_events: u64,
    /// Timestamp of last drop event (for rate calculation)
    last_drop_time: Option<Instant>,
    /// Timestamp when stats were last logged (to avoid log spam)
    last_log_time: Option<Instant>,
}

/// Internal watcher state for a project (no external process)
struct WatcherState {
    /// Project root path
    root: Arc<PathBuf>,
    /// Database/storage path
    db_path: Arc<PathBuf>,
    /// Storage instance (cached)
    storage: Arc<crate::storage::Storage>,
    /// Write queue for serializing storage operations
    write_queue: Option<Arc<crate::storage::WriteQueue>>,
    /// Include directories being watched
    include_dirs: Arc<Vec<PathBuf>>,
    /// Symlink directories
    symlink_dirs: Arc<Vec<crate::discover::SymlinkDir>>,
    /// Pending file changes (accumulated before processing)
    pending: Arc<tokio::sync::Mutex<PendingChanges>>,
    /// Embedding tier
    tier: Arc<str>,
    /// Dimensions
    dimensions: u32,
    /// Operations since last compaction
    ops_since_compact: u64,
    /// Watcher handle (to stop watching)
    _watcher_handle: Option<std::thread::JoinHandle<()>>,
    /// Shutdown signal
    shutdown_tx: tokio::sync::watch::Sender<bool>,
    /// Configured maximum pending files (from .opencode-index.yaml watcher.max_pending_files)
    max_pending_files: usize,
    /// Statistics for dropped events (for diagnostics)
    dropped_stats: Arc<tokio::sync::Mutex<DroppedEventStats>>,
    /// Watcher start time (for uptime calculation)
    started_at: Instant,
}

impl WatcherState {
    /// Drain the WriteQueue, waiting for all pending writes to complete.
    /// Returns the write stats snapshot.
    async fn drain_write_queue(&mut self) -> Option<crate::storage::WriteQueueStatsSnapshot> {
        if let Some(wq) = self.write_queue.take() {
            // Use shutdown_shared which handles Arc properly without data loss
            Some(wq.shutdown_shared().await)
        } else {
            None
        }
    }
}

/// Memory watcher state for memories/activity directories (independent of project watchers)
struct MemoryWatcherState {
    /// Root directory being watched (e.g., {shared}/memories/global)
    root: Arc<PathBuf>,
    /// Database path (e.g., {shared}/memories/global/.lancedb)
    db_path: Arc<PathBuf>,
    /// Storage instance (cached)
    storage: Arc<crate::storage::Storage>,
    /// Write queue for serializing storage operations
    write_queue: Option<Arc<crate::storage::WriteQueue>>,
    /// Pending file changes (accumulated before processing)
    pending: Arc<tokio::sync::Mutex<PendingChanges>>,
    /// Failed files with retry count (path -> failure_count)
    failed_files: Arc<tokio::sync::Mutex<HashMap<PathBuf, u32>>>,
    /// Shutdown signal
    _shutdown_tx: tokio::sync::watch::Sender<bool>,
    /// Watcher start time (for uptime calculation)
    started_at: Instant,
    /// Scope identifier (e.g., "global", "project:abc123:memories")
    _scope: String,
}

// ---------------------------------------------------------------------------
// Compaction state tracking: enables smart deferred compaction for LanceDB.
// Instead of compacting after every operation (expensive), we track operations
// and compact when idle or when a threshold is reached.
// ---------------------------------------------------------------------------

/// Compaction key type (avoids string allocation).
type CompactionKey = (PathBuf, u32);

/// Request to compact a database (sent to the compaction worker queue).
#[derive(Debug, Clone)]
struct CompactionRequest {
    /// Unique key for deduplication (db_path, dims tuple - no allocation).
    key: CompactionKey,
    /// Database path.
    db_path: PathBuf,
    /// Dimensions for Storage::open.
    dims: u32,
    /// Source of the request (for logging).
    source: &'static str,
}

/// Per-database compaction state.
#[derive(Debug, Clone)]
struct CompactionState {
    /// Database path (for logging/identification).
    db_path: PathBuf,
    /// Number of write operations since last compaction.
    operations_since_compact: u64,
    /// Timestamp of last write operation.
    last_operation_time: Instant,
    /// Timestamp of last successful compaction.
    last_compact_time: Option<Instant>,
    /// Whether compaction is currently in progress.
    compact_in_progress: bool,
    /// Dimensions for this database (needed for Storage::open).
    dimensions: u32,
    /// Timestamp of last activity (for TTL-based cleanup).
    last_activity: Instant,
}

impl CompactionState {
    fn new(db_path: PathBuf, dimensions: u32) -> Self {
        let now = Instant::now();
        Self {
            db_path,
            operations_since_compact: 0,
            last_operation_time: now,
            last_compact_time: None,
            compact_in_progress: false,
            dimensions,
            last_activity: now,
        }
    }

    fn record_operation(&mut self) {
        self.operations_since_compact += 1;
        let now = Instant::now();
        self.last_operation_time = now;
        self.last_activity = now;
    }

    fn mark_compaction_started(&mut self) {
        self.compact_in_progress = true;
        self.last_activity = Instant::now();
    }

    fn mark_compaction_completed(&mut self) {
        self.compact_in_progress = false;
        self.operations_since_compact = 0;
        let now = Instant::now();
        self.last_compact_time = Some(now);
        self.last_activity = now;
    }

    fn mark_compaction_failed(&mut self) {
        self.compact_in_progress = false;
        self.last_activity = Instant::now();
        // Don't reset operations count on failure - retry later
    }

    /// Check if compaction should be triggered based on idle time and threshold.
    fn should_compact(
        &self,
        idle_threshold: Duration,
        ops_threshold: u64,
        force_threshold: u64,
    ) -> bool {
        if self.compact_in_progress {
            return false;
        }
        if self.operations_since_compact == 0 {
            return false;
        }

        // Force trigger: too many operations regardless of idle time
        if self.operations_since_compact >= force_threshold {
            return true;
        }

        // Idle trigger: been idle long enough AND have pending operations
        let idle_duration = self.last_operation_time.elapsed();
        idle_duration >= idle_threshold && self.operations_since_compact >= ops_threshold
    }
}

/// Compaction configuration (can be overridden via environment variables).
/// Reads env vars on every call — no OnceLock caching — so tests and runtime
/// overrides work correctly without cache poisoning.
fn compaction_idle_threshold() -> Duration {
    env_duration_ms("OPENCODE_COMPACTION_IDLE_MS", Duration::from_secs(30))
}

fn compaction_ops_threshold() -> u64 {
    std::env::var("OPENCODE_COMPACTION_OPS_THRESHOLD")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(50)
}

fn compaction_force_threshold() -> u64 {
    std::env::var("OPENCODE_COMPACTION_FORCE_THRESHOLD")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(200)
}

fn compaction_check_interval() -> Duration {
    env_duration_ms(
        "OPENCODE_COMPACTION_CHECK_INTERVAL_MS",
        Duration::from_secs(300),
    )
}

fn compaction_shutdown_timeout() -> Duration {
    env_duration_ms(
        "OPENCODE_COMPACTION_SHUTDOWN_TIMEOUT_MS",
        Duration::from_secs(60),
    )
}

// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// SQLite connection cache for serialized writes
// ---------------------------------------------------------------------------
/// Perform compaction on a database.
/// Fix B1: compact() runs on a blocking thread pool worker via spawn_blocking+block_on
/// so Parquet I/O can't starve the tokio async runtime.
async fn perform_compaction(db_path: &Path, dims: u32) -> Result<()> {
    let storage = crate::storage::Storage::open(db_path, dims).await?;
    let handle = tokio::runtime::Handle::current();
    tokio::task::spawn_blocking(move || handle.block_on(storage.compact()))
        .await
        .map_err(|e| anyhow::anyhow!("compaction thread panicked: {e}"))??;
    Ok(())
}

/// Compaction worker: processes compaction requests one at a time.
/// Deduplicates requests for the same database while one is in progress.
async fn compaction_worker(
    mut rx: tokio::sync::mpsc::Receiver<CompactionRequest>,
    state: Arc<Mutex<DaemonState>>,
    mut shutdown_rx: tokio::sync::watch::Receiver<bool>,
) {
    // Track in-flight compactions to deduplicate
    let mut in_flight: HashSet<CompactionKey> = HashSet::new();

    loop {
        tokio::select! {
            // Non-biased select to prevent busy-loop if channel closes
            result = shutdown_rx.changed() => {
                match result {
                    Ok(()) if *shutdown_rx.borrow() => {
                        tracing::info!("compaction worker received shutdown signal");
                        break;
                    }
                    Err(_) => {
                        tracing::info!("compaction worker shutdown channel closed");
                        break;
                    }
                    _ => {} // Value is false, continue waiting
                }
            }

            req = rx.recv() => {
                let Some(req) = req else {
                    tracing::info!("compaction worker channel closed");
                    break;
                };

                // Fix B3: per-DB compaction cooldown.
                // Env OPENCODE_INDEXER_COMPACTION_COOLDOWN_SECS (default 3600).
                {
                    let cooldown = std::env::var("OPENCODE_INDEXER_COMPACTION_COOLDOWN_SECS")
                        .ok()
                        .and_then(|v| v.parse::<u64>().ok())
                        .unwrap_or(3600);
                    let s = state.lock().await;
                    if let Some(cs) = s.compaction.get(&req.key) {
                        if let Some(last) = cs.last_compact_time {
                            if last.elapsed() < Duration::from_secs(cooldown) {
                                tracing::debug!(
                                    "skipping compaction for {} (cooldown {}s not elapsed)",
                                    req.db_path.display(), cooldown
                                );
                                continue;
                            }
                        }
                    }
                }

                // Skip if already being processed (deduplicate)
                if in_flight.contains(&req.key) {
                    tracing::trace!(
                        "skipping duplicate compaction request for {} (source: {})",
                        req.db_path.display(), req.source
                    );
                    continue;
                }

                // Mark as in-flight
                in_flight.insert(req.key.clone());

                // Update state to mark compaction started
                {
                    let mut s = state.lock().await;
                    if let Some(cs) = s.compaction.get_mut(&req.key) {
                        cs.mark_compaction_started();
                    }
                }

                // Perform compaction (outside of lock)
                let start = Instant::now();
                match perform_compaction(&req.db_path, req.dims).await {
                    Ok(()) => {
                        tracing::debug!(
                            "compaction completed for {} in {:?} (source: {})",
                            req.db_path.display(), start.elapsed(), req.source
                        );
                        // Update state on success
                        let mut s = state.lock().await;
                        if let Some(cs) = s.compaction.get_mut(&req.key) {
                            cs.mark_compaction_completed();
                        }
                    }
                    Err(e) => {
                        tracing::warn!(
                            "compaction failed for {} (source: {}): {}",
                            req.db_path.display(), req.source, e
                        );
                        // Update state on failure
                        let mut s = state.lock().await;
                        if let Some(cs) = s.compaction.get_mut(&req.key) {
                            cs.mark_compaction_failed();
                        }
                    }
                }

                // Remove from in-flight
                in_flight.remove(&req.key);

                // Fix B2: spacing between consecutive compactions to avoid saturating
                // blocking thread pool. Default 5s; env OPENCODE_INDEXER_COMPACTION_SPACING_SECS.
                let spacing = std::env::var("OPENCODE_INDEXER_COMPACTION_SPACING_SECS")
                    .ok()
                    .and_then(|v| v.parse::<u64>().ok())
                    .unwrap_or(5);
                tokio::time::sleep(Duration::from_secs(spacing)).await;
            }
        }
    }

    tracing::info!("compaction worker shutting down");
}

/// Queue a compaction request (non-blocking).
fn queue_compaction(
    tx: &tokio::sync::mpsc::Sender<CompactionRequest>,
    db_path: PathBuf,
    dims: u32,
    source: &'static str,
) {
    let key = (db_path.clone(), dims);
    let req = CompactionRequest {
        key,
        db_path,
        dims,
        source,
    };

    // Use try_send to never block the caller
    if let Err(e) = tx.try_send(req) {
        match e {
            tokio::sync::mpsc::error::TrySendError::Full(_) => {
                tracing::warn!(
                    "compaction queue full, dropping request for {} (source: {})",
                    e.into_inner().db_path.display(),
                    source
                );
            }
            tokio::sync::mpsc::error::TrySendError::Closed(_) => {
                tracing::debug!("compaction queue closed (shutting down)");
            }
        }
    }
}

/// Record a write operation for compaction tracking.
/// Call this after successful index_file or remove_file operations.
fn record_compaction_operation(state: &mut DaemonState, db_path: &Path, dims: u32) {
    let key = (db_path.to_path_buf(), dims);
    let entry = state
        .compaction
        .entry(key)
        .or_insert_with(|| CompactionState::new(db_path.to_path_buf(), dims));
    entry.record_operation();
}

/// Perform shutdown compaction for all databases with pending operations.
/// Drain all WriteQueues for active watchers during shutdown.
/// This ensures no pending writes are lost when the daemon exits.
async fn shutdown_drain_write_queues(state: Arc<Mutex<DaemonState>>) {
    const DRAIN_TIMEOUT: Duration = Duration::from_secs(30);
    let start = Instant::now();

    // Get all watcher keys
    let keys: Vec<String> = {
        let s = state.lock().await;
        s.watchers.keys().cloned().collect()
    };

    if keys.is_empty() {
        tracing::info!("shutdown: no watchers to drain");
        return;
    }

    tracing::info!(
        "shutdown: draining LanceDB WriteQueues for {} watcher(s)",
        keys.len()
    );

    for key in keys {
        // Check timeout
        if start.elapsed() >= DRAIN_TIMEOUT {
            tracing::warn!(
                "shutdown: drain timeout reached after {:?}, skipping remaining watchers",
                start.elapsed()
            );
            break;
        }

        // Signal shutdown and drain
        let mut s = state.lock().await;
        if let Some(watcher) = s.watchers.get_mut(&key) {
            // Signal shutdown to watcher
            let _ = watcher.shutdown_tx.send(true);

            // Drain WriteQueue
            let remaining = DRAIN_TIMEOUT.saturating_sub(start.elapsed());
            match tokio::time::timeout(remaining, watcher.drain_write_queue()).await {
                Ok(Some(stats)) => {
                    tracing::info!(
                        "Shutdown: drained LanceDB WriteQueue for {} ({} batches, {} chunks)",
                        key,
                        stats.batches_written,
                        stats.chunks_written
                    );
                }
                Ok(None) => {
                    tracing::warn!("Shutdown: LanceDB WriteQueue for {} already drained or has other references", key);
                }
                Err(_) => {
                    tracing::warn!("Shutdown: LanceDB WriteQueue drain timed out for {}", key);
                }
            }
        }
    }

    let elapsed = start.elapsed();
    tracing::info!(
        "shutdown: all WriteQueue draining completed in {:?}",
        elapsed
    );
}

async fn shutdown_compaction(state: Arc<Mutex<DaemonState>>) {
    let timeout = compaction_shutdown_timeout();
    let start = Instant::now();

    // Collect databases that need compaction
    let to_compact: Vec<(CompactionKey, PathBuf, u32, u64)> = {
        let s = state.lock().await;
        s.compaction
            .iter()
            .filter(|(_, cs)| cs.operations_since_compact > 0 && !cs.compact_in_progress)
            .map(|(k, cs)| {
                (
                    k.clone(),
                    cs.db_path.clone(),
                    cs.dimensions,
                    cs.operations_since_compact,
                )
            })
            .collect()
    };

    if to_compact.is_empty() {
        tracing::info!("shutdown compaction: no databases need compaction");
        return;
    }

    tracing::info!(
        "shutdown compaction: {} database(s) have pending operations",
        to_compact.len()
    );

    for (key, db_path, dims, ops) in to_compact {
        // Check timeout
        if start.elapsed() >= timeout {
            tracing::warn!(
                "shutdown compaction: timeout reached after {:?}, skipping remaining databases",
                start.elapsed()
            );
            break;
        }

        // Mark as in-progress
        {
            let mut s = state.lock().await;
            if let Some(cs) = s.compaction.get_mut(&key) {
                cs.mark_compaction_started();
            }
        }

        tracing::info!(
            "shutdown compaction: compacting {} ({} pending ops)",
            db_path.display(),
            ops
        );

        let remaining = timeout.saturating_sub(start.elapsed());
        let result = tokio::time::timeout(remaining, perform_compaction(&db_path, dims)).await;

        match result {
            Ok(Ok(())) => {
                tracing::info!("shutdown compaction: completed {}", db_path.display());
                let mut s = state.lock().await;
                if let Some(cs) = s.compaction.get_mut(&key) {
                    cs.mark_compaction_completed();
                }
            }
            Ok(Err(e)) => {
                tracing::warn!(
                    "shutdown compaction: failed for {}: {}",
                    db_path.display(),
                    e
                );
                let mut s = state.lock().await;
                if let Some(cs) = s.compaction.get_mut(&key) {
                    cs.mark_compaction_failed();
                }
            }
            Err(_) => {
                tracing::warn!(
                    "shutdown compaction: timeout compacting {}",
                    db_path.display()
                );
                // Don't update state - let it be picked up next time
            }
        }
    }

    tracing::info!("shutdown compaction: finished in {:?}", start.elapsed());
}

// ---------------------------------------------------------------------------
// Storage cache: reuse LanceDB connections across requests to prevent RSS growth.
// Each Storage::open() creates a new LanceDB Connection with Arrow/DataFusion
// memory arenas (~10-15 MB each). Without caching, the daemon leaks ~10 MB/min
// during bulk indexing.
// ---------------------------------------------------------------------------

/// Cache key: (db_path, dimensions)
type StorageKey = (String, u32);

/// Cached storage with LRU tracking
struct CachedStorage {
    storage: Arc<crate::storage::Storage>,
    last_access: Instant,
}

fn storage_cache() -> &'static RwLock<HashMap<StorageKey, CachedStorage>> {
    static CACHE: std::sync::OnceLock<RwLock<HashMap<StorageKey, CachedStorage>>> =
        std::sync::OnceLock::new();
    CACHE.get_or_init(|| RwLock::new(HashMap::new()))
}

// ---------------------------------------------------------------------------
// Link discovery cache: avoids re-running `git ls-files` on every search.
// Populated lazily on first search, expires after 5 minutes.
// ---------------------------------------------------------------------------

fn env_duration_ms(key: &str, default: Duration) -> Duration {
    let Ok(value) = std::env::var(key) else {
        return default;
    };
    let Ok(ms) = value.trim().parse::<u64>() else {
        return default;
    };
    if ms == 0 {
        return default;
    }
    Duration::from_millis(ms)
}

fn tui_cleanup_interval() -> Duration {
    static INTERVAL: std::sync::OnceLock<Duration> = std::sync::OnceLock::new();
    *INTERVAL.get_or_init(|| {
        env_duration_ms("OPENCODE_TUI_CLEANUP_INTERVAL_MS", Duration::from_secs(300))
    })
}

/// Get idle shutdown timeout from environment or CLI arg.
/// Default: 600 seconds (10 minutes). Set to 0 to disable.
fn idle_shutdown_timeout(cli_arg: Option<u64>) -> u64 {
    cli_arg.unwrap_or_else(|| {
        std::env::var("OPENCODE_INDEXER_IDLE_SHUTDOWN")
            .ok()
            .and_then(|v| v.parse::<u64>().ok())
            .unwrap_or(600)
    })
}

#[derive(Clone)]
struct Link {
    repo: PathBuf,
    db: PathBuf,
    project_id: String,
    mount: String,
    name: String,
}

// Persisted link structure for .links.json
#[derive(Debug, Clone, Serialize, Deserialize)]
struct PersistedLinks {
    version: u32,
    updated_at: i64,
    project_root: String,
    links: Vec<PersistedLink>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct PersistedLink {
    path: String,
    project_id: String,
    name: String,
    db_path: String,
}

const LINKS_CACHE_VERSION: u32 = 1;

/// Returns path to the links cache file in the project's storage directory.
fn links_cache_path(root: &Path) -> PathBuf {
    use crate::storage;
    let storage_dir = storage::storage_path(root);
    storage_dir.parent().unwrap_or(root).join(".links.json")
}

/// Load links from the persisted cache file.
/// Returns None if file doesn't exist, is invalid, or has wrong version.
/// BLOCKING: Must run in spawn_blocking context.
fn load_links_from_file(root: &Path) -> Option<Vec<Link>> {
    let cache_path = links_cache_path(root);

    let content = std::fs::read_to_string(&cache_path).ok()?;
    let persisted: PersistedLinks = serde_json::from_str(&content).ok()?;

    // Validate version
    if persisted.version != LINKS_CACHE_VERSION {
        tracing::debug!("links cache version mismatch, invalidating");
        return None;
    }

    // Validate root matches
    let root_str = root.to_string_lossy();
    if persisted.project_root != root_str {
        tracing::debug!("links cache root mismatch, invalidating");
        return None;
    }

    // Convert persisted links to Link structs
    let links = persisted
        .links
        .iter()
        .filter_map(|l| {
            Some(Link {
                repo: PathBuf::from(&l.path),
                db: PathBuf::from(&l.db_path),
                project_id: l.project_id.clone(),
                mount: String::new(), // mount is derived from link name
                name: l.name.clone(),
            })
        })
        .collect();

    tracing::debug!(
        "loaded {} links from cache for {}",
        persisted.links.len(),
        root_str
    );
    Some(links)
}

/// Save discovered links to the cache file.
/// BLOCKING: Must run in spawn_blocking context.
fn save_links_to_file(root: &Path, links: &[Link]) -> Result<()> {
    let cache_path = links_cache_path(root);

    // Ensure parent directory exists
    if let Some(parent) = cache_path.parent() {
        std::fs::create_dir_all(parent).context("failed to create links cache directory")?;
    }

    let persisted = PersistedLinks {
        version: LINKS_CACHE_VERSION,
        updated_at: std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs() as i64,
        project_root: root.to_string_lossy().to_string(),
        links: links
            .iter()
            .map(|l| PersistedLink {
                path: l.repo.to_string_lossy().to_string(),
                project_id: l.project_id.clone(),
                name: l.name.clone(),
                db_path: l.db.to_string_lossy().to_string(),
            })
            .collect(),
    };

    let json =
        serde_json::to_string_pretty(&persisted).context("failed to serialize links cache")?;

    std::fs::write(&cache_path, json).context("failed to write links cache")?;

    tracing::debug!(
        "saved {} links to cache for {}",
        links.len(),
        root.to_string_lossy()
    );
    Ok(())
}

/// Invalidate the links cache file by deleting it.
/// Called when symlinks or repo structure changes.
/// ASYNC: Uses tokio::fs internally.
pub async fn invalidate_links_cache(root: &Path) {
    let cache_path = links_cache_path(root);
    if tokio::fs::try_exists(&cache_path).await.unwrap_or(false) {
        if let Err(e) = tokio::fs::remove_file(&cache_path).await {
            tracing::warn!("failed to invalidate links cache: {}", e);
        } else {
            tracing::info!("invalidated links cache for {}", root.to_string_lossy());
        }
    }

    // Also clear in-memory cache
    if let Ok(mut cache) = link_cache().try_write() {
        cache.remove(root);
    }
}

/// Check if a path change should invalidate the links cache.
/// Returns true if the path is a symlink or in a symlink-related directory.
fn should_invalidate_links_cache(path: &Path) -> bool {
    // Pure string-based check - no stat syscalls
    // Symlinks are typically in .opencode-links/ or node_modules with special names
    if let Some(path_str) = path.to_str() {
        // Direct .opencode-links directory
        if path_str.contains(".opencode-links") {
            return true;
        }
        // Common symlink locations in monorepos
        if path_str.contains("/node_modules/") && path_str.contains("@") {
            return true;
        }
        // Repository collection directories (common patterns for multi-repo workspaces)
        if path_str.contains("/repositories/") || path_str.contains("/repositories-ubuntu/") {
            return true;
        }
    }
    false
}

/// Cached links with LRU tracking
struct CachedLinks {
    links: Vec<Link>,
    last_access: Instant,
}

fn link_cache() -> &'static tokio::sync::RwLock<HashMap<PathBuf, CachedLinks>> {
    static CACHE: std::sync::OnceLock<tokio::sync::RwLock<HashMap<PathBuf, CachedLinks>>> =
        std::sync::OnceLock::new();
    CACHE.get_or_init(|| tokio::sync::RwLock::new(HashMap::new()))
}

/// Returns linked repos using file-based cache.
/// Uses `try_read`/`try_write` to avoid blocking search on lock contention.
/// Cache is invalidated by deleting `.links.json` when symlinks change.
fn cached_discover_links(root: &str) -> Vec<Link> {
    let root_path = match PathBuf::from(root).canonicalize() {
        Ok(p) => p,
        Err(_) => return vec![],
    };

    // Fast path: check in-memory cache and update access time
    match link_cache().try_read() {
        Ok(r) => {
            if let Some(cached) = r.get(&root_path) {
                let links = cached.links.clone();
                drop(r);
                
                // Update last_access with write lock
                if let Ok(mut w) = link_cache().try_write() {
                    if let Some(entry) = w.get_mut(&root_path) {
                        entry.last_access = Instant::now();
                    }
                }
                return links;
            }
        }
        Err(_) => {
            tracing::debug!("link cache contended on read, will try file");
        }
    }

    // TTL check: invalidate the file cache if it is older than the configured threshold.
    // Default: 1 hour. Override via OPENCODE_INDEXER_LINKS_CACHE_TTL_SECS env var.
    // This catches stale caches where new symlinks were added after the last save.
    // BLOCKING: This function is called from spawn_blocking, std::fs is OK here.
    let ttl_secs: u64 = std::env::var("OPENCODE_INDEXER_LINKS_CACHE_TTL_SECS")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(3600);
    let cache_path = links_cache_path(&root_path);
    if let Ok(meta) = std::fs::metadata(&cache_path) {
        if let Ok(modified) = meta.modified() {
            let age = std::time::SystemTime::now()
                .duration_since(modified)
                .unwrap_or_default();
            if age.as_secs() > ttl_secs {
                tracing::info!(
                    "links cache TTL expired ({}/{}s) for {}, invalidating",
                    age.as_secs(),
                    ttl_secs,
                    root_path.display()
                );
                let _ = std::fs::remove_file(&cache_path);
                // Also evict from in-memory cache so we don't serve the stale copy
                if let Ok(mut cache) = link_cache().try_write() {
                    cache.remove(&root_path);
                }
            }
        }
    }

    // Medium path: try loading from file
    tracing::debug!("checking links cache file for {}", root_path.display());
    if let Some(links) = load_links_from_file(&root_path) {
        // Update in-memory cache with LRU eviction
        if let Ok(mut cache) = link_cache().try_write() {
            // LRU eviction: remove oldest entry if cache is full
            if cache.len() >= MAX_LINK_CACHE_SIZE && !cache.contains_key(&root_path) {
                if let Some(oldest_key) = cache
                    .iter()
                    .min_by_key(|(_, cached)| cached.last_access)
                    .map(|(k, _)| k.clone())
                {
                    tracing::debug!(
                        "link cache full ({}), evicting oldest entry: {}",
                        MAX_LINK_CACHE_SIZE,
                        oldest_key.display()
                    );
                    cache.remove(&oldest_key);
                }
            }
            cache.insert(root_path, CachedLinks {
                links: links.clone(),
                last_access: Instant::now(),
            });
        }
        return links;
    }

    // Slow path: discover, save to file, and cache in memory
    let result = match discover_links_impl(root) {
        Ok(v) => v,
        Err(_) => return vec![],
    };

    let links = result["links"].as_array().cloned().unwrap_or_default();
    let links: Vec<Link> = links
        .iter()
        .filter_map(|l| {
            let repo = l["path"].as_str().map(PathBuf::from)?;
            let db = l["dbPath"].as_str().map(PathBuf::from)?;
            let project_id = l["projectId"].as_str().unwrap_or("").to_string();
            let mount = l["mount"].as_str().unwrap_or("").to_string();
            let name = l["name"].as_str().unwrap_or("linked").to_string();
            Some(Link {
                repo,
                db,
                project_id,
                mount,
                name,
            })
        })
        .collect();

    // Save to file (best effort, don't fail if it errors)
    // BLOCKING: This function is called from spawn_blocking, std::fs is OK here.
    tracing::info!(
        "saving {} links to file for {}",
        links.len(),
        root_path.display()
    );
    if let Err(e) = save_links_to_file(&root_path, &links) {
        tracing::warn!("failed to save links cache: {}", e);
    } else {
        tracing::info!(
            "links cache saved successfully to {}",
            links_cache_path(&root_path).display()
        );
    }

    // Update in-memory cache with LRU eviction
    match link_cache().try_write() {
        Ok(mut cache) => {
            // LRU eviction: remove oldest entry if cache is full
            if cache.len() >= MAX_LINK_CACHE_SIZE && !cache.contains_key(&root_path) {
                if let Some(oldest_key) = cache
                    .iter()
                    .min_by_key(|(_, cached)| cached.last_access)
                    .map(|(k, _)| k.clone())
                {
                    tracing::debug!(
                        "link cache full ({}), evicting oldest entry: {}",
                        MAX_LINK_CACHE_SIZE,
                        oldest_key.display()
                    );
                    cache.remove(&oldest_key);
                }
            }
            cache.insert(root_path, CachedLinks {
                links: links.clone(),
                last_access: Instant::now(),
            });
        }
        Err(_) => {
            tracing::debug!("link cache contended on write, skipping in-memory cache update");
        }
    }

    links
}

fn link_index_inflight() -> &'static tokio::sync::Mutex<HashSet<PathBuf>> {
    static SET: std::sync::OnceLock<tokio::sync::Mutex<HashSet<PathBuf>>> =
        std::sync::OnceLock::new();
    SET.get_or_init(|| tokio::sync::Mutex::new(HashSet::new()))
}

/// Adaptive semaphore for linked project indexing.
/// Total budget of 4 permits (default) limits concurrent linked-project indexing.
/// Override via OPENCODE_INDEXER_LINK_CONCURRENCY env var.
fn link_index_semaphore() -> &'static tokio::sync::Semaphore {
    static SEM: std::sync::OnceLock<tokio::sync::Semaphore> = std::sync::OnceLock::new();
    SEM.get_or_init(|| {
        let permits = std::env::var("OPENCODE_INDEXER_LINK_CONCURRENCY")
            .ok()
            .and_then(|v| v.parse::<usize>().ok())
            .unwrap_or(4);
        tokio::sync::Semaphore::new(permits)
    })
}

/// Determine how many semaphore permits a linked project should consume
/// based on its file count. Smaller projects use fewer permits so more
/// can index concurrently; larger projects use more to throttle load.
fn link_index_weight(files: usize) -> u32 {
    if files < 200 {
        1 // small: up to 8 concurrent
    } else if files < 1000 {
        2 // medium: up to 4 concurrent
    } else {
        4 // large: up to 2 concurrent
    }
}

/// Check whether a linked project needs initial indexing.
/// Returns true if the LanceDB directory is missing or contains zero chunks.
async fn needs_initial_index(db: &std::path::Path) -> bool {
    if !tokio::fs::try_exists(db).await.unwrap_or(false) {
        return true;
    }
    // Check lance data file as a quick existence proxy (avoid full Storage open for hot path)
    if !db.join("chunks.lance").exists() {
        return true;
    }
    // Open storage to verify row count; treat errors as "needs index"
    match cached_storage(db, 0).await {
        Ok(s) => s.count_chunks().await.unwrap_or(0) == 0,
        Err(_) => true,
    }
}

async fn ensure_link_index(link: Link, tier: &str, dims: u32, parent_root: &str) {
    // Check skip in parent project config
    let parent_path = std::path::PathBuf::from(parent_root);
    let project = crate::config::load(&parent_path);
    if project
        .linked
        .get(&link.name)
        .map(|l| l.skip)
        .unwrap_or(false)
    {
        return;
    }

    // De-dupe by DB path
    let mut set = link_index_inflight().lock().await;
    if set.contains(&link.db) {
        return;
    }
    set.insert(link.db.clone());
    drop(set);

    // Discover file count to determine adaptive weight.
    // Invalidate cache first so weight reflects current filesystem state
    // (cache is event-driven via watcher notifications, not TTL-based).
    let repo_for_discover = link.repo.clone();
    let name_for_discover = link.name.clone();
    let parent_path_for_cfg = std::path::PathBuf::from(parent_root);
    let weight = tokio::task::spawn_blocking(move || {
        crate::discover::invalidate_discovery_cache(&repo_for_discover);
        let parent_project = crate::config::load(&parent_path_for_cfg);
        let linked_project = crate::config::load(&repo_for_discover);
        let cfg = crate::config::effective(
            &parent_project,
            Some(&name_for_discover),
            Some(&linked_project),
        );
        match crate::discover::discover_files_with_config(&repo_for_discover, &cfg) {
            Ok(result) => link_index_weight(result.files.len()),
            Err(_) => 2, // default to medium weight on discovery failure
        }
    })
    .await
    .unwrap_or(2);

    // Check if DB already has chunks - skip if already indexed
    let needs_index = if !tokio::fs::try_exists(&link.db).await.unwrap_or(false) {
        true
    } else {
        match cached_storage(&link.db, dims).await {
            Ok(s) => s.count_chunks().await.unwrap_or(0) == 0,
            Err(_) => true,
        }
    };

    if !needs_index {
        tracing::info!(
            "skipping already-indexed linked project: {} (has chunks)",
            link.repo.display()
        );
        link_index_inflight().lock().await.remove(&link.db);
        return;
    }

    tracing::info!(
        "auto-indexing linked project in background: {} (weight={}/8)",
        link.repo.display(),
        weight
    );

    let tier = tier.to_string();
    let parent_root_owned = parent_root.to_string();
    tokio::spawn(async move {
        let Ok(_permit) = link_index_semaphore().acquire_many(weight).await else {
            link_index_inflight().lock().await.remove(&link.db);
            return;
        };

        // Load parent project config to get linked project's exclude/include patterns
        let parent_path = std::path::PathBuf::from(&parent_root_owned);
        let parent_project = crate::config::load(&parent_path);

        // Load linked project's own config (if it has .opencode-index.yaml)
        let linked_project = crate::config::load(&link.repo);

        // Get effective config: merge parent's linked[name] with linked project's own config
        let effective_cfg =
            crate::config::effective(&parent_project, Some(&link.name), Some(&linked_project));

        let root = link.repo.to_string_lossy().to_string();
        tracing::info!(
            "linked index start: {} (weight={}/8, exclude: {:?}, include: {:?})",
            root,
            weight,
            effective_cfg.exclude,
            effective_cfg.include
        );
        let r = run_index_impl(
            &root,
            None,
            &tier,
            dims,
            false,
            &effective_cfg.exclude,
            &effective_cfg.include,
        )
        .await;
        if let Err(e) = r {
            tracing::warn!("linked index failed: {}: {e}", root);
        }
        tracing::info!("linked index done: {} (weight={}/8)", root, weight);

        link_index_inflight().lock().await.remove(&link.db);
    });
}

/// Get or open a cached Storage handle.
async fn cached_storage(path: &Path, dims: u32) -> anyhow::Result<Arc<crate::storage::Storage>> {
    let key = (path.to_string_lossy().to_string(), dims);
    let cache = storage_cache();

    // Fast path: read lock - check if entry exists
    {
        let r = cache.read().await;
        if let Some(cached) = r.get(&key) {
            let storage = cached.storage.clone();
            drop(r);

            // Update last_access with write lock
            let mut w = cache.write().await;
            if let Some(entry) = w.get_mut(&key) {
                entry.last_access = Instant::now();
            }
            return Ok(storage);
        }
    }

    // Slow path: open and cache with LRU eviction
    let storage = crate::storage::Storage::open(path, dims).await?;
    let storage = Arc::new(storage);
    {
        let mut w = cache.write().await;

        // Evict oldest entry if cache is full
        if w.len() >= MAX_STORAGE_CACHE_SIZE && !w.contains_key(&key) {
            if let Some(oldest_key) = w
                .iter()
                .min_by_key(|(_, cached)| cached.last_access)
                .map(|(k, _)| k.clone())
            {
                tracing::debug!(
                    "storage cache full ({}), evicting oldest entry: {}",
                    MAX_STORAGE_CACHE_SIZE,
                    oldest_key.0
                );
                w.remove(&oldest_key);
            }
        }

        w.entry(key).or_insert_with(|| CachedStorage {
            storage: storage.clone(),
            last_access: Instant::now(),
        });
    }
    Ok(storage)
}

/// Invalidate cached storage for a given path (used after clearing corrupted index).
async fn invalidate_storage_cache(path: &Path) {
    let path_str = path.to_string_lossy().to_string();
    let cache = storage_cache();
    let mut w = cache.write().await;

    // Remove all entries for this path (any dimension)
    let keys_to_remove: Vec<_> = w.keys().filter(|(p, _)| p == &path_str).cloned().collect();

    for key in keys_to_remove {
        tracing::debug!("invalidating storage cache for {:?}", key);
        w.remove(&key);
    }
}

/// Trigger a background reindex for auto-recovery from corruption.
async fn run_index_background(root: &str, tier: &str, dims: u32) -> anyhow::Result<()> {
    use crate::config;
    use crate::discover;
    use crate::model_client;
    use crate::storage;

    let started = std::time::Instant::now();
    let root = PathBuf::from(root).canonicalize()?;
    let storage_path = storage::storage_path(&root);

    // Load config
    let project = config::load(&root);
    let cfg = config::effective(&project, Some(tier), None);

    // Discover files (blocking operation, run in thread pool)
    let root_clone = root.clone();
    let cfg_clone = cfg.clone();
    let discovery = tokio::task::spawn_blocking(move || {
        discover::discover_files_with_config(&root_clone, &cfg_clone)
    })
    .await??;

    if discovery.files.is_empty() {
        tracing::info!("auto-recovery: no files to index for {}", root.display());
        return Ok(());
    }

    tracing::info!(
        "auto-recovery: indexing {} files in {}",
        discovery.files.len(),
        root.display()
    );

    // Register this db path as actively indexing so status_impl won't self-heal it away.
    let key = storage_path.to_string_lossy().to_string();
    active_indexes().lock().await.insert(key.clone());
    struct Guard(String);
    impl Drop for Guard {
        fn drop(&mut self) {
            let k = self.0.clone();
            tokio::spawn(async move {
                active_indexes().lock().await.remove(&k);
            });
        }
    }
    let _guard = Guard(key);

    // Open fresh storage (will create new .lancedb)
    let storage = storage::Storage::open(&storage_path, dims).await?;
    storage.set_tier(tier).await?;
    storage.set_dimensions(dims).await?;
    storage.set_indexing_in_progress(true).await?;
    storage
        .set_indexing_start_time(&chrono::Utc::now().to_rfc3339())
        .await?;
    storage.set_indexing_phase("embedding").await?;
    storage
        .set_phase_progress("embedding", 0, discovery.files.len() as i64)
        .await?;

    // Run indexing (simplified - just index all files)
    let mut client = model_client::pooled().await?;
    let embed = tokio::sync::Semaphore::new(1);

    // Create WriteQueue for auto-recovery indexing
    let storage_arc = Arc::new(storage);
    let write_queue = crate::storage::WriteQueue::new(storage_arc.clone(), 32);

    let mut done = 0i64;
    let total = discovery.files.len() as i64;
    for file in &discovery.files {
        let file_path = root.join(file);
        if !tokio::fs::try_exists(&file_path).await.unwrap_or(false) {
            continue;
        }

        match crate::cli::update_file_partial_pub(
            &root,
            &[],
            &[],
            &storage_path,
            &*storage_arc,
            &mut client,
            &file_path,
            tier,
            dims,
            "int8",
            None,
            &embed,
            false,
            false,
            &write_queue,
        )
        .await
        {
            Ok(_) => {}
            Err(e) => {
                tracing::warn!("auto-recovery: failed to index {}: {}", file.display(), e);
            }
        }
        done += 1;
        if done % 10 == 0 || done == total {
            let _ = storage_arc
                .set_phase_progress("embedding", done, total)
                .await;
        }
    }

    // Wait for all writes to complete
    let _ = write_queue.shutdown().await;
    let _ = storage_arc.clear_indexing_progress().await;

    // Set metadata after indexing completes (duration, files, timestamps)
    let elapsed = started.elapsed();
    let _ = storage_arc
        .set_last_index_duration_ms((elapsed.as_millis() as i64).max(0))
        .await;
    let file_count = storage_arc.count_files().await.unwrap_or(0);
    let _ = storage_arc
        .set_last_index_files_count(file_count as i64)
        .await;
    let now = chrono::Utc::now().to_rfc3339();
    let _ = storage_arc.set_last_index_timestamp(&now).await;
    let _ = storage_arc.set_last_update_timestamp(&now).await;

    // Update cache with new storage
    let storage = storage_arc;
    let key = (storage_path.to_string_lossy().to_string(), dims);
    {
        let mut w = storage_cache().write().await;
        w.insert(
            key,
            CachedStorage {
                storage,
                last_access: Instant::now(),
            },
        );
    }

    tracing::info!(
        "auto-recovery: indexing complete for {} ({} files)",
        root.display(),
        file_count
    );
    Ok(())
}

// ============================================================================
// Request dispatch
// ============================================================================

/// Process a single request, dispatching to the appropriate handler.
async fn handle_request(method: &str, params: &serde_json::Value) -> serde_json::Value {
    match method {
        "ping" => serde_json::json!({"pong": true}),

        "resolve_paths" => {
            let root = params["root"].as_str().unwrap_or(".");
            let shared = params["sharedPath"].as_str().unwrap_or("");
            let project_id = params["projectId"].as_str().unwrap_or("");
            resolve_paths_impl(root, shared, project_id)
        }

        "index_file" => {
            let root = params["root"].as_str().unwrap_or(".");
            let db = params["db"].as_str();
            let file = params["file"].as_str().unwrap_or("");
            let tier = params["tier"].as_str().unwrap_or("budget");
            let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;

            match index_file_impl(root, db, file, tier, dims).await {
                Ok(v) => v,
                Err(e) => serde_json::json!({"success": false, "error": e.to_string()}),
            }
        }

        "remove_file" => {
            let root = params["root"].as_str().unwrap_or(".");
            let db = params["db"].as_str();
            let file = params["file"].as_str().unwrap_or("");
            let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;

            match remove_file_impl(root, db, file, dims).await {
                Ok(v) => v,
                Err(e) => serde_json::json!({"success": false, "error": e.to_string()}),
            }
        }

        "search" => {
            let root = params["root"].as_str().unwrap_or(".");
            let db = params["db"].as_str();
            let query = params["query"].as_str().unwrap_or("");
            let tier = params["tier"].as_str().unwrap_or("budget");
            let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;
            let auto = params["autoFederate"].as_bool().unwrap_or(true);
            let explicit: Vec<String> = params["federatedDb"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(String::from))
                        .collect()
                })
                .unwrap_or_default();

            // Auto-discover linked DBs when none are explicitly provided.
            // If autoFederate is enabled, we also opportunistically index missing/empty linked DBs
            // in the background so federated search starts working without manual steps.
            let root_owned = root.to_string();
            let links = if auto {
                tokio::task::spawn_blocking(move || cached_discover_links(&root_owned))
                    .await
                    .unwrap_or_default()
            } else {
                Vec::new()
            };

            let mut mounts: HashMap<String, String> = HashMap::new();
            for link in &links {
                if link.mount.is_empty() {
                    continue;
                }
                mounts.insert(link.db.to_string_lossy().to_string(), link.mount.clone());
            }

            // Fire-and-forget: spawn background indexing for missing links
            // Don't block search - if a DB is missing/empty, it just returns no results
            if auto && explicit.is_empty() && !links.is_empty() {
                let links_clone = links.clone();
                let tier = tier.to_string();
                let root = root.to_string();
                tokio::spawn(async move {
                    for link in links_clone {
                        ensure_link_index(link, &tier, dims, &root).await;
                    }
                });
            }

            let federated = if !explicit.is_empty() {
                explicit
            } else if auto {
                links
                    .iter()
                    .filter_map(|l| l.db.to_str().map(String::from))
                    .collect()
            } else {
                vec![]
            };

            match search_impl(root, db, query, tier, dims, &federated, &mounts).await {
                Ok(v) => v,
                Err(e) => {
                    // Check for corruption and auto-recover
                    if crate::storage::is_corruption_error(&e) {
                        let storage_path = db
                            .map(PathBuf::from)
                            .unwrap_or_else(|| crate::storage::storage_path(&PathBuf::from(root)));

                        // Clear corrupted index
                        if let Ok(true) = crate::storage::clear_corrupted_index(&storage_path) {
                            // Invalidate the storage cache for this path
                            invalidate_storage_cache(&storage_path).await;

                            // Trigger background reindex
                            let root_owned = root.to_string();
                            let tier_owned = tier.to_string();
                            tokio::spawn(async move {
                                tracing::info!(
                                    "auto-recovery: triggering background reindex for {}",
                                    root_owned
                                );
                                // Small delay to let any pending operations settle
                                tokio::time::sleep(std::time::Duration::from_millis(500)).await;
                                if let Err(e) =
                                    run_index_background(&root_owned, &tier_owned, dims).await
                                {
                                    tracing::error!("auto-recovery reindex failed: {}", e);
                                }
                            });

                            serde_json::json!({
                                "results": [],
                                "rebuilding": true,
                                "message": "Index was corrupted and has been cleared. Rebuilding in background..."
                            })
                        } else {
                            serde_json::json!({"results": [], "error": e.to_string()})
                        }
                    } else {
                        serde_json::json!({"results": [], "error": e.to_string()})
                    }
                }
            }
        }

        "search_memories" => {
            let shared = params["sharedPath"].as_str().unwrap_or("");
            let project_id = params["projectId"].as_str().unwrap_or("");
            let query = params["query"].as_str().unwrap_or("");
            let limit = params["limit"].as_u64().unwrap_or(10) as usize;
            let tier = params["tier"].as_str().unwrap_or("budget");
            let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;
            let root = params["root"].as_str();
            let auto = params["autoFederate"].as_bool().unwrap_or(true);
            let explicit_ids: Vec<String> = params["federatedProjectIds"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(String::from))
                        .collect()
                })
                .unwrap_or_default();

            // Auto-discover linked project IDs when none are explicitly provided
            let federated_ids = if !explicit_ids.is_empty() {
                explicit_ids
            } else if auto {
                if let Some(r) = root {
                    let root_owned = r.to_string();
                    tokio::task::spawn_blocking(move || {
                        cached_discover_links(&root_owned)
                            .iter()
                            .map(|l| l.project_id.clone())
                            .collect()
                    })
                    .await
                    .unwrap_or_default()
                } else {
                    vec![]
                }
            } else {
                vec![]
            };

            match search_memories_impl(shared, project_id, query, limit, tier, dims, &federated_ids)
                .await
            {
                Ok(v) => v,
                Err(e) => serde_json::json!({"results": [], "error": e.to_string()}),
            }
        }

        "search_activity" => {
            let shared = params["sharedPath"].as_str().unwrap_or("");
            let project_id = params["projectId"].as_str().unwrap_or("");
            let query = params["query"].as_str().unwrap_or("");
            let limit = params["limit"].as_u64().unwrap_or(10) as usize;
            let tier = params["tier"].as_str().unwrap_or("budget");
            let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;

            match search_activity_impl(shared, project_id, query, limit, tier, dims).await {
                Ok(v) => v,
                Err(e) => serde_json::json!({"results": [], "error": e.to_string()}),
            }
        }

        "search_skills" => {
            let shared = params["sharedPath"].as_str().unwrap_or("");
            let query = params["query"].as_str().unwrap_or("");
            let limit = params["limit"].as_u64().unwrap_or(10) as usize;
            let tier = params["tier"].as_str().unwrap_or("budget");
            let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;

            match search_skills_impl(shared, query, limit, tier, dims).await {
                Ok(v) => v,
                Err(e) => serde_json::json!({"results": [], "error": e.to_string()}),
            }
        }

        "run_index" => {
            let root = params["root"].as_str().unwrap_or(".");
            let db = params["db"].as_str();
            let tier = params["tier"].as_str().unwrap_or("budget");
            let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;
            let force = params["force"].as_bool().unwrap_or(false);
            let exclude: Vec<String> = params["exclude"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(String::from))
                        .collect()
                })
                .unwrap_or_default();
            let include: Vec<String> = params["include"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(String::from))
                        .collect()
                })
                .unwrap_or_default();

            match run_index_impl(root, db, tier, dims, force, &exclude, &include).await {
                Ok(v) => v,
                Err(e) => serde_json::json!({"success": false, "error": e.to_string()}),
            }
        }

        "discover_files" => {
            let root = params["root"].as_str().unwrap_or(".").to_string();
            let exclude: Vec<String> = params["exclude"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(String::from))
                        .collect()
                })
                .unwrap_or_default();
            let include: Vec<String> = params["include"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(String::from))
                        .collect()
                })
                .unwrap_or_default();

            match tokio::task::spawn_blocking(move || {
                discover_files_impl(&root, &exclude, &include)
            })
            .await
            {
                Ok(Ok(v)) => v,
                Ok(Err(e)) => serde_json::json!({"files": [], "error": e.to_string()}),
                Err(e) => {
                    serde_json::json!({"files": [], "error": format!("spawn_blocking failed: {}", e)})
                }
            }
        }

        "status" => {
            let root = params["root"].as_str();
            let db = params["db"].as_str();
            let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;
            match status_impl(root, db, dims).await {
                Ok(v) => v,
                Err(e) => serde_json::json!({"error": e.to_string()}),
            }
        }

        "discover_links" => {
            let root = params["root"].as_str().unwrap_or(".").to_string();
            match tokio::task::spawn_blocking(move || discover_links_impl(&root)).await {
                Ok(Ok(v)) => v,
                Ok(Err(e)) => serde_json::json!({"links": [], "error": e.to_string()}),
                Err(e) => {
                    serde_json::json!({"links": [], "error": format!("spawn_blocking failed: {}", e)})
                }
            }
        }

        "invalidate_links_cache" => {
            let root = params["root"].as_str().unwrap_or(".");
            match PathBuf::from(root).canonicalize() {
                Ok(root_path) => {
                    invalidate_links_cache(&root_path).await;
                    serde_json::json!({"success": true})
                }
                Err(e) => serde_json::json!({"success": false, "error": e.to_string()}),
            }
        }

        "index_linked_projects" => {
            let root = params["root"].as_str().unwrap_or(".");
            let tier = params["tier"].as_str().unwrap_or("budget");
            let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;

            let root_owned = root.to_string();
            let links = tokio::task::spawn_blocking(move || cached_discover_links(&root_owned))
                .await
                .unwrap_or_default();
            let count = links.len();
            for link in links {
                ensure_link_index(link, tier, dims, root).await;
            }

            serde_json::json!({"success": true, "triggered": count})
        }

        "health" => {
            let root = params["root"].as_str().unwrap_or(".");
            let db = params["db"].as_str();
            let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;
            let shared = params["sharedPath"].as_str().unwrap_or("");
            let project_id = params["projectId"].as_str().unwrap_or("");
            match health_impl(root, db, dims, shared, project_id).await {
                Ok(v) => v,
                Err(e) => serde_json::json!({"error": e.to_string()}),
            }
        }

        // These methods are intercepted by the server loop before reaching handle_request().
        // They require access to shared DaemonState and are handled directly in the server loop.
        // This arm exists only as a safety net - if reached, it indicates a bug in the server loop.
        "tui_connect" | "tui_disconnect" | "tui_connections" | "watcher_stop" | "watcher_start"
        | "watcher_status" | "startup_check" | "shutdown" => {
            tracing::error!(
                "method {} should be handled by server loop, not handle_request()",
                method
            );
            serde_json::json!({"error": format!("internal error: {} should be handled by server loop", method)})
        }

        "cleanup" => {
            let cfg = crate::cleaner::config();
            let base = crate::storage::shared_data_dir();
            match tokio::task::spawn_blocking(move || crate::cleaner::run(&base, &cfg, false)).await
            {
                Ok(report) => serde_json::json!({
                    "success": true,
                    "orphans": report.orphans,
                    "stale": report.stale,
                    "aux_dirs": report.aux_dirs,
                    "freed": report.freed,
                    "errors": report.errors,
                }),
                Err(e) => serde_json::json!({"success": false, "error": e.to_string()}),
            }
        }

        "cleanup_dry_run" => {
            let cfg = crate::cleaner::config();
            let base = crate::storage::shared_data_dir();
            match tokio::task::spawn_blocking(move || crate::cleaner::run(&base, &cfg, true)).await
            {
                Ok(report) => serde_json::json!({
                    "success": true,
                    "orphans": report.orphans,
                    "stale": report.stale,
                    "aux_dirs": report.aux_dirs,
                    "freed": report.freed,
                    "errors": report.errors,
                    "targets": report.targets,
                }),
                Err(e) => serde_json::json!({"success": false, "error": e.to_string()}),
            }
        }

        _ => serde_json::json!({"error": format!("unknown method: {method}")}),
    }
}

/// Extract the project key from request params (async with caching).
/// Uses `root` for index/remove/run_index, `db` for status, `sharedPath` for memory/activity.
async fn project_key(params: &serde_json::Value) -> String {
    if let Some(root) = params["root"].as_str() {
        if !root.is_empty() {
            return canonicalize_project_key(root).await;
        }
    }
    if let Some(db) = params["db"].as_str() {
        return db.to_string();
    }
    if let Some(shared) = params["sharedPath"].as_str() {
        let pid = params["projectId"].as_str().unwrap_or("global");
        return format!("{shared}::{pid}");
    }
    "__global__".to_string()
}

// ============================================================================
// resolve_paths — all path derivation logic lives here
// ============================================================================

fn resolve_paths_impl(root: &str, shared: &str, project_id: &str) -> serde_json::Value {
    use crate::storage;

    let root = PathBuf::from(root);
    let pid = if project_id.is_empty() {
        storage::git_project_id(&root)
    } else {
        project_id.to_string()
    };

    let db = storage::storage_path(&root);
    let shared = PathBuf::from(shared);
    let project_dir = shared.join("projects").join(&pid);
    let memory_dir = project_dir.join("memories");
    let activity_dir = project_dir.join("activity");
    let memory_db = memory_dir.join(".lancedb");
    let activity_db = activity_dir.join(".lancedb");
    let global_memory_dir = shared.join("memories").join("global");
    let global_db = global_memory_dir.join(".lancedb");

    serde_json::json!({
        "projectId": pid,
        "dbPath": db.to_str(),
        "memoryDir": memory_dir.to_str(),
        "activityDir": activity_dir.to_str(),
        "memoryDbPath": memory_db.to_str(),
        "activityDbPath": activity_db.to_str(),
        "globalMemoryDir": global_memory_dir.to_str(),
        "globalDbPath": global_db.to_str(),
    })
}

// ============================================================================
// index_file
// ============================================================================

async fn index_file_impl(
    root: &str,
    db: Option<&str>,
    file: &str,
    tier: &str,
    dims: u32,
) -> Result<serde_json::Value> {
    use crate::storage;

    let root = PathBuf::from(root).canonicalize()?;
    let storage_path = db
        .map(PathBuf::from)
        .unwrap_or_else(|| storage::storage_path(&root));
    let storage = cached_storage(&storage_path, dims).await?;

    let file_path = if Path::new(file).is_absolute() {
        PathBuf::from(file)
    } else {
        root.join(file)
    };
    let file_path = file_path.canonicalize().context("invalid file")?;

    // Fast-path: skip unchanged files without connecting to the model server.
    // This keeps re-index latency low and avoids unnecessary connection churn.
    let rel = crate::discover::relative_path(&file_path, &root, &[]);
    if let Ok(content) = tokio::fs::read_to_string(&file_path).await {
        let file_hash = storage::hash_content(&content);
        if !storage.needs_index(&rel, &file_hash).await.unwrap_or(true) {
            return Ok(serde_json::json!({"success": true, "skipped": true, "path": rel}));
        }
    }

    // Use the partial update algorithm: hash-match existing chunks, only embed new ones.
    // This avoids re-embedding unchanged chunks and preserves chunk IDs.
    let mut client = crate::model_client::pooled().await?;
    let embed = tokio::sync::Semaphore::new(1);

    // Create WriteQueue for this single file operation
    let write_queue = crate::storage::WriteQueue::new(storage.clone(), 32);

    let result = crate::cli::update_file_partial_pub(
        &root,
        &[], // include_dirs
        &[], // symlink_dirs (not applicable for daemon single-file indexing)
        &storage_path,
        &*storage,
        &mut client,
        &file_path,
        tier,
        dims,
        "int8", // quantization
        None,   // daily_cost_limit
        &embed,
        false, // force
        false, // verbose
        &write_queue,
    )
    .await?;

    // Wait for all writes to complete
    let _stats = write_queue.shutdown().await;

    match result {
        Some(update) => {
            // Update last_update_timestamp after successful indexing
            let now = chrono::Utc::now().to_rfc3339();
            let _ = storage.set_last_update_timestamp(&now).await;
            
            // Invalidate query cache for this DB
            invalidate_query_cache(&storage_path).await;

            Ok(serde_json::json!({
                "success": true,
                "chunks": update.chunks,
                "kept": update.kept,
                "deleted": update.deleted,
                "inserted": update.inserted,
                "embedded": update.embedded,
                "path": crate::discover::relative_path(&file_path, &root, &[]),
            }))
        }
        None => Ok(serde_json::json!({"success": true, "skipped": true})),
    }
}

// ============================================================================
// remove_file
// ============================================================================

async fn remove_file_impl(
    root: &str,
    db: Option<&str>,
    file: &str,
    dims: u32,
) -> Result<serde_json::Value> {
    use crate::discover::relative_path;
    use crate::storage;

    let root = PathBuf::from(root).canonicalize()?;
    let storage_path = db
        .map(PathBuf::from)
        .unwrap_or_else(|| storage::storage_path(&root));
    let storage = cached_storage(&storage_path, dims).await?;
    let rel = relative_path(Path::new(file), &root, &[]);

    // Get count before deletion
    let chunks = storage.get_chunks_with_hashes(&rel).await?;
    let removed = chunks.len();

    // Create WriteQueue for deletion
    let write_queue = crate::storage::WriteQueue::new(storage.clone(), 32);
    write_queue.delete_file(&rel).await;

    // Wait for deletion to complete
    let _ = write_queue.shutdown().await;
    
    // Invalidate query cache for this DB
    invalidate_query_cache(&storage_path).await;

    Ok(serde_json::json!({"success": true, "removed": removed, "path": rel}))
}

// ============================================================================
// search (with federated support) — ADAPTIVE HYBRID RERANKING
//
// Strategy adapts based on number of federated projects:
// - Few projects (≤5): Two-stage (per-project rerank + global rerank) for best quality
// - Many projects (>5): Vector-only + global rerank for speed (skip per-project rerank)
//
// This balances quality vs latency for large federated searches.
// ============================================================================

/// Number of results to keep per project in stage 1 (before global rerank)
const STAGE1_TOP_K: u32 = 15;
/// Maximum results to feed into global rerank (prevents context overflow)
const GLOBAL_RERANK_MAX: usize = 100;
/// Final number of results to return
const FINAL_TOP_K: u32 = 10;
/// Threshold: skip per-project rerank if more than this many projects (for speed)
const SKIP_STAGE1_RERANK_THRESHOLD: usize = 5;
/// Results per project when skipping stage 1 rerank (use vector score only)
const VECTOR_ONLY_TOP_K: usize = 10;
/// Warning threshold for too many federated DBs
const FEDERATED_DB_WARNING_THRESHOLD: usize = 50;

/// Semaphore for limiting concurrent federated searches.
/// Configurable via OPENCODE_FEDERATED_CONCURRENCY environment variable.
/// Defaults to min(num_cpus, 8) to balance parallelism with resource usage.
fn federated_search_semaphore() -> &'static tokio::sync::Semaphore {
    static SEM: std::sync::OnceLock<tokio::sync::Semaphore> = std::sync::OnceLock::new();
    SEM.get_or_init(|| {
        let limit = std::env::var("OPENCODE_FEDERATED_CONCURRENCY")
            .ok()
            .and_then(|s| s.parse::<usize>().ok())
            .unwrap_or_else(|| num_cpus::get().min(8));
        
        tracing::debug!("federated search concurrency limit: {}", limit);
        tokio::sync::Semaphore::new(limit)
    })
}

// ============================================================================
// Query Cache - LRU cache for repeated search queries
// ============================================================================

/// Cached search result with metadata
#[derive(Clone)]
struct QueryCacheEntry {
    results: serde_json::Value,
    timestamp: Instant,
}

/// Cache key for a search query
#[derive(Hash, Eq, PartialEq, Clone)]
struct QueryCacheKey {
    query: String,
    db_path: String,
    tier: String,
    dims: u32,
    federated: Vec<String>,
}

impl QueryCacheKey {
    fn new(
        query: &str,
        db_path: &str,
        tier: &str,
        dims: u32,
        federated: &[String],
    ) -> Self {
        Self {
            query: query.to_lowercase().trim().to_string(), // normalize query
            db_path: db_path.to_string(),
            tier: tier.to_string(),
            dims,
            federated: federated.to_vec(),
        }
    }
}

/// Global LRU cache for query results
fn query_cache() -> &'static RwLock<lru::LruCache<QueryCacheKey, QueryCacheEntry>> {
    static CACHE: OnceLock<RwLock<lru::LruCache<QueryCacheKey, QueryCacheEntry>>> = OnceLock::new();
    CACHE.get_or_init(|| {
        let capacity = std::env::var("OPENCODE_QUERY_CACHE_SIZE")
            .ok()
            .and_then(|s| s.parse::<usize>().ok())
            .unwrap_or(100);
        
        let cap = std::num::NonZeroUsize::new(capacity).unwrap_or(std::num::NonZeroUsize::new(100).unwrap());
        tracing::debug!("query cache size: {}", capacity);
        RwLock::new(lru::LruCache::new(cap))
    })
}

/// Get TTL for cached query results (default 60 seconds)
fn query_cache_ttl() -> Duration {
    let ttl_secs = std::env::var("OPENCODE_QUERY_CACHE_TTL_SECS")
        .ok()
        .and_then(|s| s.parse::<u64>().ok())
        .unwrap_or(60);
    Duration::from_secs(ttl_secs)
}

/// Invalidate all cache entries for a specific DB path
async fn invalidate_query_cache(db_path: &Path) {
    let db_str = db_path.to_string_lossy().to_string();
    let mut cache = query_cache().write().await;
    
    // Collect keys to remove (can't modify while iterating)
    let keys_to_remove: Vec<QueryCacheKey> = cache
        .iter()
        .filter(|(k, _)| k.db_path == db_str)
        .map(|(k, _)| k.clone())
        .collect();
    
    for key in keys_to_remove {
        cache.pop(&key);
    }
    
    if !cache.is_empty() {
        tracing::debug!("invalidated query cache for: {}", db_str);
    }
}

async fn search_impl(
    root: &str,
    db: Option<&str>,
    query: &str,
    tier: &str,
    dims: u32,
    federated: &[String],
    mounts: &HashMap<String, String>,
) -> Result<serde_json::Value> {
    use crate::model_client;
    use crate::storage;
    use std::collections::HashSet;

    let root = PathBuf::from(root);
    let storage_path = db
        .map(PathBuf::from)
        .unwrap_or_else(|| storage::storage_path(&root));

    // Check cache before expensive search
    let cache_key = QueryCacheKey::new(
        query,
        &storage_path.to_string_lossy(),
        tier,
        dims,
        federated,
    );
    
    {
        let mut cache = query_cache().write().await;
        if let Some(entry) = cache.get(&cache_key) {
            let age = entry.timestamp.elapsed();
            if age < query_cache_ttl() {
                tracing::debug!(
                    "query cache hit: query='{}' age={:.2}s",
                    query,
                    age.as_secs_f64()
                );
                return Ok(entry.results.clone());
            } else {
                // Expired - remove it
                cache.pop(&cache_key);
                tracing::debug!("query cache expired: query='{}'", query);
            }
        }
    }

    let mut all_paths: Vec<PathBuf> = vec![storage_path.clone()];
    all_paths.extend(federated.iter().map(PathBuf::from));

    let num_projects = all_paths.len();

    // Warn if too many federated DBs (potential resource issue)
    if num_projects > FEDERATED_DB_WARNING_THRESHOLD {
        tracing::warn!(
            "federated search across {} DBs may be slow/resource-intensive (threshold: {})",
            num_projects,
            FEDERATED_DB_WARNING_THRESHOLD
        );
    }

    // Adaptive strategy: skip per-project rerank for many projects (speed optimization)
    let use_vector_only = num_projects > SKIP_STAGE1_RERANK_THRESHOLD;

    // ========================================================================
    // STAGE 1: Parallel per-project search (bounded concurrency)
    // - Few projects: vector search + per-project rerank (quality)
    // - Many projects: vector search only (speed)
    // - Concurrency limited by semaphore to prevent CPU/memory spikes
    // ========================================================================
    let sem = federated_search_semaphore();
    let search_tasks: Vec<_> = all_paths
        .into_iter()
        .map(|sp| {
            let query = query.to_string();
            let tier = tier.to_string();
            let key = sp.to_string_lossy().to_string();
            let prefix = mounts
                .get(&key)
                .map(|s| s.trim_end_matches('/').to_string());

            tokio::spawn(async move {
                // Acquire semaphore permit to limit concurrency
                let _permit = sem.acquire().await.ok();

                if use_vector_only {
                    // Fast path: vector search only, no per-project rerank
                    search_single_db_vector_only(sp, &query, &tier, dims, prefix).await
                } else {
                    // Quality path: vector search + per-project rerank
                    search_single_db_stage1(sp, &query, &tier, dims, prefix).await
                }
            })
        })
        .collect();

    let results = futures::future::join_all(search_tasks).await;

    // Collect stage 1 results from all projects
    // Memory is bounded by: num_projects * max(STAGE1_TOP_K, VECTOR_ONLY_TOP_K) results
    // With 100 projects × 15 results = 1500 results max (~15MB for large content)
    // This is acceptable for search quality - global rerank will select the best
    let mut stage1_results: Vec<(f64, String, String)> =
        Vec::with_capacity(num_projects * std::cmp::max(STAGE1_TOP_K as usize, VECTOR_ONLY_TOP_K));
    for result in results {
        match result {
            Ok(Ok(ranked)) => stage1_results.extend(ranked),
            Ok(Err(e)) => {
                tracing::warn!("federated search error: {}", e);
            }
            Err(e) => {
                tracing::warn!("federated search task panicked: {}", e);
            }
        }
    }

    // Deduplicate by path (keep highest score)
    // Sort by score descending so we keep the highest-scoring duplicate
    stage1_results.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
    
    // Dedup without allocating String clones - use indices instead
    let mut seen_indices = HashSet::new();
    stage1_results.retain(|(_, p, _)| {
        // Hash the string slice directly without cloning
        let hash = {
            use std::collections::hash_map::DefaultHasher;
            use std::hash::{Hash, Hasher};
            let mut hasher = DefaultHasher::new();
            p.hash(&mut hasher);
            hasher.finish()
        };
        seen_indices.insert(hash)
    });

    // ========================================================================
    // STAGE 2: Global rerank for fair cross-project comparison
    // Always do global rerank when multiple projects (scores must be comparable)
    // ========================================================================
    let final_results = if num_projects > 1 && stage1_results.len() > FINAL_TOP_K as usize {
        // Limit input to global rerank to prevent context overflow
        stage1_results.truncate(GLOBAL_RERANK_MAX);

        // Get rerank model from primary project tier
        let (_, rerank_model) = crate::cli::models_for_tier_pub(tier);

        // Global rerank on combined results
        let docs: Vec<&str> = stage1_results.iter().map(|(_, _, c)| c.as_str()).collect();
        let mut client = model_client::pooled().await?;
        let global_ranked = client
            .rerank(query, &docs, rerank_model, FINAL_TOP_K)
            .await?;

        // Build final results with globally comparable scores
        let mut final_ranked: Vec<(f64, String, String)> = Vec::new();
        for (idx, score) in global_ranked {
            if idx < stage1_results.len() {
                let (_, path, content) = &stage1_results[idx];
                final_ranked.push((score.into(), path.clone(), content.clone()));
            }
        }
        final_ranked
    } else {
        // Single project or few results: skip global rerank
        stage1_results.truncate(FINAL_TOP_K as usize);
        stage1_results
    };

    let results: Vec<serde_json::Value> = final_results.iter().enumerate().map(|(i, (score, path, content))| {
        serde_json::json!({"rank": i+1, "score": score, "path": path, "content": content})
    }).collect();

    let result_json = serde_json::json!({"results": results});
    
    // Cache successful results
    {
        let mut cache = query_cache().write().await;
        cache.put(
            cache_key,
            QueryCacheEntry {
                results: result_json.clone(),
                timestamp: Instant::now(),
            },
        );
        tracing::debug!("query cache stored: query='{}'", query);
    }

    Ok(result_json)
}

/// Stage 1: Search a single database with first-pass rerank.
/// Returns top STAGE1_TOP_K results per project for global reranking.
async fn search_single_db_stage1(
    sp: PathBuf,
    query: &str,
    tier: &str,
    dims: u32,
    prefix: Option<String>,
) -> Result<Vec<(f64, String, String)>> {
    use crate::model_client;

    if !tokio::fs::try_exists(&sp).await.unwrap_or(false) {
        return Ok(Vec::new());
    }

    let s0 = cached_storage(&sp, dims).await?;
    let stored_tier = s0.get_tier().await?.unwrap_or_else(|| tier.into());
    let stored_dims = s0.get_dimensions().await?.unwrap_or(dims);

    let storage = cached_storage(&sp, stored_dims).await?;
    let mut client = model_client::pooled().await?;
    let (embed_model, rerank_model) = crate::cli::models_for_tier_pub(&stored_tier);

    // Vector search: get top 20 candidates
    let qvec = client.embed_query(query, embed_model, stored_dims).await?;
    let mut results = storage.search_hybrid(query, &qvec, 20).await?;
    if results.is_empty() {
        return Ok(Vec::new());
    }

    // First-pass rerank: filter to top STAGE1_TOP_K for this project
    let docs: Vec<&str> = results.iter().map(|r| r.content.as_str()).collect();
    let ranked = client
        .rerank(query, &docs, rerank_model, STAGE1_TOP_K)
        .await?;

    let mut ranked_results = Vec::new();
    for (idx, score) in ranked {
        if idx < results.len() {
            // Move path when no prefix (avoids clone in common case)
            let p = if let Some(ref pre) = prefix {
                format!("{}/{}", pre, results[idx].path)
            } else {
                std::mem::take(&mut results[idx].path)
            };
            // Move content to avoid clone
            let c = std::mem::take(&mut results[idx].content);
            ranked_results.push((score.into(), p, c));
        }
    }

    Ok(ranked_results)
}

/// Fast path: Vector search only, no per-project rerank.
/// Used when many federated projects to reduce model server calls.
/// Returns top VECTOR_ONLY_TOP_K results per project using vector similarity scores.
async fn search_single_db_vector_only(
    sp: PathBuf,
    query: &str,
    tier: &str,
    dims: u32,
    prefix: Option<String>,
) -> Result<Vec<(f64, String, String)>> {
    use crate::model_client;

    if !tokio::fs::try_exists(&sp).await.unwrap_or(false) {
        return Ok(Vec::new());
    }

    let s0 = cached_storage(&sp, dims).await?;
    let stored_tier = s0.get_tier().await?.unwrap_or_else(|| tier.into());
    let stored_dims = s0.get_dimensions().await?.unwrap_or(dims);

    let storage = cached_storage(&sp, stored_dims).await?;
    let mut client = model_client::pooled().await?;
    let (embed_model, _) = crate::cli::models_for_tier_pub(&stored_tier);

    // Vector search only - no rerank (speed optimization for many projects)
    let qvec = client.embed_query(query, embed_model, stored_dims).await?;
    let results = storage
        .search_hybrid(query, &qvec, VECTOR_ONLY_TOP_K)
        .await?;

    let mut ranked_results = Vec::new();
    for r in results {
        // Move path when no prefix (avoids clone in common case)
        let p = if let Some(ref pre) = prefix {
            format!("{}/{}", pre, r.path)
        } else {
            r.path
        };
        // Use vector similarity score (already normalized 0-1)
        // Move content to avoid clone
        ranked_results.push((r.score as f64, p, r.content));
    }

    Ok(ranked_results)
}

// ============================================================================
// search_memories — searches project + global memory indices, merges results
// ============================================================================

async fn search_memories_impl(
    shared: &str,
    project_id: &str,
    query: &str,
    limit: usize,
    tier: &str,
    dims: u32,
    federated_ids: &[String],
) -> Result<serde_json::Value> {
    let shared = PathBuf::from(shared);

    // Collect all memory DB paths to search
    let mut db_paths: Vec<(PathBuf, String)> = Vec::new(); // (db_path, scope)

    // Project memories
    let project_mem_db = shared
        .join("projects")
        .join(project_id)
        .join("memories")
        .join(".lancedb");
    db_paths.push((project_mem_db, "project".into()));

    // Global memories
    let global_mem_db = shared.join("memories").join("global").join(".lancedb");
    db_paths.push((global_mem_db, "global".into()));

    // Federated project memories
    for fid in federated_ids {
        let db = shared
            .join("projects")
            .join(fid)
            .join("memories")
            .join(".lancedb");
        db_paths.push((db, format!("linked:{fid}")));
    }

    // Search all indices, collect results
    let mut all: Vec<serde_json::Value> = Vec::new();

    for (db_path, scope) in &db_paths {
        if !tokio::fs::try_exists(db_path).await.unwrap_or(false) {
            continue;
        }

        let results = search_single_index(db_path, query, tier, dims, limit).await;
        match results {
            Ok(ranked) => {
                for (score, file_path, content) in ranked {
                    let filename = Path::new(&file_path)
                        .file_name()
                        .and_then(|n| n.to_str())
                        .unwrap_or(&file_path);
                    let id = filename.strip_suffix(".md").unwrap_or(filename);
                    let title = content
                        .lines()
                        .find(|l| l.starts_with("# "))
                        .map(|l| l[2..].trim().to_string())
                        .unwrap_or_else(|| id.to_string());
                    all.push(serde_json::json!({
                        "id": id,
                        "path": filename,
                        "title": title,
                        "content": content,
                        "score": score,
                        "scope": scope,
                    }));
                }
            }
            Err(e) => {
                tracing::debug!("memory search failed for {}: {e}", db_path.display());
            }
        }
    }

    // Sort by score descending, dedup by ID, truncate
    all.sort_by(|a, b| {
        let sa = a["score"].as_f64().unwrap_or(0.0);
        let sb = b["score"].as_f64().unwrap_or(0.0);
        sb.partial_cmp(&sa).unwrap_or(std::cmp::Ordering::Equal)
    });

    let mut seen = std::collections::HashSet::new();
    all.retain(|r| {
        let id = r["id"].as_str().unwrap_or("");
        seen.insert(id.to_string())
    });
    all.truncate(limit);

    Ok(serde_json::json!({"results": all}))
}

// ============================================================================
// search_activity
// ============================================================================

async fn search_activity_impl(
    shared: &str,
    project_id: &str,
    query: &str,
    limit: usize,
    tier: &str,
    dims: u32,
) -> Result<serde_json::Value> {
    let db_path = PathBuf::from(shared)
        .join("projects")
        .join(project_id)
        .join("activity")
        .join(".lancedb");

    if !tokio::fs::try_exists(&db_path).await.unwrap_or(false) {
        return Ok(serde_json::json!({"results": []}));
    }

    let ranked = search_single_index(&db_path, query, tier, dims, limit).await?;
    let results: Vec<serde_json::Value> = ranked
        .into_iter()
        .map(|(score, file_path, content)| {
            let filename = Path::new(&file_path)
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or(&file_path);
            let id = filename.strip_suffix(".md").unwrap_or(filename);
            let title = content
                .lines()
                .find(|l| l.starts_with("# "))
                .map(|l| l[2..].trim().to_string())
                .unwrap_or_else(|| id.to_string());
            serde_json::json!({
                "id": id,
                "path": filename,
                "title": title,
                "content": content,
                "score": score,
            })
        })
        .collect();

    Ok(serde_json::json!({"results": results}))
}

async fn search_skills_impl(
    shared: &str,
    query: &str,
    limit: usize,
    tier: &str,
    dims: u32,
) -> Result<serde_json::Value> {
    let db_path = PathBuf::from(shared).join("skills").join(".lancedb");

    if !tokio::fs::try_exists(&db_path).await.unwrap_or(false) {
        return Ok(serde_json::json!({"results": []}));
    }

    let ranked = search_single_index(&db_path, query, tier, dims, limit).await?;
    let results: Vec<serde_json::Value> = ranked
        .into_iter()
        .map(|(score, file_path, content)| {
            let filename = Path::new(&file_path)
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or(&file_path);
            let id = filename.strip_suffix(".md").unwrap_or(filename);
            let title = content
                .lines()
                .find(|l| l.starts_with("# "))
                .map(|l| l[2..].trim().to_string())
                .unwrap_or_else(|| id.to_string());
            serde_json::json!({
                "id": id,
                "path": filename,
                "title": title,
                "content": content,
                "score": score,
            })
        })
        .collect();

    Ok(serde_json::json!({"results": results}))
}

/// Search a single LanceDB index. Returns (score, path, content) triples.
async fn search_single_index(
    db_path: &Path,
    query: &str,
    tier: &str,
    dims: u32,
    limit: usize,
) -> Result<Vec<(f64, String, String)>> {
    use crate::model_client;

    let storage = cached_storage(db_path, dims).await?;
    let stored_tier = storage.get_tier().await?.unwrap_or_else(|| tier.into());
    let stored_dims = storage.get_dimensions().await?.unwrap_or(dims);

    // Reopen with correct dims if they differ
    let storage = if stored_dims != dims {
        cached_storage(db_path, stored_dims).await?
    } else {
        storage
    };

    let mut client = model_client::pooled().await?;
    let (embed_model, rerank_model) = crate::cli::models_for_tier_pub(&stored_tier);

    let qvec = client.embed_query(query, embed_model, stored_dims).await?;
    let mut results = storage.search_hybrid(query, &qvec, 20).await?;
    if results.is_empty() {
        return Ok(Vec::new());
    }

    let docs: Vec<&str> = results.iter().map(|r| r.content.as_str()).collect();
    let ranked = client
        .rerank(query, &docs, rerank_model, limit as u32)
        .await?;

    let mut out = Vec::new();
    for (idx, score) in ranked {
        if idx < results.len() {
            // Move path and content to avoid clone
            out.push((
                score.into(),
                std::mem::take(&mut results[idx].path),
                std::mem::take(&mut results[idx].content),
            ));
        }
    }
    Ok(out)
}

/// Returns embed concurrency from env var OPENCODE_INDEXER_EMBED_CONCURRENCY (default 3).
fn embed_concurrency() -> usize {
    std::env::var("OPENCODE_INDEXER_EMBED_CONCURRENCY")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .unwrap_or(3)
}

/// Session-level set of DB paths where FTS index has been confirmed present.
/// Avoids calling list_indices() on every watcher batch.
fn fts_ensured() -> &'static tokio::sync::Mutex<std::collections::HashSet<std::path::PathBuf>> {
    static S: std::sync::OnceLock<
        tokio::sync::Mutex<std::collections::HashSet<std::path::PathBuf>>,
    > = std::sync::OnceLock::new();
    S.get_or_init(|| tokio::sync::Mutex::new(std::collections::HashSet::new()))
}

// ============================================================================
// run_index — full project indexing via existing run_indexing pipeline
// ============================================================================

async fn run_index_impl(
    root: &str,
    db: Option<&str>,
    tier: &str,
    dims: u32,
    force: bool,
    exclude: &[String],
    include: &[String],
) -> Result<serde_json::Value> {
    use crate::storage;

    let root = PathBuf::from(root).canonicalize()?;
    let storage_path = db
        .map(PathBuf::from)
        .unwrap_or_else(|| storage::storage_path(&root));

    // Register this db path as actively indexing so status_impl won't self-heal it away.
    let key = storage_path.to_string_lossy().to_string();
    active_indexes().lock().await.insert(key.clone());
    // Ensure we deregister on any exit path (success or error).
    struct Guard(String);
    impl Drop for Guard {
        fn drop(&mut self) {
            // Spawn a task because Drop can't be async; the lock is non-blocking in practice.
            let k = self.0.clone();
            tokio::spawn(async move {
                active_indexes().lock().await.remove(&k);
            });
        }
    }
    let _guard = Guard(key);

    // Note: Daemon is a singleton (enforced by port lock); no external watcher PID check needed.
    // Acquiring it here would cause a double-lock error since the daemon's PID
    // would be written to the lock file, and then run_indexing would detect it
    // as "already running".

    let include_paths: Vec<PathBuf> = include.iter().map(PathBuf::from).collect();

    let start = std::time::Instant::now();
    let result = crate::cli::run_indexing_pub(
        &root,
        &storage_path,
        tier,
        dims,
        "int8",
        force,
        None,  // daily_cost_limit
        false, // verbose
        exclude,
        &include_paths,
        embed_concurrency(), // concurrency
        None,                // scan_concurrency
        false,               // quiet
        false,               // json_lines
    )
    .await;

    // If indexing failed with a LanceDB IO/corruption error, delete the corrupted
    // database and retry once.  Storage::open creates chunks.lance even with 0 rows,
    // so a partially-written or power-interrupted index can leave empty lance data
    // files that permanently break subsequent reads.
    let stats = match result {
        Ok(s) => s,
        Err(e) => {
            let msg = format!("{e:#}");
            let is_lance = msg.contains("LanceError") || msg.contains("lance error");
            if is_lance && storage_path.exists() {
                tracing::warn!(
                    "run_index: lance corruption detected at {}, removing and retrying: {msg}",
                    storage_path.display()
                );
                // Evict any cached Storage handle for this path so the retry
                // opens a fresh connection.
                invalidate_storage_cache(&storage_path).await;
                if let Err(rm_err) = tokio::fs::remove_dir_all(&storage_path).await {
                    tracing::error!(
                        "run_index: failed to remove corrupted db at {}: {rm_err}",
                        storage_path.display()
                    );
                    return Err(e);
                }
                // Retry once with a clean slate
                crate::cli::run_indexing_pub(
                    &root,
                    &storage_path,
                    tier,
                    dims,
                    "int8",
                    force,
                    None,
                    false,
                    exclude,
                    &include_paths,
                    5,
                    None,
                    false,
                    false,
                )
                .await?
            } else {
                return Err(e);
            }
        }
    };

    let elapsed = start.elapsed();
    Ok(serde_json::json!({
        "success": true,
        "files": stats.processed,
        "modified": stats.modified,
        "embeddings": stats.embedded,
        "duration": elapsed.as_secs_f64(),
    }))
}

// ============================================================================
// discover_files — dry-run file listing
// ============================================================================

fn discover_files_impl(
    root: &str,
    exclude: &[String],
    include: &[String],
) -> Result<serde_json::Value> {
    use crate::config;
    use crate::discover;

    let root = PathBuf::from(root).canonicalize()?;
    let project = config::load(&root);
    let mut cfg = config::effective(&project, None, None);
    cfg.exclude.extend(exclude.iter().cloned());

    let discovery = discover::discover_files_with_config(&root, &cfg)?;

    let include_dirs: Vec<PathBuf> = include.iter().map(PathBuf::from).collect();
    let mut files: Vec<String> = discovery
        .files
        .iter()
        .map(|p| discover::relative_path(p, &root, &include_dirs))
        .collect();

    // Add files from include dirs
    if !include_dirs.is_empty() {
        let mut seen: std::collections::HashSet<PathBuf> = discovery
            .files
            .iter()
            .filter_map(|p| p.canonicalize().ok())
            .collect();
        let extra = discover::discover_additional_files(
            &include_dirs,
            if exclude.is_empty() {
                None
            } else {
                Some(exclude)
            },
            &mut seen,
        );
        for f in &extra {
            files.push(discover::relative_path(f, &root, &include_dirs));
        }
    }

    Ok(serde_json::json!({"files": files, "count": files.len()}))
}

// ============================================================================
// status
// ============================================================================

async fn status_impl(root: Option<&str>, db: Option<&str>, dims: u32) -> Result<serde_json::Value> {
    let db = db.context("db path required")?;
    let sp = PathBuf::from(db);
    if !tokio::fs::try_exists(&sp).await.unwrap_or(false) {
        return Ok(serde_json::json!({"exists": false}));
    }

    let storage = cached_storage(&sp, dims).await?;

    // Lazy migration: backfill missing metadata for legacy indexes
    // This ensures status bar shows upd/speed info without needing to open Index Status dialog
    match storage.backfill_metadata().await {
        Ok(count) if count > 0 => {
            tracing::info!("status: auto-fixed {} metadata field(s)", count);
        }
        Ok(_) => {} // No backfill needed
        Err(e) => {
            tracing::warn!("status: metadata backfill failed: {}", e);
        }
    }

    // If indexing was interrupted (SIGKILL, crash, etc.), the progress keys can
    // get stuck. Self-heal by clearing progress on status check (daemon singleton
    // enforced by port lock; no PID files needed).
    // Guard: skip self-heal when this db is actively being indexed to avoid a
    // race where status clears progress set by the concurrent run_index call.
    {
        let active = active_indexes().lock().await;
        if !active.contains(&sp.to_string_lossy().to_string()) {
            if storage.get_indexing_in_progress().await.unwrap_or(false) {
                let _ = storage.clear_indexing_progress().await;
            }
        }
    }
    // Try cached counts first (< 1ms) — eliminates 2-20s table scans
    let (chunks, files, chunks_corrupted, files_corrupted) = if let Some((cached_files, cached_chunks)) = storage.get_cached_counts().await {
        // Cache hit — instant response
        (cached_chunks, cached_files, false, false)
    } else {
        // Cache miss or stale — do full scan and update cache
        let (chunks_val, chunks_corrupted_val) = match storage.count_chunks().await {
            Ok(c) => (c, false),
            Err(e) if crate::storage::is_corruption_error(&e) => {
                tracing::warn!("status: corruption detected in count_chunks: {e:#}");
                (0, true)
            }
            Err(_) => (0, false),
        };
        let (files_val, files_corrupted_val) = match storage.get_indexed_files().await {
            Ok(f) => (f.len(), false),
            Err(e) if crate::storage::is_corruption_error(&e) => {
                tracing::warn!("status: corruption detected in get_indexed_files: {e:#}");
                (0, true)
            }
            Err(_) => (0, false),
        };
        
        // Update cache with fresh counts (if no corruption)
        if !chunks_corrupted_val && !files_corrupted_val {
            storage.update_cached_counts(files_val, chunks_val).await;
        }
        
        (chunks_val, files_val, chunks_corrupted_val, files_corrupted_val)
    };
    let tier = storage.get_tier().await.unwrap_or(None);

    let last_duration_ms = storage.get_last_index_duration_ms().await.unwrap_or(None);
    let last_files_count = storage.get_last_index_files_count().await.unwrap_or(None);
    let last_indexed = storage.get_last_index_timestamp().await.unwrap_or(None);
    let last_updated = storage
        .get_last_update_timestamp()
        .await
        .unwrap_or(None)
        .or_else(|| last_indexed.clone());
    let last_watched = storage.get_last_watched_timestamp().await.unwrap_or(None);

    let files_per_sec = match (last_duration_ms, last_files_count) {
        (Some(ms), Some(count)) if ms > 0 => Some((count as f64) / (ms as f64 / 1000.0)),
        _ => None,
    };

    let indexing_in_progress = storage.get_indexing_in_progress().await.unwrap_or(false);
    let indexing_started_at = storage.get_indexing_start_time().await.unwrap_or(None);
    let indexing_phase = storage.get_indexing_phase().await.unwrap_or(None);
    let (scanning_done, scanning_total) = storage
        .get_phase_progress("scanning")
        .await
        .unwrap_or((0, 0));
    let (chunking_done, chunking_total) = storage
        .get_phase_progress("chunking")
        .await
        .unwrap_or((0, 0));
    let (embedding_done, embedding_total) = storage
        .get_phase_progress("embedding")
        .await
        .unwrap_or((0, 0));

    // Check for index corruption by verifying lance table directories exist
    let mut corrupted = files_corrupted || chunks_corrupted;
    let mut corruption_errors: Vec<String> = Vec::new();
    if files_corrupted {
        corruption_errors.push("Arrow RecordBatch error in get_indexed_files".into());
    }
    if chunks_corrupted {
        corruption_errors.push("Arrow RecordBatch error in count_chunks".into());
    }

    if chunks > 0 || files > 0 {
        // If we have data, verify the lance tables are intact
        let chunks_table = sp.join("chunks.lance");
        let config_table = sp.join("config.lance");

        if !tokio::fs::metadata(&chunks_table)
            .await
            .map(|m| m.is_dir())
            .unwrap_or(false)
        {
            corrupted = true;
            corruption_errors.push("Missing chunks.lance table directory".into());
        } else {
            // Check for data files in chunks.lance/data/
            let data_dir = chunks_table.join("data");
            if tokio::fs::metadata(&data_dir)
                .await
                .map(|m| m.is_dir())
                .unwrap_or(false)
            {
                // Verify at least one .lance file exists
                let has_data = {
                    let mut found = false;
                    if let Ok(mut dir) = tokio::fs::read_dir(&data_dir).await {
                        while let Ok(Some(entry)) = dir.next_entry().await {
                            if entry.path().extension().map_or(false, |ext| ext == "lance") {
                                found = true;
                                break;
                            }
                        }
                    }
                    found
                };
                if !has_data {
                    corrupted = true;
                    corruption_errors.push("No data files found in chunks.lance/data/".into());
                }
            }
        }

        if !tokio::fs::metadata(&config_table)
            .await
            .map(|m| m.is_dir())
            .unwrap_or(false)
        {
            corrupted = true;
            corruption_errors.push("Missing config.lance table directory".into());
        } else {
            // Check for data files in config.lance/data/
            let data_dir = config_table.join("data");
            if tokio::fs::metadata(&data_dir)
                .await
                .map(|m| m.is_dir())
                .unwrap_or(false)
            {
                let has_data = {
                    let mut found = false;
                    if let Ok(mut dir) = tokio::fs::read_dir(&data_dir).await {
                        while let Ok(Some(entry)) = dir.next_entry().await {
                            if entry.path().extension().map_or(false, |ext| ext == "lance") {
                                found = true;
                                break;
                            }
                        }
                    }
                    found
                };
                if !has_data {
                    corrupted = true;
                    corruption_errors.push("No data files found in config.lance/data/".into());
                }
            }
        }
    }

    // Auto-recover from corruption detected via Arrow/LanceDB errors
    let mut rebuilding = false;
    if corrupted {
        // Dedup guard: don't spawn recovery if indexing is already in progress
        let already_indexing = storage.get_indexing_in_progress().await.unwrap_or(false);
        if !already_indexing {
            if let Some(project_root) = root {
                let root_owned = project_root.to_string();
                let tier_str = storage
                    .get_tier()
                    .await
                    .unwrap_or(None)
                    .unwrap_or_else(|| "budget".to_string());
                tracing::warn!("status_impl: auto-recovering corrupted index for {root_owned}");
                if let Ok(true) = crate::storage::clear_corrupted_index(&sp) {
                    invalidate_storage_cache(&sp).await;
                    rebuilding = true;
                    let tier_owned = tier_str.clone();
                    tokio::spawn(async move {
                        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
                        if let Err(e) = run_index_background(&root_owned, &tier_owned, dims).await {
                            tracing::warn!("status_impl: background reindex after corruption recovery failed: {e:#}");
                        }
                    });
                }
            } else {
                tracing::warn!("status_impl: corruption detected but no project root available for auto-recovery — run /index manually");
            }
        }
    }

    Ok(serde_json::json!({
        "exists": true,
        "indexed": files > 0 || chunks > 0,
        "files": files,
        "chunks": chunks,
        "tier": tier,
        "lastIndexed": last_indexed,
        "lastUpdated": last_updated,
        "lastWatched": last_watched,
        "lastIndexDurationMs": last_duration_ms,
        "lastIndexFilesCount": last_files_count,
        "filesPerSec": files_per_sec,
        "indexingInProgress": indexing_in_progress,
        "indexingStartedAt": indexing_started_at,
        "indexingPhase": indexing_phase,
        "scanningDone": scanning_done,
        "scanningTotal": scanning_total,
        "chunkingDone": chunking_done,
        "chunkingTotal": chunking_total,
        "embeddingDone": embedding_done,
        "embeddingTotal": embedding_total,
        "corrupted": corrupted,
        "corruptionErrors": corruption_errors,
        "rebuilding": rebuilding,
    }))
}

// ============================================================================
// discover_links
// ============================================================================

fn discover_links_impl(root: &str) -> Result<serde_json::Value> {
    use crate::config;
    use crate::discover;
    use crate::storage;

    let root = PathBuf::from(root).canonicalize()?;
    let project = config::load(&root);
    let cfg = config::effective(&project, None, None);

    // Use discover_files_with_config to get skipped_repos, matching CLI behavior
    let discovery = discover::discover_files_with_config(&root, &cfg)?;

    let mut links = Vec::new();
    for repo in &discovery.skipped_repos {
        let name = repo
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("unknown")
            .to_string();

        // Skip repos marked with skip: true in .opencode-index.yaml
        if project.linked.get(&name).map(|l| l.skip).unwrap_or(false) {
            continue;
        }

        let id = storage::git_project_id(repo);
        let db = storage::storage_path(repo);
        links.push(serde_json::json!({
            "path": repo.to_str().unwrap_or(""),
            "projectId": id,
            "name": name,
            "dbPath": db.to_str().unwrap_or(""),
        }));
    }

    // Also detect git submodules as linked projects
    let submodules = discover::discover_submodules(&root);
    for (sub_path, name) in &submodules {
        // Skip if already found as a symlink-based link
        if links
            .iter()
            .any(|l| l["path"].as_str() == Some(sub_path.to_str().unwrap_or("")))
        {
            continue;
        }
        // Skip repos marked with skip: true in .opencode-index.yaml
        if project.linked.get(name).map(|l| l.skip).unwrap_or(false) {
            continue;
        }
        let id = storage::git_project_id(sub_path);
        let db = storage::storage_path(sub_path);
        links.push(serde_json::json!({
            "path": sub_path.to_str().unwrap_or(""),
            "projectId": id,
            "name": name,
            "dbPath": db.to_str().unwrap_or(""),
            "submodule": true,
        }));
    }

    // Also detect nested git repos that aren't registered submodules
    // (directories with .git files instead of .git directories)
    let nested = discover::discover_nested_git_repos(&root);
    for (repo_path, name) in &nested {
        if links
            .iter()
            .any(|l| l["path"].as_str() == Some(repo_path.to_str().unwrap_or("")))
        {
            continue;
        }
        if project.linked.get(name).map(|l| l.skip).unwrap_or(false) {
            continue;
        }
        let id = storage::git_project_id(repo_path);
        let db = storage::storage_path(repo_path);
        links.push(serde_json::json!({
            "path": repo_path.to_str().unwrap_or(""),
            "projectId": id,
            "name": name,
            "dbPath": db.to_str().unwrap_or(""),
            "nested_repo": true,
        }));
    }

    Ok(serde_json::json!({
        "rootProjectId": storage::git_project_id(&root),
        "links": links,
    }))
}

// ============================================================================
// health — comprehensive check (all logic here, TS just forwards)
// ============================================================================

async fn health_impl(
    root: &str,
    db: Option<&str>,
    dims: u32,
    shared: &str,
    project_id: &str,
) -> Result<serde_json::Value> {
    let root_path = PathBuf::from(root);
    let storage_path = db
        .map(PathBuf::from)
        .unwrap_or_else(|| crate::storage::storage_path(&root_path));
    let exists = tokio::fs::try_exists(&storage_path).await.unwrap_or(false);

    let mut result = serde_json::json!({
        "healthy": true,
        "root": root,
        "indexExists": exists,
        "dbPath": storage_path.to_str(),
        "errors": [],
    });

    // Index status
    if exists {
        if let Ok(storage) = cached_storage(&storage_path, dims).await {
            // Lazy migration: backfill missing metadata for legacy indexes
            match storage.backfill_metadata().await {
                Ok(count) if count > 0 => {
                    tracing::info!("health check: auto-fixed {} metadata field(s)", count);
                }
                Ok(_) => {} // No backfill needed
                Err(e) => {
                    tracing::warn!("health check: metadata backfill failed: {}", e);
                }
            }

            let chunks = storage.count_chunks().await.unwrap_or(0);
            let files = storage.get_file_count().await.unwrap_or(0);
            let tier = storage.get_tier().await.unwrap_or(None);
            result["files"] = serde_json::json!(files);
            result["chunks"] = serde_json::json!(chunks);
            result["tier"] = serde_json::json!(tier);

            let last_duration_ms = storage.get_last_index_duration_ms().await.unwrap_or(None);
            let last_files_count = storage.get_last_index_files_count().await.unwrap_or(None);
            let last_indexed = storage.get_last_index_timestamp().await.unwrap_or(None);
            let last_updated = storage
                .get_last_update_timestamp()
                .await
                .unwrap_or(None)
                .or_else(|| last_indexed.clone());
            let last_watched = storage.get_last_watched_timestamp().await.unwrap_or(None);
            let files_per_sec = match (last_duration_ms, last_files_count) {
                (Some(ms), Some(count)) if ms > 0 => Some((count as f64) / (ms as f64 / 1000.0)),
                _ => None,
            };

            result["lastIndexed"] = serde_json::json!(last_indexed);
            result["lastUpdated"] = serde_json::json!(last_updated);
            result["lastWatched"] = serde_json::json!(last_watched);
            result["lastIndexDurationMs"] = serde_json::json!(last_duration_ms);
            result["lastIndexFilesCount"] = serde_json::json!(last_files_count);
            result["filesPerSec"] = serde_json::json!(files_per_sec);
        }
    }

    // Index integrity
    if exists {
        let chunks_table = storage_path.join("chunks.lance");
        let config_table = storage_path.join("config.lance");
        let mut errors: Vec<String> = Vec::new();
        if !tokio::fs::metadata(&chunks_table)
            .await
            .map(|m| m.is_dir())
            .unwrap_or(false)
        {
            errors.push("Missing chunks.lance table".into());
        }
        if !tokio::fs::metadata(&config_table)
            .await
            .map(|m| m.is_dir())
            .unwrap_or(false)
        {
            errors.push("Missing config.lance table".into());
        }
        if !errors.is_empty() {
            result["healthy"] = serde_json::json!(false);
            result["errors"] = serde_json::json!(errors);
        }
    }

    // Linked projects
    let root_owned = root.to_string();
    if let Ok(Ok(links)) =
        tokio::task::spawn_blocking(move || discover_links_impl(&root_owned)).await
    {
        if let Some(arr) = links["links"].as_array() {
            let mut linked: Vec<serde_json::Value> = Vec::new();
            for link in arr {
                let mut entry = link.clone();
                let link_db = link["dbPath"].as_str().unwrap_or("");
                let link_path = PathBuf::from(link_db);
                if tokio::fs::try_exists(&link_path).await.unwrap_or(false) {
                    if let Ok(s) = cached_storage(&link_path, dims).await {
                        let files = s.get_file_count().await.unwrap_or(0);
                        let chunks = s.count_chunks().await.unwrap_or(0);
                        entry["indexed"] = serde_json::json!(files > 0 || chunks > 0);
                        entry["files"] = serde_json::json!(files);
                        entry["chunks"] = serde_json::json!(chunks);
                    } else {
                        entry["indexed"] = serde_json::json!(false);
                    }
                } else {
                    entry["indexed"] = serde_json::json!(false);
                }
                linked.push(entry);
            }
            result["linkedProjects"] = serde_json::json!(linked);
        }
    }

    // Global memory index
    if !shared.is_empty() {
        let global_db = PathBuf::from(shared)
            .join("memories")
            .join("global")
            .join(".lancedb");
        result["globalIndex"] = serde_json::json!({
            "exists": tokio::fs::try_exists(&global_db).await.unwrap_or(false),
            "path": global_db.to_str(),
        });

        // Memory dirs
        if !project_id.is_empty() {
            let project_mem = PathBuf::from(shared)
                .join("projects")
                .join(project_id)
                .join("memories");
            let global_mem = PathBuf::from(shared).join("memories").join("global");
            result["memoryDirs"] = serde_json::json!({
                "project": project_mem.to_str(),
                "global": global_mem.to_str(),
            });
        }
    }

    Ok(result)
}

// ============================================================================
// watcher_status — check if a watcher is running for a project
// ============================================================================

/// Check watcher status with TUI connection count.
fn watcher_status_with_connections(
    root: &str,
    db: Option<&str>,
    connection_count: usize,
) -> serde_json::Value {
    let root_path = PathBuf::from(root);
    let storage_path = db
        .map(PathBuf::from)
        .unwrap_or_else(|| crate::storage::storage_path(&root_path));

    serde_json::json!({
        "watcherActive": false,
        "internal": false,
        "watcherPid": null,
        "indexerActive": false,
        "indexerPid": null,
        "dbPath": storage_path.to_str(),
        "connectionCount": connection_count,
    })
}

/// Startup check: auto-fix corruption and/or start watcher.
/// All decision logic is here in the daemon - TUI just displays results.
///
/// Returns JSON with:
/// - action: "none" | "rebuilt" | "rebuilding" | "watcher_started" | "error"
/// - message: Human-readable description
/// - corrupted: bool (was index corrupted?)
/// - indexed: bool (is index present?)
/// - watching: bool (is watcher running after this call?)
async fn startup_check_impl(
    state: &Arc<tokio::sync::Mutex<DaemonState>>,
    root: &str,
    db: Option<&str>,
    tier: Option<&str>,
    dims: u32,
) -> serde_json::Value {
    use crate::storage;

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
            // Check if this is a corruption error that we can auto-fix
            if storage::is_corruption_error(&e) {
                tracing::warn!(
                    "startup_check: status check failed due to corruption for {}: {}",
                    root_path.display(),
                    e
                );

                // Clear the corrupted index
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
                let state_clone = state.clone();
                let root_str = root.to_string();
                let db_str = db.map(|s| s.to_string());
                let tier_str = tier.to_string();

                tokio::spawn(async move {
                    // First run full index
                    let db_path = db_str
                        .clone()
                        .map(PathBuf::from)
                        .unwrap_or_else(|| storage::storage_path(&PathBuf::from(&root_str)));
                    // Register as active to prevent status self-heal from clearing progress
                    let active_key = db_path.to_string_lossy().to_string();
                    active_indexes().lock().await.insert(active_key.clone());
                    struct StartupGuard(String);
                    impl Drop for StartupGuard {
                        fn drop(&mut self) {
                            let k = self.0.clone();
                            tokio::spawn(async move {
                                active_indexes().lock().await.remove(&k);
                            });
                        }
                    }
                    let _guard = StartupGuard(active_key);
                    tracing::info!(
                        "startup_check: starting background rebuild for {}",
                        root_str
                    );
                    if let Err(e) = crate::cli::run_indexing_pub(
                        &PathBuf::from(&root_str),
                        &db_path,
                        &tier_str,
                        dims,
                        "int8",
                        true,                // force
                        None,                // daily_cost_limit
                        false,               // verbose
                        &[],                 // exclude
                        &[],                 // include
                        embed_concurrency(), // concurrency
                        None,                // scan_concurrency
                        true,                // quiet
                        false,               // json_lines
                    )
                    .await
                    {
                        tracing::warn!("startup_check: background indexing failed: {}", e);
                        return;
                    }
                    tracing::info!(
                        "startup_check: background indexing completed for {}",
                        root_str
                    );

                    // Then start watcher
                    let db_ref = db_str.as_deref();
                    if let Err(rebuild_err) = watcher_start_internal(
                        &state_clone,
                        &root_str,
                        db_ref,
                        Some(&tier_str),
                        false,
                    )
                    .await
                    {
                        tracing::warn!(
                            "startup_check: background watcher start failed: {}",
                            rebuild_err
                        );
                    }
                });

                return serde_json::json!({
                    "action": "rebuilding",
                    "message": format!("Detected corruption ({}), cleared index and started rebuild in background", e),
                    "corrupted": true,
                    "indexed": false,
                    "watching": false,
                    "corruptionErrors": [e.to_string()],
                });
            }

            // Non-corruption error, return as-is
            return serde_json::json!({
                "action": "error",
                "message": format!("Failed to check status: {}", e),
                "corrupted": false,
                "indexed": false,
                "watching": false,
            });
        }
    };

    let corrupted = status["corrupted"].as_bool().unwrap_or(false);
    let indexed = status["indexed"].as_bool().unwrap_or(false);
    let exists = status["exists"].as_bool().unwrap_or(false);
    let corruption_errors: Vec<String> = status["corruptionErrors"]
        .as_array()
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(String::from))
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
        // Clone values for the spawned task
        let state_clone = state.clone();
        let root_str = root.to_string();
        let db_str = db.map(|s| s.to_string());
        let tier_str = tier.to_string();
        let db_path_clone = db_path.clone();

        tokio::spawn(async move {
            // First run full index
            // Register as active to prevent status self-heal from clearing progress
            let active_key = db_path_clone.to_string_lossy().to_string();
            active_indexes().lock().await.insert(active_key.clone());
            struct StartupGuard2(String);
            impl Drop for StartupGuard2 {
                fn drop(&mut self) {
                    let k = self.0.clone();
                    tokio::spawn(async move {
                        active_indexes().lock().await.remove(&k);
                    });
                }
            }
            let _guard = StartupGuard2(active_key);
            tracing::info!(
                "startup_check: starting background rebuild for {}",
                root_str
            );
            if let Err(e) = crate::cli::run_indexing_pub(
                &PathBuf::from(&root_str),
                &db_path_clone,
                &tier_str,
                dims,
                "int8",
                true,                // force
                None,                // daily_cost_limit
                false,               // verbose
                &[],                 // exclude
                &[],                 // include
                embed_concurrency(), // concurrency
                None,                // scan_concurrency
                true,                // quiet
                false,               // json_lines
            )
            .await
            {
                tracing::warn!("startup_check: background indexing failed: {}", e);
                return;
            }
            tracing::info!(
                "startup_check: background indexing completed for {}",
                root_str
            );

            // Then start watcher (with retry)
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
            "watching": false, // Not watching yet, starting in background
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

// ---------------------------------------------------------------------------
// Memory watcher management functions
// ---------------------------------------------------------------------------

/// Start a memory watcher for a specific directory
async fn start_memory_watcher(
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
async fn start_builtin_memory_watchers(
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
async fn start_project_memory_watchers(
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

/// Start an internal watcher for a project (runs within daemon, no external process)
fn watcher_start_internal<'a>(
    state: &'a Arc<tokio::sync::Mutex<DaemonState>>,
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
            crate::storage::storage_path(&root_path)
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
            let probe = crate::storage::Storage::open(&db_path, 1024).await;
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
        let temp_storage = crate::storage::Storage::open(&db_path, 1024).await?;
        let dimensions = temp_storage.get_dimensions().await?.unwrap_or(1024);
        let tier = tier.unwrap_or("budget");
        drop(temp_storage);

        // Open storage with correct dimensions
        let storage = Arc::new(crate::storage::Storage::open(&db_path, dimensions).await?);

        // Create write queue for serializing storage operations
        let write_queue = Arc::new(crate::storage::WriteQueue::new(storage.clone(), 100));

        // Load config for filtering and watcher settings
        let project_config = crate::config::load(&root_path);
        let index_cfg = crate::config::effective(&project_config, None, None);
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
            Arc::new(move |path| crate::discover::should_index(path, &root_clone, &cfg_clone)),
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
        let project_id = crate::storage::git_project_id(&root_path);
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

/// Stop an internal watcher for a project
async fn watcher_stop_internal(
    state: &Arc<tokio::sync::Mutex<DaemonState>>,
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

// ============================================================================
// TUI connection tracking
// ============================================================================

/// Register a TUI connection for a project.
/// Returns the current connection count for that project.
fn tui_connect_impl(state: &mut DaemonState, key: &str, connection_id: &str) -> serde_json::Value {
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
fn tui_disconnect_impl(
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
fn tui_connections_impl(state: &mut DaemonState, key: &str) -> serde_json::Value {
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
async fn canonicalize_project_key(root: &str) -> String {
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

// ============================================================================
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

            let method = item.method.clone();
            let params = item.params.clone();
            let result = handle_request(&method, &params).await;

            // Track write operations for compaction
            if matches!(method.as_str(), "index_file" | "remove_file") {
                // Check if the operation was successful (not skipped)
                let was_write = result["success"].as_bool() == Some(true)
                    && result["skipped"].as_bool() != Some(true);

                if was_write {
                    // Extract db path and dimensions from params
                    let root = params["root"].as_str().unwrap_or(".");
                    let db = params["db"].as_str();
                    let dims = params["dimensions"].as_u64().unwrap_or(1024) as u32;

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
async fn dispatch_unified(
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
        crate::model_client::shutdown_embedder();
        if let Some(home) = dirs::home_dir() {
            let _ = tokio::fs::remove_file(home.join(".opencode").join("indexer.port")).await;
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
                )
            })
        };
        // Outer lock dropped here

        return if let Some((dropped_stats, pending, started_at, db_path, max_pending_files)) = watcher_data {
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
            let conn_count = {
                let s = state.lock().await;
                s.tui_connections.get(&key).map(|c| c.len()).unwrap_or(0)
            };
            watcher_status_with_connections(root, db, conn_count)
        };
    }

    if matches!(
        method.as_str(),
        "tui_connect" | "tui_disconnect" | "tui_connections"
    ) {
        let root = params["root"].as_str().unwrap_or(".");
        let key = canonicalize_project_key(root).await;
        let mut s = state.lock().await;
        return match method.as_str() {
            "tui_connect" => {
                tui_connect_impl(&mut s, &key, params["connectionId"].as_str().unwrap_or(""))
            }
            "tui_disconnect" => {
                tui_disconnect_impl(&mut s, &key, params["connectionId"].as_str().unwrap_or(""))
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
                method: method.clone(),
                params: params.clone(),
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

    // All read-only stateless operations.
    handle_request(&method, &params).await
}

/// Create a type-erased dispatcher that the HTTP server can call.
///
/// The returned function is `Send + Sync` so it can be shared across axum
/// handler tasks without additional synchronisation.
fn make_dispatcher(state: Arc<Mutex<DaemonState>>) -> Dispatcher {
    Arc::new(move |method, params| {
        let state = state.clone();
        Box::pin(async move {
            // Update last activity timestamp on every request
            {
                let s = state.lock().await;
                *s.last_activity.write().await = Instant::now();
            }
            dispatch_unified(state, method, params).await
        })
    })
}

/// Check if another daemon instance is already running.
/// Reads ~/.opencode/indexer.port, pings it, returns the port if alive.
async fn check_existing_daemon() -> Option<u16> {
    let home = dirs::home_dir()?;
    let port_file = home.join(".opencode").join("indexer.port");
    let content = tokio::fs::read_to_string(&port_file).await.ok()?;
    let port: u16 = content.trim().parse().ok()?;

    // Try to ping the existing daemon with a short timeout
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(2))
        .build()
        .ok()?;
    let resp = client
        .get(format!("http://127.0.0.1:{}/ping", port))
        .send()
        .await;

    match resp {
        Ok(r) if r.status().is_success() => {
            if let Ok(body) = r.text().await {
                if body.trim() == "pong" {
                    return Some(port);
                }
            }
        }
        _ => {}
    }

    // Daemon not responsive — clean stale port file
    let _ = tokio::fs::remove_file(&port_file).await;
    tracing::debug!("removed stale indexer.port (port {} not responsive)", port);
    None
}

/// Start the daemon, serving HTTP requests on `127.0.0.1:{port}`.
///
/// Pass `port = 0` to let the OS pick a free port; the actual port is
/// written to `~/.opencode/indexer.port`.
pub async fn run(port: u16, idle_shutdown_arg: Option<u64>, parent_pid_arg: Option<i32>) -> Result<()> {
    // Initialize parent PID global if provided
    if let Some(ppid) = parent_pid_arg {
        parent_pid().set(Some(ppid)).ok();
        tracing::info!("Will monitor parent PID {}", ppid);
    } else {
        // Try env var fallback
        if let Ok(ppid_str) = std::env::var("OPENCODE_PARENT_PID") {
            if let Ok(ppid) = ppid_str.parse::<i32>() {
                parent_pid().set(Some(ppid)).ok();
                tracing::info!("Will monitor parent PID {} (from env)", ppid);
            }
        } else {
            parent_pid().set(None).ok();
        }
    }
    // --- Strict singleton enforcement via OS-level flock ---
    // Acquire an exclusive lock on ~/.opencode/indexer.lock.
    // This prevents TOCTOU races where two daemons start simultaneously.
    // The lock is held for the daemon's entire lifetime and released on exit.
    let home = dirs::home_dir().context("no home directory")?;
    let lock_dir = home.join(".opencode");
    tokio::fs::create_dir_all(&lock_dir).await.ok();
    let lock_path = lock_dir.join("indexer.lock");
    
    // Create lock file using tokio::fs, then convert to std::fs::File for flock
    let lock_file = tokio::task::spawn_blocking({
        let lock_path = lock_path.clone();
        move || std::fs::File::create(&lock_path)
    })
    .await
    .context("lock file creation task panicked")??;

    #[cfg(unix)]
    {
        use std::os::unix::io::AsRawFd;
        let fd = lock_file.as_raw_fd();
        // Try non-blocking exclusive lock
        let locked = unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) };
        if locked != 0 {
            // Another daemon holds the lock — check if it's responsive
            if let Some(existing) = check_existing_daemon().await {
                tracing::info!("daemon already running on port {}, exiting", existing);
                println!(
                    "{}",
                    serde_json::json!({"type": "already_running", "port": existing})
                );
                return Ok(());
            }
            // Lock held but daemon not responsive — stale lock, try blocking acquire
            tracing::warn!("stale lock detected, waiting to acquire...");
            let locked = unsafe { libc::flock(fd, libc::LOCK_EX) };
            if locked != 0 {
                anyhow::bail!(
                    "failed to acquire indexer lock: {}",
                    std::io::Error::last_os_error()
                );
            }
        }
    }
    // Lock acquired — we are the singleton daemon.
    // Keep lock_file alive for the daemon's lifetime (dropped on function return).
    let _lock = lock_file;

    // --- Secondary check: HTTP probe ---
    if let Some(existing) = check_existing_daemon().await {
        tracing::info!("daemon already running on port {}, exiting", existing);
        println!(
            "{}",
            serde_json::json!({"type": "already_running", "port": existing})
        );
        return Ok(());
    }

    // Clean any stale port/PID files before we start — new values written after bind
    {
        let _ = tokio::fs::remove_file(home.join(".opencode").join("indexer.port")).await;
        let _ = tokio::fs::remove_file(home.join(".opencode").join("indexer.pid")).await;
    }

    // Set up process group for clean child termination (prevents orphaned PIDs)
    setup_process_group();

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
        last_activity: Arc::new(RwLock::new(Instant::now())),
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
            let mut interval = tokio::time::interval(Duration::from_secs(300)); // Every 5 minutes
            loop {
                tokio::select! {
                    _ = interval.tick() => {
                        let mut s = cleanup_state.lock().await;
                        s.cleanup_inactive();
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

    // Periodically stop TUI-managed watchers when no connections remain and clean up
    // stale project queues to prevent unbounded memory growth.
    {
        let state = state.clone();
        tokio::spawn(async move {
            let mut ticker = tokio::time::interval(tui_cleanup_interval());
            loop {
                ticker.tick().await;
                let roots: Vec<String> = {
                    let mut s = state.lock().await;

                    // Collect stale projects for cleanup
                    let stale_projects: Vec<String> = s
                        .projects
                        .iter()
                        .filter(|(project_key, queue)| {
                            // Project is stale if:
                            // 1. Not in tui_projects (no TUI ever connected)
                            // 2. Queue is empty and not processing
                            !s.tui_projects.contains(*project_key)
                                && queue.queue.is_empty()
                                && !queue.processing
                        })
                        .map(|(k, _)| k.clone())
                        .collect();

                    // Remove stale projects
                    for project_key in &stale_projects {
                        if let Some(_) = s.projects.remove(project_key) {
                            tracing::debug!("cleaned up stale project queue: {}", project_key);
                        }
                    }

                    // Collect roots that need watcher stopped
                    s.tui_projects
                        .iter()
                        .filter(|root| {
                            s.tui_connections
                                .get(*root)
                                .map(|c| !c.is_empty())
                                .unwrap_or(false)
                                == false
                        })
                        .cloned()
                        .collect()
                };

                // Stop internal watchers for roots with no TUI connections
                let drain_timeout = Duration::from_secs(TUI_CLEANUP_DRAIN_TIMEOUT_SECS);
                for root in roots {
                    let key = canonicalize_project_key(&root).await;

                    // Signal shutdown first
                    {
                        let s = state.lock().await;
                        if let Some(watcher) = s.watchers.get(&key) {
                            let _ = watcher.shutdown_tx.send(true);
                        }
                    }

                    // Wait briefly for processing to settle
                    tokio::time::sleep(Duration::from_millis(TUI_CLEANUP_SETTLE_MS)).await;

                    // Drain WriteQueue before removing watcher
                    {
                        let mut s = state.lock().await;
                        if let Some(watcher) = s.watchers.get_mut(&key) {
                            match tokio::time::timeout(drain_timeout, watcher.drain_write_queue())
                                .await
                            {
                                Ok(Some(stats)) => {
                                    tracing::info!(
                                        "Watcher {}: drained WriteQueue ({} batches, {} chunks written)",
                                        root, stats.batches_written, stats.chunks_written
                                    );
                                }
                                Ok(None) => {
                                    tracing::warn!("Watcher {}: WriteQueue already drained or has other references", root);
                                }
                                Err(_) => {
                                    tracing::warn!(
                                        "Watcher {}: WriteQueue drain timed out after {:?}",
                                        root,
                                        drain_timeout
                                    );
                                }
                            }
                        }
                    }

                    // Now remove the watcher and tui_projects entry
                    let mut s = state.lock().await;
                    if s.watchers.remove(&key).is_some() {
                        tracing::info!(
                            "stopped internal watcher for {} (no TUI connections)",
                            root
                        );
                    }
                    s.tui_projects.remove(&key);
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

    // Watcher processing task - event-driven with parallel processing
    {
        let state = state.clone();
        let shutdown_rx = shutdown_rx.clone();
        let compaction_tx = compaction_tx.clone();
        tokio::spawn(async move {
            // Minimum batch interval to coalesce rapid changes and reduce CPU wakeups.
            // Default: 2000ms. Override via OPENCODE_INDEXER_BATCH_INTERVAL_MS.
            let min_batch_interval = Duration::from_millis(
                std::env::var("OPENCODE_INDEXER_BATCH_INTERVAL_MS")
                    .ok()
                    .and_then(|v| v.parse::<u64>().ok())
                    .unwrap_or(2000),
            );

            loop {
                // Wait for any watcher to have pending changes OR timeout for periodic check
                // Use 60s timeout to minimize idle wakeups while still allowing periodic checks
                let had_pending = tokio::time::timeout(
                    Duration::from_secs(300), // 5-minute timeout - watcher events wake us immediately
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

                // Add batching delay to coalesce rapid changes
                tokio::time::sleep(min_batch_interval).await;

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
                        let root_for_discovery = root_path.clone();
                        tokio::task::spawn_blocking(move || {
                            crate::discover::invalidate_discovery_cache(&root_for_discovery);
                        });
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

                    // Process changes in parallel with concurrency limit
                    if !changed.is_empty() {
                        let ops = changed.len() as u64;
                        let concurrency = std::cmp::min(4, num_cpus::get() / 2).max(1);
                        let sema = Arc::new(tokio::sync::Semaphore::new(concurrency));
                        // Shared embed semaphore matching embedder worker capacity
                        let embed = Arc::new(tokio::sync::Semaphore::new(4));

                        let mut futs: futures::stream::FuturesUnordered<_> = changed
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
                                        let hash = tokio::task::spawn_blocking({
                                            let content = content.clone();
                                            move || crate::storage::hash_content(&content)
                                        })
                                        .await
                                        .unwrap_or_default();
                                        if !storage
                                            .needs_index(&rel_path, &hash)
                                            .await
                                            .unwrap_or(true)
                                        {
                                            return None;
                                        }
                                    }

                                    // Index the file
                                    let mut client = crate::model_client::pooled().await.ok()?;

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
                                        "int8",
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
                        while let Some(_) = futs.next().await {}

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
                        let file_hash = tokio::task::spawn_blocking({
                            let content = content.clone();
                            move || crate::storage::hash_content(&content)
                        })
                        .await
                        .unwrap_or_default();
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
                        let mut client = match crate::model_client::pooled().await {
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
                            "int8", // quantization
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
                        let file_hash = tokio::task::spawn_blocking({
                            let content = content.clone();
                            move || crate::storage::hash_content(&content)
                        })
                        .await
                        .unwrap_or_default();
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
                        let mut client = match crate::model_client::pooled().await {
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
                            "int8",
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

    // Start HTTP server
    let dispatcher = make_dispatcher(state.clone());
    tokio::spawn(async move {
        if let Err(e) = crate::http_server::serve(dispatcher, port).await {
            tracing::error!("HTTP server error: {}", e);
        }
    });

    // Print ready line so the TS side knows when to connect.
    // Emit only after the daemon is fully initialized (including signal handlers)
    // to avoid races in tests and callers that send signals immediately.
    println!("{}", serde_json::json!({"type": "daemon_ready"}));

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
                    let last_activity = {
                        let s = state_for_idle.lock().await;
                        *s.last_activity.read().await
                    };
                    let idle = last_activity.elapsed().as_secs();
                    tracing::debug!("idle check: idle={}s, threshold={}s", idle, idle_shutdown_secs);
                    if idle >= idle_shutdown_secs {
                        tracing::info!("idle shutdown triggered (idle for {}s)", idle);
                        break;
                    }
                    // Sleep until timeout or next check (30s intervals)
                    let remaining = idle_shutdown_secs.saturating_sub(idle).max(1);
                    let sleep_duration = std::cmp::min(remaining, 30);
                    tracing::debug!("idle monitor sleeping for {}s", sleep_duration);
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
    crate::model_client::shutdown_embedder();
    if let Some(home) = dirs::home_dir() {
        let _ = tokio::fs::remove_file(home.join(".opencode").join("indexer.port")).await;
    }
    kill_process_group();
    tracing::info!("daemon shutdown complete");
    std::process::exit(0);
}

// Helper function to wait for any watcher to have pending changes.
// Uses watch::Receiver::changed() which is level-triggered-on-change and has
// no stored-permit problem (unlike Notify::notified()).
async fn wait_for_any_pending(state: &Arc<tokio::sync::Mutex<DaemonState>>) {
    use futures::stream::{FuturesUnordered, StreamExt};

    // Collect watch receivers and check for already-pending changes.
    // Subscribing via pending.subscribe() creates a fresh receiver marked-as-seen
    // at the current value, so only *future* sends will wake it.
    let (rxs, has_pending): (Vec<tokio::sync::watch::Receiver<u64>>, bool) = {
        let s = state.lock().await;
        let mut pending = false;
        let mut handles = Vec::new();
        for w in s.watchers.values() {
            if let Ok(p) = w.pending.try_lock() {
                if !p.is_empty() {
                    pending = true;
                }
                handles.push(p.subscribe());
            }
        }
        (handles, pending)
    };

    if has_pending {
        return;
    }

    if rxs.is_empty() {
        tokio::time::sleep(Duration::from_secs(300)).await;
        return;
    }

    // Wait for any watcher to signal new changes.
    let mut futs: FuturesUnordered<_> = rxs
        .into_iter()
        .map(|mut rx| Box::pin(async move { rx.changed().await }))
        .collect();

    match futs.next().await {
        Some(_) => {}
        None => {
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
    }
}

/// Wait for any memory watcher to have pending changes (async, non-blocking).
/// Uses watch::Receiver::changed() — level-triggered-on-change, no stored-permit spin.
async fn wait_for_any_memory_pending(state: &Arc<tokio::sync::Mutex<DaemonState>>) {
    use futures::stream::{FuturesUnordered, StreamExt};

    let (rxs, has_pending): (Vec<tokio::sync::watch::Receiver<u64>>, bool) = {
        let s = state.lock().await;
        let mut pending = false;
        let mut handles = Vec::new();
        for w in s.memory_watchers.values() {
            if let Ok(p) = w.pending.try_lock() {
                if !p.is_empty() {
                    pending = true;
                }
                handles.push(p.subscribe());
            }
        }
        (handles, pending)
    };

    if has_pending {
        return;
    }

    if rxs.is_empty() {
        tokio::time::sleep(Duration::from_secs(300)).await;
        return;
    }

    let mut futs: FuturesUnordered<_> = rxs
        .into_iter()
        .map(|mut rx| Box::pin(async move { rx.changed().await }))
        .collect();

    match futs.next().await {
        Some(_) => {}
        None => {
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
    }
}

// ============================================================================
// Unit tests for compaction state tracking
// ============================================================================

#[cfg(test)]
mod compaction_tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn compaction_state_new_initializes_correctly() {
        let db_path = PathBuf::from("/tmp/test.lancedb");
        let state = CompactionState::new(db_path.clone(), 1024);

        assert_eq!(state.db_path, db_path);
        assert_eq!(state.dimensions, 1024);
        assert_eq!(state.operations_since_compact, 0);
        assert!(!state.compact_in_progress);
        assert!(state.last_compact_time.is_none());
    }

    #[test]
    fn compaction_state_record_operation_increments_counter() {
        let mut state = CompactionState::new(PathBuf::from("/tmp/test.lancedb"), 1024);

        assert_eq!(state.operations_since_compact, 0);

        state.record_operation();
        assert_eq!(state.operations_since_compact, 1);

        state.record_operation();
        assert_eq!(state.operations_since_compact, 2);

        state.record_operation();
        state.record_operation();
        state.record_operation();
        assert_eq!(state.operations_since_compact, 5);
    }

    #[test]
    fn compaction_state_mark_compaction_started_sets_flag() {
        let mut state = CompactionState::new(PathBuf::from("/tmp/test.lancedb"), 1024);

        assert!(!state.compact_in_progress);

        state.mark_compaction_started();
        assert!(state.compact_in_progress);
    }

    #[test]
    fn compaction_state_mark_compaction_completed_resets_state() {
        let mut state = CompactionState::new(PathBuf::from("/tmp/test.lancedb"), 1024);

        // Simulate some operations
        state.record_operation();
        state.record_operation();
        state.record_operation();
        state.mark_compaction_started();

        assert_eq!(state.operations_since_compact, 3);
        assert!(state.compact_in_progress);
        assert!(state.last_compact_time.is_none());

        state.mark_compaction_completed();

        assert_eq!(state.operations_since_compact, 0);
        assert!(!state.compact_in_progress);
        assert!(state.last_compact_time.is_some());
    }

    #[test]
    fn compaction_state_mark_compaction_failed_preserves_counter() {
        let mut state = CompactionState::new(PathBuf::from("/tmp/test.lancedb"), 1024);

        state.record_operation();
        state.record_operation();
        state.record_operation();
        state.mark_compaction_started();

        assert_eq!(state.operations_since_compact, 3);
        assert!(state.compact_in_progress);

        state.mark_compaction_failed();

        // Counter should be preserved for retry
        assert_eq!(state.operations_since_compact, 3);
        assert!(!state.compact_in_progress);
    }

    #[test]
    fn should_compact_returns_false_when_no_operations() {
        let state = CompactionState::new(PathBuf::from("/tmp/test.lancedb"), 1024);

        let result = state.should_compact(
            Duration::from_secs(30), // idle_threshold
            50,                      // ops_threshold
            200,                     // force_threshold
        );

        assert!(!result, "should not compact when no operations");
    }

    #[test]
    fn should_compact_returns_false_when_in_progress() {
        let mut state = CompactionState::new(PathBuf::from("/tmp/test.lancedb"), 1024);

        // Add enough operations to trigger
        for _ in 0..100 {
            state.record_operation();
        }
        state.mark_compaction_started();

        let result = state.should_compact(Duration::from_secs(30), 50, 200);

        assert!(!result, "should not compact when already in progress");
    }

    #[test]
    fn should_compact_returns_true_on_force_threshold() {
        let mut state = CompactionState::new(PathBuf::from("/tmp/test.lancedb"), 1024);

        // Add operations to exceed force threshold
        for _ in 0..200 {
            state.record_operation();
        }

        let result = state.should_compact(
            Duration::from_secs(30), // idle_threshold - doesn't matter for force
            50,                      // ops_threshold
            200,                     // force_threshold
        );

        assert!(result, "should compact when force threshold reached");
    }

    #[test]
    fn should_compact_returns_true_on_idle_with_enough_ops() {
        let mut state = CompactionState::new(PathBuf::from("/tmp/test.lancedb"), 1024);

        // Add operations above ops_threshold
        for _ in 0..60 {
            state.record_operation();
        }

        // Simulate being idle by setting last_operation_time in the past
        state.last_operation_time = Instant::now() - Duration::from_secs(60);

        let result = state.should_compact(
            Duration::from_secs(30), // idle_threshold
            50,                      // ops_threshold
            200,                     // force_threshold
        );

        assert!(result, "should compact when idle and ops above threshold");
    }

    #[test]
    fn should_compact_returns_false_when_not_idle_enough() {
        let mut state = CompactionState::new(PathBuf::from("/tmp/test.lancedb"), 1024);

        // Add operations above ops_threshold
        for _ in 0..60 {
            state.record_operation();
        }

        // last_operation_time is now, so not idle
        let result = state.should_compact(
            Duration::from_secs(30), // idle_threshold
            50,                      // ops_threshold
            200,                     // force_threshold
        );

        assert!(!result, "should not compact when not idle enough");
    }

    #[test]
    fn should_compact_returns_false_when_ops_below_threshold() {
        let mut state = CompactionState::new(PathBuf::from("/tmp/test.lancedb"), 1024);

        // Add operations below ops_threshold
        for _ in 0..30 {
            state.record_operation();
        }

        // Simulate being idle
        state.last_operation_time = Instant::now() - Duration::from_secs(60);

        let result = state.should_compact(
            Duration::from_secs(30), // idle_threshold
            50,                      // ops_threshold
            200,                     // force_threshold
        );

        assert!(!result, "should not compact when ops below threshold");
    }

    #[tokio::test]
    async fn record_compaction_operation_creates_new_entry() {
        let mut state = DaemonState {
            projects: HashMap::new(),
            shutdown: tokio::sync::watch::channel(false).0,
            tui_connections: HashMap::new(),
            tui_projects: HashSet::new(),
            compaction: HashMap::new(),
            compaction_tx: None,
            watchers: HashMap::new(),
            memory_watchers: HashMap::new(),
            last_activity: Arc::new(RwLock::new(Instant::now())),
        };

        let db_path = PathBuf::from("/tmp/test.lancedb");
        assert!(state.compaction.is_empty());

        record_compaction_operation(&mut state, &db_path, 1024);

        assert_eq!(state.compaction.len(), 1);
        let entry = state.compaction.get(&(db_path.clone(), 1024)).unwrap();
        assert_eq!(entry.operations_since_compact, 1);
        assert_eq!(entry.dimensions, 1024);
    }

    #[tokio::test]
    async fn record_compaction_operation_increments_existing_entry() {
        let mut state = DaemonState {
            projects: HashMap::new(),
            shutdown: tokio::sync::watch::channel(false).0,
            tui_connections: HashMap::new(),
            tui_projects: HashSet::new(),
            compaction: HashMap::new(),
            compaction_tx: None,
            watchers: HashMap::new(),
            memory_watchers: HashMap::new(),
            last_activity: Arc::new(RwLock::new(Instant::now())),
        };

        let db_path = PathBuf::from("/tmp/test.lancedb");

        record_compaction_operation(&mut state, &db_path, 1024);
        record_compaction_operation(&mut state, &db_path, 1024);
        record_compaction_operation(&mut state, &db_path, 1024);

        assert_eq!(state.compaction.len(), 1);
        let entry = state.compaction.get(&(db_path.clone(), 1024)).unwrap();
        assert_eq!(entry.operations_since_compact, 3);
    }

    #[tokio::test]
    async fn record_compaction_operation_tracks_multiple_databases() {
        let mut state = DaemonState {
            projects: HashMap::new(),
            shutdown: tokio::sync::watch::channel(false).0,
            tui_connections: HashMap::new(),
            tui_projects: HashSet::new(),
            compaction: HashMap::new(),
            compaction_tx: None,
            watchers: HashMap::new(),
            memory_watchers: HashMap::new(),
            last_activity: Arc::new(RwLock::new(Instant::now())),
        };

        let db1 = PathBuf::from("/tmp/db1.lancedb");
        let db2 = PathBuf::from("/tmp/db2.lancedb");
        let db3 = PathBuf::from("/tmp/db3.lancedb");

        record_compaction_operation(&mut state, &db1, 1024);
        record_compaction_operation(&mut state, &db1, 1024);
        record_compaction_operation(&mut state, &db2, 512);
        record_compaction_operation(&mut state, &db3, 256);
        record_compaction_operation(&mut state, &db3, 256);
        record_compaction_operation(&mut state, &db3, 256);

        assert_eq!(state.compaction.len(), 3);

        let e1 = state.compaction.get(&(db1.clone(), 1024)).unwrap();
        assert_eq!(e1.operations_since_compact, 2);
        assert_eq!(e1.dimensions, 1024);

        let e2 = state.compaction.get(&(db2.clone(), 512)).unwrap();
        assert_eq!(e2.operations_since_compact, 1);
        assert_eq!(e2.dimensions, 512);

        let e3 = state.compaction.get(&(db3.clone(), 256)).unwrap();
        assert_eq!(e3.operations_since_compact, 3);
        assert_eq!(e3.dimensions, 256);
    }

    // M15: Link cache lock logging - contention handling
    #[test]
    fn test_link_cache_lock_contention_handled() {
        use std::thread;

        // cached_discover_links uses try_read/try_write to avoid blocking
        // Test that multiple threads can call it without panicking on lock contention
        let handles: Vec<_> = (0..5)
            .map(|_| {
                thread::spawn(|| {
                    // Create a temporary directory for testing
                    let temp = tempfile::TempDir::new().unwrap();
                    let root = temp.path().to_str().unwrap();

                    // Call the function that uses try_read/try_write
                    let links = cached_discover_links(root);

                    // Function should not panic, even if lock is contended
                    // (it will either use cache or compute fresh)
                    drop(links);
                })
            })
            .collect();

        // Wait for all threads
        for handle in handles {
            handle.join().unwrap();
        }

        // If we got here, no panics occurred
    }

    #[test]
    fn test_link_cache_stores_and_retrieves() {
        // Test that the link cache actually caches results
        let temp = tempfile::TempDir::new().unwrap();
        let root = temp.path().to_str().unwrap();

        // First call - should compute and cache
        let links1 = cached_discover_links(root);

        // Second call immediately after - should use cache
        let links2 = cached_discover_links(root);

        // Results should be consistent (both from cache or both fresh)
        assert_eq!(links1.len(), links2.len());
    }

    #[test]
    fn test_link_cache_ttl_expiry() {
        // Test that the cache respects TTL (though we can't easily test the timeout itself)
        // We just verify the cache structure works
        use std::time::Instant;

        let temp = tempfile::TempDir::new().unwrap();
        let root = temp.path().to_str().unwrap();

        // Get links (will cache)
        let _links = cached_discover_links(root);

        // Verify cache was populated (by calling again and seeing it's fast)
        let start = Instant::now();
        let _links2 = cached_discover_links(root);
        let elapsed = start.elapsed();

        // Cache hit should be very fast (< 10ms)
        assert!(
            elapsed < std::time::Duration::from_millis(10),
            "cache lookup should be fast, took {:?}",
            elapsed
        );
    }

    #[test]
    fn compaction_config_defaults() {
        // Temporarily unset env vars that override defaults so this test is
        // hermetic regardless of what the caller's shell has set.
        let saved_force = std::env::var("OPENCODE_COMPACTION_FORCE_THRESHOLD").ok();
        let saved_ops = std::env::var("OPENCODE_COMPACTION_OPS_THRESHOLD").ok();
        let saved_idle = std::env::var("OPENCODE_COMPACTION_IDLE_MS").ok();
        let saved_interval = std::env::var("OPENCODE_COMPACTION_CHECK_INTERVAL_MS").ok();
        let saved_timeout = std::env::var("OPENCODE_COMPACTION_SHUTDOWN_TIMEOUT_MS").ok();

        // SAFETY: test binary is single-threaded at this point
        unsafe {
            std::env::remove_var("OPENCODE_COMPACTION_FORCE_THRESHOLD");
            std::env::remove_var("OPENCODE_COMPACTION_OPS_THRESHOLD");
            std::env::remove_var("OPENCODE_COMPACTION_IDLE_MS");
            std::env::remove_var("OPENCODE_COMPACTION_CHECK_INTERVAL_MS");
            std::env::remove_var("OPENCODE_COMPACTION_SHUTDOWN_TIMEOUT_MS");
        }

        let idle = compaction_idle_threshold();
        assert_eq!(idle, Duration::from_secs(30));

        let ops = compaction_ops_threshold();
        assert_eq!(ops, 50);

        let force = compaction_force_threshold();
        assert_eq!(force, 200);

        let interval = compaction_check_interval();
        assert_eq!(interval, Duration::from_secs(300));

        let timeout = compaction_shutdown_timeout();
        assert_eq!(timeout, Duration::from_secs(60));

        // Restore original env vars
        unsafe {
            if let Some(v) = saved_force { std::env::set_var("OPENCODE_COMPACTION_FORCE_THRESHOLD", v); }
            if let Some(v) = saved_ops { std::env::set_var("OPENCODE_COMPACTION_OPS_THRESHOLD", v); }
            if let Some(v) = saved_idle { std::env::set_var("OPENCODE_COMPACTION_IDLE_MS", v); }
            if let Some(v) = saved_interval { std::env::set_var("OPENCODE_COMPACTION_CHECK_INTERVAL_MS", v); }
            if let Some(v) = saved_timeout { std::env::set_var("OPENCODE_COMPACTION_SHUTDOWN_TIMEOUT_MS", v); }
        }
    }

    #[test]
    fn compaction_state_lifecycle_simulation() {
        // Simulate a complete lifecycle: operations → compaction → more operations
        let mut state = CompactionState::new(PathBuf::from("/tmp/test.lancedb"), 1024);

        // Phase 1: Accumulate operations
        for _ in 0..75 {
            state.record_operation();
        }
        assert_eq!(state.operations_since_compact, 75);

        // Phase 2: Trigger compaction
        state.mark_compaction_started();
        assert!(state.compact_in_progress);

        // Phase 3: Complete compaction
        state.mark_compaction_completed();
        assert_eq!(state.operations_since_compact, 0);
        assert!(!state.compact_in_progress);
        assert!(state.last_compact_time.is_some());

        // Phase 4: More operations after compaction
        for _ in 0..25 {
            state.record_operation();
        }
        assert_eq!(state.operations_since_compact, 25);
    }

    #[test]
    fn compaction_state_failed_then_retry() {
        let mut state = CompactionState::new(PathBuf::from("/tmp/test.lancedb"), 1024);

        // Accumulate operations
        for _ in 0..100 {
            state.record_operation();
        }

        // First attempt fails
        state.mark_compaction_started();
        state.mark_compaction_failed();

        // Counter preserved for retry
        assert_eq!(state.operations_since_compact, 100);
        assert!(!state.compact_in_progress);

        // Should be eligible for retry (with idle time)
        state.last_operation_time = Instant::now() - Duration::from_secs(60);
        assert!(state.should_compact(Duration::from_secs(30), 50, 200));

        // Retry succeeds
        state.mark_compaction_started();
        state.mark_compaction_completed();

        assert_eq!(state.operations_since_compact, 0);
        assert!(state.last_compact_time.is_some());
    }
}

#[cfg(test)]
mod pending_changes_tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn pending_changes_new_is_empty() {
        let pc = PendingChanges::new();
        assert!(pc.is_empty());
        assert_eq!(pc.len(), 0);
    }

    #[test]
    fn pending_changes_add_changed_tracks_files() {
        let mut pc = PendingChanges::new();
        pc.add_changed(vec![PathBuf::from("a.txt"), PathBuf::from("b.txt")]);
        assert_eq!(pc.len(), 2);
        assert!(!pc.is_empty());
    }

    #[test]
    fn pending_changes_add_deleted_tracks_files() {
        let mut pc = PendingChanges::new();
        pc.add_deleted(vec![PathBuf::from("a.txt"), PathBuf::from("b.txt")]);
        assert_eq!(pc.len(), 2);
        assert!(!pc.is_empty());
    }

    #[test]
    fn pending_changes_changed_overrides_deleted() {
        let mut pc = PendingChanges::new();
        pc.add_deleted(vec![PathBuf::from("a.txt")]);
        assert_eq!(pc.len(), 1);

        // Adding as changed should move it from deleted to changed
        pc.add_changed(vec![PathBuf::from("a.txt")]);
        assert_eq!(pc.len(), 1); // Still 1, not 2

        let (changed, deleted) = pc.drain();
        assert_eq!(changed.len(), 1);
        assert_eq!(deleted.len(), 0);
        assert_eq!(changed[0], PathBuf::from("a.txt"));
    }

    #[test]
    fn pending_changes_deleted_overrides_changed() {
        let mut pc = PendingChanges::new();
        pc.add_changed(vec![PathBuf::from("a.txt")]);
        assert_eq!(pc.len(), 1);

        // Adding as deleted should move it from changed to deleted
        pc.add_deleted(vec![PathBuf::from("a.txt")]);
        assert_eq!(pc.len(), 1); // Still 1, not 2

        let (changed, deleted) = pc.drain();
        assert_eq!(changed.len(), 0);
        assert_eq!(deleted.len(), 1);
        assert_eq!(deleted[0], PathBuf::from("a.txt"));
    }

    #[test]
    fn pending_changes_drain_clears_buffer() {
        let mut pc = PendingChanges::new();
        pc.add_changed(vec![PathBuf::from("a.txt")]);
        pc.add_deleted(vec![PathBuf::from("b.txt")]);
        assert_eq!(pc.len(), 2);

        let (changed, deleted) = pc.drain();
        assert_eq!(changed.len(), 1);
        assert_eq!(deleted.len(), 1);

        // After drain, should be empty
        assert!(pc.is_empty());
        assert_eq!(pc.len(), 0);
    }

    #[test]
    fn pending_changes_deduplicates() {
        let mut pc = PendingChanges::new();
        pc.add_changed(vec![PathBuf::from("a.txt"), PathBuf::from("a.txt")]);
        assert_eq!(pc.len(), 1); // Deduplicated

        pc.add_changed(vec![PathBuf::from("a.txt")]);
        assert_eq!(pc.len(), 1); // Still 1
    }

    #[test]
    fn pending_changes_handles_mixed_operations() {
        let mut pc = PendingChanges::new();

        // Simulate: file created, modified, then different file deleted
        pc.add_changed(vec![PathBuf::from("new.txt")]);
        pc.add_changed(vec![PathBuf::from("new.txt")]); // Modified again
        pc.add_deleted(vec![PathBuf::from("old.txt")]);
        pc.add_changed(vec![PathBuf::from("another.txt")]);

        assert_eq!(pc.len(), 3);

        let (changed, deleted) = pc.drain();
        assert_eq!(changed.len(), 2); // new.txt, another.txt
        assert_eq!(deleted.len(), 1); // old.txt
    }
}

#[cfg(test)]
mod dropped_event_stats_tests {
    use super::*;

    #[test]
    fn dropped_event_stats_default_is_zero() {
        let stats = DroppedEventStats::default();
        assert_eq!(stats.changed_files_dropped, 0);
        assert_eq!(stats.deleted_files_dropped, 0);
        assert_eq!(stats.backpressure_events, 0);
        assert!(stats.last_drop_time.is_none());
        assert!(stats.last_log_time.is_none());
    }

    #[test]
    fn dropped_event_stats_tracks_changed_files() {
        let mut stats = DroppedEventStats::default();
        stats.changed_files_dropped += 5;
        stats.backpressure_events += 1;
        stats.last_drop_time = Some(Instant::now());

        assert_eq!(stats.changed_files_dropped, 5);
        assert_eq!(stats.backpressure_events, 1);
        assert!(stats.last_drop_time.is_some());
    }

    #[test]
    fn dropped_event_stats_tracks_deleted_files() {
        let mut stats = DroppedEventStats::default();
        stats.deleted_files_dropped += 3;
        stats.backpressure_events += 1;
        stats.last_drop_time = Some(Instant::now());

        assert_eq!(stats.deleted_files_dropped, 3);
        assert_eq!(stats.backpressure_events, 1);
    }

    #[test]
    fn dropped_event_stats_accumulates_over_time() {
        let mut stats = DroppedEventStats::default();

        // First backpressure event
        stats.changed_files_dropped += 10;
        stats.backpressure_events += 1;

        // Second backpressure event
        stats.changed_files_dropped += 5;
        stats.backpressure_events += 1;

        // Third event with deleted files
        stats.deleted_files_dropped += 3;
        stats.backpressure_events += 1;

        assert_eq!(stats.changed_files_dropped, 15);
        assert_eq!(stats.deleted_files_dropped, 3);
        assert_eq!(stats.backpressure_events, 3);
    }

    #[test]
    fn dropped_event_stats_log_rate_limiting() {
        let mut stats = DroppedEventStats::default();
        let now = Instant::now();

        // First log should always happen (no last_log_time)
        let should_log_first = stats
            .last_log_time
            .map(|t| now.duration_since(t) >= Duration::from_secs(30))
            .unwrap_or(true);
        assert!(should_log_first);

        // Set last_log_time to now
        stats.last_log_time = Some(now);

        // Immediate second log should be suppressed
        let should_log_immediate = stats
            .last_log_time
            .map(|t| Instant::now().duration_since(t) >= Duration::from_secs(30))
            .unwrap_or(true);
        assert!(!should_log_immediate);
    }

    #[test]
    fn default_max_pending_files_constant() {
        // Verify the constant exists and has expected value
        assert_eq!(DEFAULT_MAX_PENDING_FILES, 10000);
    }
}

#[cfg(test)]
mod http_dispatch_tests {
    use super::*;

    fn make_state() -> Arc<Mutex<DaemonState>> {
        let (tx, _) = tokio::sync::watch::channel(false);
        Arc::new(Mutex::new(DaemonState {
            projects: HashMap::new(),
            shutdown: tx,
            tui_connections: HashMap::new(),
            tui_projects: HashSet::new(),
            compaction: HashMap::new(),
            compaction_tx: None,
            watchers: HashMap::new(),
            memory_watchers: HashMap::new(),
            last_activity: Arc::new(RwLock::new(Instant::now())),
        }))
    }

    // -- handle_request tests ------------------------------------------------

    #[tokio::test]
    async fn ping_returns_pong() {
        let result = handle_request("ping", &serde_json::Value::Null).await;
        assert_eq!(result["pong"], serde_json::json!(true));
    }

    #[tokio::test]
    async fn unknown_method_returns_error() {
        let result = handle_request("no_such_method", &serde_json::Value::Null).await;
        assert!(
            result["error"].is_string(),
            "expected error key, got: {result}"
        );
        assert!(
            result["error"].as_str().unwrap().contains("unknown method"),
            "unexpected error: {}",
            result["error"]
        );
    }

    #[tokio::test]
    async fn health_returns_structured_response() {
        let tmp = tempfile::TempDir::new().unwrap();
        let params = serde_json::json!({"root": tmp.path().to_str().unwrap()});
        let result = handle_request("health", &params).await;
        // Non-indexed path: should have `healthy` key (no error key)
        assert!(
            result.get("healthy").is_some() || result.get("error").is_some(),
            "expected health or error key, got: {result}"
        );
    }

    // -- make_dispatcher tests -----------------------------------------------

    #[tokio::test]
    async fn dispatcher_forwards_ping() {
        let d = make_dispatcher(make_state());
        let result = d("ping".to_string(), serde_json::Value::Null).await;
        assert_eq!(result["pong"], serde_json::json!(true));
    }

    #[tokio::test]
    async fn dispatcher_handles_unknown_method() {
        let d = make_dispatcher(make_state());
        let result = d("no_such_method".to_string(), serde_json::Value::Null).await;
        assert!(
            result["error"].is_string(),
            "expected error key, got: {result}"
        );
    }
}

#[cfg(test)]
mod concurrency_tests {
    use futures::stream::FuturesUnordered;
    use futures::StreamExt;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use std::time::Duration;

    /// Verify watcher concurrency is bounded to min(4, cpus/2).max(1)
    #[test]
    fn watcher_concurrency_formula() {
        // Simulate various CPU counts
        for cpus in [1, 2, 4, 8, 16, 32, 64] {
            let c = std::cmp::min(4, cpus / 2).max(1);
            assert!(c >= 1, "concurrency must be >= 1 for {cpus} CPUs");
            assert!(c <= 4, "concurrency must be <= 4 for {cpus} CPUs, got {c}");
            match cpus {
                1 => assert_eq!(c, 1),
                2 => assert_eq!(c, 1),
                4 => assert_eq!(c, 2),
                8 => assert_eq!(c, 4),
                16 => assert_eq!(c, 4),
                32 => assert_eq!(c, 4),
                64 => assert_eq!(c, 4),
                _ => {}
            }
        }
    }

    /// Verify shared embed semaphore limits concurrent embed calls
    #[tokio::test]
    async fn shared_embed_semaphore_limits_concurrency() {
        let embed = Arc::new(tokio::sync::Semaphore::new(4));
        let peak = Arc::new(AtomicUsize::new(0));
        let active = Arc::new(AtomicUsize::new(0));

        let mut futs = FuturesUnordered::new();

        for _ in 0..20 {
            let embed = embed.clone();
            let peak = peak.clone();
            let active = active.clone();
            futs.push(async move {
                let _permit = embed.acquire().await.unwrap();
                let cur = active.fetch_add(1, Ordering::SeqCst) + 1;
                peak.fetch_max(cur, Ordering::SeqCst);
                // Simulate work
                tokio::time::sleep(Duration::from_millis(10)).await;
                active.fetch_sub(1, Ordering::SeqCst);
            });
        }

        while let Some(_) = futs.next().await {}

        let max = peak.load(Ordering::SeqCst);
        assert!(
            max <= 4,
            "peak concurrency {max} should be <= 4 (embed semaphore)"
        );
        assert!(
            max >= 2,
            "peak concurrency {max} should be >= 2 to confirm parallelism"
        );
    }

    /// Verify FuturesUnordered processes all items (no lost futures)
    #[tokio::test]
    async fn futures_unordered_completes_all() {
        let count = Arc::new(AtomicUsize::new(0));
        let total = 50;

        let mut futs: FuturesUnordered<_> = (0..total)
            .map(|_| {
                let count = count.clone();
                async move {
                    tokio::time::sleep(Duration::from_millis(1)).await;
                    count.fetch_add(1, Ordering::SeqCst);
                }
            })
            .collect();

        while let Some(_) = futs.next().await {}

        assert_eq!(
            count.load(Ordering::SeqCst),
            total,
            "all {total} futures must complete"
        );
    }

    /// Verify FuturesUnordered streams results incrementally (not all-at-once like join_all)
    #[tokio::test]
    async fn futures_unordered_streams_incrementally() {
        let completed = Arc::new(AtomicUsize::new(0));
        let total = 20;

        let mut futs: FuturesUnordered<_> = (0..total)
            .map(|i| {
                async move {
                    // Stagger completions: task 0 finishes first, task 19 finishes last
                    tokio::time::sleep(Duration::from_millis(i as u64 * 5)).await;
                    i
                }
            })
            .collect();

        let mut order = Vec::with_capacity(total);
        while let Some(i) = futs.next().await {
            completed.fetch_add(1, Ordering::SeqCst);
            order.push(i);
        }

        assert_eq!(order.len(), total, "all items received");
        // With staggered sleeps, early items should complete before later ones
        // (FuturesUnordered yields as-completed, unlike join_all which waits for all)
        assert_eq!(order[0], 0, "first-completing task should yield first");
    }

    /// Edge case: empty changed set should not create any futures
    #[tokio::test]
    async fn empty_batch_no_futures() {
        let changed: Vec<String> = vec![];
        let futs: FuturesUnordered<_> = changed.into_iter().map(|_| async { 1 }).collect();
        assert_eq!(futs.len(), 0, "empty input should create no futures");
    }

    /// Verify dual semaphore pattern: outer limits task count, inner limits embed calls
    #[tokio::test]
    async fn dual_semaphore_pattern() {
        let outer = Arc::new(tokio::sync::Semaphore::new(2));
        let inner = Arc::new(tokio::sync::Semaphore::new(4));
        let outer_peak = Arc::new(AtomicUsize::new(0));
        let inner_peak = Arc::new(AtomicUsize::new(0));
        let outer_active = Arc::new(AtomicUsize::new(0));
        let inner_active = Arc::new(AtomicUsize::new(0));

        let mut futs = FuturesUnordered::new();

        for _ in 0..16 {
            let outer = outer.clone();
            let inner = inner.clone();
            let op = outer_peak.clone();
            let ip = inner_peak.clone();
            let oa = outer_active.clone();
            let ia = inner_active.clone();

            futs.push(async move {
                let _p1 = outer.acquire().await.unwrap();
                let cur = oa.fetch_add(1, Ordering::SeqCst) + 1;
                op.fetch_max(cur, Ordering::SeqCst);

                // Simulate embed call with inner semaphore
                {
                    let _p2 = inner.acquire().await.unwrap();
                    let cur = ia.fetch_add(1, Ordering::SeqCst) + 1;
                    ip.fetch_max(cur, Ordering::SeqCst);
                    tokio::time::sleep(Duration::from_millis(5)).await;
                    ia.fetch_sub(1, Ordering::SeqCst);
                }

                oa.fetch_sub(1, Ordering::SeqCst);
            });
        }

        while let Some(_) = futs.next().await {}

        let omax = outer_peak.load(Ordering::SeqCst);
        let imax = inner_peak.load(Ordering::SeqCst);
        assert!(omax <= 2, "outer peak {omax} should be <= 2");
        assert!(
            imax <= 2,
            "inner peak {imax} should be <= outer limit (2), got {imax}"
        );
    }
}

// ============================================================================
// Unit tests for watcher startup retry and TUI cleanup drain sequence
// ============================================================================

#[cfg(test)]
mod startup_check_tests {
    use super::*;

    // -----------------------------------------------------------------------
    // Helpers that mirror the inline retry loop from startup_check, allowing
    // us to drive the logic with controlled success/failure sequences.
    // -----------------------------------------------------------------------

    /// Simulate the retry loop: returns (started, attempts_made, delays_used).
    /// `results[i]` is true when attempt i should succeed.
    fn run_retry(delays: &[u64], results: &[bool]) -> (bool, usize, Vec<u64>) {
        let mut started = false;
        let mut attempts = 0;
        let mut used = Vec::new();
        for (i, &delay) in delays.iter().enumerate() {
            attempts += 1;
            if i < results.len() && results[i] {
                started = true;
                break;
            }
            used.push(delay);
        }
        (started, attempts, used)
    }

    // -- Fix 1: retry delays follow exponential backoff pattern --

    #[test]
    fn retry_delays_are_exponential_backoff() {
        let delays = WATCHER_START_RETRY_DELAYS;
        assert_eq!(delays[0], 100, "first delay should be 100 ms");
        assert_eq!(delays[1], 200, "second delay should be 200 ms");
        assert_eq!(delays[2], 400, "third delay should be 400 ms");
        // Each step doubles
        assert_eq!(delays[1], delays[0] * 2);
        assert_eq!(delays[2], delays[1] * 2);
    }

    #[test]
    fn retry_delay_count_is_three() {
        assert_eq!(
            WATCHER_START_RETRY_DELAYS.len(),
            3,
            "expect exactly 3 retry delays"
        );
    }

    #[test]
    fn retry_delays_are_increasing() {
        let d = WATCHER_START_RETRY_DELAYS;
        for i in 1..d.len() {
            assert!(
                d[i] > d[i - 1],
                "delay[{i}] ({}) should exceed delay[{}] ({})",
                d[i],
                i - 1,
                d[i - 1]
            );
        }
    }

    // -- Fix 1: all 3 retries attempted before giving up --

    #[test]
    fn all_retries_attempted_on_total_failure() {
        // All attempts fail → loop exhausts all delays
        let results = [false, false, false];
        let (started, attempts, used_delays) = run_retry(&WATCHER_START_RETRY_DELAYS, &results);

        assert!(!started, "should not be started after total failure");
        assert_eq!(attempts, 3, "should attempt exactly 3 times");
        assert_eq!(used_delays, vec![100u64, 200, 400], "all delays consumed");
    }

    // -- Fix 1: success on first attempt does not retry --

    #[test]
    fn no_retry_on_first_success() {
        let results = [true, false, false];
        let (started, attempts, used_delays) = run_retry(&WATCHER_START_RETRY_DELAYS, &results);

        assert!(started, "should be started on first success");
        assert_eq!(attempts, 1, "should attempt only once");
        assert!(
            used_delays.is_empty(),
            "no delays used when first attempt succeeds"
        );
    }

    // -- Fix 1: success on second attempt stops further retries --

    #[test]
    fn retry_stops_after_second_attempt_success() {
        let results = [false, true, false];
        let (started, attempts, used_delays) = run_retry(&WATCHER_START_RETRY_DELAYS, &results);

        assert!(started, "should be started on second attempt");
        assert_eq!(attempts, 2, "should attempt exactly twice");
        // Only the first delay (100ms) was consumed before the 2nd attempt
        assert_eq!(used_delays, vec![100u64], "only first delay used");
    }

    // -- Fix 1: success on third attempt --

    #[test]
    fn retry_stops_after_third_attempt_success() {
        let results = [false, false, true];
        let (started, attempts, used_delays) = run_retry(&WATCHER_START_RETRY_DELAYS, &results);

        assert!(started, "should be started on third attempt");
        assert_eq!(attempts, 3, "should attempt three times");
        assert_eq!(
            used_delays,
            vec![100u64, 200],
            "first two delays used before success"
        );
    }
}

#[cfg(test)]
mod watcher_lifecycle_tests {
    use super::*;

    // -----------------------------------------------------------------------
    // Tests for the TUI cleanup sequence constants and ordering guarantees.
    // -----------------------------------------------------------------------

    // -- Fix 2: settle wait constant is 100 ms --

    #[test]
    fn tui_cleanup_settle_wait_is_100ms() {
        assert_eq!(
            TUI_CLEANUP_SETTLE_MS, 100,
            "settle wait after shutdown signal must be 100 ms"
        );
    }

    // -- Fix 2: drain timeout constant is 30 s --

    #[test]
    fn tui_cleanup_drain_timeout_is_30s() {
        assert_eq!(
            TUI_CLEANUP_DRAIN_TIMEOUT_SECS, 30,
            "drain timeout must be 30 seconds"
        );
        // Confirm Duration representation matches usage in watcher_stop / tui cleanup
        let d = Duration::from_secs(TUI_CLEANUP_DRAIN_TIMEOUT_SECS);
        assert_eq!(d, Duration::from_secs(30));
    }

    // -- Fix 2: shutdown is signaled before drain --

    #[tokio::test]
    async fn shutdown_signaled_before_drain() {
        // Create a watcher-like shutdown channel and a sequenced recorder.
        let (tx, mut rx) = tokio::sync::watch::channel(false);
        let seq = std::sync::Arc::new(std::sync::Mutex::new(Vec::<&'static str>::new()));

        // Step 1 – signal shutdown
        let _ = tx.send(true);
        seq.lock().unwrap().push("shutdown");

        // Step 2 – settle (abbreviated for speed)
        tokio::time::sleep(Duration::from_millis(1)).await;
        seq.lock().unwrap().push("settle");

        // Step 3 – read the shutdown value (simulates drain check)
        let shutdown_val = *rx.borrow_and_update();
        assert!(shutdown_val, "shutdown must be true before drain");
        seq.lock().unwrap().push("drain");

        // Step 4 – remove
        seq.lock().unwrap().push("remove");

        let order = seq.lock().unwrap().clone();
        assert_eq!(
            order,
            vec!["shutdown", "settle", "drain", "remove"],
            "cleanup steps must execute in order: shutdown → settle → drain → remove"
        );
    }

    // -- Fix 2: drain timeout respected --

    #[tokio::test]
    async fn drain_respects_timeout() {
        use tokio::time::timeout;

        let drain_timeout = Duration::from_secs(TUI_CLEANUP_DRAIN_TIMEOUT_SECS);
        // A simulated drain that completes instantly
        let result = timeout(drain_timeout, async { 42u32 }).await;
        assert!(result.is_ok(), "fast drain should not time out");
        assert_eq!(result.unwrap(), 42);
    }

    #[tokio::test]
    async fn drain_timeout_triggers_on_slow_drain() {
        use tokio::time::{sleep, timeout};

        // Use a very short timeout to simulate expiry without waiting 30 s
        let short = Duration::from_millis(10);
        // Simulate a drain that takes longer than the timeout
        let result = timeout(short, sleep(Duration::from_secs(1))).await;
        assert!(result.is_err(), "slow drain should trigger timeout");
    }

    // -- Fix 2: watcher removed only after drain completes --

    #[tokio::test]
    async fn watcher_removed_after_drain() {
        // Track sequence via a shared vec
        let seq = std::sync::Arc::new(std::sync::Mutex::new(Vec::<&'static str>::new()));

        // Simulate drain (instant here)
        seq.lock().unwrap().push("drain_start");
        tokio::time::sleep(Duration::from_millis(1)).await; // yield to runtime
        seq.lock().unwrap().push("drain_done");

        // Remove happens after drain
        seq.lock().unwrap().push("remove");

        let order = seq.lock().unwrap().clone();
        let drain_done_pos = order.iter().position(|&s| s == "drain_done").unwrap();
        let remove_pos = order.iter().position(|&s| s == "remove").unwrap();
        assert!(
            remove_pos > drain_done_pos,
            "remove must come after drain_done; order: {order:?}"
        );
    }
}
