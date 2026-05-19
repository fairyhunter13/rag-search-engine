use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::Result;
use tokio::sync::Mutex;

use crate::daemon::{cached_storage, storage_cache, DaemonState};
use crate::links::env_duration_ms;

// Compaction state tracking: enables smart deferred compaction for LanceDB.
// Instead of compacting after every operation (expensive), we track operations
// and compact when idle or when a threshold is reached.
// ---------------------------------------------------------------------------

/// Compaction key type (avoids string allocation).
pub type CompactionKey = (PathBuf, u32);

/// Request to compact a database (sent to the compaction worker queue).
#[derive(Debug, Clone)]
pub struct CompactionRequest {
    /// Unique key for deduplication (db_path, dims tuple - no allocation).
    pub key: CompactionKey,
    /// Database path.
    pub db_path: PathBuf,
    /// Dimensions for Storage::open.
    pub dims: u32,
    /// Source of the request (for logging).
    pub source: &'static str,
}

/// Per-database compaction state.
#[derive(Debug, Clone)]
pub struct CompactionState {
    /// Database path (for logging/identification).
    pub db_path: PathBuf,
    /// Number of write operations since last compaction.
    pub operations_since_compact: u64,
    /// Timestamp of last write operation.
    pub last_operation_time: Instant,
    /// Timestamp of last successful compaction.
    pub last_compact_time: Option<Instant>,
    /// Whether compaction is currently in progress.
    pub compact_in_progress: bool,
    /// Dimensions for this database (needed for Storage::open).
    pub dimensions: u32,
    /// Timestamp of last activity (for TTL-based cleanup).
    pub last_activity: Instant,
}

impl CompactionState {
    pub fn new(db_path: PathBuf, dimensions: u32) -> Self {
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

    pub fn record_operation(&mut self) {
        self.operations_since_compact += 1;
        let now = Instant::now();
        self.last_operation_time = now;
        self.last_activity = now;
    }

    pub fn mark_compaction_started(&mut self) {
        self.compact_in_progress = true;
        self.last_activity = Instant::now();
    }

    pub fn mark_compaction_completed(&mut self) {
        self.compact_in_progress = false;
        self.operations_since_compact = 0;
        let now = Instant::now();
        self.last_compact_time = Some(now);
        self.last_activity = now;
    }

    pub fn mark_compaction_failed(&mut self) {
        self.compact_in_progress = false;
        self.last_activity = Instant::now();
        // Don't reset operations count on failure - retry later
    }

    /// Check if compaction should be triggered based on idle time and threshold.
    pub fn should_compact(
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
pub fn compaction_idle_threshold() -> Duration {
    env_duration_ms("OPENCODE_COMPACTION_IDLE_MS", Duration::from_secs(300))
}

pub fn compaction_ops_threshold() -> u64 {
    std::env::var("OPENCODE_COMPACTION_OPS_THRESHOLD")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(100)
}

pub fn compaction_force_threshold() -> u64 {
    std::env::var("OPENCODE_COMPACTION_FORCE_THRESHOLD")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(500)
}

pub fn compaction_check_interval() -> Duration {
    env_duration_ms(
        "OPENCODE_COMPACTION_CHECK_INTERVAL_MS",
        Duration::from_secs(600),
    )
}

fn compaction_shutdown_timeout() -> Duration {
    env_duration_ms(
        "OPENCODE_COMPACTION_SHUTDOWN_TIMEOUT_MS",
        Duration::from_secs(60),
    )
}

/// Perform compaction on a database.
/// Fix B1: call compact() directly on async runtime (no spawn_blocking self-blocking).
async fn perform_compaction(db_path: &Path, dims: u32) -> Result<()> {
    let storage = cached_storage(db_path, dims).await?;
    storage.compact().await?;
    release_all_memory_pressure().await;
    Ok(())
}

fn purge_jemalloc() {
    #[cfg(not(target_env = "msvc"))]
    {
        if let Err(e) = tikv_jemalloc_ctl::epoch::advance() {
            tracing::debug!("jemalloc epoch advance failed: {}", e);
            return;
        }
        // MALLCTL_ARENAS_ALL = u32::MAX — purge all arenas
        let purge_key = b"arena.4294967295.purge\0";
        unsafe {
            let _ = tikv_jemalloc_ctl::raw::write(purge_key, 0u64);
        }
        tracing::debug!("jemalloc: forced purge complete");
    }
}

pub async fn release_all_memory_pressure() {
    let cache = storage_cache();
    {
        let r = cache.read().await;
        for (_, entry) in r.iter() {
            let _ = entry.storage.release_memory_pressure().await;
        }
    }
    // Evict half of storage cache (LruCache handles LRU ordering natively)
    {
        let mut w = cache.write().await;
        let keep = (w.len() + 1) / 2;
        while w.len() > keep {
            w.pop_lru();
        }
    }
    purge_jemalloc();
    tracing::info!("memory pressure release complete");
}

/// Compaction worker: processes compaction requests one at a time.
/// Deduplicates requests for the same database while one is in progress.
/// Uses the same CPU budget governor as the watcher processor to stay
/// within 5% of total system cores during compaction.
pub async fn compaction_worker(
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
                        .unwrap_or(7200);
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

                // No spacing between compactions — max_blocking_threads(1) physically
                // bounds CPU to 100% of 1 core (= 4.2% of 24 cores). Compaction
                // shares the single blocking thread with file processing, so they
                // are naturally serialized. Run back-to-back when queued.
            }
        }
    }

    tracing::info!("compaction worker shutting down");
}

/// Queue a compaction request (non-blocking).
pub fn queue_compaction(
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
pub fn record_compaction_operation(state: &mut DaemonState, db_path: &Path, dims: u32) {
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
pub async fn shutdown_drain_write_queues(state: Arc<Mutex<DaemonState>>) {
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

        // Signal shutdown and extract write queue handle (drop lock before await)
        let write_queue = {
            let mut s = state.lock().await;
            if let Some(watcher) = s.watchers.get_mut(&key) {
                let _ = watcher.shutdown_tx.send(true);
                watcher.write_queue.take()
            } else {
                None
            }
        }; // lock dropped

        if let Some(wq) = write_queue {
            let remaining = DRAIN_TIMEOUT.saturating_sub(start.elapsed());
            match tokio::time::timeout(remaining, wq.shutdown_shared()).await {
                Ok(stats) => {
                    tracing::info!(
                        "Shutdown: drained LanceDB WriteQueue for {} ({} batches, {} chunks)",
                        key,
                        stats.batches_written,
                        stats.chunks_written
                    );
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

pub async fn shutdown_compaction(state: Arc<Mutex<DaemonState>>) {
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
