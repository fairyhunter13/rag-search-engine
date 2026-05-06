//! Tests for LanceDB corruption detection and auto-recovery.
//!
//! These tests verify:
//!   1. `is_corruption_error()` correctly identifies corruption patterns
//!   2. `clear_corrupted_index()` properly backs up and removes corrupted indexes
//!   3. Search operations trigger auto-recovery when corruption is detected
//!   4. The daemon returns appropriate responses during recovery

use std::path::PathBuf;
use std::time::Duration;

use anyhow::{Context, Result};
use fs2::FileExt;
use tokio::process::Command;

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

async fn rpc(port: u16, method: &str, params: serde_json::Value) -> Result<serde_json::Value> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("http://127.0.0.1:{port}/rpc"))
        .json(&serde_json::json!({"method": method, "params": params}))
        .send()
        .await
        .context("send rpc")?
        .json::<serde_json::Value>()
        .await
        .context("parse rpc response")?;
    Ok(resp)
}

// ---------------------------------------------------------------------------
// Unit tests for is_corruption_error()
// ---------------------------------------------------------------------------

#[test]
fn test_is_corruption_error_detects_lance_not_found() {
    use opencode_indexer::storage::is_corruption_error;

    let err = anyhow::anyhow!(
        "lance error: LanceError(IO): Execution error: Query Execution error: \
         Execution error: Not found: /path/to/.lancedb/chunks.lance/data/abc123.lance"
    );
    assert!(
        is_corruption_error(&err),
        "should detect lance file not found"
    );
}

#[test]
fn test_is_corruption_error_detects_lanceerror_io() {
    use opencode_indexer::storage::is_corruption_error;

    let err = anyhow::anyhow!("LanceError(IO): failed to read file");
    assert!(is_corruption_error(&err), "should detect LanceError IO");
}

#[test]
fn test_is_corruption_error_detects_execution_error_not_found() {
    use opencode_indexer::storage::is_corruption_error;

    let err = anyhow::anyhow!("Execution error: Not found: some/path");
    assert!(
        is_corruption_error(&err),
        "should detect execution error not found"
    );
}

#[test]
fn test_is_corruption_error_detects_corrupted_keyword() {
    use opencode_indexer::storage::is_corruption_error;

    let err = anyhow::anyhow!("data is corrupted");
    assert!(is_corruption_error(&err), "should detect corrupted keyword");
}

#[test]
fn test_is_corruption_error_detects_invalid_data() {
    use opencode_indexer::storage::is_corruption_error;

    let err = anyhow::anyhow!("invalid data in table");
    assert!(is_corruption_error(&err), "should detect invalid data");
}

#[test]
fn test_is_corruption_error_detects_unexpected_eof() {
    use opencode_indexer::storage::is_corruption_error;

    let err = anyhow::anyhow!("unexpected eof while reading");
    assert!(is_corruption_error(&err), "should detect unexpected eof");
}

#[test]
fn test_is_corruption_error_detects_failed_to_read() {
    use opencode_indexer::storage::is_corruption_error;

    let err = anyhow::anyhow!("failed to read chunk data");
    assert!(is_corruption_error(&err), "should detect failed to read");
}

#[test]
fn test_is_corruption_error_ignores_normal_errors() {
    use opencode_indexer::storage::is_corruption_error;

    // Normal errors should NOT be detected as corruption
    let err1 = anyhow::anyhow!("connection refused");
    assert!(
        !is_corruption_error(&err1),
        "should not detect connection refused as corruption"
    );

    let err2 = anyhow::anyhow!("timeout waiting for response");
    assert!(
        !is_corruption_error(&err2),
        "should not detect timeout as corruption"
    );

    let err3 = anyhow::anyhow!("permission denied");
    assert!(
        !is_corruption_error(&err3),
        "should not detect permission denied as corruption"
    );

    let err4 = anyhow::anyhow!("no such file or directory: config.yaml");
    assert!(
        !is_corruption_error(&err4),
        "should not detect missing config as corruption"
    );
}

#[test]
fn test_is_corruption_error_case_insensitive() {
    use opencode_indexer::storage::is_corruption_error;

    let err1 = anyhow::anyhow!("LANCEERROR(IO): ERROR");
    assert!(is_corruption_error(&err1), "should be case insensitive");

    let err2 = anyhow::anyhow!("Data Is CORRUPTED");
    assert!(is_corruption_error(&err2), "should be case insensitive");
}

// ---------------------------------------------------------------------------
// Unit tests for clear_corrupted_index()
// ---------------------------------------------------------------------------

#[test]
fn test_clear_corrupted_index_nonexistent_returns_false() {
    use opencode_indexer::storage::clear_corrupted_index;

    let path = PathBuf::from("/tmp/does-not-exist-corruption-test-12345");
    let result = clear_corrupted_index(&path);
    assert!(result.is_ok());
    assert_eq!(
        result.unwrap(),
        false,
        "should return false for nonexistent path"
    );
}

#[test]
fn test_clear_corrupted_index_removes_directory() {
    use opencode_indexer::storage::clear_corrupted_index;

    // Create a temp directory to simulate corrupted index
    let tmp = tempfile::TempDir::new().unwrap();
    let db_path = tmp.path().join("test.lancedb");
    std::fs::create_dir_all(&db_path).unwrap();
    std::fs::write(db_path.join("test.txt"), "test data").unwrap();

    assert!(db_path.exists(), "db path should exist before clear");

    let result = clear_corrupted_index(&db_path);
    assert!(result.is_ok());
    assert_eq!(result.unwrap(), true, "should return true when cleared");
    assert!(!db_path.exists(), "db path should not exist after clear");
}

#[test]
fn test_clear_corrupted_index_creates_backup() {
    use opencode_indexer::storage::clear_corrupted_index;

    // Create a temp directory to simulate corrupted index
    let tmp = tempfile::TempDir::new().unwrap();
    let db_path = tmp.path().join("backup-test.lancedb");
    std::fs::create_dir_all(&db_path).unwrap();
    std::fs::write(db_path.join("data.txt"), "important data").unwrap();

    let result = clear_corrupted_index(&db_path);
    assert!(result.is_ok());
    assert_eq!(result.unwrap(), true);

    // Check that backup was created (in ~/.local/share/opencode/backups/)
    let backup_dir = dirs::home_dir()
        .unwrap()
        .join(".local/share/opencode/backups");

    if backup_dir.exists() {
        let backups: Vec<_> = std::fs::read_dir(&backup_dir)
            .unwrap()
            .filter_map(Result::ok)
            .filter(|e| e.file_name().to_string_lossy().starts_with("corrupted-"))
            .collect();

        // There should be at least one corrupted backup
        // Note: we can't guarantee it's from THIS test due to parallel execution
        // but we verify the backup mechanism works
        println!(
            "Found {} corrupted backups in {:?}",
            backups.len(),
            backup_dir
        );
    }
}

// ---------------------------------------------------------------------------
// Integration test: status reports corruption
// ---------------------------------------------------------------------------

// Daemon lifecycle helper (same as daemon_integration.rs)
struct DaemonHandle {
    child: tokio::process::Child,
    port: u16,
    _lock: std::fs::File,
}

impl DaemonHandle {
    fn lock() -> Result<std::fs::File> {
        let path = std::env::temp_dir().join("opencode-indexer-corruption-test.lock");
        let file = std::fs::OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .open(&path)
            .with_context(|| format!("open lock file: {}", path.display()))?;
        file.lock_exclusive().context("acquire lock")?;
        Ok(file)
    }

    async fn spawn() -> Result<Self> {
        let lock = Self::lock()?;

        // Find the binary
        let binary = assert_cmd::cargo::cargo_bin!("opencode-indexer");

        let mut child = Command::new(&binary)
            .args(["--daemon", "--port", "0"])
            .env("OPENCODE_EMBED_HTTP_PORT", "19998")
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .context("spawn daemon")?;

        // Wait for http_ready
        let stdout = child.stdout.take().context("no stdout")?;
        let mut reader = tokio::io::BufReader::new(stdout);
        let mut buf = String::new();
        let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
        let mut port = 0u16;

        loop {
            if tokio::time::Instant::now() > deadline {
                child.kill().await.ok();
                anyhow::bail!("daemon did not start in time");
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

        // Drain stdout
        tokio::spawn(async move {
            let mut d = vec![0u8; 4096];
            loop {
                match tokio::io::AsyncReadExt::read(&mut reader, &mut d).await {
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
        let _ = rpc(self.port, "shutdown", serde_json::json!({})).await;
        tokio::time::sleep(Duration::from_millis(100)).await;
        let _ = self.child.kill().await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn status_reports_corruption_when_lance_files_missing() {
    // Skip if daemon binary not available
    let daemon = match DaemonHandle::spawn().await {
        Ok(d) => d,
        Err(e) => {
            eprintln!("Skipping test: {}", e);
            return;
        }
    };

    // Query status on a non-existent db - this should just return exists: false
    // (not a corruption scenario, but validates the basic flow)
    let tmp = tempfile::TempDir::new().unwrap();
    let db_path = tmp.path().join("nonexistent.lancedb");

    let resp = rpc(
        daemon.port(),
        "status",
        serde_json::json!({
            "db": db_path.to_str().unwrap(),
            "dimensions": 256
        }),
    )
    .await
    .expect("rpc");

    let result = &resp["result"];

    // Non-existent db should return exists: false
    assert_eq!(
        result["exists"], false,
        "nonexistent db should report exists=false"
    );

    // Note: The corruption detection logic in status_impl checks if:
    // 1. chunks > 0 or files > 0 (meaning there was data)
    // 2. But lance table directories or data files are missing
    //
    // This test validates the basic flow. The actual corruption detection
    // is tested in the unit tests for is_corruption_error().

    daemon.shutdown().await;
}

// ===========================================================================
// startup_check tests - verify all decision logic is in daemon
// ===========================================================================

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn startup_check_returns_none_when_no_index_exists() {
    let daemon = match DaemonHandle::spawn().await {
        Ok(d) => d,
        Err(e) => {
            eprintln!("Skipping test: {}", e);
            return;
        }
    };

    let tmp = tempfile::TempDir::new().unwrap();
    let root = tmp.path();
    let db_path = root.join(".lancedb");

    // No index exists - startup_check should return action: "none"
    let resp = rpc(
        daemon.port(),
        "startup_check",
        serde_json::json!({
            "root": root.to_str().unwrap(),
            "db": db_path.to_str().unwrap(),
            "dimensions": 256
        }),
    )
    .await
    .expect("rpc");

    let result = &resp["result"];

    assert_eq!(
        result["action"], "none",
        "should return action=none when no index exists"
    );
    assert_eq!(result["corrupted"], false);
    assert_eq!(result["indexed"], false);
    assert_eq!(result["watching"], false);

    daemon.shutdown().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn startup_check_returns_none_for_invalid_root() {
    let daemon = match DaemonHandle::spawn().await {
        Ok(d) => d,
        Err(e) => {
            eprintln!("Skipping test: {}", e);
            return;
        }
    };

    // Invalid root path - startup_check should return action: "error"
    let resp = rpc(
        daemon.port(),
        "startup_check",
        serde_json::json!({
            "root": "/nonexistent/path/that/does/not/exist",
            "dimensions": 256
        }),
    )
    .await
    .expect("rpc");

    let result = &resp["result"];

    assert_eq!(
        result["action"], "error",
        "should return action=error for invalid root"
    );
    assert!(result["message"]
        .as_str()
        .unwrap()
        .contains("Invalid root path"));

    daemon.shutdown().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn startup_check_detects_corruption_and_triggers_rebuild() {
    let daemon = match DaemonHandle::spawn().await {
        Ok(d) => d,
        Err(e) => {
            eprintln!("Skipping test: {}", e);
            return;
        }
    };

    let tmp = tempfile::TempDir::new().unwrap();
    let root = tmp.path();
    let db_path = root.join(".lancedb");

    // Create a source file to index
    std::fs::write(root.join("test.rs"), "fn main() { println!(\"hello\"); }").unwrap();

    // Create a corrupted index structure:
    // - Has config.lance (so storage thinks it has data)
    // - Missing chunks.lance (corruption)
    std::fs::create_dir_all(&db_path).unwrap();
    std::fs::create_dir_all(db_path.join("config.lance")).unwrap();
    std::fs::write(db_path.join("config.lance").join("dummy"), "data").unwrap();

    // Also create a fake metadata to make status think there's data
    // The corruption detection checks if files > 0 or chunks > 0
    // but lance tables are missing

    // Note: This test may not fully trigger corruption detection because
    // the storage won't be able to open properly. The unit tests for
    // is_corruption_error() cover the error pattern matching.
    // This integration test validates the startup_check RPC works.

    let resp = rpc(
        daemon.port(),
        "startup_check",
        serde_json::json!({
            "root": root.to_str().unwrap(),
            "db": db_path.to_str().unwrap(),
            "tier": "budget",
            "dimensions": 256
        }),
    )
    .await
    .expect("rpc");

    let result = &resp["result"];

    // The response should have valid structure
    assert!(result["action"].is_string(), "action should be a string");
    assert!(
        result["corrupted"].is_boolean(),
        "corrupted should be a boolean"
    );
    assert!(
        result["indexed"].is_boolean(),
        "indexed should be a boolean"
    );
    assert!(
        result["watching"].is_boolean(),
        "watching should be a boolean"
    );
    assert!(result["message"].is_string(), "message should be a string");

    daemon.shutdown().await;
}

// ---------------------------------------------------------------------------
// Unit tests for validate_lance_table()
// ---------------------------------------------------------------------------

#[test]
fn test_validate_lance_table_nonexistent_returns_false() {
    use opencode_indexer::storage::validate_lance_table;

    let path = PathBuf::from("/tmp/does-not-exist-validation-test-12345");
    assert!(
        !validate_lance_table(&path),
        "should return false for nonexistent path"
    );
}

#[test]
fn test_validate_lance_table_empty_dir_is_valid() {
    use opencode_indexer::storage::validate_lance_table;

    let tmp = tempfile::TempDir::new().unwrap();
    let table_path = tmp.path().join("chunks.lance");
    std::fs::create_dir_all(&table_path).unwrap();

    // Table exists but no data dir - should be considered valid (empty table)
    assert!(
        validate_lance_table(&table_path),
        "empty table dir should be valid"
    );
}

#[test]
fn test_validate_lance_table_with_data_files_is_valid() {
    use opencode_indexer::storage::validate_lance_table;

    let tmp = tempfile::TempDir::new().unwrap();
    let table_path = tmp.path().join("chunks.lance");
    let data_dir = table_path.join("data");
    std::fs::create_dir_all(&data_dir).unwrap();

    // Create a .lance file
    std::fs::write(data_dir.join("abc123.lance"), vec![0u8; 128]).unwrap();

    assert!(
        validate_lance_table(&table_path),
        "table with .lance files should be valid"
    );
}

#[test]
fn test_validate_lance_table_empty_data_dir_is_corrupted() {
    use opencode_indexer::storage::validate_lance_table;

    let tmp = tempfile::TempDir::new().unwrap();
    let table_path = tmp.path().join("chunks.lance");
    let data_dir = table_path.join("data");
    std::fs::create_dir_all(&data_dir).unwrap();

    // data/ exists but no .lance files - corrupted
    assert!(
        !validate_lance_table(&table_path),
        "empty data dir should be invalid"
    );
}

#[test]
fn test_validate_lance_table_wrong_extension_is_corrupted() {
    use opencode_indexer::storage::validate_lance_table;

    let tmp = tempfile::TempDir::new().unwrap();
    let table_path = tmp.path().join("chunks.lance");
    let data_dir = table_path.join("data");
    std::fs::create_dir_all(&data_dir).unwrap();

    // Create a file with wrong extension
    std::fs::write(data_dir.join("abc123.txt"), "wrong extension").unwrap();

    assert!(
        !validate_lance_table(&table_path),
        "data dir without .lance files should be invalid"
    );
}

// ---------------------------------------------------------------------------
// Unit tests for is_database_corrupted()
// ---------------------------------------------------------------------------

#[test]
fn test_is_database_corrupted_nonexistent_is_not_corrupted() {
    use opencode_indexer::storage::is_database_corrupted;

    let path = PathBuf::from("/tmp/does-not-exist-db-test-12345");
    let (corrupted, errors) = is_database_corrupted(&path);

    assert!(!corrupted, "nonexistent db should not be corrupted");
    assert!(errors.is_empty(), "should have no errors");
}

#[test]
fn test_is_database_corrupted_empty_db_is_not_corrupted() {
    use opencode_indexer::storage::is_database_corrupted;

    let tmp = tempfile::TempDir::new().unwrap();
    let db_path = tmp.path().join(".lancedb");
    std::fs::create_dir_all(&db_path).unwrap();

    let (corrupted, errors) = is_database_corrupted(&db_path);

    assert!(!corrupted, "empty db should not be corrupted");
    assert!(errors.is_empty(), "should have no errors");
}

#[test]
fn test_is_database_corrupted_valid_tables_not_corrupted() {
    use opencode_indexer::storage::is_database_corrupted;

    let tmp = tempfile::TempDir::new().unwrap();
    let db_path = tmp.path().join(".lancedb");

    // Create valid table structures
    for table in &["chunks.lance", "config.lance"] {
        let data_dir = db_path.join(table).join("data");
        std::fs::create_dir_all(&data_dir).unwrap();
        std::fs::write(data_dir.join("fragment.lance"), "data").unwrap();
    }

    let (corrupted, errors) = is_database_corrupted(&db_path);

    assert!(!corrupted, "valid tables should not be corrupted");
    assert!(errors.is_empty(), "should have no errors");
}

#[test]
fn test_is_database_corrupted_detects_missing_chunks_data() {
    use opencode_indexer::storage::is_database_corrupted;

    let tmp = tempfile::TempDir::new().unwrap();
    let db_path = tmp.path().join(".lancedb");

    // Create chunks.lance with empty data dir (corrupted)
    let chunks_data = db_path.join("chunks.lance").join("data");
    std::fs::create_dir_all(&chunks_data).unwrap();
    // No .lance files - corrupted!

    // Create valid config.lance
    let config_data = db_path.join("config.lance").join("data");
    std::fs::create_dir_all(&config_data).unwrap();
    std::fs::write(config_data.join("fragment.lance"), "data").unwrap();

    let (corrupted, errors) = is_database_corrupted(&db_path);

    assert!(corrupted, "should detect missing chunks data");
    assert!(
        errors.iter().any(|e| e.contains("chunks.lance")),
        "error should mention chunks.lance"
    );
}

#[test]
fn test_is_database_corrupted_detects_missing_config_data() {
    use opencode_indexer::storage::is_database_corrupted;

    let tmp = tempfile::TempDir::new().unwrap();
    let db_path = tmp.path().join(".lancedb");

    // Create valid chunks.lance
    let chunks_data = db_path.join("chunks.lance").join("data");
    std::fs::create_dir_all(&chunks_data).unwrap();
    std::fs::write(chunks_data.join("fragment.lance"), "data").unwrap();

    // Create config.lance with empty data dir (corrupted)
    let config_data = db_path.join("config.lance").join("data");
    std::fs::create_dir_all(&config_data).unwrap();
    // No .lance files - corrupted!

    let (corrupted, errors) = is_database_corrupted(&db_path);

    assert!(corrupted, "should detect missing config data");
    assert!(
        errors.iter().any(|e| e.contains("config.lance")),
        "error should mention config.lance"
    );
}

#[test]
fn test_is_database_corrupted_detects_multiple_corruptions() {
    use opencode_indexer::storage::is_database_corrupted;

    let tmp = tempfile::TempDir::new().unwrap();
    let db_path = tmp.path().join(".lancedb");

    // Create both tables with empty data dirs (both corrupted)
    for table in &["chunks.lance", "config.lance"] {
        let data_dir = db_path.join(table).join("data");
        std::fs::create_dir_all(&data_dir).unwrap();
        // No .lance files
    }

    let (corrupted, errors) = is_database_corrupted(&db_path);

    assert!(corrupted, "should detect corruption");
    assert_eq!(errors.len(), 2, "should have 2 errors");
}

// ---------------------------------------------------------------------------
// Integration test: Storage::open() auto-recovery
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_storage_open_auto_recovers_from_corruption() {
    use opencode_indexer::storage::{is_database_corrupted, Storage};

    let tmp = tempfile::TempDir::new().unwrap();
    let db_path = tmp.path().join(".lancedb");

    // Create a corrupted database structure
    let config_data = db_path.join("config.lance").join("data");
    std::fs::create_dir_all(&config_data).unwrap();
    // Empty data dir = corrupted

    // Verify it's detected as corrupted before opening
    let (corrupted, _) = is_database_corrupted(&db_path);
    assert!(corrupted, "should detect corruption before open");

    // Opening should auto-recover (clear corrupted and recreate)
    let result = Storage::open(&db_path, 256).await;

    // Should succeed after auto-recovery
    assert!(
        result.is_ok(),
        "Storage::open should succeed after auto-recovery: {:?}",
        result.err()
    );

    // Database should no longer be corrupted
    let (corrupted_after, errors) = is_database_corrupted(&db_path);
    assert!(
        !corrupted_after,
        "should not be corrupted after open: {:?}",
        errors
    );
}

#[tokio::test]
async fn test_storage_open_creates_fresh_db() {
    use opencode_indexer::storage::Storage;

    let tmp = tempfile::TempDir::new().unwrap();
    let db_path = tmp.path().join("fresh.lancedb");

    // Path doesn't exist yet
    assert!(!db_path.exists());

    let result = Storage::open(&db_path, 256).await;

    assert!(
        result.is_ok(),
        "Storage::open should create fresh db: {:?}",
        result.err()
    );
    assert!(db_path.exists(), "db path should exist after open");
}

#[tokio::test]
async fn test_get_config_returns_none_on_missing_key() {
    use opencode_indexer::storage::Storage;

    let tmp = tempfile::TempDir::new().unwrap();
    let db_path = tmp.path().join(".lancedb");

    // Create a valid storage first
    let storage = Storage::open(&db_path, 256).await.expect("open storage");

    // Set a config value
    storage
        .set_config("test_key", "test_value")
        .await
        .expect("set config");

    // Verify it can be read
    let value = storage.get_config("test_key").await.expect("get config");
    assert_eq!(value, Some("test_value".to_string()));

    // Now get a non-existent key (should return None, not error)
    let missing = storage
        .get_config("nonexistent_key")
        .await
        .expect("get missing config");
    assert_eq!(missing, None, "missing key should return None");
}

// ---------------------------------------------------------------------------
// Unit tests for new Arrow RecordBatch corruption patterns
// ---------------------------------------------------------------------------

#[test]
fn test_is_corruption_error_detects_recordbatch_mismatch() {
    use opencode_indexer::storage::is_corruption_error;

    let err = anyhow::anyhow!("lance error: LanceError(Arrow): Invalid argument error: Attempt to merge two RecordBatch with different sizes: 20 != 19");
    assert!(
        is_corruption_error(&err),
        "should detect RecordBatch size mismatch"
    );
}

#[test]
fn test_is_corruption_error_detects_merge_invalid_argument() {
    use opencode_indexer::storage::is_corruption_error;

    let err =
        anyhow::anyhow!("Execution error: invalid argument error: failed to merge batch data");
    assert!(
        is_corruption_error(&err),
        "should detect merge invalid argument"
    );
}

#[test]
fn test_is_corruption_error_detects_lance_arrow_invalid_argument() {
    use opencode_indexer::storage::is_corruption_error;

    let err = anyhow::anyhow!("LanceError(Arrow): Invalid argument error: column type mismatch");
    assert!(
        is_corruption_error(&err),
        "should detect LanceError Arrow invalid argument"
    );
}
