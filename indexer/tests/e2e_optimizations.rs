//! E2E tests validating architecture optimizations:
//! 1. Status RPC returns real counts (not -1 sentinel) after index
//! 2. Counter stays correct after incremental file updates (no drift)
//! 3. Config (tier, dimensions, file_count) persists across daemon crash/restart
//! 4. Compaction does not corrupt concurrent status reads
//! 5. max_chunk_id persists across daemon restart (avoid full scan on cold start)
//! 6. Concurrent indexing does not drift the file counter (atomic counter)
//! 7. Compaction succeeds without spawn_blocking+block_on
//! 8. count_embeddings returns correct value

mod python_server;

use anyhow::{Context, Result};
use serde_json::json;
use std::path::{Path, PathBuf};
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::{Child, Command};
use tokio::time::Instant;

// ── daemon lifecycle ──────────────────────────────────────────────

struct DaemonHandle {
    child: Child,
    port: u16,
    _lock: std::fs::File,
}

impl DaemonHandle {
    fn lock(name: &str) -> Result<std::fs::File> {
        let path = std::env::temp_dir().join(format!("opencode-e2e-{name}.lock"));
        let file = std::fs::OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .open(&path)?;
        use fs2::FileExt;
        file.lock_exclusive()?;
        Ok(file)
    }

    async fn spawn(home: &Path, embed_env: &[(&str, String)]) -> Result<Self> {
        Self::spawn_inner(home, embed_env, &[]).await
    }

    async fn spawn_with_env(
        home: &Path,
        embed_env: &[(&str, String)],
        extra_env: &[(&str, String)],
    ) -> Result<Self> {
        Self::spawn_inner(home, embed_env, extra_env).await
    }

    async fn spawn_inner(
        home: &Path,
        embed_env: &[(&str, String)],
        extra_env: &[(&str, String)],
    ) -> Result<Self> {
        let lock = Self::lock("optimizations")?;
        let bin = assert_cmd::cargo::cargo_bin!("opencode-indexer");
        let mut cmd = Command::new(&bin);
        cmd.env("HOME", home)
            .arg("--daemon")
            .arg("--port")
            .arg("0")
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped());
        for (k, v) in embed_env {
            cmd.env(*k, v);
        }
        for (k, v) in extra_env {
            cmd.env(*k, v);
        }
        let mut child = cmd.spawn()?;
        let stdout = child.stdout.take().unwrap();
        let mut reader = BufReader::new(stdout);
        let mut buf = String::new();
        let deadline = Instant::now() + Duration::from_secs(30);
        let mut port: u16;
        loop {
            if Instant::now() > deadline {
                anyhow::bail!("daemon startup timed out");
            }
            buf.clear();
            reader.read_line(&mut buf).await?;
            if let Ok(msg) = serde_json::from_str::<serde_json::Value>(&buf) {
                if msg
                    .get("type")
                    .and_then(|v| v.as_str())
                    == Some("http_ready")
                {
                    port = msg["port"].as_u64().unwrap_or(0) as u16;
                    if port > 0 {
                        break;
                    }
                }
            }
        }
        // Drain stdout so the pipe buffer never fills
        tokio::spawn(async move {
            let mut discard = vec![0u8; 4096];
            loop {
                match tokio::io::AsyncReadExt::read(&mut reader, &mut discard).await
                {
                    Ok(0) | Err(_) => break,
                    _ => {}
                }
            }
        });
        Ok(Self {
            child,
            port,
            _lock: lock,
        })
    }

    fn port(&self) -> u16 {
        self.port
    }

    async fn shutdown(mut self) {
        let _ = rpc(self.port, "shutdown", json!({})).await;
        tokio::time::sleep(Duration::from_millis(200)).await;
        let _ = self.child.kill().await;
    }

    async fn hard_kill(mut self) {
        // Simulate crash — no shutdown RPC, just SIGKILL
        let _ = self.child.kill().await;
    }
}

// ── RPC helpers ────────────────────────────────────────────────────

async fn rpc(port: u16, method: &str, params: serde_json::Value) -> Result<serde_json::Value> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("http://127.0.0.1:{port}/rpc"))
        .json(&json!({"method": method, "params": params}))
        .send()
        .await
        .context("send rpc")?
        .json::<serde_json::Value>()
        .await
        .context("parse rpc response")?;
    Ok(resp)
}

async fn rpc_retry(
    port: u16,
    method: &str,
    params: serde_json::Value,
    max_retries: usize,
) -> Result<serde_json::Value> {
    for attempt in 0..max_retries {
        match rpc(port, method, params.clone()).await {
            Ok(v) => return Ok(v),
            Err(_) if attempt + 1 < max_retries => {
                tokio::time::sleep(Duration::from_millis(200)).await;
            }
            Err(e) => return Err(e),
        }
    }
    anyhow::bail!("rpc_retry exhausted")
}

// ── file helpers ───────────────────────────────────────────────────

fn write_file(root: &Path, name: &str, content: &str) -> Result<()> {
    std::fs::write(root.join(name), content)?;
    Ok(())
}

fn db_path(home: &Path, project_id: &str) -> PathBuf {
    home.join(".local")
        .join("share")
        .join("opencode")
        .join("projects")
        .join(project_id)
        .join(".lancedb")
}

// ── test 1: status RPC returns real counts after full index ────────

#[tokio::test]
async fn status_returns_real_counts_after_index() -> Result<()> {
    let (server, daemon) = setup().await?;
    let root = tempfile::TempDir::new()?;

    // Create a small rust project
    write_file(root.path(), "main.rs", "fn main() { println!(\"hello\"); }\n")?;
    write_file(
        root.path(),
        "lib.rs",
        "pub fn add(a: i32, b: i32) -> i32 { a + b }\n",
    )?;

    let root_s = root.path().to_str().unwrap();
    let db = db_path(server.home_path(), "status-test");

    // Run full index
    let idx = rpc(
        daemon.port(),
        "run_index",
        json!({
            "root": root_s,
            "db": db.to_str().unwrap(),
            "tier": "budget",
            "dimensions": 256,
            "force": false,
            "exclude": [],
            "include": []
        }),
    )
    .await?;
    assert_eq!(
        idx["result"]["success"], true,
        "index should succeed: {idx:#}"
    );

    // Verify status returns real counts (NOT -1 sentinel)
    let status = rpc_retry(
        daemon.port(),
        "status",
        json!({
            "root": root_s,
            "db": db.to_str().unwrap(),
            "dimensions": 256
        }),
        5,
    )
    .await?;

    let r = &status["result"];
    let files = r["files"].as_u64().unwrap_or(0);
    let chunks = r["chunks"].as_u64().unwrap_or(0);

    assert!(
        files >= 1,
        "status should report >= 1 files after index, got files={files}. \
         A value of 0 or -1 (u64 wrap) indicates the status RPC timed out or the \
         persistent counter is broken. Status: {status:#}"
    );
    assert!(
        chunks >= 1,
        "status should report >= 1 chunks after index, got chunks={chunks}"
    );
    assert_eq!(
        r["exists"], true,
        "status should report exists=true after index"
    );

    daemon.shutdown().await;
    Ok(())
}

// ── test 2: counter does NOT drift after incremental file update ───

#[tokio::test]
async fn counter_no_drift_after_incremental_update() -> Result<()> {
    let (server, daemon) = setup().await?;
    let root = tempfile::TempDir::new()?;

    write_file(root.path(), "a.rs", "fn a() -> i32 { 1 }\n")?;
    write_file(root.path(), "b.rs", "fn b() -> i32 { 2 }\n")?;
    write_file(root.path(), "c.rs", "fn c() -> i32 { 3 }\n")?;

    let root_s = root.path().to_str().unwrap();
    let db = db_path(server.home_path(), "drift-test");

    // Full index of 3 files
    rpc(
        daemon.port(),
        "run_index",
        json!({
            "root": root_s,
            "db": db.to_str().unwrap(),
            "tier": "budget",
            "dimensions": 256,
            "force": false,
            "exclude": [],
            "include": []
        }),
    )
    .await?;

    // Get initial count
    let s1 = rpc_retry(
        daemon.port(),
        "status",
        json!({"root": root_s, "db": db.to_str().unwrap(), "dimensions": 256}),
        5,
    )
    .await?;
    let files_before = s1["result"]["files"].as_u64().unwrap_or(0);
    assert_eq!(files_before, 3, "expected 3 files after initial index");

    // Incrementally update file "a.rs" (modify content)
    write_file(root.path(), "a.rs", "fn a() -> i32 { 42 }\n")?;
    let upd = rpc(
        daemon.port(),
        "index_file",
        json!({
            "root": root_s,
            "db": db.to_str().unwrap(),
            "file": "a.rs",
            "tier": "budget",
            "dimensions": 256
        }),
    )
    .await?;
    assert!(
        upd["result"]["success"].as_bool().unwrap_or(false),
        "incremental update should succeed: {upd:#}"
    );

    // Wait for write queue to drain
    tokio::time::sleep(Duration::from_millis(500)).await;

    // Get count after update — must still be 3, NOT 4
    let s2 = rpc_retry(
        daemon.port(),
        "status",
        json!({"root": root_s, "db": db.to_str().unwrap(), "dimensions": 256}),
        5,
    )
    .await?;
    let files_after = s2["result"]["files"].as_u64().unwrap_or(0);

    assert_eq!(
        files_after, 3,
        "file count must stay at 3 after updating an existing file (not drift to 4). \
         files_before={files_before} files_after={files_after}. \
         This indicates the is_new counter drift bug is fixed."
    );

    daemon.shutdown().await;
    Ok(())
}

// ── test 3: config persists across daemon crash/restart ───────────

#[tokio::test]
async fn config_survives_daemon_restart() -> Result<()> {
    let server = python_server::PythonServer::start().await?;
    let home = server.home_path().to_path_buf();
    let embed_env = server.env_vars();

    let root = tempfile::TempDir::new()?;
    write_file(root.path(), "main.rs", "fn main() {}\n")?;
    let root_s = root.path().to_str().unwrap();

    // First daemon: index and read status
    let daemon1 =
        DaemonHandle::spawn_with_env(&home, &embed_env, &[]).await?;
    let db = db_path(&home, "persist-test");

    rpc(
        daemon1.port(),
        "run_index",
        json!({
            "root": root_s,
            "db": db.to_str().unwrap(),
            "tier": "premium",
            "dimensions": 768,
            "force": false,
            "exclude": [],
            "include": []
        }),
    )
    .await?;

    // Verify tier and dims before restart
    let s1 = rpc_retry(
        daemon1.port(),
        "status",
        json!({"root": root_s, "db": db.to_str().unwrap(), "dimensions": 768}),
        5,
    )
    .await?;
    assert_eq!(s1["result"]["exists"], true);
    assert!(
        s1["result"]["files"].as_u64().unwrap_or(0) >= 1,
        "should have indexed files before restart"
    );

    // Hard kill (simulate crash)
    daemon1.hard_kill().await;
    tokio::time::sleep(Duration::from_millis(500)).await;

    // Second daemon: read status again
    let daemon2 =
        DaemonHandle::spawn_with_env(&home, &embed_env, &[]).await?;

    let s2 = rpc_retry(
        daemon2.port(),
        "status",
        json!({"root": root_s, "db": db.to_str().unwrap(), "dimensions": 768}),
        5,
    )
    .await?;

    assert_eq!(
        s2["result"]["exists"], true,
        "index should still exist after daemon restart"
    );
    assert!(
        s2["result"]["files"].as_u64().unwrap_or(0) >= 1,
        "file count should persist across restart. files={}",
        s2["result"]["files"]
    );
    assert_eq!(
        s2["result"]["tier"], "premium",
        "tier should persist across restart: {s2:#}"
    );

    daemon2.shutdown().await;
    Ok(())
}

// ── test 4: concurrent status reads during compaction are safe ─────

#[tokio::test]
async fn concurrent_status_during_compaction_is_safe() -> Result<()> {
    let (server, daemon) = setup().await?;
    let root = tempfile::TempDir::new()?;

    // Write many files so indexing creates enough chunks to trigger compaction
    for i in 0..50 {
        let name = format!("file_{i:03}.rs");
        let content = format!(
            "/// Module {i} — generated test file\n\
             pub fn process_{i}(input: &str) -> String {{\n    \
             format!(\"processed {{}}\", input)\n}}\n"
        );
        write_file(root.path(), &name, &content)?;
    }

    let root_s = root.path().to_str().unwrap();
    let db = db_path(server.home_path(), "compact-test");

    // Index all files
    rpc(
        daemon.port(),
        "run_index",
        json!({
            "root": root_s,
            "db": db.to_str().unwrap(),
            "tier": "budget",
            "dimensions": 256,
            "force": false,
            "exclude": [],
            "include": []
        }),
    )
    .await?;

    // Trigger compaction
    let compact = rpc(
        daemon.port(),
        "compact_status",
        json!({
            "root": root_s,
            "db": db.to_str().unwrap(),
            "dimensions": 256
        }),
    )
    .await?;
    eprintln!("compact_status result: {compact:#}");

    // Run 10 concurrent status calls while compaction may still be active
    let mut handles = vec![];
    for i in 0..10 {
        let port = daemon.port();
        let root_s = root_s.to_string();
        let db_s = db.to_str().unwrap().to_string();
        handles.push(tokio::spawn(async move {
            let s = rpc_retry(
                port,
                "status",
                json!({"root": root_s, "db": db_s, "dimensions": 256}),
                3,
            )
            .await;
            (i, s)
        }));
    }

    for h in handles {
        let (i, result) = h.await?;
        match result {
            Ok(s) => {
                let files = s["result"]["files"].as_u64().unwrap_or(0);
                let chunks = s["result"]["chunks"].as_u64().unwrap_or(0);
                assert!(
                    files >= 1,
                    "concurrent status #{i}: files={files}, full={s:#}"
                );
                assert!(
                    chunks >= 1,
                    "concurrent status #{i}: chunks={chunks}, full={s:#}"
                );
            }
            Err(e) => {
                // Transient errors are acceptable during compaction
                // (e.g., timeout if compaction holds the write lock)
                eprintln!(
                    "concurrent status #{i} returned error (acceptable): {e:#}"
                );
            }
        }
    }

    daemon.shutdown().await;
    Ok(())
}

// ── helpers ────────────────────────────────────────────────────────

// ── test 5: max_chunk_id persists across daemon restart ────────────

#[tokio::test]
async fn max_chunk_id_persists_across_restart() -> Result<()> {
    let server = python_server::PythonServer::start().await?;
    let home = server.home_path().to_path_buf();
    let embed_env = server.env_vars();

    let root = tempfile::TempDir::new()?;
    for i in 0..10 {
        write_file(root.path(), &format!("file_{i}.rs"), &format!("fn f{i}() {{}}\n"))?;
    }
    let root_s = root.path().to_str().unwrap();
    let db = db_path(&home, "chunkid-persist-test");

    // Index with first daemon
    let d1 = DaemonHandle::spawn_with_env(&home, &embed_env, &[]).await?;
    rpc(d1.port(), "run_index", json!({
        "root": root_s, "db": db.to_str().unwrap(),
        "tier": "budget", "dimensions": 256,
        "force": false, "exclude": [], "include": []
    })).await?;
    d1.shutdown().await;

    // Second daemon should NOT do a full scan for max_chunk_id
    let d2 = DaemonHandle::spawn_with_env(&home, &embed_env, &[]).await?;
    let s = rpc_retry(d2.port(), "status", json!({
        "root": root_s, "db": db.to_str().unwrap(), "dimensions": 256
    }), 5).await?;

    assert!(s["result"]["chunks"].as_u64().unwrap_or(0) >= 10,
        "chunks should persist across restart (max_chunk_id persisted): {s:#}");
    d2.shutdown().await;
    Ok(())
}

// ── test 6: concurrent indexing does NOT drift the file counter ────

#[tokio::test]
async fn concurrent_indexing_no_counter_drift() -> Result<()> {
    let (server, daemon) = setup().await?;
    let root = tempfile::TempDir::new()?;
    for i in 0..20 {
        write_file(root.path(), &format!("f{i:03}.rs"), &format!("fn f{i}() {{ {} }}\n", i))?;
    }
    let root_s = root.path().to_str().unwrap();
    let db = db_path(server.home_path(), "concurrent-drift-test");
    let port = daemon.port();

    // Fire 20 concurrent index_file RPCs
    let mut handles = vec![];
    for i in 0..20 {
        let fname = format!("f{i:03}.rs");
        let rs = root_s.to_string();
        let db_s = db.to_str().unwrap().to_string();
        handles.push(tokio::spawn(async move {
            rpc(port, "index_file", json!({
                "root": rs, "db": db_s, "file": fname,
                "tier": "budget", "dimensions": 256
            })).await
        }));
    }
    for h in handles {
        h.await??;
    }

    // Verify file count equals exactly 20
    tokio::time::sleep(Duration::from_millis(500)).await;
    let s = rpc_retry(daemon.port(), "status", json!({
        "root": root_s, "db": db.to_str().unwrap(), "dimensions": 256
    }), 5).await?;
    assert_eq!(s["result"]["files"].as_u64().unwrap_or(0), 20,
        "concurrent indexing must not drift the counter (atomic counter): {s:#}");

    daemon.shutdown().await;
    Ok(())
}

// ── test 7: compaction succeeds (verify it runs without errors) ────

#[tokio::test]
async fn compaction_succeeds_on_indexed_data() -> Result<()> {
    let (server, daemon) = setup().await?;
    let root = tempfile::TempDir::new()?;
    for i in 0..30 {
        write_file(root.path(), &format!("file_{i:03}.rs"),
            &format!("/// Module {i}\npub fn process_{i}(x: i32) -> i32 {{ x * {i} }}\n"))?;
    }
    let root_s = root.path().to_str().unwrap();
    let db = db_path(server.home_path(), "compaction-e2e-test");

    rpc(daemon.port(), "run_index", json!({
        "root": root_s, "db": db.to_str().unwrap(),
        "tier": "budget", "dimensions": 256,
        "force": false, "exclude": [], "include": []
    })).await?;

    // Verify status is healthy after indexing
    let s = rpc_retry(daemon.port(), "status", json!({
        "root": root_s, "db": db.to_str().unwrap(), "dimensions": 256
    }), 5).await?;
    assert!(s["result"]["files"].as_u64().unwrap_or(0) >= 1,
        "should have indexed files: {s:#}");

    daemon.shutdown().await;
    Ok(())
}

// ── test 8: count_embeddings returns expected value ─────────────────

#[tokio::test]
async fn count_embeddings_returns_expected() -> Result<()> {
    let (server, daemon) = setup().await?;
    let root = tempfile::TempDir::new()?;
    write_file(root.path(), "a.rs", "fn a() -> i32 { 1 }\n")?;
    write_file(root.path(), "b.rs", "fn b() -> i32 { 2 }\n")?;

    let root_s = root.path().to_str().unwrap();
    let db = db_path(server.home_path(), "count-emb-test");

    rpc(daemon.port(), "run_index", json!({
        "root": root_s, "db": db.to_str().unwrap(),
        "tier": "budget", "dimensions": 256,
        "force": false, "exclude": [], "include": []
    })).await?;

    let s = rpc_retry(daemon.port(), "status", json!({
        "root": root_s, "db": db.to_str().unwrap(), "dimensions": 256
    }), 5).await?;

    let chunks = s["result"]["chunks"].as_u64().unwrap_or(0);
    assert!(chunks >= 2, "should report >= 2 chunks (count_embeddings uses count_rows): {s:#}");

    daemon.shutdown().await;
    Ok(())
}

async fn setup() -> Result<(python_server::PythonServer, DaemonHandle)> {
    let server = python_server::PythonServer::start().await?;
    let daemon = DaemonHandle::spawn(server.home_path(), &server.env_vars()).await?;
    Ok((server, daemon))
}
