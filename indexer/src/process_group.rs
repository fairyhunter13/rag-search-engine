//! Process group cleanup utilities for the indexer daemon.
//! Handles process group creation, OOM score adjustment,
//! process group termination, and parent process monitoring.

use std::time::Duration;

/// Set up process group for clean child process termination.
///
/// On Linux, uses prctl(PR_SET_PDEATHSIG) to ensure child processes receive
/// SIGTERM when the daemon dies (even from SIGKILL). Also creates a new
/// process group so we can kill all children on shutdown.
#[cfg(target_os = "linux")]
pub fn setup_process_group() {
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
pub fn setup_process_group() {
    // Try to become session leader on non-Linux (best-effort)
    unsafe {
        libc::setpgid(0, 0);
    }
}

/// Adjust OOM score so the indexer is killed before user-facing processes
/// during memory pressure. Fails silently on non-Linux or without permission.
/// score: 0=never kill first, 1000=kill first. 500 = strongly prefer killing indexer.
#[cfg(target_os = "linux")]
pub fn set_oom_score(score: i32) {
    if let Err(e) = std::fs::write("/proc/self/oom_score_adj", score.to_string()) {
        tracing::debug!("failed to set oom_score_adj={}: {}", score, e);
    } else {
        tracing::debug!("set oom_score_adj={}", score);
    }
}

#[cfg(not(target_os = "linux"))]
pub fn set_oom_score(_score: i32) {}

/// Kill all processes in our process group.
///
/// Called on exit to ensure no orphaned child processes.
/// Skips if OPENCODE_NO_KILL_PROCESS_GROUP=1 (used by tests to prevent
/// cross-daemon SIGTERM when setpgid(0,0) is not permitted by the OS).
pub fn kill_process_group() {
    if std::env::var("OPENCODE_NO_KILL_PROCESS_GROUP").as_deref() == Ok("1") {
        tracing::debug!("kill_process_group: skipped (OPENCODE_NO_KILL_PROCESS_GROUP=1)");
        return;
    }
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
pub async fn spawn_parent_monitor(parent_pid: i32, shutdown_tx: tokio::sync::watch::Sender<bool>) {
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
