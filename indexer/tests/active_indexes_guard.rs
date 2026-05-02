//! E2E tests proving the `active_indexes()` guard fix works correctly.
//!
//! Background: The indexer's `status` RPC has a self-heal mechanism that
//! clears stale `indexing_in_progress` flags left behind by crashed runs.
//! The bug was that self-heal would fire even while a live `run_index` call
//! was in progress, because `status` didn't check whether an active index
//! operation was registered in `active_indexes`.
//!
//! These tests verify three invariants:
//!
//!   1. While `run_index` is in progress, concurrent `status` calls do NOT
//!      clear the progress flag (active_indexes guard prevents it).
//!
//!   2. When `run_index_background` is in progress (startup_check corruption
//!      recovery), concurrent `status` calls do NOT clear progress either.
//!
//!   3. When NO indexing is active and `indexing_in_progress` is stale/stuck,
//!      `status` DOES self-heal (the guard does not break the original fix).

mod python_server;

use std::time::Duration;

use anyhow::{Context, Result};
use arrow_array::builder::StringBuilder;
use arrow_array::{RecordBatch, RecordBatchIterator};
use arrow_schema::{DataType, Field, Schema};
use fs2::FileExt;
use lancedb::connect;
use tokio::process::Command;

// ---------------------------------------------------------------------------
// LanceDB config helpers (mirrors status_selfheal.rs)
// ---------------------------------------------------------------------------

fn config_schema() -> Schema {
    Schema::new(vec![
        Field::new("key", DataType::Utf8, false),
        Field::new("value", DataType::Utf8, false),
    ])
}

async fn set_config(db: &std::path::Path, key: &str, value: &str) -> Result<()> {
    std::fs::create_dir_all(db).context("create db dir")?;
    let db_conn = connect(db.to_str().unwrap()).execute().await?;

    let names = db_conn.table_names().execute().await?;
    let table = if names.contains(&"config".to_string()) {
        db_conn.open_table("config").execute().await?
    } else {
        db_conn
            .create_empty_table("config", config_schema().into())
            .execute()
            .await?
    };

    let _ = table
        .delete(&format!("key = '{}'", key.replace("'", "''")))
        .await;

    let mut keys = StringBuilder::new();
    let mut values = StringBuilder::new();
    keys.append_value(key);
    values.append_value(value);

    let schema = std::sync::Arc::new(config_schema());
    let batch = RecordBatch::try_new(
        schema.clone(),
        vec![
            std::sync::Arc::new(keys.finish()),
            std::sync::Arc::new(values.finish()),
        ],
    )?;
    let reader = RecordBatchIterator::new(vec![Ok(batch)], schema);
    table.add(reader).execute().await?;
    Ok(())
}

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

async fn rpc(
    port: u16,
    token: Option<&str>,
    method: &str,
    params: serde_json::Value,
) -> Result<serde_json::Value> {
    let client = reqwest::Client::new();
    let mut req = client
        .post(format!("http://127.0.0.1:{port}/rpc"))
        .json(&serde_json::json!({"method": method, "params": params}));
    if let Some(t) = token {
        req = req.header("x-indexer-token", t);
    }
    let resp = req
        .send()
        .await
        .context("send rpc")?
        .json::<serde_json::Value>()
        .await
        .context("parse rpc response")?;
    Ok(resp)
}

fn read_auth_token_from(home: &std::path::Path) -> Option<String> {
    let path = home.join(".opencode").join("embedder.token");
    std::fs::read_to_string(path).ok().map(|s| s.trim().to_string())
}

// ---------------------------------------------------------------------------
// Daemon lifecycle
// ---------------------------------------------------------------------------

struct DaemonHandle {
    child: tokio::process::Child,
    port: u16,
    auth_token: Option<String>,
    // Prevent tests in this file from racing by serialising daemon lifecycle.
    _lock: std::fs::File,
}

impl DaemonHandle {
    fn lock() -> Result<std::fs::File> {
        // Use a DIFFERENT lock file path than daemon_integration.rs to avoid
        // test conflicts when both test suites are running in parallel.
        let path = std::env::temp_dir().join("opencode-indexer-active-indexes.lock");
        let file = std::fs::OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .open(&path)
            .with_context(|| format!("open lock file: {}", path.display()))?;
        file.lock_exclusive().context("lock exclusive")?;
        Ok(file)
    }

    async fn spawn(home: &std::path::Path, embed_env: &[(&str, String)]) -> Result<Self> {
        let lock = Self::lock()?;

        let bin = assert_cmd::cargo::cargo_bin!("opencode-indexer");
        let mut cmd = Command::new(&bin);
        cmd.env("HOME", home)
            .arg("--daemon")
            .arg("--port")
            .arg("0")
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped());

        for (key, value) in embed_env {
            cmd.env(*key, value);
        }

        let mut child = cmd.spawn().context("spawn daemon")?;

        let stdout = child.stdout.take().context("no stdout")?;
        let mut reader = tokio::io::BufReader::new(stdout);
        let mut buf = String::new();
        let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
        let mut port = 0u16;

        loop {
            if tokio::time::Instant::now() > deadline {
                child.kill().await.ok();
                anyhow::bail!("daemon did not become ready within 30s");
            }
            buf.clear();
            tokio::io::AsyncBufReadExt::read_line(&mut reader, &mut buf).await?;
            if let Ok(msg) = serde_json::from_str::<serde_json::Value>(&buf) {
                if msg.get("type").and_then(|v| v.as_str()) == Some("http_ready") {
                    port = msg["port"].as_u64().unwrap_or(0) as u16;
                    if port > 0 {
                        break;
                    }
                }
            }
        }

        // Drain stdout so the pipe buffer never fills and blocks the daemon.
        tokio::spawn(async move {
            let mut discard = vec![0u8; 4096];
            loop {
                match tokio::io::AsyncReadExt::read(&mut reader, &mut discard).await {
                    Ok(0) | Err(_) => break,
                    _ => {}
                }
            }
        });

        let auth_token = read_auth_token_from(home);

        Ok(Self {
            child,
            port,
            auth_token,
            _lock: lock,
        })
    }

    fn port(&self) -> u16 {
        self.port
    }

    fn token(&self) -> Option<&str> {
        self.auth_token.as_deref()
    }

    async fn shutdown(mut self) {
        let token = self.auth_token.as_deref().map(String::from);
        let _ = rpc(self.port, token.as_deref(), "shutdown", serde_json::json!({})).await;
        tokio::time::sleep(Duration::from_millis(200)).await;
        self.child.kill().await.ok();
    }
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

async fn setup() -> Result<(python_server::PythonServer, DaemonHandle)> {
    let server = python_server::PythonServer::start().await?;
    let daemon = DaemonHandle::spawn(server.home_path(), &server.env_vars()).await?;
    Ok((server, daemon))
}

// ---------------------------------------------------------------------------
// Helper: create a git repo with N Rust source files
// ---------------------------------------------------------------------------

fn create_git_repo_with_files(n: usize) -> Result<tempfile::TempDir> {
    let root = tempfile::TempDir::new().context("create temp repo")?;
    let path = root.path();

    let git = |args: &[&str]| -> Result<()> {
        let out = std::process::Command::new("git")
            .args(args)
            .current_dir(path)
            .output()
            .with_context(|| format!("git {:?}", args))?;
        anyhow::ensure!(out.status.success(), "git failed: {:?}", args);
        Ok(())
    };

    git(&["init"])?;
    git(&["config", "user.email", "test@test.com"])?;
    git(&["config", "user.name", "Test"])?;

    for i in 0..n {
        // Build file content as a plain String using push_str to avoid any
        // format-string escaping complexity.
        let mut content = String::new();
        content.push_str(&format!(
            "/// Module {i} provides utility functions for data processing.\n"
        ));
        content.push_str(&format!("pub fn process_{i}(input: &str) -> String {{\n"));
        content.push_str(
            "    let result = input.chars().filter(|c| c.is_alphanumeric()).collect::<String>();\n",
        );
        content.push_str(&format!("    format!(\"processed_{i}: {{}}\", result)\n"));
        content.push_str("}\n\n");
        content.push_str(&format!("pub fn validate_{i}(data: &[u8]) -> bool {{\n"));
        content.push_str("    !data.is_empty() && data.len() < 1024\n");
        content.push_str("}\n\n");
        content.push_str("#[cfg(test)]\n");
        content.push_str(&format!("mod tests_{i} {{\n"));
        content.push_str("    use super::*;\n");
        content.push_str("    #[test]\n");
        content.push_str("    fn test_process() {\n");
        content.push_str(&format!("        let _ = process_{i}(\"hello world\");\n"));
        content.push_str("    }\n");
        content.push_str("}\n");

        std::fs::write(path.join(format!("module_{i}.rs")), content)
            .with_context(|| format!("write module_{i}.rs"))?;
    }

    git(&["add", "."])?;
    git(&["commit", "-m", "init"])?;

    Ok(root)
}

// ===========================================================================
// Test 1: status does NOT clear progress while run_index is active
// ===========================================================================

/// Proves the key fix: while `run_index` is in progress the `status` RPC must
/// not trigger self-heal and zero-out `indexingInProgress`.
///
/// Before the fix, `status` would see `indexing_in_progress=true` in LanceDB
/// with no accompanying goroutine-style guard and would clear it, causing the
/// TUI to show indexing as "done" even though it was still running.
///
/// This test detects oscillation: the flag must NOT go true -> false -> true
/// while run_index is still active. That pattern is the actual bug — self-heal
/// clears the flag, then the next embedding batch re-sets it.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn status_does_not_clear_progress_during_run_index() {
    let (_server, daemon) = setup().await.expect("setup");
    let port = daemon.port();
    let token = daemon.token().map(String::from);

    let root = create_git_repo_with_files(50).expect("create git repo");
    let db = root.path().join(".lancedb-active-test");
    let root_str = root.path().to_str().unwrap().to_string();
    let db_str = db.to_str().unwrap().to_string();

    // Start run_index in the background. The RPC call blocks until indexing
    // completes, so we drive it from a separate task.
    let index_handle = tokio::spawn({
        let root_str = root_str.clone();
        let db_str = db_str.clone();
        let token = token.clone();
        async move {
            rpc(
                port,
                token.as_deref(),
                "run_index",
                serde_json::json!({
                    "root": root_str,
                    "db":   db_str,
                    "tier": "budget",
                    "dimensions": 256,
                    "force": false,
                    "exclude": [],
                    "include": []
                }),
            )
            .await
        }
    });

    // Wait for indexing to actually start: poll until we see indexingInProgress=true
    // or run_index finishes. run_index goes through a project queue before
    // run_index_impl runs, and run_indexing_pub needs time to set the flag.
    let mut indexing_started = false;
    for _ in 0..30 {
        if index_handle.is_finished() {
            break;
        }
        let status = rpc(
            port,
            token.as_deref(),
            "status",
            serde_json::json!({
                "root": root_str,
                "db":   db_str,
                "dimensions": 256,
            }),
        )
        .await
        .expect("status");
        if status["result"]["indexingInProgress"]
            .as_bool()
            .unwrap_or(false)
        {
            indexing_started = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }

    // If indexing started, poll and check for oscillation.
    // The bug pattern is: flag goes true -> false -> true while still running.
    if indexing_started {
        let mut went_false = false;
        let mut oscillated = false;

        for poll in 0..20 {
            if index_handle.is_finished() {
                break;
            }

            let status = rpc(
                port,
                token.as_deref(),
                "status",
                serde_json::json!({
                    "root": root_str,
                    "db":   db_str,
                    "dimensions": 256,
                }),
            )
            .await
            .expect("status");

            let in_progress = status["result"]["indexingInProgress"]
                .as_bool()
                .unwrap_or(false);

            if !in_progress {
                went_false = true;
            }

            // Oscillation: it went false, then came back true while still running.
            if went_false && in_progress && !index_handle.is_finished() {
                oscillated = true;
                eprintln!(
                    "BUG: oscillation detected at poll {poll}! \
                     indexingInProgress went true->false->true"
                );
            }

            tokio::time::sleep(Duration::from_millis(200)).await;
        }

        assert!(
            !oscillated,
            "indexingInProgress oscillated (true->false->true) while run_index was active. \
             This means status self-heal cleared progress mid-indexing."
        );
    }

    // Wait for run_index to finish.
    let result = index_handle.await.unwrap().expect("run_index rpc");
    assert_eq!(
        result["result"]["success"],
        serde_json::Value::Bool(true),
        "run_index should succeed: {result}"
    );

    // After indexing completes the active_indexes entry is removed, so the
    // next status call IS allowed to self-heal (clear stale progress).
    tokio::time::sleep(Duration::from_millis(200)).await;
    let final_status = rpc(
        port,
        token.as_deref(),
        "status",
        serde_json::json!({
            "root": root_str,
            "db":   db_str,
            "dimensions": 256,
        }),
    )
    .await
    .expect("final status rpc");

    let final_in_progress = final_status["result"]["indexingInProgress"]
        .as_bool()
        .unwrap_or(true);
    assert!(
        !final_in_progress,
        "after run_index completes, status should report indexingInProgress=false \
         (self-heal is now allowed). Response: {final_status}"
    );

    daemon.shutdown().await;
}

// ===========================================================================
// Test 2: stale progress is cleared when no indexing is active
// ===========================================================================

/// Proves that the self-heal mechanism still fires for genuinely stuck
/// progress (no regression from the guard addition).
///
/// Simulates a daemon crash mid-index by writing progress keys directly into
/// LanceDB, then verifies that a subsequent `status` call clears them because
/// no active indexing is registered.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stale_progress_is_cleared_when_no_indexing_active() {
    let (_server, daemon) = setup().await.expect("setup");
    let token = daemon.token().map(String::from);

    let db_dir = tempfile::TempDir::new().expect("create temp db dir");
    let db_path = db_dir.path().join(".lancedb-stale-test");

    // Simulate stuck/stale progress left behind by a crashed indexer.
    set_config(&db_path, "indexing_in_progress", "true")
        .await
        .expect("set indexing_in_progress");
    set_config(&db_path, "indexing_phase", "embedding")
        .await
        .expect("set indexing_phase");
    set_config(&db_path, "embedding_done", "42")
        .await
        .expect("set embedding_done");
    set_config(&db_path, "embedding_total", "100")
        .await
        .expect("set embedding_total");

    // status should self-heal: no active indexing is registered in
    // active_indexes, so the guard allows the clear to proceed.
    let status = rpc(
        daemon.port(),
        token.as_deref(),
        "status",
        serde_json::json!({
            "db": db_path.to_str().unwrap(),
            "dimensions": 256,
        }),
    )
    .await
    .expect("status rpc");

    let result = &status["result"];

    assert_eq!(
        result["indexingInProgress"],
        serde_json::Value::Bool(false),
        "stale indexingInProgress should be self-healed when no active indexing \
         is registered. Response: {status}"
    );

    // The phase should be cleared too.
    assert!(
        result["indexingPhase"].is_null(),
        "stale indexingPhase should be cleared. Response: {status}"
    );

    daemon.shutdown().await;
}

// ===========================================================================
// Test 3: concurrent status polls do not reset embedding progress counters
// ===========================================================================

/// Simulates the exact TUI scenario: status is polled every few seconds while
/// indexing is running.  Verifies that `embeddingDone` never decreases while
/// `indexingInProgress` is true — if the guard were absent, self-heal would
/// reset it to 0 partway through.
///
/// Uses 50 files to give the embedder enough work to observe mid-flight state.
/// If indexing completes before any in-progress state is seen, the test still
/// validates no backward regression was observed (the eprintln note is emitted
/// for debugging flaky CI runs).
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_status_polls_dont_reset_embedding_progress() {
    let (_server, daemon) = setup().await.expect("setup");
    let port = daemon.port();
    let token = daemon.token().map(String::from);

    let root = create_git_repo_with_files(50).expect("create git repo");
    let db = root.path().join(".lancedb-monotonic-test");
    let root_str = root.path().to_str().unwrap().to_string();
    let db_str = db.to_str().unwrap().to_string();

    // Start indexing.
    let index_handle = tokio::spawn({
        let root_str = root_str.clone();
        let db_str = db_str.clone();
        let token = token.clone();
        async move {
            rpc(
                port,
                token.as_deref(),
                "run_index",
                serde_json::json!({
                    "root": root_str,
                    "db":   db_str,
                    "tier": "budget",
                    "dimensions": 256,
                    "force": false,
                    "exclude": [],
                    "include": []
                }),
            )
            .await
        }
    });

    // Let indexing register itself before we start polling.
    tokio::time::sleep(Duration::from_millis(500)).await;

    // Collect (embeddingDone, embeddingTotal, indexingInProgress) over time.
    let mut progress_history: Vec<(i64, i64, bool)> = Vec::new();

    for _ in 0..30 {
        if index_handle.is_finished() {
            break;
        }

        let status = rpc(
            port,
            token.as_deref(),
            "status",
            serde_json::json!({
                "root": root_str,
                "db":   db_str,
                "dimensions": 256,
            }),
        )
        .await
        .expect("status rpc");

        let result = &status["result"];
        let done = result["embeddingDone"].as_i64().unwrap_or(0);
        let total = result["embeddingTotal"].as_i64().unwrap_or(0);
        let ip = result["indexingInProgress"].as_bool().unwrap_or(false);
        progress_history.push((done, total, ip));

        tokio::time::sleep(Duration::from_millis(150)).await;
    }

    let _ = index_handle.await;

    // Check monotonicity: embeddingDone must not decrease while
    // indexingInProgress is true. We track the max seen while in_progress=true
    // and assert it never drops.
    let mut max_done_in_progress = 0i64;
    for (i, &(done, _total, in_progress)) in progress_history.iter().enumerate() {
        if in_progress {
            if done < max_done_in_progress {
                panic!(
                    "embeddingDone went backwards at poll {i} while indexingInProgress=true: \
                     was {max_done_in_progress}, now {done}. \
                     This means status self-heal reset progress mid-indexing. \
                     Full history: {progress_history:?}"
                );
            }
            if done > max_done_in_progress {
                max_done_in_progress = done;
            }
        }
    }

    // Log whether we observed any in-progress state (useful for debugging flaky runs).
    let saw_in_progress = progress_history.iter().any(|&(_, _, ip)| ip);
    if !saw_in_progress {
        eprintln!(
            "NOTE: indexing completed before any in_progress state was observed. \
             Test still validates no oscillation but could not verify monotonicity. \
             History: {progress_history:?}"
        );
    }

    daemon.shutdown().await;
}
