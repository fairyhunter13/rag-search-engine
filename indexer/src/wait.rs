use std::sync::Arc;
use std::time::Duration;

use futures::stream::{FuturesUnordered, StreamExt};
use tokio::sync::Mutex;

use crate::daemon::{DaemonState, PendingChanges};

/// Helper function to wait for any watcher to have pending changes.
/// Uses watch::Receiver::changed() which is level-triggered-on-change and has
/// no stored-permit problem (unlike Notify::notified()).
pub async fn wait_for_any_pending(state: &Arc<Mutex<DaemonState>>) {
    wait_for_any_pending_impl(state, |s| {
        s.watchers
            .values()
            .map(|w| &w.pending)
            .collect::<Vec<_>>()
    })
    .await
}

/// Wait for any memory watcher to have pending changes (async, non-blocking).
/// Uses watch::Receiver::changed() — level-triggered-on-change, no stored-permit spin.
pub async fn wait_for_any_memory_pending(state: &Arc<Mutex<DaemonState>>) {
    wait_for_any_pending_impl(state, |s| {
        s.memory_watchers
            .values()
            .map(|w| &w.pending)
            .collect::<Vec<_>>()
    })
    .await
}

async fn wait_for_any_pending_impl(
    state: &Arc<Mutex<DaemonState>>,
    pendings: impl Fn(&DaemonState) -> Vec<&Arc<Mutex<PendingChanges>>>,
) {
    let (rxs, has_pending): (Vec<tokio::sync::watch::Receiver<u64>>, bool) = {
        let s = state.lock().await;
        let mut pending = false;
        let mut handles = Vec::new();
        for w in pendings(&s) {
            if let Ok(p) = w.try_lock() {
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
