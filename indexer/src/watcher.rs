//! File watcher with debouncing.
//!
//! Watches project directories for changes and triggers re-indexing
//! of modified files via the Python model server.
//!
//! # Architecture
//!
//! This module uses a fully event-driven architecture:
//! 1. `notify` crate provides native filesystem notifications (inotify/FSEvents)
//! 2. Events are bridged to tokio via an async channel
//! 3. Debouncing uses `tokio::select!` — the debounce task blocks efficiently on either:
//!    - New events arriving (process immediately)
//!    - Debounce timer expiring (flush batch)
//! 4. Inter-flush rate limiting enforces a minimum gap between consecutive flushes,
//!    preventing burst processing after mass changes (e.g. `git checkout`, `npm install`)
//!
//! This achieves ~0% CPU when idle because:
//! - inotify blocks in kernel waiting for filesystem events
//! - tokio::select! blocks waiting for channel message OR timer
//!
//! # Platform Notes
//! - Linux: Uses inotify. Check `/proc/sys/fs/inotify/max_user_watches` for limits.
//! - macOS: Uses FSEvents. Works out of the box.
//! - Windows: Uses ReadDirectoryChangesW. Ensure file handles are closed.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use notify::{Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use tracing::{debug, info, warn};

const DEBOUNCE_DELAY: Duration = Duration::from_millis(1000);
const MIN_FLUSH_INTERVAL: Duration = Duration::from_secs(5);

/// Events emitted by the file watcher.
#[derive(Debug)]
pub enum WatchEvent {
    /// Files were modified/created — need re-indexing.
    Changed(Vec<PathBuf>),
    /// Files were deleted — need removal from index.
    Deleted(Vec<PathBuf>),
}

/// Walk `dir` recursively, adding non-excluded subdirectories to the watcher
/// using `RecursiveMode::NonRecursive`. This avoids exhausting inotify limits
/// by skipping well-known heavy directories like `target/`, `node_modules/`, etc.
///
/// Returns the number of directories successfully watched.
fn add_watches(
    watcher: &mut RecommendedWatcher,
    dir: &Path,
    root: &Path,
    excludes: &[String],
) -> usize {
    // Skip directories whose name is in the well-known ignore list
    if let Some(name) = dir.file_name().and_then(|n| n.to_str()) {
        if crate::discover::is_ignored_dir(name) {
            debug!("skipping ignored dir: {}", dir.display());
            return 0;
        }
    }

    // Skip directories matching user-provided exclude patterns
    if !excludes.is_empty() && crate::config::matches_any_pattern(dir, excludes, root) {
        debug!("skipping excluded dir: {}", dir.display());
        return 0;
    }

    if let Err(e) = watcher.watch(dir, RecursiveMode::NonRecursive) {
        warn!("failed to watch {}: {}", dir.display(), e);
        return 0;
    }

    let mut count = 1;

    let Ok(entries) = std::fs::read_dir(dir) else {
        return count;
    };

    for entry in entries.flatten() {
        // Use file_type() to avoid following symlinks into external trees
        if entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
            count += add_watches(watcher, &entry.path(), root, excludes);
        }
    }

    count
}

/// Start watching a directory for file changes.
///
/// Returns a receiver that yields batched, debounced events.
///
/// # inotify limit awareness
///
/// Rather than `RecursiveMode::Recursive` (which creates an inotify watch on
/// every single subdirectory including `target/`, `node_modules/`, etc.),
/// this function walks the tree manually and skips directories that match
/// `discover::IGNORED_DIRECTORIES` or any user-supplied `excludes` pattern.
/// Each kept directory is watched with `RecursiveMode::NonRecursive`.
///
/// # Architecture
///
/// Uses an event-driven design:
/// 1. `notify::RecommendedWatcher` sends events via std::sync::mpsc (from inotify thread)
/// 2. A bridge thread forwards events to a tokio mpsc channel (async-safe)
/// 3. A tokio task debounces events using `select!` on channel + timer
///
/// The bridge thread is necessary because notify's callback runs on its internal
/// thread, and we need to get events into the tokio runtime safely.
pub fn watch(
    root: &Path,
    include_dirs: &[PathBuf],
    excludes: &[String],
    filter: Arc<dyn Fn(&Path) -> bool + Send + Sync>,
) -> Result<tokio::sync::mpsc::Receiver<WatchEvent>> {
    // Bounded channels prevent memory exhaustion from event buildup
    // output: 512 slots for batched WatchEvents (small, high-level events)
    let (output_tx, output_rx) = tokio::sync::mpsc::channel(512);
    let root = root.to_path_buf();

    // Channel from notify's callback thread to our bridge thread
    let (notify_tx, notify_rx) = std::sync::mpsc::channel::<Event>();

    // Channel from bridge thread to async debounce task
    // bridge: 2048 slots for raw filesystem events (large volume before debouncing)
    let (bridge_tx, bridge_rx) = tokio::sync::mpsc::channel::<Event>(2048);

    // Create the filesystem watcher
    let mut watcher = RecommendedWatcher::new(
        move |res: notify::Result<Event>| {
            if let Ok(event) = res {
                let _ = notify_tx.send(event);
            }
        },
        // Use default config for native event-driven watching (inotify/fsevents)
        notify::Config::default(),
    )?;

    // Walk the root directory tree, skipping excluded dirs, and add NonRecursive watches.
    // This prevents exhausting the inotify watch limit (default 65536) on large repos.
    let watched = add_watches(&mut watcher, &root, &root, excludes);

    // Watch each included directory tree with the same exclusion logic
    let mut inc_watched = 0;
    for dir in include_dirs {
        if dir.exists() {
            // Use the include dir itself as root for pattern matching
            inc_watched += add_watches(&mut watcher, dir, dir, excludes);
        }
    }

    info!(
        "watching {} dirs under {} (and {} dirs under {} include paths)",
        watched,
        root.display(),
        inc_watched,
        include_dirs.len()
    );

    // Bridge thread: forwards from std::sync::mpsc to tokio channel
    // This thread blocks on recv() which is efficient (no CPU when idle)
    std::thread::spawn(move || {
        let _watcher = watcher; // keep watcher alive
        
        while let Ok(event) = notify_rx.recv() {
            // blocking_send: blocks this bridge thread until channel has capacity.
            // This is safe because this is a dedicated std::thread, not a tokio worker.
            // Only exits if the receiver is dropped (legitimate shutdown).
            if bridge_tx.blocking_send(event).is_err() {
                break; // receiver dropped — shutdown
            }
        }
    });

    let root_clone = root.clone();
    tokio::spawn(async move {
        debounce_loop(bridge_rx, output_tx, filter, root_clone).await;
    });

    Ok(output_rx)
}

/// Debounce loop using tokio::select!
///
/// Achieves ~0% CPU when idle because:
/// - `event_rx.recv()` blocks efficiently waiting for channel message
/// - `tokio::time::sleep()` blocks efficiently waiting for timer
/// - `tokio::select!` wakes on whichever completes first
async fn debounce_loop(
    mut event_rx: tokio::sync::mpsc::Receiver<Event>,
    output_tx: tokio::sync::mpsc::Sender<WatchEvent>,
    filter: Arc<dyn Fn(&Path) -> bool + Send + Sync>,
    root: PathBuf,
) {
    // Initialize so the first flush is never delayed
    let mut last_flush = tokio::time::Instant::now() - MIN_FLUSH_INTERVAL;

    loop {
        // Phase 1: Wait for first event (blocks efficiently)
        let first_event = match event_rx.recv().await {
            Some(e) => e,
            None => break, // channel closed
        };
        
        let mut changed: HashSet<PathBuf> = HashSet::new();
        let mut deleted: HashSet<PathBuf> = HashSet::new();
        classify_event(first_event, &mut changed, &mut deleted, &root);
        
        // Phase 2: Drain events until debounce timer expires
        let debounce_timer = tokio::time::sleep(DEBOUNCE_DELAY);
        tokio::pin!(debounce_timer);
        
        loop {
            // Tokio randomly checks both branches to prevent starvation.
            // During heavy filesystem activity, this ensures the timer can fire
            // even when events are continuously arriving, preventing unbounded growth.
            tokio::select! {
                // More events arrived - process them and reset timer
                event = event_rx.recv() => {
                    match event {
                        Some(e) => {
                            classify_event(e, &mut changed, &mut deleted, &root);
                            // Note: We don't reset the timer - we want to flush after
                            // DEBOUNCE_DELAY from the *first* event, not the *last* event.
                            // This prevents indefinite batching during continuous changes.
                        }
                        None => break, // channel closed — flush remaining batch
                    }
                }
                
                // Debounce timer expired - flush the batch
                _ = &mut debounce_timer => {
                    break;
                }
            }
        }
        
        // Inter-flush cooldown: one-shot sleep for any remaining gap since last flush.
        let elapsed = last_flush.elapsed();
        if elapsed < MIN_FLUSH_INTERVAL {
            tokio::time::sleep(MIN_FLUSH_INTERVAL - elapsed).await;
        }

        // Phase 3: Filter and send batch
        let changed: Vec<PathBuf> = changed.into_iter().filter(|p| filter(p)).collect();
        let deleted: Vec<PathBuf> = deleted.into_iter().filter(|p| filter(p)).collect();

        if !changed.is_empty() {
            debug!("{} files changed", changed.len());
            // Use .await for bounded channel (applies backpressure if receiver is slow)
            if output_tx.send(WatchEvent::Changed(changed)).await.is_err() {
                break; // receiver dropped
            }
        }
        if !deleted.is_empty() {
            debug!("{} files deleted", deleted.len());
            // Use .await for bounded channel (applies backpressure if receiver is slow)
            if output_tx.send(WatchEvent::Deleted(deleted)).await.is_err() {
                break; // receiver dropped
            }
        }

        last_flush = tokio::time::Instant::now();
    }
}

fn classify_event(
    event: Event,
    changed: &mut HashSet<PathBuf>,
    deleted: &mut HashSet<PathBuf>,
    _root: &Path,
) {
    match event.kind {
        EventKind::Create(_) | EventKind::Modify(_) => {
            for path in event.paths {
                // NO SYSCALL HERE - we deliberately skip is_dir() check for performance.
                //
                // Why: is_dir() is a stat syscall that was causing ~90% CPU usage when
                // processing many events. Instead, we let directories slip through and
                // the worker actor handles them gracefully:
                // - tokio::fs::read_to_string() fails on directories
                // - Worker returns Ok(false) on read failure
                // - This is rare anyway: directories rarely trigger Create/Modify events
                //
                // The filter() callback handles path-based filtering (extensions, ignores).
                deleted.remove(&path);
                changed.insert(path);
            }
        }
        EventKind::Remove(_) => {
            for path in event.paths {
                changed.remove(&path);
                deleted.insert(path);
            }
        }
        _ => {}
    }
}
