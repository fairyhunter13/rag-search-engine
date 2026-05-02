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

#[cfg(test)]
mod tests {
    use super::*;
    use notify::{
        event::{CreateKind, ModifyKind, RemoveKind},
        Event, EventKind,
    };

    fn make_event(kind: EventKind, paths: Vec<&str>) -> Event {
        Event {
            kind,
            paths: paths.into_iter().map(PathBuf::from).collect(),
            attrs: Default::default(),
        }
    }

    #[test]
    fn classify_event_create_adds_to_changed() {
        let mut changed = HashSet::new();
        let mut deleted = HashSet::new();
        let root = PathBuf::from("/tmp");

        // Note: This test uses a non-existent path. Since the path doesn't exist,
        // is_dir() returns false, so the path is added to changed.
        // In real usage, files being written may also fail is_file() but pass !is_dir().
        let event = make_event(
            EventKind::Create(CreateKind::File),
            vec!["/tmp/nonexistent_test.txt"],
        );

        classify_event(event, &mut changed, &mut deleted, &root);
        // Non-existent paths are added (they're not directories)
        assert!(changed.contains(&PathBuf::from("/tmp/nonexistent_test.txt")));
    }

    #[test]
    fn classify_event_modify_adds_to_changed() {
        let mut changed = HashSet::new();
        let mut deleted = HashSet::new();
        let root = PathBuf::from("/tmp");

        let event = make_event(
            EventKind::Modify(ModifyKind::Data(notify::event::DataChange::Content)),
            vec!["/tmp/nonexistent_test.txt"],
        );

        classify_event(event, &mut changed, &mut deleted, &root);
        // Modify events for non-directories are added to changed
        assert!(changed.contains(&PathBuf::from("/tmp/nonexistent_test.txt")));
    }

    #[test]
    fn classify_event_allows_directories_through() {
        let mut changed = HashSet::new();
        let mut deleted = HashSet::new();
        let root = PathBuf::from("/tmp");

        // /tmp is a real directory - but we NO LONGER filter it here
        // (is_dir() syscall was causing 90% CPU; worker handles dirs gracefully)
        let event = make_event(EventKind::Create(CreateKind::Any), vec!["/tmp"]);

        classify_event(event, &mut changed, &mut deleted, &root);
        // Directories are now allowed through - worker will filter them via read failure
        assert!(changed.contains(&PathBuf::from("/tmp")));
    }

    #[test]
    fn classify_event_modify_no_race_check() {
        let mut changed = HashSet::new();
        let mut deleted = HashSet::new();
        let root = PathBuf::from("/tmp");

        let event = make_event(
            EventKind::Modify(ModifyKind::Data(notify::event::DataChange::Content)),
            vec!["/tmp/test.txt"],
        );

        classify_event(event, &mut changed, &mut deleted, &root);
        // Modify events are now added to changed (no is_file() check that could race)
        assert!(changed.contains(&PathBuf::from("/tmp/test.txt")));
    }

    #[test]
    fn classify_event_remove_adds_to_deleted() {
        let mut changed = HashSet::new();
        let mut deleted = HashSet::new();
        let root = PathBuf::from("/tmp");

        let event = make_event(EventKind::Remove(RemoveKind::File), vec!["/tmp/test.txt"]);

        classify_event(event, &mut changed, &mut deleted, &root);

        assert!(deleted.contains(&PathBuf::from("/tmp/test.txt")));
        assert!(changed.is_empty());
    }

    #[test]
    fn classify_event_remove_clears_from_changed() {
        let mut changed = HashSet::new();
        let mut deleted = HashSet::new();
        let root = PathBuf::from("/tmp");

        // Pre-populate changed
        changed.insert(PathBuf::from("/tmp/test.txt"));

        let event = make_event(EventKind::Remove(RemoveKind::File), vec!["/tmp/test.txt"]);

        classify_event(event, &mut changed, &mut deleted, &root);

        // Should move from changed to deleted
        assert!(deleted.contains(&PathBuf::from("/tmp/test.txt")));
        assert!(!changed.contains(&PathBuf::from("/tmp/test.txt")));
    }

    #[test]
    fn classify_event_ignores_other_events() {
        let mut changed = HashSet::new();
        let mut deleted = HashSet::new();
        let root = PathBuf::from("/tmp");

        let event = make_event(
            EventKind::Access(notify::event::AccessKind::Read),
            vec!["/tmp/test.txt"],
        );

        classify_event(event, &mut changed, &mut deleted, &root);

        assert!(changed.is_empty());
        assert!(deleted.is_empty());
    }

    #[test]
    fn classify_event_moves_ownership() {
        let mut changed = HashSet::new();
        let mut deleted = HashSet::new();
        let root = PathBuf::from("/tmp");

        // Create event with paths
        let event = Event {
            kind: EventKind::Remove(RemoveKind::File),
            paths: vec![PathBuf::from("/tmp/a.txt"), PathBuf::from("/tmp/b.txt")],
            attrs: Default::default(),
        };

        // After classify_event, the paths should be moved (not cloned)
        classify_event(event, &mut changed, &mut deleted, &root);

        assert_eq!(deleted.len(), 2);
    }

    #[test]
    fn documents_native_watcher_intent() {
        // RecommendedWatcher selects the best native backend per platform:
        //   Linux: inotify, macOS: FSEvents, Windows: ReadDirectoryChangesW
        assert_eq!(DEBOUNCE_DELAY, Duration::from_millis(1000));
        assert_eq!(MIN_FLUSH_INTERVAL, Duration::from_secs(5));
        assert!(MIN_FLUSH_INTERVAL > DEBOUNCE_DELAY,
            "flush interval must exceed debounce to prevent burst processing");
    }

    #[test]
    fn constants_prevent_burst_processing() {
        // MIN_FLUSH_INTERVAL must be > DEBOUNCE_DELAY to ensure
        // consecutive flushes are spaced out during mass changes
        let ratio = MIN_FLUSH_INTERVAL.as_millis() as f64 / DEBOUNCE_DELAY.as_millis() as f64;
        assert!(ratio >= 2.0, "flush interval should be at least 2x debounce: {ratio}");
    }

    #[tokio::test]
    async fn debounce_batches_rapid_events() {
        // Verify debounce_loop batches multiple events into one flush
        let (tx, rx) = tokio::sync::mpsc::channel::<Event>(100);
        let (out_tx, mut out_rx) = tokio::sync::mpsc::channel::<WatchEvent>(100);
        let filter = Arc::new(|_: &Path| true);
        let root = PathBuf::from("/test");

        tokio::spawn(async move {
            debounce_loop(rx, out_tx, filter, root).await;
        });

        // Send 5 create events rapidly
        for i in 0..5 {
            let event = Event {
                kind: EventKind::Create(notify::event::CreateKind::File),
                paths: vec![PathBuf::from(format!("/test/file{i}.txt"))],
                attrs: Default::default(),
            };
            tx.send(event).await.unwrap();
        }
        // Drop sender to close the loop after flush
        drop(tx);

        // Should receive exactly one batched Changed event
        let batch = tokio::time::timeout(Duration::from_secs(5), out_rx.recv())
            .await
            .expect("timeout waiting for batch")
            .expect("channel closed");

        match batch {
            WatchEvent::Changed(paths) => {
                assert_eq!(paths.len(), 5, "all 5 files should be batched together");
            }
            WatchEvent::Deleted(_) => panic!("expected Changed, got Deleted"),
        }
    }

    #[tokio::test]
    async fn debounce_deduplicates_paths() {
        // Same file modified multiple times → appears once in batch
        let (tx, rx) = tokio::sync::mpsc::channel::<Event>(100);
        let (out_tx, mut out_rx) = tokio::sync::mpsc::channel::<WatchEvent>(100);
        let filter = Arc::new(|_: &Path| true);
        let root = PathBuf::from("/test");

        tokio::spawn(async move {
            debounce_loop(rx, out_tx, filter, root).await;
        });

        // Send same path 3 times
        for _ in 0..3 {
            let event = Event {
                kind: EventKind::Modify(notify::event::ModifyKind::Data(
                    notify::event::DataChange::Content,
                )),
                paths: vec![PathBuf::from("/test/same.txt")],
                attrs: Default::default(),
            };
            tx.send(event).await.unwrap();
        }
        drop(tx);

        let batch = tokio::time::timeout(Duration::from_secs(5), out_rx.recv())
            .await
            .expect("timeout")
            .expect("closed");

        match batch {
            WatchEvent::Changed(paths) => {
                assert_eq!(paths.len(), 1, "duplicate paths should be deduplicated");
            }
            WatchEvent::Deleted(_) => panic!("expected Changed"),
        }
    }

    #[tokio::test]
    async fn debounce_separates_changed_and_deleted() {
        let (tx, rx) = tokio::sync::mpsc::channel::<Event>(100);
        let (out_tx, mut out_rx) = tokio::sync::mpsc::channel::<WatchEvent>(100);
        let filter = Arc::new(|_: &Path| true);
        let root = PathBuf::from("/test");

        tokio::spawn(async move {
            debounce_loop(rx, out_tx, filter, root).await;
        });

        // Create then delete in same batch
        tx.send(Event {
            kind: EventKind::Create(notify::event::CreateKind::File),
            paths: vec![PathBuf::from("/test/a.txt")],
            attrs: Default::default(),
        }).await.unwrap();

        tx.send(Event {
            kind: EventKind::Remove(notify::event::RemoveKind::File),
            paths: vec![PathBuf::from("/test/b.txt")],
            attrs: Default::default(),
        }).await.unwrap();

        drop(tx);

        // Collect both events
        let mut changed = 0;
        let mut deleted = 0;
        let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
        loop {
            match tokio::time::timeout_at(deadline, out_rx.recv()).await {
                Ok(Some(WatchEvent::Changed(p))) => changed += p.len(),
                Ok(Some(WatchEvent::Deleted(p))) => deleted += p.len(),
                Ok(None) => break,
                Err(_) => break,
            }
        }

        assert_eq!(changed, 1, "one file changed");
        assert_eq!(deleted, 1, "one file deleted");
    }

    #[tokio::test]
    async fn debounce_applies_filter() {
        let (tx, rx) = tokio::sync::mpsc::channel::<Event>(100);
        let (out_tx, mut out_rx) = tokio::sync::mpsc::channel::<WatchEvent>(100);
        // Filter: only .rs files
        let filter = Arc::new(|p: &Path| {
            p.extension().map_or(false, |e| e == "rs")
        });
        let root = PathBuf::from("/test");

        tokio::spawn(async move {
            debounce_loop(rx, out_tx, filter, root).await;
        });

        tx.send(Event {
            kind: EventKind::Create(notify::event::CreateKind::File),
            paths: vec![
                PathBuf::from("/test/keep.rs"),
                PathBuf::from("/test/skip.txt"),
                PathBuf::from("/test/also.rs"),
            ],
            attrs: Default::default(),
        }).await.unwrap();

        drop(tx);

        let batch = tokio::time::timeout(Duration::from_secs(5), out_rx.recv())
            .await
            .expect("timeout")
            .expect("closed");

        match batch {
            WatchEvent::Changed(paths) => {
                assert_eq!(paths.len(), 2, "only .rs files should pass filter");
                assert!(paths.iter().all(|p| p.extension().unwrap() == "rs"));
            }
            _ => panic!("expected Changed"),
        }
    }

    #[tokio::test]
    async fn inter_flush_cooldown_spaces_consecutive_flushes() {
        // Verify that two rapid batches are spaced by MIN_FLUSH_INTERVAL
        let (tx, rx) = tokio::sync::mpsc::channel::<Event>(100);
        let (out_tx, mut out_rx) = tokio::sync::mpsc::channel::<WatchEvent>(100);
        let filter = Arc::new(|_: &Path| true);
        let root = PathBuf::from("/test");

        tokio::spawn(async move {
            debounce_loop(rx, out_tx, filter, root).await;
        });

        // First batch
        tx.send(Event {
            kind: EventKind::Create(notify::event::CreateKind::File),
            paths: vec![PathBuf::from("/test/batch1.txt")],
            attrs: Default::default(),
        }).await.unwrap();

        let t0 = tokio::time::Instant::now();

        // Wait for first flush
        let _ = tokio::time::timeout(Duration::from_secs(5), out_rx.recv())
            .await
            .expect("timeout on batch 1")
            .expect("closed");

        let t1 = tokio::time::Instant::now();

        // Immediately send second batch
        tx.send(Event {
            kind: EventKind::Create(notify::event::CreateKind::File),
            paths: vec![PathBuf::from("/test/batch2.txt")],
            attrs: Default::default(),
        }).await.unwrap();

        // Wait for second flush — must wait up to DEBOUNCE_DELAY + MIN_FLUSH_INTERVAL
        let _ = tokio::time::timeout(Duration::from_secs(8), out_rx.recv())
            .await
            .expect("timeout on batch 2")
            .expect("closed");

        let t2 = tokio::time::Instant::now();

        // The gap between the two flushes must be >= MIN_FLUSH_INTERVAL
        // (minus some tolerance for timer precision)
        let gap = t2 - t1;
        let tolerance = Duration::from_millis(100);
        assert!(
            gap + tolerance >= MIN_FLUSH_INTERVAL,
            "gap between flushes ({gap:?}) should be >= MIN_FLUSH_INTERVAL ({:?})",
            MIN_FLUSH_INTERVAL,
        );

        // First flush should NOT be delayed (initial last_flush is in the past)
        let first = t1 - t0;
        assert!(
            first < MIN_FLUSH_INTERVAL,
            "first flush ({first:?}) should not be delayed by inter-flush cooldown",
        );

        drop(tx);
    }

    #[tokio::test]
    async fn debounce_delete_overrides_create_in_same_batch() {
        // If a file is created then deleted in the same debounce window,
        // it should appear only in deleted, not in changed
        let (tx, rx) = tokio::sync::mpsc::channel::<Event>(100);
        let (out_tx, mut out_rx) = tokio::sync::mpsc::channel::<WatchEvent>(100);
        let filter = Arc::new(|_: &Path| true);
        let root = PathBuf::from("/test");

        tokio::spawn(async move {
            debounce_loop(rx, out_tx, filter, root).await;
        });

        // Create then delete same file
        tx.send(Event {
            kind: EventKind::Create(notify::event::CreateKind::File),
            paths: vec![PathBuf::from("/test/temp.txt")],
            attrs: Default::default(),
        }).await.unwrap();

        tx.send(Event {
            kind: EventKind::Remove(notify::event::RemoveKind::File),
            paths: vec![PathBuf::from("/test/temp.txt")],
            attrs: Default::default(),
        }).await.unwrap();

        drop(tx);

        // Should receive only Deleted, not Changed
        let mut changed = Vec::new();
        let mut deleted = Vec::new();
        let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
        loop {
            match tokio::time::timeout_at(deadline, out_rx.recv()).await {
                Ok(Some(WatchEvent::Changed(p))) => changed.extend(p),
                Ok(Some(WatchEvent::Deleted(p))) => deleted.extend(p),
                Ok(None) => break,
                Err(_) => break,
            }
        }

        assert!(changed.is_empty(), "create+delete should not appear in changed");
        assert_eq!(deleted.len(), 1, "should appear in deleted");
        assert_eq!(deleted[0], PathBuf::from("/test/temp.txt"));
    }

    #[tokio::test]
    async fn debounce_empty_batch_after_filter_sends_nothing() {
        // All events filtered out → no WatchEvent sent
        let (tx, rx) = tokio::sync::mpsc::channel::<Event>(100);
        let (out_tx, mut out_rx) = tokio::sync::mpsc::channel::<WatchEvent>(100);
        // Reject everything
        let filter = Arc::new(|_: &Path| false);
        let root = PathBuf::from("/test");

        tokio::spawn(async move {
            debounce_loop(rx, out_tx, filter, root).await;
        });

        tx.send(Event {
            kind: EventKind::Create(notify::event::CreateKind::File),
            paths: vec![PathBuf::from("/test/rejected.txt")],
            attrs: Default::default(),
        }).await.unwrap();

        drop(tx);

        // Should receive nothing (channel closes without events)
        let result = tokio::time::timeout(Duration::from_secs(4), out_rx.recv()).await;
        match result {
            Ok(None) => {} // channel closed, no events — correct
            Ok(Some(_)) => panic!("should not receive any events when filter rejects all"),
            Err(_) => {} // timeout is also acceptable (debounce + cooldown)
        }
    }
}
