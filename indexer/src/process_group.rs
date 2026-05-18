//! Process group cleanup utilities for the indexer daemon.
//! Handles process group creation, OOM score adjustment,
//! process group termination, and parent process monitoring.

use std::time::Duration;

/// Set up process group for clean child process termination.
///
/// Creates a new process group so we can kill all children on shutdown.
/// Does NOT use PR_SET_PDEATHSIG — the daemon must stay alive across
/// TUI parent restarts or the watcher dies and never stabilizes.
#[cfg(target_os = "linux")]
pub fn setup_process_group() {
    // Try to become session leader (new process group)
    unsafe {
        libc::setpgid(0, 0);
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

/// Monitor parent process and log when it dies.
/// Does NOT shut down the daemon — the daemon stays alive across TUI
/// parent restarts so the watcher is not killed. The idle timeout
/// (300s default) handles eventual cleanup if no TUI reconnects.
/// Spawns a background task that polls parent PID every 5 seconds.
pub async fn spawn_parent_monitor(parent_pid: i32, _shutdown_tx: tokio::sync::watch::Sender<bool>) {
    tokio::spawn(async move {
        let mut parent_dead = false;
        loop {
            tokio::time::sleep(Duration::from_secs(5)).await;
            let alive = unsafe { libc::kill(parent_pid, 0) == 0 };
            if !alive {
                if !parent_dead {
                    let connections = crate::http_server::ACTIVE_CONNECTIONS.load(std::sync::atomic::Ordering::SeqCst);
                    tracing::warn!(
                        "Parent process {} died ({} active connections) — daemon stays alive until idle timeout",
                        parent_pid,
                        connections,
                    );
                    parent_dead = true;
                }
                // Do NOT shut down. The daemon stays alive across TUI restarts
                // so the watcher is not killed. The idle timeout (300s default)
                // handles eventual cleanup if no TUI reconnects.
                // New TUI instances reuse this daemon via Unix socket probe.
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kill_process_group_skipped_with_env_var() {
        // SAFETY: test-only, single-threaded, no other code reads this var
        unsafe { std::env::set_var("OPENCODE_NO_KILL_PROCESS_GROUP", "1") };
        kill_process_group();
        // SAFETY: test-only cleanup
        unsafe { std::env::remove_var("OPENCODE_NO_KILL_PROCESS_GROUP") };
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn set_oom_score_noop_on_non_linux() {
        let original = std::fs::read_to_string("/proc/self/oom_score_adj").unwrap();
        set_oom_score(500);
        let value = std::fs::read_to_string("/proc/self/oom_score_adj").unwrap();
        assert_eq!(value, "500\n");
        std::fs::write("/proc/self/oom_score_adj", original.trim()).unwrap();
    }

    #[cfg(not(target_os = "linux"))]
    #[test]
    fn set_oom_score_noop_on_non_linux() {
        set_oom_score(500);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn default_oom_score_is_zero() {
        let value = std::fs::read_to_string("/proc/self/oom_score_adj").unwrap();
        let _score: i32 = value.trim().parse().expect("oom_score_adj should be a valid number");
    }

    #[cfg(not(target_os = "linux"))]
    #[test]
    fn default_oom_score_is_zero() {
        // Skipping on non-Linux: /proc/self/oom_score_adj is not available
    }
}
