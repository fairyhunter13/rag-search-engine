use anyhow::{Context, Result};
use arrow_array::builder::StringBuilder;
use arrow_array::{RecordBatch, RecordBatchIterator};
use arrow_schema::{DataType, Field, Schema};
use lancedb::connect;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, BufReader};
use tokio::process::Command;

fn schema() -> Schema {
    Schema::new(vec![
        Field::new("key", DataType::Utf8, false),
        Field::new("value", DataType::Utf8, false),
    ])
}

async fn set_config(db: &Path, key: &str, value: &str) -> Result<()> {
    std::fs::create_dir_all(db).context("create db dir")?;
    let db = connect(db.to_str().unwrap()).execute().await?;

    let names = db.table_names().execute().await?;
    let table = if names.contains(&"config".to_string()) {
        db.open_table("config").execute().await?
    } else {
        db.create_empty_table("config", schema().into())
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

    let schema = std::sync::Arc::new(schema());
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

fn project_id(root: &Path) -> Result<String> {
    let out = std::process::Command::new("git")
        .args(["rev-list", "--max-parents=0", "HEAD"])
        .current_dir(root)
        .output()
        .context("git rev-list")?;
    anyhow::ensure!(out.status.success(), "git rev-list failed");
    let stdout = String::from_utf8_lossy(&out.stdout);
    let commit = stdout.lines().last().unwrap_or("").trim();
    anyhow::ensure!(commit.len() >= 16, "invalid root commit hash");
    Ok(commit[..16].to_string())
}

fn init_repo() -> Result<tempfile::TempDir> {
    let dir = tempfile::TempDir::new().context("temp repo")?;
    let root = dir.path();

    let run = |args: &[&str]| -> Result<()> {
        let out = std::process::Command::new("git")
            .args(args)
            .current_dir(root)
            .output()
            .with_context(|| format!("git {:?}", args))?;
        anyhow::ensure!(out.status.success(), "git command failed: {:?}", args);
        Ok(())
    };

    run(&["init"])?;
    run(&["config", "user.email", "test@example.com"])?;
    run(&["config", "user.name", "Test"])?;
    std::fs::write(root.join("README.md"), "hello\n").context("write file")?;
    run(&["add", "."])?;
    run(&["commit", "-m", "init"])?;
    Ok(dir)
}

fn db_path(home: &Path, id: &str) -> PathBuf {
    home.join(".local")
        .join("share")
        .join("opencode")
        .join("projects")
        .join(id)
        .join(".lancedb")
}

async fn rpc(port: u16, method: &str, params: serde_json::Value) -> Result<serde_json::Value> {
    let client = reqwest::Client::new();
    let mut req = client
        .post(format!("http://127.0.0.1:{port}/rpc"))
        .json(&serde_json::json!({"method": method, "params": params}));
    if let Some(token) = read_auth_token() {
        req = req.header("x-indexer-token", token);
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

fn read_auth_token() -> Option<String> {
    let path = dirs::home_dir()?.join(".opencode").join("embedder.token");
    std::fs::read_to_string(path).ok().map(|s| s.trim().to_string())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn cli_status_self_heals_stuck_progress() -> Result<()> {
    let home = tempfile::TempDir::new().context("temp home")?;
    let repo = init_repo()?;
    let id = project_id(repo.path())?;
    let db = db_path(home.path(), &id);

    // Simulate an interrupted indexing run.
    set_config(&db, "indexing_in_progress", "true").await?;
    set_config(&db, "indexing_phase", "embedding").await?;
    set_config(&db, "embedding_done", "10").await?;
    set_config(&db, "embedding_total", "100").await?;

    let out = tokio::task::spawn_blocking({
        let home = home.path().to_path_buf();
        let root = repo.path().to_path_buf();
        move || {
            std::process::Command::new(assert_cmd::cargo::cargo_bin!("opencode-indexer"))
                .env("HOME", &home)
                .arg("--root")
                .arg(&root)
                .arg("--status")
                .arg("--json")
                .output()
        }
    })
    .await
    .unwrap()
    .context("run status")?;

    anyhow::ensure!(out.status.success(), "status command failed");
    let json: serde_json::Value = serde_json::from_slice(&out.stdout).context("parse json")?;
    assert_eq!(json["indexing_in_progress"], serde_json::Value::Bool(false));
    assert!(json["indexing_phase"].is_null());
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn daemon_status_self_heals_stuck_progress() -> Result<()> {
    let home = tempfile::TempDir::new().context("temp home")?;
    let repo = init_repo()?;
    let id = project_id(repo.path())?;
    let db = db_path(home.path(), &id);

    set_config(&db, "indexing_in_progress", "true").await?;
    set_config(&db, "indexing_phase", "embedding").await?;

    let mut child = Command::new(assert_cmd::cargo::cargo_bin!("opencode-indexer"))
        .env("HOME", home.path())
        .arg("--daemon")
        .arg("--port")
        .arg("0")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .context("spawn daemon")?;

    let stdout = child.stdout.take().context("no stdout")?;
    let mut reader = BufReader::new(stdout);
    let mut line = String::new();
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(10);
    let mut port = 0u16;

    loop {
        if tokio::time::Instant::now() > deadline {
            let _ = child.kill().await;
            anyhow::bail!("daemon did not become ready");
        }
        line.clear();
        reader.read_line(&mut line).await?;
        if let Ok(v) = serde_json::from_str::<serde_json::Value>(&line) {
            if v.get("type").and_then(|x| x.as_str()) == Some("http_ready") {
                port = v["port"].as_u64().unwrap_or(0) as u16;
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
            match reader.read(&mut d).await {
                Ok(0) | Err(_) => break,
                _ => {}
            }
        }
    });

    let result = rpc(
        port,
        "status",
        serde_json::json!({
            "db": db.to_string_lossy(),
            "dimensions": 1024,
        }),
    )
    .await?;

    assert_eq!(
        result["result"]["indexingInProgress"],
        serde_json::Value::Bool(false)
    );
    assert!(result["result"]["indexingPhase"].is_null());

    let _ = child.kill().await;
    Ok(())
}
