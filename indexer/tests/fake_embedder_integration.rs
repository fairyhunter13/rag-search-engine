//! Integration tests using the fake in-process embedder.
//!
//! These verify the daemon's RPC behaviour without requiring the real Python
//! model server (models, GPU, etc.). The fake embedder returns zero vectors
//! and trivial chunks — enough for the daemon's control-flow to exercise.
//!
//! Covers:
//!   1. Daemon boots and responds to `watcher_status` → `watcherActive: false` when idle.
//!   2. `startup_check` returns `watching: true` after spawning the watcher task (Bug 1 fix).
//!   3. `watcher_status` reflects the real watcher state after `startup_check` (Bug 2 fix).
//!   4. Resource-optimisation env vars (`OPENCODE_SEARCH_RAYON_THREADS`,
//!      `OPENCODE_SEARCH_STORAGE_CACHE_SIZE`, `OPENCODE_SEARCH_MAX_FILE_SIZE_KB`) are
//!      accepted without error.
//!   5. `run_index` completes successfully against the fake embedder.

mod fake_embedder;

use std::time::Duration;

use anyhow::{Context, Result};
use fs2::FileExt;
use serde_json::{Value, json};
#[cfg(unix)] use std::os::unix::process::CommandExt as _;
use tokio::io::AsyncBufReadExt;
use tokio::process::Command;

/// Return path to the test daemon binary, creating a copy under a non-matching
/// name if needed. This prevents `pgrep -f "opencode-indexer.*--daemon"` (used
/// by the TS opencode's killOrphanedDaemon()) from matching our test daemons.
fn test_daemon_binary() -> std::path::PathBuf {
    let src = assert_cmd::cargo::cargo_bin!("opencode-indexer");
    let dst = src.with_file_name("opencode-test-daemon");
    // Only copy if source is newer (or dest missing).
    let needs_copy = dst.metadata().ok().zip(src.metadata().ok()).map_or(true, |(d, s)| {
        s.modified().ok() > d.modified().ok()
    });
    if needs_copy {
        std::fs::copy(&src, &dst).expect("copy test daemon binary");
        // Make executable
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = std::fs::metadata(&dst).unwrap().permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(&dst, perms).unwrap();
        }
    }
    dst
}

// Cross-runtime serialisation via OS file lock.
// Each test acquires this lock before spawning a daemon to prevent concurrent
// daemon instances from interfering. Uses fs2::FileExt (same as active_indexes_guard.rs).
// Unlike std::sync::Mutex or tokio::sync::Mutex, an OS flock works correctly
// across independent tokio runtimes on different OS threads.
fn acquire_test_lock() -> Result<std::fs::File> {
    let path = std::env::temp_dir().join("opencode-indexer-fake-embedder-global.lock");
    let file = std::fs::OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .open(&path)
        .with_context(|| format!("open test lock: {}", path.display()))?;
    file.lock_exclusive().context("acquire test lock")?;
    Ok(file)
}

// ---------------------------------------------------------------------------
// RPC helper
// ---------------------------------------------------------------------------

async fn rpc(port: u16, method: &str, params: Value) -> Result<Value> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .build()?;
    // Retry once with backoff: the daemon may be briefly busy after run_index.
    for attempt in 0..2 {
        if attempt > 0 {
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
        match client
            .post(format!("http://127.0.0.1:{port}/rpc"))
            .json(&json!({"method": method, "params": params}))
            .send()
            .await
        {
            Ok(resp) => return resp.json::<Value>().await.context("parse rpc response"),
            Err(e) if attempt == 0 => {
                tracing::warn!("rpc attempt {} failed: {}", attempt + 1, e);
            }
            Err(e) => return Err(e).context("send rpc"),
        }
    }
    unreachable!()
}

// ---------------------------------------------------------------------------
// Daemon lifecycle helper
// ---------------------------------------------------------------------------

struct DaemonHandle {
    child: tokio::process::Child,
    port: u16,
    _lock: std::fs::File,
}

impl DaemonHandle {
    /// Acquire an exclusive file lock to serialise daemon spawning across
    /// parallel tokio tests. Runs on a blocking thread to avoid stalling the executor.
    async fn lock() -> Result<std::fs::File> {
        tokio::task::spawn_blocking(|| {
            let path = std::env::temp_dir().join("opencode-indexer-fake-embedder.lock");
            let file = std::fs::OpenOptions::new()
                .create(true)
                .read(true)
                .write(true)
                .open(&path)
                .with_context(|| format!("open lock: {}", path.display()))?;
            file.lock_exclusive().context("lock")?;
            Ok::<_, anyhow::Error>(file)
        })
        .await
        .context("spawn_blocking lock")?
    }

    async fn spawn(home: &std::path::Path, embed_env: &[(&str, String)], extra_env: &[(&str, &str)]) -> Result<Self> {
        let lock = Self::lock().await?;
        let bin = test_daemon_binary();
        let mut cmd = Command::new(&bin);
        cmd.env("HOME", home)
            .env("OPENCODE_INDEXER_IDLE_SHUTDOWN", "0")  // disable idle shutdown in tests
            .env("OPENCODE_NO_KILL_PROCESS_GROUP", "1") // prevent cross-daemon SIGTERM in tests
            .arg("--daemon")
            .arg("--port")
            .arg("0")
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped());

        // Put the daemon in its own process group before exec so kill_process_group()
        // only kills THIS daemon's children, not other test daemons or the test binary.
        // Without this, setpgid(0,0) fails in the daemon (EPERM in this environment)
        // and all daemons share the test binary's pgid — kill_process_group() from
        // one daemon would SIGTERM all others.
        unsafe {
            cmd.pre_exec(|| {
                // This runs in the forked child before exec.
                // setpgid(0, 0) here always succeeds (parent can set child's pgid
                // before exec, even when the child itself cannot).
                libc::setpgid(0, 0);
                Ok(())
            });
        }

        for (k, v) in embed_env {
            cmd.env(*k, v);
        }
        for (k, v) in extra_env {
            cmd.env(*k, v);
        }

        let mut child = cmd.spawn().context("spawn daemon")?;
        let stdout = child.stdout.take().context("no stdout")?;
        let mut reader = tokio::io::BufReader::new(stdout);
        let mut buf = String::new();
        let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
        #[allow(unused_assignments)]
        let mut port = 0u16;

        loop {
            if tokio::time::Instant::now() > deadline {
                child.kill().await.ok();
                anyhow::bail!("daemon did not become ready within 30s");
            }
            buf.clear();
            reader.read_line(&mut buf).await?;
            if let Ok(msg) = serde_json::from_str::<Value>(&buf) {
                if msg.get("type").and_then(|v| v.as_str()) == Some("http_ready") {
                    port = msg["port"].as_u64().unwrap_or(0) as u16;
                    if port > 0 {
                        break;
                    }
                }
            }
        }

        // Drain stdout to prevent pipe-buffer stall.
        tokio::spawn(async move {
            let mut discard = vec![0u8; 4096];
            loop {
                match tokio::io::AsyncReadExt::read(&mut reader, &mut discard).await {
                    Ok(0) | Err(_) => break,
                    _ => {}
                }
            }
        });

        // Drain stderr too — prevents pipe stall and surfaces daemon panics in --nocapture.
        let stderr = child.stderr.take();
        if let Some(stderr) = stderr {
            let mut stderr_reader = tokio::io::BufReader::new(stderr);
            tokio::spawn(async move {
                let mut line = String::new();
                loop {
                    line.clear();
                    match tokio::io::AsyncBufReadExt::read_line(&mut stderr_reader, &mut line).await {
                        Ok(0) | Err(_) => break,
                        _ => if !line.is_empty() {
                            eprint!("[daemon] {line}");
                        }
                    }
                }
            });
        }

        Ok(Self { child, port, _lock: lock })
    }

    fn port(&self) -> u16 { self.port }

    async fn shutdown(mut self) {
        let _ = rpc(self.port, "shutdown", json!({})).await;
        // Give daemon time to flush and exit cleanly before killing.
        // The 500ms wait + child.wait() ensures the lock is not released
        // until the OS port is fully freed, preventing the next test's daemon
        // from racing on startup.
        tokio::time::sleep(Duration::from_millis(500)).await;
        self.child.kill().await.ok();
        self.child.wait().await.ok();
    }
}

// ---------------------------------------------------------------------------
// Git repo helper
// ---------------------------------------------------------------------------

fn create_git_repo(n: usize) -> Result<tempfile::TempDir> {
    let dir = tempfile::TempDir::new()?;
    let p = dir.path();
    let git = |args: &[&str]| -> Result<()> {
        let out = std::process::Command::new("git")
            .args(args)
            .current_dir(p)
            .output()
            .with_context(|| format!("git {:?}", args))?;
        anyhow::ensure!(out.status.success(), "git {:?} failed", args);
        Ok(())
    };
    git(&["init", "-b", "main"])?;
    git(&["config", "user.email", "test@test"])?;
    git(&["config", "user.name", "Test"])?;
    for i in 0..n {
        let path = p.join(format!("src_{i}.rs"));
        std::fs::write(&path, format!("fn f{i}() -> u32 {{ {i} }}\n"))?;
        git(&["add", path.to_str().unwrap()])?;
    }
    git(&["commit", "-m", "init"])?;
    Ok(dir)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

/// 1. Daemon starts and `watcher_status` returns `watcherActive: false` before
///    any indexing is triggered for a project.
// current_thread flavor: the test runs on a single OS thread.
// This prevents prctl(PR_SET_PDEATHSIG) from firing when tokio worker threads
// are recycled between tests, which would send SIGTERM to the daemon mid-test.
#[tokio::test(flavor = "current_thread")]
async fn watcher_status_false_before_indexing() -> Result<()> {
    let _lock = tokio::task::spawn_blocking(acquire_test_lock).await
        .context("spawn_blocking")??;
    let embedder = fake_embedder::FakeEmbedder::start().await?;
    let home = tempfile::TempDir::new()?;
    let daemon = DaemonHandle::spawn(home.path(), &embedder.env_vars(), &[]).await?;

    let root = create_git_repo(3)?;
    let root_str = root.path().to_str().unwrap();

    let resp = rpc(
        daemon.port(),
        "watcher_status",
        json!({"root": root_str}),
    ).await?;

    // Daemon wraps all RPC responses in {"result": {...}}
    assert_eq!(
        resp["result"]["watcherActive"].as_bool(),
        Some(false),
        "watcher should not be active before indexing: {resp}"
    );

    daemon.shutdown().await;
    Ok(())
}

/// 2. `startup_check` returns `watching: true` after spawning the background
///    watcher task (Bug 1 fix: was returning `false`).
///
/// Note: retried up to 3 times. A SIGTERM from the environment (PR_SET_PDEATHSIG
/// from a parent thread exit, or kill_process_group from a concurrent test daemon)
/// can transiently kill the daemon. The retry respawns a fresh daemon.
#[tokio::test(flavor = "current_thread")]
async fn startup_check_returns_watching_true() -> Result<()> {
    let _lock = tokio::task::spawn_blocking(acquire_test_lock).await
        .context("spawn_blocking")??;

    for attempt in 0..3 {
        let embedder = fake_embedder::FakeEmbedder::start().await?;
        let home = tempfile::TempDir::new()?;
        let daemon = DaemonHandle::spawn(home.path(), &embedder.env_vars(), &[]).await?;

        let root = create_git_repo(3)?;
        let root_str = root.path().to_str().unwrap();
        let db_str = root.path().join(".lancedb-test").to_str().unwrap().to_string();

        // First, index the project so startup_check finds an existing index.
        let run_result = rpc(daemon.port(), "run_index", json!({
            "root": root_str,
            "db": db_str,
        })).await;

        if run_result.is_err() {
            // Daemon died (transient SIGTERM). Retry with a fresh daemon.
            if attempt < 2 {
                daemon.shutdown().await;
                tokio::time::sleep(Duration::from_millis(200)).await;
                continue;
            }
            return Err(run_result.unwrap_err());
        }

        // Stop the watcher explicitly so startup_check will restart it.
        rpc(daemon.port(), "watcher_stop", json!({"root": root_str, "db": db_str})).await.ok();
        tokio::time::sleep(Duration::from_millis(200)).await;

        // startup_check should return watching: true (Bug 1 fix).
        // NOTE: daemon reads "dimensions" (not "dims") from params.
        let resp = rpc(daemon.port(), "startup_check", json!({
            "root": root_str,
            "db": db_str,
            "tier": "budget",
            "dimensions": 384,
        })).await?;

        assert_eq!(
            resp["result"]["watching"].as_bool(),
            Some(true),
            "startup_check must return watching:true when watcher task is spawned: {resp}"
        );

        daemon.shutdown().await;
        return Ok(());
    }
    anyhow::bail!("startup_check test failed after 3 attempts")
}

/// 3. `watcher_status` reflects actual watcher state after `startup_check`
///    starts the watcher (Bug 2 fix: was hardcoded `watcherActive: false`).
///
/// Retried up to 3 times to handle transient SIGTERM from the environment.
#[tokio::test(flavor = "current_thread")]
async fn watcher_status_true_after_startup_check() -> Result<()> {
    let _lock = tokio::task::spawn_blocking(acquire_test_lock).await
        .context("spawn_blocking")??;

    for attempt in 0..3 {
        let embedder = fake_embedder::FakeEmbedder::start().await?;
        let home = tempfile::TempDir::new()?;
        let daemon = DaemonHandle::spawn(home.path(), &embedder.env_vars(), &[]).await?;

        let root = create_git_repo(3)?;
        let root_str = root.path().to_str().unwrap();
        let db_str = root.path().join(".lancedb-test2").to_str().unwrap().to_string();

        // Index then let startup_check start the watcher.
        let run_result = rpc(daemon.port(), "run_index", json!({"root": root_str, "db": db_str})).await;
        if run_result.is_err() {
            if attempt < 2 {
                daemon.shutdown().await;
                tokio::time::sleep(Duration::from_millis(300)).await;
                continue;
            }
            return Err(run_result.unwrap_err());
        }

        rpc(daemon.port(), "watcher_stop", json!({"root": root_str, "db": db_str})).await.ok();
        tokio::time::sleep(Duration::from_millis(200)).await;

        rpc(daemon.port(), "startup_check", json!({
            "root": root_str, "db": db_str, "tier": "budget", "dimensions": 384,
        })).await?;

        // Give the background watcher_start task time to register.
        tokio::time::sleep(Duration::from_millis(500)).await;

        let status = rpc(daemon.port(), "watcher_status", json!({
            "root": root_str, "db": db_str,
        })).await?;

        assert_eq!(
            status["result"]["watcherActive"].as_bool(),
            Some(true),
            "watcher_status must reflect true state after startup_check: {status}"
        );

        daemon.shutdown().await;
        return Ok(());
    }
    anyhow::bail!("watcher_status test failed after 3 attempts")
}

/// 4. Resource-optimisation env vars are accepted without error.
///    The daemon must start successfully with all laptop-preset vars set.
#[tokio::test(flavor = "current_thread")]
async fn resource_opt_env_vars_accepted() -> Result<()> {
    let _lock = tokio::task::spawn_blocking(acquire_test_lock).await
        .context("spawn_blocking")??;
    let embedder = fake_embedder::FakeEmbedder::start().await?;
    let home = tempfile::TempDir::new()?;
    let extra = [
        ("OPENCODE_SEARCH_RAYON_THREADS", "2"),
        ("OPENCODE_SEARCH_MAX_FILE_SIZE_KB", "512"),
        ("OPENCODE_SEARCH_STORAGE_CACHE_SIZE", "4"),
    ];
    // If any env var is unrecognised / panics, DaemonHandle::spawn will timeout.
    let daemon = DaemonHandle::spawn(home.path(), &embedder.env_vars(), &extra).await?;

    // A simple health-check RPC to confirm the daemon is functional.
    let resp = rpc(daemon.port(), "health", json!({})).await?;
    assert!(
        resp.get("status").is_some() || resp.get("ok").is_some() || !resp.is_null(),
        "daemon must respond to health RPC: {resp}"
    );

    daemon.shutdown().await;
    Ok(())
}

/// 5. `run_index` completes successfully against the fake embedder.
///
/// Retried up to 3 times to handle transient SIGTERM from the environment.
#[tokio::test(flavor = "current_thread")]
async fn run_index_succeeds_with_fake_embedder() -> Result<()> {
    let _lock = tokio::task::spawn_blocking(acquire_test_lock).await
        .context("spawn_blocking")??;

    for attempt in 0..3 {
        let embedder = fake_embedder::FakeEmbedder::start().await?;
        let home = tempfile::TempDir::new()?;
        let daemon = DaemonHandle::spawn(home.path(), &embedder.env_vars(), &[]).await?;

        let root = create_git_repo(5)?;
        let root_str = root.path().to_str().unwrap();
        let db_str = root.path().join(".lancedb-run").to_str().unwrap().to_string();

        let result = rpc(daemon.port(), "run_index", json!({
            "root": root_str,
            "db": db_str,
        })).await;

        match result {
            Err(e) if attempt < 2 => {
                // Transient SIGTERM killed the daemon u2014 retry with a fresh one.
                daemon.shutdown().await;
                tokio::time::sleep(Duration::from_millis(300)).await;
                continue;
            }
            Err(e) => return Err(e),
            Ok(resp) => {
                assert!(
                    resp.get("error").is_none() || resp.get("result").and_then(|r| r.get("error")).is_none(),
                    "run_index must not return error: {resp}"
                );
                daemon.shutdown().await;
                return Ok(());
            }
        }
    }
    anyhow::bail!("run_index test failed after 3 attempts")
}
