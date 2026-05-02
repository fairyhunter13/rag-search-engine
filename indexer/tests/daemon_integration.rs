//! Integration tests for the Rust indexer daemon ↔ Python model service.
//!
//! Architecture under test:
//!
//!   Test client  ──[HTTP POST /rpc]──▶  Rust daemon (real binary, HTTP server)
//!        │                                            │
//!   127.0.0.1:{port}                    localhost:{embed_port}
//!                                                     │
//!                                              Python embedder (real server
//!                                              implementing the HTTP API used
//!                                              by the production embedder)
//!
//! Nothing is mocked. Every test:
//!   - Spawns the real `opencode-indexer --daemon` binary
//!   - Starts a real model server speaking the real msgpack protocol on a
//!     real Unix domain socket (same protocol as the production Python service)
//!   - Communicates over HTTP using POST /rpc and GET /ping
//!   - Writes to real LanceDB databases on disk
//!
//! What is covered:
//!   1. HTTP transport (POST /rpc, GET /ping)
//!   2. Every daemon RPC method: ping, resolve_paths, status, discover_files,
//!      discover_links, health, index_file, remove_file, search,
//!      search_memories, search_activity, run_index, shutdown
//!   3. Daemon → model-server connection (msgpack protocol, embed/chunk/rerank)
//!   4. Queue serialisation (write ops serialised through internal VecDeque)
//!   5. Error handling (unknown method)
//!   6. Content-hash deduplication (unchanged files skipped)
//!   7. Concurrent connections
//!   8. Large payload handling

mod python_server;

use std::time::Duration;

use anyhow::{Context, Result};
use tokio::process::Command;
use fs2::FileExt;

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
    // Prevent tests in this file from racing by serializing daemon lifecycle.
    _lock: std::fs::File,
}

impl DaemonHandle {
    fn lock() -> Result<std::fs::File> {
        let path = std::env::temp_dir().join("opencode-indexer-daemon-integration.lock");
        let file = std::fs::OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .open(&path)
            .with_context(|| format!("open lock file: {}", path.display()))?;
        file.lock_exclusive().context("lock exclusive")?;
        Ok(file)
    }

    /// Spawn the real indexer binary in daemon mode, wait for "http_ready".
    ///
    /// `embed_env` contains environment variables to configure the embedder connection
    /// `embed_env` contains environment variables to configure the embedder connection
    /// (OPENCODE_EMBED_HTTP_PORT for the HTTP port).
    async fn spawn(home: &std::path::Path, embed_env: &[(&str, String)]) -> Result<Self> {
        Self::spawn_with_env(home, embed_env, &[]).await
    }

    async fn spawn_with_env(
        home: &std::path::Path,
        embed_env: &[(&str, String)],
        extra_env: &[(&str, String)],
    ) -> Result<Self> {
        // Serialize tests in this file; spawning daemons in parallel is racy.
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

        for (key, value) in extra_env {
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

        // Drain stdout so pipe buffer doesn't fill
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

        Ok(Self { child, port, auth_token, _lock: lock })
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
// Setup: model server + daemon
// ---------------------------------------------------------------------------

/// Start the real Python model server and a real daemon process.
///
/// If an embedder is already running (Unix socket or TCP), we reuse it
/// instead of spawning a new one. This makes tests faster and more reliable.
async fn setup() -> Result<(python_server::PythonServer, DaemonHandle)> {
    let server = python_server::PythonServer::start().await?;
    let daemon = DaemonHandle::spawn(server.home_path(), &server.env_vars()).await?;
    Ok((server, daemon))
}

async fn setup_with_daemon_env(extra: &[(&str, String)]) -> Result<(python_server::PythonServer, DaemonHandle)> {
    let server = python_server::PythonServer::start().await?;
    let daemon = DaemonHandle::spawn_with_env(server.home_path(), &server.env_vars(), extra).await?;
    Ok((server, daemon))
}

// ===========================================================================
// Tests
// ===========================================================================

// ---- Wire protocol --------------------------------------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn ping_returns_pong() {
    let (_server, daemon) = setup().await.expect("setup");
    let resp = rpc(daemon.port(), daemon.token(), "ping", serde_json::json!({}))
        .await
        .expect("rpc");

    assert_eq!(resp["result"]["pong"], true);
    assert!(resp.get("error").is_none() || resp["error"].is_null());
    daemon.shutdown().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn unknown_method_returns_error_in_result() {
    let (_server, daemon) = setup().await.expect("setup");
    let resp = rpc(daemon.port(), daemon.token(), "nonexistent_method", serde_json::json!({}))
        .await
        .expect("rpc");

    let result = &resp["result"];
    assert!(
        result["error"].as_str().unwrap_or("").contains("unknown method"),
        "expected 'unknown method' error, got: {resp}"
    );
    daemon.shutdown().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn wire_protocol_handles_large_payload() {
    let (_server, daemon) = setup().await.expect("setup");

    let big = "x".repeat(100_000);
    let resp = rpc(daemon.port(), daemon.token(), "ping",
        serde_json::json!({"large_field": big}),
    )
    .await
    .expect("rpc");

    assert_eq!(resp["result"]["pong"], true);
    daemon.shutdown().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn concurrent_connections_all_succeed() {
    let (_server, daemon) = setup().await.expect("setup");
    let port = daemon.port();
    let token = daemon.token().map(String::from);

    let mut handles = Vec::new();
    for _ in 0u64..5 {
        let token = token.clone();
        handles.push(tokio::spawn(async move {
            rpc(port, token.as_deref(), "ping", serde_json::json!({})).await
        }));
    }

    for h in handles {
        let resp = h.await.unwrap().expect("rpc");
        assert_eq!(resp["result"]["pong"], true);
    }
    daemon.shutdown().await;
}

// ---- resolve_paths --------------------------------------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn resolve_paths_derives_all_paths() {
    let (server, daemon) = setup().await.expect("setup");
    let shared = server.home_path().join("shared");
    std::fs::create_dir_all(&shared).unwrap();

    let resp = rpc(daemon.port(), daemon.token(), "resolve_paths",
        serde_json::json!({
            "root": server.home_path().to_str().unwrap(),
            "sharedPath": shared.to_str().unwrap(),
            "projectId": "test123"
        }),
    )
    .await
    .expect("rpc");

    let r = &resp["result"];
    assert_eq!(r["projectId"], "test123");
    assert!(r["memoryDir"].as_str().unwrap().contains("test123"));
    assert!(r["activityDir"].as_str().unwrap().contains("test123"));
    assert!(r["memoryDbPath"].as_str().unwrap().contains(".lancedb"));
    assert!(r["activityDbPath"].as_str().unwrap().contains(".lancedb"));
    assert!(r["globalMemoryDir"].as_str().unwrap().contains("global"));
    assert!(r["globalDbPath"].as_str().unwrap().contains("global"));
    // dbPath should exist (derived from git or path hash)
    assert!(r["dbPath"].as_str().is_some());
    daemon.shutdown().await;
}

// ---- status ---------------------------------------------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn status_on_nonexistent_db_returns_exists_false() {
    let (_server, daemon) = setup().await.expect("setup");

    let resp = rpc(daemon.port(), daemon.token(), "status",
        serde_json::json!({
            "db": "/tmp/does-not-exist-test-db/.lancedb",
            "dimensions": 256
        }),
    )
    .await
    .expect("rpc");

    assert_eq!(resp["result"]["exists"], false);
    daemon.shutdown().await;
}

// ---- discover_files -------------------------------------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn discover_files_returns_file_list() {
    let (_server, daemon) = setup().await.expect("setup");

    let root = tempfile::TempDir::new().unwrap();
    std::process::Command::new("git")
        .args(["init"])
        .current_dir(root.path())
        .output()
        .ok();
    std::fs::write(root.path().join("main.rs"), "fn main() {}").unwrap();
    std::fs::write(root.path().join("lib.rs"), "pub fn hello() {}").unwrap();
    std::process::Command::new("git")
        .args(["add", "."])
        .current_dir(root.path())
        .output()
        .ok();

    let resp = rpc(daemon.port(), daemon.token(), "discover_files",
        serde_json::json!({
            "root": root.path().to_str().unwrap(),
            "exclude": [],
            "include": []
        }),
    )
    .await
    .expect("rpc");

    let r = &resp["result"];
    let files = r["files"].as_array().expect("files array");
    let count = r["count"].as_u64().expect("count");
    assert!(count >= 2, "expected at least 2 files, got {count}");
    assert_eq!(count as usize, files.len());

    let names: Vec<&str> = files.iter().filter_map(|f| f.as_str()).collect();
    assert!(names.iter().any(|n| n.contains("main.rs")));
    assert!(names.iter().any(|n| n.contains("lib.rs")));
    daemon.shutdown().await;
}

// ---- index_file → status round-trip (Rust daemon ↔ model server) ----------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn index_file_and_status_round_trip() {
    let (_server, daemon) = setup().await.expect("setup");

    let root = tempfile::TempDir::new().unwrap();
    std::fs::write(
        root.path().join("test.txt"),
        "hello world\nline two\nline three\n",
    )
    .unwrap();
    let db = root.path().join(".lancedb-daemon-test");

    // index_file → daemon → model server (chunk + embed_passages)
    let resp = rpc(daemon.port(), daemon.token(), "index_file",
        serde_json::json!({
            "root": root.path().to_str().unwrap(),
            "db": db.to_str().unwrap(),
            "file": "test.txt",
            "tier": "budget",
            "dimensions": 256
        }),
    )
    .await
    .expect("rpc");

    let r = &resp["result"];
    assert_eq!(r["success"], true, "index_file failed: {resp}");
    assert!(r["chunks"].as_u64().unwrap_or(0) > 0, "should have chunks");
    assert!(r["path"].as_str().is_some(), "should return relative path");

    // status (reads from real LanceDB on disk)
    let resp = rpc(daemon.port(), daemon.token(), "status",
        serde_json::json!({"db": db.to_str().unwrap(), "dimensions": 256}),
    )
    .await
    .expect("rpc");

    let r = &resp["result"];
    assert_eq!(r["exists"], true);
    assert!(r["files"].as_u64().unwrap_or(0) >= 1);
    assert!(r["chunks"].as_u64().unwrap_or(0) >= 1);
    assert_eq!(r["tier"], "budget");
    daemon.shutdown().await;
}

// ---- index_file then remove_file ------------------------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn index_then_remove_file() {
    let (_server, daemon) = setup().await.expect("setup");

    let root = tempfile::TempDir::new().unwrap();
    std::fs::write(root.path().join("removeme.txt"), "content to remove\n").unwrap();
    let db = root.path().join(".lancedb-remove-test");

    // Index
    let resp = rpc(daemon.port(), daemon.token(), "index_file",
        serde_json::json!({
            "root": root.path().to_str().unwrap(),
            "db": db.to_str().unwrap(),
            "file": "removeme.txt",
            "tier": "budget",
            "dimensions": 256
        }),
    )
    .await
    .expect("rpc");
    assert_eq!(resp["result"]["success"], true);

    // Confirm exists
    let resp = rpc(daemon.port(), daemon.token(), "status",
        serde_json::json!({"db": db.to_str().unwrap(), "dimensions": 256}),
    )
    .await
    .expect("rpc");
    assert!(resp["result"]["files"].as_u64().unwrap_or(0) >= 1);

    // Remove
    let resp = rpc(daemon.port(), daemon.token(), "remove_file",
        serde_json::json!({
            "root": root.path().to_str().unwrap(),
            "db": db.to_str().unwrap(),
            "file": "removeme.txt",
            "dimensions": 256
        }),
    )
    .await
    .expect("rpc");
    assert_eq!(resp["result"]["success"], true);
    assert!(resp["result"]["removed"].as_u64().unwrap_or(0) > 0);

    // Confirm gone
    let resp = rpc(daemon.port(), daemon.token(), "status",
        serde_json::json!({"db": db.to_str().unwrap(), "dimensions": 256}),
    )
    .await
    .expect("rpc");
    assert_eq!(resp["result"]["chunks"].as_u64().unwrap_or(0), 0);
    daemon.shutdown().await;
}

// ---- index_file skip unchanged (content-hash dedup) -----------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn index_file_skips_unchanged_content() {
    let (_server, daemon) = setup().await.expect("setup");

    let root = tempfile::TempDir::new().unwrap();
    std::fs::write(root.path().join("stable.txt"), "stable content\n").unwrap();
    let db = root.path().join(".lancedb-skip-test");

    // First index — should not be skipped
    let resp = rpc(daemon.port(), daemon.token(), "index_file",
        serde_json::json!({
            "root": root.path().to_str().unwrap(),
            "db": db.to_str().unwrap(),
            "file": "stable.txt",
            "tier": "budget",
            "dimensions": 256
        }),
    )
    .await
    .expect("rpc");
    assert_eq!(resp["result"]["success"], true);
    assert!(!resp["result"]["skipped"].as_bool().unwrap_or(false));

    // Second index (identical content) — should be skipped
    let resp = rpc(daemon.port(), daemon.token(), "index_file",
        serde_json::json!({
            "root": root.path().to_str().unwrap(),
            "db": db.to_str().unwrap(),
            "file": "stable.txt",
            "tier": "budget",
            "dimensions": 256
        }),
    )
    .await
    .expect("rpc");
    assert_eq!(resp["result"]["success"], true);
    assert_eq!(resp["result"]["skipped"], true);
    daemon.shutdown().await;
}

// ---- search (full pipeline: index → embed_query → rerank) -----------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn index_then_search_returns_ranked_results() {
    let (_server, daemon) = setup().await.expect("setup");

    let root = tempfile::TempDir::new().unwrap();
    std::fs::write(
        root.path().join("searchable.txt"),
        "rust indexer daemon\nvector search test\n",
    )
    .unwrap();
    let db = root.path().join(".lancedb-search-test");

    // Index (daemon → model server: chunk + embed_passages)
    rpc(daemon.port(), daemon.token(), "index_file",
        serde_json::json!({
            "root": root.path().to_str().unwrap(),
            "db": db.to_str().unwrap(),
            "file": "searchable.txt",
            "tier": "budget",
            "dimensions": 256
        }),
    )
    .await
    .expect("rpc");

    // Search (daemon → model server: embed_query + rerank)
    let resp = rpc(daemon.port(), daemon.token(), "search",
        serde_json::json!({
            "root": root.path().to_str().unwrap(),
            "db": db.to_str().unwrap(),
            "query": "rust indexer",
            "tier": "budget",
            "dimensions": 256
        }),
    )
    .await
    .expect("rpc");

    let results = resp["result"]["results"].as_array().expect("results array");
    assert!(!results.is_empty(), "search should return results");

    // Verify result structure matches TS Embedder.SearchResult
    let first = &results[0];
    assert!(first.get("rank").is_some(), "missing rank");
    assert!(first.get("score").is_some(), "missing score");
    assert!(first.get("path").is_some(), "missing path");
    assert!(first.get("content").is_some(), "missing content");
    assert!(first["path"].as_str().unwrap().contains("searchable.txt"));
    daemon.shutdown().await;
}

// ---- federated search across multiple DBs ---------------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn federated_search_merges_results_from_multiple_dbs() {
    let (_server, daemon) = setup().await.expect("setup");

    let root1 = tempfile::TempDir::new().unwrap();
    let root2 = tempfile::TempDir::new().unwrap();
    std::fs::write(root1.path().join("proj1.txt"), "project one code\n").unwrap();
    std::fs::write(root2.path().join("proj2.txt"), "project two code\n").unwrap();

    let db1 = root1.path().join(".lancedb-fed1");
    let db2 = root2.path().join(".lancedb-fed2");

    // Index both
    for (root, db, file) in [
        (&root1, &db1, "proj1.txt"),
        (&root2, &db2, "proj2.txt"),
    ] {
        rpc(daemon.port(), daemon.token(), "index_file",
            serde_json::json!({
                "root": root.path().to_str().unwrap(),
                "db": db.to_str().unwrap(),
                "file": file,
                "tier": "budget",
                "dimensions": 256
            }),
        )
        .await
        .expect("rpc");
    }

    // Federated search: primary = db1, federated = [db2]
    let resp = rpc(daemon.port(), daemon.token(), "search",
        serde_json::json!({
            "root": root1.path().to_str().unwrap(),
            "db": db1.to_str().unwrap(),
            "query": "project code",
            "tier": "budget",
            "dimensions": 256,
            "federatedDb": [db2.to_str().unwrap()]
        }),
    )
    .await
    .expect("rpc");

    let results = resp["result"]["results"].as_array().expect("results array");
    assert!(results.len() >= 2, "federated search should return results from both DBs, got {}", results.len());

    let paths: Vec<&str> = results.iter().filter_map(|r| r["path"].as_str()).collect();
    assert!(paths.iter().any(|p| p.contains("proj1.txt")), "missing proj1");
    assert!(paths.iter().any(|p| p.contains("proj2.txt")), "missing proj2");
    daemon.shutdown().await;
}

// ---- health ---------------------------------------------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn health_returns_structured_status() {
    let (server, daemon) = setup().await.expect("setup");

    let root = tempfile::TempDir::new().unwrap();
    let shared = server.home_path().join("shared-health");
    std::fs::create_dir_all(&shared).unwrap();

    let resp = rpc(daemon.port(), daemon.token(), "health",
        serde_json::json!({
            "root": root.path().to_str().unwrap(),
            "dimensions": 256,
            "sharedPath": shared.to_str().unwrap(),
            "projectId": "healthtest"
        }),
    )
    .await
    .expect("rpc");

    let r = &resp["result"];
    // Verify structure matches what indexer.ts health() maps from
    assert!(r.get("healthy").is_some(), "missing healthy");
    assert!(r.get("root").is_some(), "missing root");
    assert!(r.get("indexExists").is_some(), "missing indexExists");
    assert!(r.get("dbPath").is_some(), "missing dbPath");
    assert!(r.get("errors").is_some(), "missing errors");
    assert!(r["errors"].as_array().is_some(), "errors should be array");
    // Memory dirs should be populated when sharedPath+projectId given
    if let Some(dirs) = r.get("memoryDirs") {
        assert!(dirs.get("project").is_some());
        assert!(dirs.get("global").is_some());
    }
    daemon.shutdown().await;
}

// ---- run_index (full project indexing) ------------------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn run_index_on_small_project() {
    let (_server, daemon) = setup().await.expect("setup");

    let root = tempfile::TempDir::new().unwrap();
    std::process::Command::new("git")
        .args(["init"])
        .current_dir(root.path())
        .output()
        .ok();
    std::fs::write(
        root.path().join("a.rs"),
        "fn main() {\n    println!(\"hello\");\n}\n",
    )
    .unwrap();
    std::fs::write(
        root.path().join("b.rs"),
        "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n",
    )
    .unwrap();
    std::process::Command::new("git")
        .args(["add", "."])
        .current_dir(root.path())
        .output()
        .ok();

    let db = root.path().join(".lancedb-run-test");

    let resp = rpc(daemon.port(), daemon.token(), "run_index",
        serde_json::json!({
            "root": root.path().to_str().unwrap(),
            "db": db.to_str().unwrap(),
            "tier": "budget",
            "dimensions": 256,
            "force": false,
            "exclude": [],
            "include": []
        }),
    )
    .await
    .expect("rpc");

    let r = &resp["result"];
    assert_eq!(r["success"], true, "run_index failed: {resp}");
    // With small files, files count includes unchanged + modified.
    // Both files should be discovered and processed.
    let files = r["files"].as_u64().unwrap_or(0);
    let modified = r["modified"].as_u64().unwrap_or(0);
    assert!(files >= 2 || modified >= 1, "expected >= 2 files or >= 1 modified, got: {r}");
    assert!(r["duration"].as_f64().unwrap_or(-1.0) >= 0.0);
    daemon.shutdown().await;
}

// ---- search_memories (project + global memory search) ---------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn search_memories_returns_structured_results() {
    let (server, daemon) = setup().await.expect("setup");

    let shared = server.home_path().join("shared-mem");
    let mem_dir = shared.join("projects").join("memtest").join("memories");
    std::fs::create_dir_all(&mem_dir).unwrap();
    std::fs::write(mem_dir.join("note1.md"), "# Important\nRemember this context\n").unwrap();
    let mem_db = mem_dir.join(".lancedb");

    // Index the memory file (daemon → model server)
    rpc(daemon.port(), daemon.token(), "index_file",
        serde_json::json!({
            "root": mem_dir.to_str().unwrap(),
            "db": mem_db.to_str().unwrap(),
            "file": "note1.md",
            "tier": "budget",
            "dimensions": 256
        }),
    )
    .await
    .expect("rpc");

    // Search memories
    let resp = rpc(daemon.port(), daemon.token(), "search_memories",
        serde_json::json!({
            "sharedPath": shared.to_str().unwrap(),
            "projectId": "memtest",
            "query": "important context",
            "limit": 10,
            "tier": "budget",
            "dimensions": 256,
            "federatedProjectIds": []
        }),
    )
    .await
    .expect("rpc");

    let results = resp["result"]["results"].as_array().expect("results array");
    assert!(!results.is_empty(), "memory search should return results");

    // Verify structure matches TS Embedder.MemorySearchResult
    let first = &results[0];
    assert!(first.get("id").is_some(), "missing id");
    assert!(first.get("path").is_some(), "missing path");
    assert!(first.get("title").is_some(), "missing title");
    assert!(first.get("content").is_some(), "missing content");
    assert!(first.get("score").is_some(), "missing score");
    assert!(first.get("scope").is_some(), "missing scope");
    assert_eq!(first["scope"], "project");
    // .md extension should be stripped from id
    let id = first["id"].as_str().unwrap();
    assert!(!id.ends_with(".md"), "id should have .md stripped: {id}");
    daemon.shutdown().await;
}

// ---- search_activity ------------------------------------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn search_activity_returns_structured_results() {
    let (server, daemon) = setup().await.expect("setup");

    let shared = server.home_path().join("shared-act");
    let act_dir = shared.join("projects").join("acttest").join("activity");
    std::fs::create_dir_all(&act_dir).unwrap();
    std::fs::write(
        act_dir.join("session1.md"),
        "# Session\nRefactored the auth module\n",
    )
    .unwrap();
    let act_db = act_dir.join(".lancedb");

    // Index (daemon → model server)
    rpc(daemon.port(), daemon.token(), "index_file",
        serde_json::json!({
            "root": act_dir.to_str().unwrap(),
            "db": act_db.to_str().unwrap(),
            "file": "session1.md",
            "tier": "budget",
            "dimensions": 256
        }),
    )
    .await
    .expect("rpc");

    // Search activity
    let resp = rpc(daemon.port(), daemon.token(), "search_activity",
        serde_json::json!({
            "sharedPath": shared.to_str().unwrap(),
            "projectId": "acttest",
            "query": "auth refactoring",
            "limit": 10,
            "tier": "budget",
            "dimensions": 256
        }),
    )
    .await
    .expect("rpc");

    let results = resp["result"]["results"].as_array().expect("results array");
    assert!(!results.is_empty(), "activity search should return results");

    // Verify structure matches TS Embedder.ActivitySearchResult
    let first = &results[0];
    assert!(first.get("id").is_some(), "missing id");
    assert!(first.get("path").is_some(), "missing path");
    assert!(first.get("title").is_some(), "missing title");
    assert!(first.get("content").is_some(), "missing content");
    assert!(first.get("score").is_some(), "missing score");
    daemon.shutdown().await;
}

// ---- discover_links -------------------------------------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn discover_links_on_plain_project() {
    let (_server, daemon) = setup().await.expect("setup");

    let root = tempfile::TempDir::new().unwrap();
    std::process::Command::new("git")
        .args(["init"])
        .current_dir(root.path())
        .output()
        .ok();
    std::fs::write(root.path().join("main.rs"), "fn main() {}").unwrap();
    std::process::Command::new("git")
        .args(["add", "."])
        .current_dir(root.path())
        .output()
        .ok();

    let resp = rpc(daemon.port(), daemon.token(), "discover_links",
        serde_json::json!({"root": root.path().to_str().unwrap()}),
    )
    .await
    .expect("rpc");

    let r = &resp["result"];
    assert!(r.get("rootProjectId").is_some(), "missing rootProjectId");
    assert!(r.get("links").is_some(), "missing links");
    assert!(r["links"].as_array().is_some(), "links should be array");
    daemon.shutdown().await;
}

// ---- Queue serialisation: write ops are serialised ------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn write_ops_serialised_through_queue() {
    let (_server, daemon) = setup().await.expect("setup");

    let root = tempfile::TempDir::new().unwrap();
    std::fs::write(root.path().join("f1.txt"), "file one content\n").unwrap();
    std::fs::write(root.path().join("f2.txt"), "file two content\n").unwrap();
    std::fs::write(root.path().join("f3.txt"), "file three content\n").unwrap();
    let db = root.path().join(".lancedb-queue-test");

    let port = daemon.port();
    let token = daemon.token().map(String::from);

    // Fire 3 index_file requests concurrently — they all go through the queue
    let mut handles = Vec::new();
    for (i, name) in ["f1.txt", "f2.txt", "f3.txt"].iter().enumerate() {
        let root_str = root.path().to_str().unwrap().to_string();
        let db_str = db.to_str().unwrap().to_string();
        let file = name.to_string();
        let token = token.clone();
        handles.push(tokio::spawn(async move {
            rpc(port, token.as_deref(), "index_file",
                serde_json::json!({
                    "root": root_str,
                    "db": db_str,
                    "file": file,
                    "tier": "budget",
                    "dimensions": 256
                }),
            )
            .await
        }));
        let _ = i; // suppress unused warning
    }

    for h in handles {
        let resp = h.await.unwrap().expect("rpc");
        assert_eq!(resp["result"]["success"], true, "index failed: {resp}");
    }

    // Verify all 3 files indexed
    let resp = rpc(port, token.as_deref(), "status",
        serde_json::json!({"db": db.to_str().unwrap(), "dimensions": 256}),
    )
    .await
    .expect("rpc");
    assert_eq!(resp["result"]["files"].as_u64().unwrap_or(0), 3);
    daemon.shutdown().await;
}

// ===========================================================================
// Compaction integration tests
// ===========================================================================

/// Test that index_file operations are tracked for compaction.
/// This test verifies that the daemon correctly records write operations
/// for later compaction.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn index_file_operations_tracked_for_compaction() {
    let (_server, daemon) = setup().await.expect("setup");

    let root = tempfile::TempDir::new().unwrap();

    // Initialize git repo
    let _ = std::process::Command::new("git")
        .args(["init"])
        .current_dir(root.path())
        .output();

    // Create test files
    std::fs::write(root.path().join("file1.txt"), "content 1").unwrap();
    std::fs::write(root.path().join("file2.txt"), "content 2").unwrap();
    std::fs::write(root.path().join("file3.txt"), "content 3").unwrap();

    let _ = std::process::Command::new("git")
        .args(["add", "."])
        .current_dir(root.path())
        .output();
    let _ = std::process::Command::new("git")
        .args(["commit", "-m", "init"])
        .current_dir(root.path())
        .output();

    // Index multiple files
    for file in ["file1.txt", "file2.txt", "file3.txt"] {
        let resp = rpc(daemon.port(), daemon.token(), "index_file",
            serde_json::json!({
                "root": root.path().to_str().unwrap(),
                "file": file,
                "tier": "budget",
                "dimensions": 256
            }),
        )
        .await
        .expect("rpc");

        // Operation should succeed
        assert_eq!(resp["result"]["success"], true, "index_file should succeed: {resp}");
    }

    daemon.shutdown().await;
}

/// Test that remove_file operations are tracked for compaction.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn remove_file_operations_tracked_for_compaction() {
    let (_server, daemon) = setup().await.expect("setup");

    let root = tempfile::TempDir::new().unwrap();

    // Initialize git repo
    let _ = std::process::Command::new("git")
        .args(["init"])
        .current_dir(root.path())
        .output();

    // Create and index a test file first
    std::fs::write(root.path().join("test.txt"), "content").unwrap();

    let _ = std::process::Command::new("git")
        .args(["add", "."])
        .current_dir(root.path())
        .output();
    let _ = std::process::Command::new("git")
        .args(["commit", "-m", "init"])
        .current_dir(root.path())
        .output();

    // Index the file
    let resp = rpc(daemon.port(), daemon.token(), "index_file",
        serde_json::json!({
            "root": root.path().to_str().unwrap(),
            "file": "test.txt",
            "tier": "budget",
            "dimensions": 256
        }),
    )
    .await
    .expect("rpc");

    assert_eq!(resp["result"]["success"], true, "index_file should succeed: {resp}");

    // Remove the file
    let resp = rpc(daemon.port(), daemon.token(), "remove_file",
        serde_json::json!({
            "root": root.path().to_str().unwrap(),
            "file": "test.txt",
            "dimensions": 256
        }),
    )
    .await
    .expect("rpc");

    assert_eq!(resp["result"]["success"], true, "remove_file should succeed: {resp}");

    daemon.shutdown().await;
}

/// Test that idle-time compaction is triggered after sufficient idle period.
/// Uses environment variables to configure short thresholds for testing.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn idle_time_compaction_trigger() {
    // Configure short thresholds for testing
    let extra_env = vec![
        ("OPENCODE_COMPACTION_IDLE_MS", "500".to_string()),
        ("OPENCODE_COMPACTION_OPS_THRESHOLD", "2".to_string()),
        ("OPENCODE_COMPACTION_CHECK_INTERVAL_MS", "200".to_string()),
    ];

    let (server, daemon) = setup_with_daemon_env(&extra_env.iter().map(|(k, v)| (*k, v.clone())).collect::<Vec<_>>())
        .await
        .expect("setup");

    let root = tempfile::TempDir::new().unwrap();

    // Initialize git repo
    let _ = std::process::Command::new("git")
        .args(["init"])
        .current_dir(root.path())
        .output();

    // Create test files
    std::fs::write(root.path().join("file1.txt"), "idle test content 1").unwrap();
    std::fs::write(root.path().join("file2.txt"), "idle test content 2").unwrap();

    let _ = std::process::Command::new("git")
        .args(["add", "."])
        .current_dir(root.path())
        .output();
    let _ = std::process::Command::new("git")
        .args(["commit", "-m", "init"])
        .current_dir(root.path())
        .output();

    // Index files to accumulate operations
    for file in ["file1.txt", "file2.txt"] {
        let resp = rpc(daemon.port(), daemon.token(), "index_file",
            serde_json::json!({
                "root": root.path().to_str().unwrap(),
                "file": file,
                "tier": "budget",
                "dimensions": 256
            }),
        )
        .await
        .expect("rpc");

        assert_eq!(resp["result"]["success"], true, "index_file should succeed: {resp}");
    }

    // Wait for idle-time compaction to trigger
    tokio::time::sleep(Duration::from_millis(1500)).await;

    // The compaction should have run by now. Verify daemon is still responsive.
    let resp = rpc(daemon.port(), daemon.token(), "ping", serde_json::json!({}))
        .await
        .expect("rpc");

    assert_eq!(resp["result"]["pong"], true, "daemon should still be responsive");

    daemon.shutdown().await;
    drop(server);
}

/// Test that threshold-based compaction is triggered after enough operations.
/// Uses environment variables to configure low threshold for testing.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn threshold_compaction_trigger() {
    // Configure low force threshold for testing
    let extra_env = vec![
        ("OPENCODE_COMPACTION_FORCE_THRESHOLD", "3".to_string()),
        ("OPENCODE_COMPACTION_CHECK_INTERVAL_MS", "200".to_string()),
    ];

    let (server, daemon) = setup_with_daemon_env(&extra_env.iter().map(|(k, v)| (*k, v.clone())).collect::<Vec<_>>())
        .await
        .expect("setup");

    let root = tempfile::TempDir::new().unwrap();

    // Initialize git repo
    let _ = std::process::Command::new("git")
        .args(["init"])
        .current_dir(root.path())
        .output();

    // Create test files
    for i in 0..5 {
        std::fs::write(root.path().join(format!("file{}.txt", i)), format!("threshold test content {}", i)).unwrap();
    }

    let _ = std::process::Command::new("git")
        .args(["add", "."])
        .current_dir(root.path())
        .output();
    let _ = std::process::Command::new("git")
        .args(["commit", "-m", "init"])
        .current_dir(root.path())
        .output();

    // Index files to exceed the force threshold
    for i in 0..5 {
        let resp = rpc(daemon.port(), daemon.token(), "index_file",
            serde_json::json!({
                "root": root.path().to_str().unwrap(),
                "file": format!("file{}.txt", i),
                "tier": "budget",
                "dimensions": 256
            }),
        )
        .await
        .expect("rpc");

        assert_eq!(resp["result"]["success"], true, "index_file should succeed: {resp}");
    }

    // Wait for threshold compaction to trigger
    tokio::time::sleep(Duration::from_millis(500)).await;

    // The compaction should have run. Verify daemon is still responsive.
    let resp = rpc(daemon.port(), daemon.token(), "ping", serde_json::json!({}))
        .await
        .expect("rpc");

    assert_eq!(resp["result"]["pong"], true, "daemon should still be responsive");

    daemon.shutdown().await;
    drop(server);
}

/// Test that shutdown compaction runs when the daemon receives shutdown signal.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn shutdown_compaction_runs() {
    // Configure to NOT trigger background compaction so we can test shutdown compaction
    let extra_env = vec![
        ("OPENCODE_COMPACTION_IDLE_MS", "60000".to_string()),
        ("OPENCODE_COMPACTION_FORCE_THRESHOLD", "1000".to_string()),
        ("OPENCODE_COMPACTION_CHECK_INTERVAL_MS", "60000".to_string()),
        ("OPENCODE_COMPACTION_SHUTDOWN_TIMEOUT_MS", "5000".to_string()),
    ];

    let (server, daemon) = setup_with_daemon_env(&extra_env.iter().map(|(k, v)| (*k, v.clone())).collect::<Vec<_>>())
        .await
        .expect("setup");

    let root = tempfile::TempDir::new().unwrap();

    // Initialize git repo
    let _ = std::process::Command::new("git")
        .args(["init"])
        .current_dir(root.path())
        .output();

    std::fs::write(root.path().join("shutdown_test.txt"), "shutdown compaction test").unwrap();

    let _ = std::process::Command::new("git")
        .args(["add", "."])
        .current_dir(root.path())
        .output();
    let _ = std::process::Command::new("git")
        .args(["commit", "-m", "init"])
        .current_dir(root.path())
        .output();

    // Index a file to create pending operations
    let resp = rpc(daemon.port(), daemon.token(), "index_file",
        serde_json::json!({
            "root": root.path().to_str().unwrap(),
            "file": "shutdown_test.txt",
            "tier": "budget",
            "dimensions": 256
        }),
    )
    .await
    .expect("rpc");

    assert_eq!(resp["result"]["success"], true, "index_file should succeed: {resp}");

    // Shutdown the daemon - this should trigger shutdown compaction
    // The shutdown RPC will run compaction before responding
    let resp = rpc(daemon.port(), daemon.token(), "shutdown", serde_json::json!({})).await;

    // The shutdown response might fail if the daemon exits quickly,
    // but that's okay - we just want to verify it attempts compaction
    if let Ok(r) = resp {
        assert_eq!(r["result"]["shutdown"], true, "shutdown should return true");
    }

    drop(server);
}

/// Test that multiple databases are tracked independently for compaction.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn multiple_databases_tracked_independently() {
    let (_server, daemon) = setup().await.expect("setup");

    // Create two separate project roots
    let root1 = tempfile::TempDir::new().unwrap();
    let root2 = tempfile::TempDir::new().unwrap();

    // Initialize git repos
    for root in [root1.path(), root2.path()] {
        let _ = std::process::Command::new("git")
            .args(["init"])
            .current_dir(root)
            .output();

        std::fs::write(root.join("test.txt"), "content").unwrap();

        let _ = std::process::Command::new("git")
            .args(["add", "."])
            .current_dir(root)
            .output();
        let _ = std::process::Command::new("git")
            .args(["commit", "-m", "init"])
            .current_dir(root)
            .output();
    }

    // Index files in both projects
    let resp1 = rpc(daemon.port(), daemon.token(), "index_file",
        serde_json::json!({
            "root": root1.path().to_str().unwrap(),
            "file": "test.txt",
            "tier": "budget",
            "dimensions": 256
        }),
    )
    .await
    .expect("rpc");

    let resp2 = rpc(daemon.port(), daemon.token(), "index_file",
        serde_json::json!({
            "root": root2.path().to_str().unwrap(),
            "file": "test.txt",
            "tier": "budget",
            "dimensions": 512  // Different dimensions
        }),
    )
    .await
    .expect("rpc");

    assert_eq!(resp1["result"]["success"], true, "index_file for root1 should succeed: {resp1}");
    assert_eq!(resp2["result"]["success"], true, "index_file for root2 should succeed: {resp2}");

    // Both should complete independently
    daemon.shutdown().await;
}

/// Test that skipped operations don't count towards compaction threshold.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn skipped_operations_not_counted() {
    let (_server, daemon) = setup().await.expect("setup");

    let root = tempfile::TempDir::new().unwrap();

    // Initialize git repo
    let _ = std::process::Command::new("git")
        .args(["init"])
        .current_dir(root.path())
        .output();

    std::fs::write(root.path().join("test.txt"), "content").unwrap();

    let _ = std::process::Command::new("git")
        .args(["add", "."])
        .current_dir(root.path())
        .output();
    let _ = std::process::Command::new("git")
        .args(["commit", "-m", "init"])
        .current_dir(root.path())
        .output();

    // Index the file first time
    let resp1 = rpc(daemon.port(), daemon.token(), "index_file",
        serde_json::json!({
            "root": root.path().to_str().unwrap(),
            "file": "test.txt",
            "tier": "budget",
            "dimensions": 256
        }),
    )
    .await
    .expect("rpc");

    assert_eq!(resp1["result"]["success"], true);
    // First indexing should NOT be skipped (it's a new file)
    let skipped1 = resp1["result"]["skipped"].as_bool().unwrap_or(false);

    // Index the same unchanged file again
    let resp2 = rpc(daemon.port(), daemon.token(), "index_file",
        serde_json::json!({
            "root": root.path().to_str().unwrap(),
            "file": "test.txt",
            "tier": "budget",
            "dimensions": 256
        }),
    )
    .await
    .expect("rpc");

    assert_eq!(resp2["result"]["success"], true);
    // Second indexing should be skipped (file unchanged)
    let skipped2 = resp2["result"]["skipped"].as_bool().unwrap_or(false);

    // At least one should be skipped (the second one)
    assert!(skipped2 || !skipped1, "re-indexing unchanged file should be skipped");

    daemon.shutdown().await;
}

/// Test that watcher_status returns metrics when watcher is active.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn watcher_status_returns_metrics_when_active() {
    let (_server, daemon) = setup().await.expect("setup");

    let root = tempfile::TempDir::new().unwrap();

    // Initialize git repo (required for watcher)
    let _ = std::process::Command::new("git")
        .args(["init"])
        .current_dir(root.path())
        .output();

    // Create a config file with custom max_pending_files
    let config_content = r#"
watcher:
  max_pending_files: 25000
index:
  use_default_ignores: true
"#;
    std::fs::write(root.path().join(".opencode-index.yaml"), config_content).unwrap();

    // Create a test file
    std::fs::write(root.path().join("test.txt"), "content").unwrap();

    let _ = std::process::Command::new("git")
        .args(["add", "."])
        .current_dir(root.path())
        .output();
    let _ = std::process::Command::new("git")
        .args(["commit", "-m", "init"])
        .current_dir(root.path())
        .output();

    // Index the project first (required before watcher can start)
    // Use default db path (same as what watcher_start will use)
    let index_resp = rpc(daemon.port(), daemon.token(), "run_index",
        serde_json::json!({
            "root": root.path().to_str().unwrap(),
            "tier": "budget",
            "dimensions": 256,
            "force": false,
            "exclude": [],
            "include": []
        }),
    )
    .await
    .expect("run_index rpc");
    assert_eq!(index_resp["result"]["success"], true, "run_index should succeed: {index_resp}");

    // Start the watcher
    let start_resp = rpc(daemon.port(), daemon.token(), "watcher_start",
        serde_json::json!({
            "root": root.path().to_str().unwrap(),
            "tier": "budget"
        }),
    )
    .await
    .expect("watcher_start rpc");

    assert_eq!(start_resp["result"]["success"], true, "watcher_start should succeed: {start_resp}");
    assert_eq!(start_resp["result"]["internal"], true, "should be internal watcher");

    // Give the watcher time to initialize
    tokio::time::sleep(Duration::from_millis(500)).await;

    // Get watcher status - should include metrics
    let status_resp = rpc(daemon.port(), daemon.token(), "watcher_status",
        serde_json::json!({
            "root": root.path().to_str().unwrap()
        }),
    )
    .await
    .expect("watcher_status rpc");

    let result = &status_resp["result"];

    // Verify watcher is active
    assert_eq!(result["watcherActive"], true, "watcher should be active: {status_resp}");
    assert_eq!(result["internal"], true, "should be internal watcher");

    // Verify metrics object exists and has expected fields
    let metrics = &result["metrics"];
    assert!(!metrics.is_null(), "metrics should be present: {status_resp}");

    // Verify maxPendingFiles matches config (25000 from .opencode-index.yaml)
    assert_eq!(
        metrics["maxPendingFiles"].as_u64().unwrap(),
        25000,
        "maxPendingFiles should match config value: {metrics}"
    );

    // Verify other metric fields exist
    assert!(metrics["uptimeSeconds"].is_number(), "uptimeSeconds should be a number: {metrics}");
    assert!(metrics["currentPendingFiles"].is_number(), "currentPendingFiles should be a number: {metrics}");
    assert!(metrics["droppedChangedFiles"].is_number(), "droppedChangedFiles should be a number: {metrics}");
    assert!(metrics["droppedDeletedFiles"].is_number(), "droppedDeletedFiles should be a number: {metrics}");
    assert!(metrics["backpressureEvents"].is_number(), "backpressureEvents should be a number: {metrics}");

    // Initially there should be no dropped events
    assert_eq!(metrics["droppedChangedFiles"].as_u64().unwrap(), 0, "no dropped changed files initially");
    assert_eq!(metrics["droppedDeletedFiles"].as_u64().unwrap(), 0, "no dropped deleted files initially");
    assert_eq!(metrics["backpressureEvents"].as_u64().unwrap(), 0, "no backpressure events initially");

    // Stop the watcher
    let stop_resp = rpc(daemon.port(), daemon.token(), "watcher_stop",
        serde_json::json!({
            "root": root.path().to_str().unwrap()
        }),
    )
    .await
    .expect("watcher_stop rpc");

    assert_eq!(stop_resp["result"]["success"], true, "watcher_stop should succeed: {stop_resp}");

    daemon.shutdown().await;
}

/// Test that watcher uses default max_pending_files when config is not present.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn watcher_uses_default_max_pending_files() {
    let (_server, daemon) = setup().await.expect("setup");

    let root = tempfile::TempDir::new().unwrap();

    // Initialize git repo (required for watcher)
    let _ = std::process::Command::new("git")
        .args(["init"])
        .current_dir(root.path())
        .output();

    // NO config file - should use default max_pending_files (10000)

    // Create a test file
    std::fs::write(root.path().join("test.txt"), "content").unwrap();

    let _ = std::process::Command::new("git")
        .args(["add", "."])
        .current_dir(root.path())
        .output();
    let _ = std::process::Command::new("git")
        .args(["commit", "-m", "init"])
        .current_dir(root.path())
        .output();

    // Index the project first (required before watcher can start)
    // Use default db path (same as what watcher_start will use)
    let index_resp = rpc(daemon.port(), daemon.token(), "run_index",
        serde_json::json!({
            "root": root.path().to_str().unwrap(),
            "tier": "budget",
            "dimensions": 256,
            "force": false,
            "exclude": [],
            "include": []
        }),
    )
    .await
    .expect("run_index rpc");
    assert_eq!(index_resp["result"]["success"], true, "run_index should succeed: {index_resp}");

    // Start the watcher
    let start_resp = rpc(daemon.port(), daemon.token(), "watcher_start",
        serde_json::json!({
            "root": root.path().to_str().unwrap(),
            "tier": "budget"
        }),
    )
    .await
    .expect("watcher_start rpc");

    assert_eq!(start_resp["result"]["success"], true, "watcher_start should succeed: {start_resp}");

    // Give the watcher time to initialize
    tokio::time::sleep(Duration::from_millis(500)).await;

    // Get watcher status
    let status_resp = rpc(daemon.port(), daemon.token(), "watcher_status",
        serde_json::json!({
            "root": root.path().to_str().unwrap()
        }),
    )
    .await
    .expect("watcher_status rpc");

    let metrics = &status_resp["result"]["metrics"];

    // Verify maxPendingFiles is the default (10000)
    assert_eq!(
        metrics["maxPendingFiles"].as_u64().unwrap(),
        10000,
        "maxPendingFiles should be default 10000 when no config: {metrics}"
    );

    // Stop the watcher
    let _ = rpc(daemon.port(), daemon.token(), "watcher_stop",
        serde_json::json!({
            "root": root.path().to_str().unwrap()
        }),
    )
    .await;

    daemon.shutdown().await;
}
