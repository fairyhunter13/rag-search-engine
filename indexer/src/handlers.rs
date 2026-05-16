//! RPC handler functions for the indexer daemon.
//!
//! These are the "`_impl`" functions that implement each JSON-RPC method.
//! Extracted from `daemon.rs` for module isolation.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};

use crate::daemon::{active_indexes, cached_storage, invalidate_storage_cache};
use crate::links::*;
use crate::search::*;
use crate::tui::canonicalize_project_key;

// ============================================================================
// Request dispatch
// ============================================================================

/// Process a single request, dispatching to the appropriate handler.
pub(crate) async fn handle_request(method: &str, params: &serde_json::Value) -> serde_json::Value {
    match method {
        "ping" => serde_json::json!({"pong": true}),

        "resolve_paths" => {
            let root = params["root"].as_str().unwrap_or(".");
            let shared = params["sharedPath"].as_str().unwrap_or("");
            let project_id = params["projectId"].as_str().unwrap_or("");
            resolve_paths_impl(root, shared, project_id)
        }

        "index_file" => {
            let root = params["root"].as_str().unwrap_or(".");
            let db = params["db"].as_str();
            let file = params["file"].as_str().unwrap_or("");
            let tier = params["tier"].as_str().unwrap_or("budget");
            let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;

            match index_file_impl(root, db, file, tier, dims).await {
                Ok(v) => v,
                Err(e) => serde_json::json!({"success": false, "error": e.to_string()}),
            }
        }

        // Batch variant: index multiple files in one call.
        // Returns {results: [{success, file, ...}]} matching IndexFileResult[] in TS.
        "index_files" => {
            let root = params["root"].as_str().unwrap_or(".");
            let db = params["db"].as_str();
            let tier = params["tier"].as_str().unwrap_or("budget");
            let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;
            let files: Vec<String> = params["files"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(String::from))
                        .collect()
                })
                .unwrap_or_default();

            let mut results = Vec::with_capacity(files.len());
            for file in &files {
                let r = match index_file_impl(root, db, file, tier, dims).await {
                    Ok(v) => v,
                    Err(e) => serde_json::json!({"success": false, "file": file, "error": e.to_string()}),
                };
                results.push(r);
            }
            serde_json::json!({"results": results})
        }

        "remove_file" => {
            let root = params["root"].as_str().unwrap_or(".");
            let db = params["db"].as_str();
            let file = params["file"].as_str().unwrap_or("");
            let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;

            match remove_file_impl(root, db, file, dims).await {
                Ok(v) => v,
                Err(e) => serde_json::json!({"success": false, "error": e.to_string()}),
            }
        }

        "search" => {
            let root = params["root"].as_str().unwrap_or(".");
            let db = params["db"].as_str();
            let query = params["query"].as_str().unwrap_or("");
            let tier = params["tier"].as_str().unwrap_or("budget");
            let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;
            let auto = params["autoFederate"].as_bool().unwrap_or(true);
            let explicit: Vec<String> = params["federatedDb"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(String::from))
                        .collect()
                })
                .unwrap_or_default();

            // Auto-discover linked DBs when none are explicitly provided.
            // If autoFederate is enabled, we also opportunistically index missing/empty linked DBs
            // in the background so federated search starts working without manual steps.
            let root_owned = root.to_string();
            let links = if auto {
                tokio::task::spawn_blocking(move || cached_discover_links(&root_owned))
                    .await
                    .unwrap_or_default()
            } else {
                Vec::new()
            };

            let mut mounts: HashMap<String, String> = HashMap::new();
            for link in &links {
                if link.mount.is_empty() {
                    continue;
                }
                mounts.insert(link.db.to_string_lossy().to_string(), link.mount.clone());
            }

            // Fire-and-forget: spawn background indexing for missing links
            // Don't block search - if a DB is missing/empty, it just returns no results
            if auto && explicit.is_empty() && !links.is_empty() {
                let links_clone = links.clone();
                let tier = tier.to_string();
                let root = root.to_string();
                tokio::spawn(async move {
                    for link in links_clone {
                        ensure_link_index(link, &tier, dims, &root).await;
                    }
                });
            }

            let federated = if !explicit.is_empty() {
                explicit
            } else if auto {
                links
                    .iter()
                    .filter_map(|l| l.db.to_str().map(String::from))
                    .collect()
            } else {
                vec![]
            };

            match search_impl(root, db, query, tier, dims, &federated, &mounts).await {
                Ok(v) => v,
                Err(e) => {
                    // Check for corruption and auto-recover
                    if crate::storage::is_corruption_error(&e) {
                        let storage_path = db
                            .map(PathBuf::from)
                            .unwrap_or_else(|| crate::storage::storage_path(&PathBuf::from(root)));

                        // Clear corrupted index
                        if let Ok(true) = crate::storage::clear_corrupted_index(&storage_path) {
                            // Invalidate the storage cache for this path
                            invalidate_storage_cache(&storage_path).await;

                            // Trigger background reindex
                            let root_owned = root.to_string();
                            let tier_owned = tier.to_string();
                            tokio::spawn(async move {
                                tracing::info!(
                                    "auto-recovery: triggering background reindex for {}",
                                    root_owned
                                );
                                // Small delay to let any pending operations settle
                                tokio::time::sleep(std::time::Duration::from_millis(500)).await;
                                if let Err(e) =
                                    run_index_background(&root_owned, &tier_owned, dims).await
                                {
                                    tracing::error!("auto-recovery reindex failed: {}", e);
                                }
                            });

                            serde_json::json!({
                                "results": [],
                                "rebuilding": true,
                                "message": "Index was corrupted and has been cleared. Rebuilding in background..."
                            })
                        } else {
                            serde_json::json!({"results": [], "error": e.to_string()})
                        }
                    } else {
                        serde_json::json!({"results": [], "error": e.to_string()})
                    }
                }
            }
        }

        "search_memories" => {
            let shared = params["sharedPath"].as_str().unwrap_or("");
            let project_id = params["projectId"].as_str().unwrap_or("");
            let query = params["query"].as_str().unwrap_or("");
            let limit = params["limit"].as_u64().unwrap_or(10) as usize;
            let tier = params["tier"].as_str().unwrap_or("budget");
            let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;
            let root = params["root"].as_str();
            let auto = params["autoFederate"].as_bool().unwrap_or(true);
            let explicit_ids: Vec<String> = params["federatedProjectIds"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(String::from))
                        .collect()
                })
                .unwrap_or_default();

            // Auto-discover linked project IDs when none are explicitly provided
            let federated_ids = if !explicit_ids.is_empty() {
                explicit_ids
            } else if auto {
                if let Some(r) = root {
                    let root_owned = r.to_string();
                    tokio::task::spawn_blocking(move || {
                        cached_discover_links(&root_owned)
                            .iter()
                            .map(|l| l.project_id.clone())
                            .collect()
                    })
                    .await
                    .unwrap_or_default()
                } else {
                    vec![]
                }
            } else {
                vec![]
            };

            match search_memories_impl(shared, project_id, query, limit, tier, dims, &federated_ids)
                .await
            {
                Ok(v) => v,
                Err(e) => serde_json::json!({"results": [], "error": e.to_string()}),
            }
        }

        "search_activity" => {
            let shared = params["sharedPath"].as_str().unwrap_or("");
            let project_id = params["projectId"].as_str().unwrap_or("");
            let query = params["query"].as_str().unwrap_or("");
            let limit = params["limit"].as_u64().unwrap_or(10) as usize;
            let tier = params["tier"].as_str().unwrap_or("budget");
            let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;

            match search_activity_impl(shared, project_id, query, limit, tier, dims).await {
                Ok(v) => v,
                Err(e) => serde_json::json!({"results": [], "error": e.to_string()}),
            }
        }

        "search_skills" => {
            let shared = params["sharedPath"].as_str().unwrap_or("");
            let query = params["query"].as_str().unwrap_or("");
            let limit = params["limit"].as_u64().unwrap_or(10) as usize;
            let tier = params["tier"].as_str().unwrap_or("budget");
            let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;

            match search_skills_impl(shared, query, limit, tier, dims).await {
                Ok(v) => v,
                Err(e) => serde_json::json!({"results": [], "error": e.to_string()}),
            }
        }

        "run_index" => {
            let root = params["root"].as_str().unwrap_or(".");
            let db = params["db"].as_str();
            let tier = params["tier"].as_str().unwrap_or("budget");
            let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;
            let force = params["force"].as_bool().unwrap_or(false);
            let exclude: Vec<String> = params["exclude"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(String::from))
                        .collect()
                })
                .unwrap_or_default();
            let include: Vec<String> = params["include"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(String::from))
                        .collect()
                })
                .unwrap_or_default();

            match run_index_impl(root, db, tier, dims, force, &exclude, &include).await {
                Ok(v) => v,
                Err(e) => serde_json::json!({"success": false, "error": e.to_string()}),
            }
        }

        "discover_files" => {
            let root = params["root"].as_str().unwrap_or(".").to_string();
            let exclude: Vec<String> = params["exclude"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(String::from))
                        .collect()
                })
                .unwrap_or_default();
            let include: Vec<String> = params["include"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(String::from))
                        .collect()
                })
                .unwrap_or_default();

            match tokio::task::spawn_blocking(move || {
                discover_files_impl(&root, &exclude, &include)
            })
            .await
            {
                Ok(Ok(v)) => v,
                Ok(Err(e)) => serde_json::json!({"files": [], "error": e.to_string()}),
                Err(e) => {
                    serde_json::json!({"files": [], "error": format!("spawn_blocking failed: {}", e)})
                }
            }
        }

        "status" => {
            let root = params["root"].as_str();
            let db = params["db"].as_str();
            let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;
            match status_impl(root, db, dims).await {
                Ok(v) => v,
                Err(e) => serde_json::json!({"error": e.to_string()}),
            }
        }

        "discover_links" => {
            let root = params["root"].as_str().unwrap_or(".").to_string();
            match tokio::task::spawn_blocking(move || discover_links_impl(&root)).await {
                Ok(Ok(v)) => v,
                Ok(Err(e)) => serde_json::json!({"links": [], "error": e.to_string()}),
                Err(e) => {
                    serde_json::json!({"links": [], "error": format!("spawn_blocking failed: {}", e)})
                }
            }
        }

        "invalidate_links_cache" => {
            let root = params["root"].as_str().unwrap_or(".");
            match PathBuf::from(root).canonicalize() {
                Ok(root_path) => {
                    invalidate_links_cache(&root_path).await;
                    serde_json::json!({"success": true})
                }
                Err(e) => serde_json::json!({"success": false, "error": e.to_string()}),
            }
        }

        "index_linked_projects" => {
            let root = params["root"].as_str().unwrap_or(".");
            let tier = params["tier"].as_str().unwrap_or("budget");
            let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;

            let root_owned = root.to_string();
            let links = tokio::task::spawn_blocking(move || cached_discover_links(&root_owned))
                .await
                .unwrap_or_default();
            let count = links.len();
            for link in links {
                ensure_link_index(link, tier, dims, root).await;
            }

            serde_json::json!({"success": true, "triggered": count})
        }

        "health" => {
            let root = params["root"].as_str().unwrap_or(".");
            let db = params["db"].as_str();
            let dims: u32 = params["dimensions"].as_u64().unwrap_or(1024) as u32;
            let shared = params["sharedPath"].as_str().unwrap_or("");
            let project_id = params["projectId"].as_str().unwrap_or("");
            match health_impl(root, db, dims, shared, project_id).await {
                Ok(v) => v,
                Err(e) => serde_json::json!({"error": e.to_string()}),
            }
        }

        // These methods are intercepted by the server loop before reaching handle_request().
        // They require access to shared DaemonState and are handled directly in the server loop.
        // This arm exists only as a safety net - if reached, it indicates a bug in the server loop.
        "tui_connect" | "tui_disconnect" | "tui_connections" | "watcher_stop" | "watcher_start"
        | "watcher_status" | "startup_check" | "shutdown" => {
            tracing::error!(
                "method {} should be handled by server loop, not handle_request()",
                method
            );
            serde_json::json!({"error": format!("internal error: {} should be handled by server loop", method)})
        }

        "cleanup" => {
            let cfg = crate::cleaner::config();
            let base = crate::storage::shared_data_dir();
            match tokio::task::spawn_blocking(move || crate::cleaner::run(&base, &cfg, false)).await
            {
                Ok(report) => serde_json::json!({
                    "success": true,
                    "orphans": report.orphans,
                    "stale": report.stale,
                    "aux_dirs": report.aux_dirs,
                    "freed": report.freed,
                    "errors": report.errors,
                }),
                Err(e) => serde_json::json!({"success": false, "error": e.to_string()}),
            }
        }

        "cleanup_dry_run" => {
            let cfg = crate::cleaner::config();
            let base = crate::storage::shared_data_dir();
            match tokio::task::spawn_blocking(move || crate::cleaner::run(&base, &cfg, true)).await
            {
                Ok(report) => serde_json::json!({
                    "success": true,
                    "orphans": report.orphans,
                    "stale": report.stale,
                    "aux_dirs": report.aux_dirs,
                    "freed": report.freed,
                    "errors": report.errors,
                    "targets": report.targets,
                }),
                Err(e) => serde_json::json!({"success": false, "error": e.to_string()}),
            }
        }

        // Stubs for methods defined in the TS client but not yet implemented
        // in the Rust daemon. Return a structured response instead of the
        // "unknown method" error that triggers restartDaemon() in the TS layer.
        "db_execute" => serde_json::json!({"success": false, "error": "db_execute not implemented"}),
        "db_maintenance" => serde_json::json!({"success": false, "error": "db_maintenance not implemented"}),

        _ => serde_json::json!({"error": format!("unknown method: {method}")}),
    }
}

/// Extract the project key from request params (async with caching).
/// Uses `root` for index/remove/run_index, `db` for status, `sharedPath` for memory/activity.
pub(crate) async fn project_key(params: &serde_json::Value) -> String {
    if let Some(root) = params["root"].as_str() {
        if !root.is_empty() {
            return canonicalize_project_key(root).await;
        }
    }
    if let Some(db) = params["db"].as_str() {
        return db.to_string();
    }
    if let Some(shared) = params["sharedPath"].as_str() {
        let pid = params["projectId"].as_str().unwrap_or("global");
        return format!("{shared}::{pid}");
    }
    "__global__".to_string()
}

// ============================================================================
// resolve_paths — all path derivation logic lives here
// ============================================================================

fn resolve_paths_impl(root: &str, shared: &str, project_id: &str) -> serde_json::Value {
    use crate::storage;

    let root = PathBuf::from(root);
    let pid = if project_id.is_empty() {
        storage::git_project_id(&root)
    } else {
        project_id.to_string()
    };

    let db = storage::storage_path(&root);
    let shared = PathBuf::from(shared);
    let project_dir = shared.join("projects").join(&pid);
    let memory_dir = project_dir.join("memories");
    let activity_dir = project_dir.join("activity");
    let memory_db = memory_dir.join(".lancedb");
    let activity_db = activity_dir.join(".lancedb");
    let global_memory_dir = shared.join("memories").join("global");
    let global_db = global_memory_dir.join(".lancedb");

    serde_json::json!({
        "projectId": pid,
        "dbPath": db.to_str(),
        "memoryDir": memory_dir.to_str(),
        "activityDir": activity_dir.to_str(),
        "memoryDbPath": memory_db.to_str(),
        "activityDbPath": activity_db.to_str(),
        "globalMemoryDir": global_memory_dir.to_str(),
        "globalDbPath": global_db.to_str(),
    })
}

// ============================================================================
// index_file
// ============================================================================

async fn index_file_impl(
    root: &str,
    db: Option<&str>,
    file: &str,
    tier: &str,
    dims: u32,
) -> Result<serde_json::Value> {
    use crate::storage;

    let root = PathBuf::from(root).canonicalize()?;
    let storage_path = db
        .map(PathBuf::from)
        .unwrap_or_else(|| storage::storage_path(&root));
    let storage = cached_storage(&storage_path, dims).await?;

    let file_path = if Path::new(file).is_absolute() {
        PathBuf::from(file)
    } else {
        root.join(file)
    };
    let file_path = file_path.canonicalize().context("invalid file")?;

    // Fast-path: skip unchanged files without connecting to the model server.
    // This keeps re-index latency low and avoids unnecessary connection churn.
    let rel = crate::discover::relative_path(&file_path, &root, &[]);
    if let Ok(content) = tokio::fs::read_to_string(&file_path).await {
        let file_hash = storage::hash_content(&content);
        if !storage.needs_index(&rel, &file_hash).await.unwrap_or(true) {
            return Ok(serde_json::json!({"success": true, "skipped": true, "path": rel}));
        }
    }

    // Use the partial update algorithm: hash-match existing chunks, only embed new ones.
    // This avoids re-embedding unchanged chunks and preserves chunk IDs.
    let mut client = crate::model_client::client().await?;
    let embed = tokio::sync::Semaphore::new(1);

    // Create WriteQueue for this single file operation
    let write_queue = crate::storage::WriteQueue::new(storage.clone(), 32);

    let result = crate::cli::update_file_partial_pub(
        &root,
        &[], // include_dirs
        &[], // symlink_dirs (not applicable for daemon single-file indexing)
        &storage_path,
        &*storage,
        &mut client,
        &file_path,
        tier,
        dims,
        "int8", // quantization
        None,   // daily_cost_limit
        &embed,
        false, // force
        false, // verbose
        &write_queue,
    )
    .await?;

    // Wait for all writes to complete
    let _stats = write_queue.shutdown().await;

    match result {
        Some(update) => {
            // Update last_update_timestamp after successful indexing
            let now = chrono::Utc::now().to_rfc3339();
            let _ = storage.set_last_update_timestamp(&now).await;

            // Invalidate query cache for this DB
            invalidate_query_cache(&storage_path).await;

            Ok(serde_json::json!({
                "success": true,
                "chunks": update.chunks,
                "kept": update.kept,
                "deleted": update.deleted,
                "inserted": update.inserted,
                "embedded": update.embedded,
                "path": crate::discover::relative_path(&file_path, &root, &[]),
            }))
        }
        None => Ok(serde_json::json!({"success": true, "skipped": true})),
    }
}

// ============================================================================
// remove_file
// ============================================================================

async fn remove_file_impl(
    root: &str,
    db: Option<&str>,
    file: &str,
    dims: u32,
) -> Result<serde_json::Value> {
    use crate::discover::relative_path;
    use crate::storage;

    let root = PathBuf::from(root).canonicalize()?;
    let storage_path = db
        .map(PathBuf::from)
        .unwrap_or_else(|| storage::storage_path(&root));
    let storage = cached_storage(&storage_path, dims).await?;
    let rel = relative_path(Path::new(file), &root, &[]);

    // Get count before deletion
    let chunks = storage.get_chunks_with_hashes(&rel).await?;
    let removed = chunks.len();

    // Create WriteQueue for deletion
    let write_queue = crate::storage::WriteQueue::new(storage.clone(), 32);
    write_queue.delete_file(&rel).await;

    // Wait for deletion to complete
    let _ = write_queue.shutdown().await;

    // Invalidate query cache for this DB
    invalidate_query_cache(&storage_path).await;

    Ok(serde_json::json!({"success": true, "removed": removed, "path": rel}))
}

// ============================================================================
// run_index — full project indexing via existing run_indexing pipeline
// ============================================================================

pub(crate) async fn run_index_impl(
    root: &str,
    db: Option<&str>,
    tier: &str,
    dims: u32,
    force: bool,
    exclude: &[String],
    include: &[String],
) -> Result<serde_json::Value> {
    use crate::storage;

    let root = PathBuf::from(root).canonicalize()?;
    let storage_path = db
        .map(PathBuf::from)
        .unwrap_or_else(|| storage::storage_path(&root));

    // Register this db path as actively indexing so status_impl won't self-heal it away.
    let key = storage_path.to_string_lossy().to_string();
    active_indexes().lock().await.insert(key.clone());
    // Ensure we deregister on any exit path (success or error).
    struct Guard(String);
    impl Drop for Guard {
        fn drop(&mut self) {
            // Spawn a task because Drop can't be async; the lock is non-blocking in practice.
            let k = self.0.clone();
            tokio::spawn(async move {
                active_indexes().lock().await.remove(&k);
            });
        }
    }
    let _guard = Guard(key);

    // Note: Daemon is a singleton (enforced by port lock); no external watcher PID check needed.
    // Acquiring it here would cause a double-lock error since the daemon's PID
    // would be written to the lock file, and then run_indexing would detect it
    // as "already running".

    let include_paths: Vec<PathBuf> = include.iter().map(PathBuf::from).collect();

    let start = std::time::Instant::now();
    let result = crate::cli::run_indexing_pub(
        &root,
        &storage_path,
        tier,
        dims,
        "int8",
        force,
        None,  // daily_cost_limit
        false, // verbose
        exclude,
        &include_paths,
        embed_concurrency(), // concurrency
        None,                // scan_concurrency
        false,               // quiet
        false,               // json_lines
    )
    .await;

    // If indexing failed with a LanceDB IO/corruption error, delete the corrupted
    // database and retry once.  Storage::open creates chunks.lance even with 0 rows,
    // so a partially-written or power-interrupted index can leave empty lance data
    // files that permanently break subsequent reads.
    let stats = match result {
        Ok(s) => s,
        Err(e) => {
            let msg = format!("{e:#}");
            let is_lance = msg.contains("LanceError") || msg.contains("lance error");
            if is_lance && storage_path.exists() {
                tracing::warn!(
                    "run_index: lance corruption detected at {}, removing and retrying: {msg}",
                    storage_path.display()
                );
                // Evict any cached Storage handle for this path so the retry
                // opens a fresh connection.
                invalidate_storage_cache(&storage_path).await;
                if let Err(rm_err) = tokio::fs::remove_dir_all(&storage_path).await {
                    tracing::error!(
                        "run_index: failed to remove corrupted db at {}: {rm_err}",
                        storage_path.display()
                    );
                    return Err(e);
                }
                // Retry once with a clean slate
                crate::cli::run_indexing_pub(
                    &root,
                    &storage_path,
                    tier,
                    dims,
                    "int8",
                    force,
                    None,
                    false,
                    exclude,
                    &include_paths,
                    5,
                    None,
                    false,
                    false,
                )
                .await?
            } else {
                return Err(e);
            }
        }
    };

    let elapsed = start.elapsed();
    Ok(serde_json::json!({
        "success": true,
        "files": stats.processed,
        "modified": stats.modified,
        "embeddings": stats.embedded,
        "duration": elapsed.as_secs_f64(),
    }))
}

// ============================================================================
// discover_files — dry-run file listing
// ============================================================================

fn discover_files_impl(
    root: &str,
    exclude: &[String],
    include: &[String],
) -> Result<serde_json::Value> {
    use crate::config;
    use crate::discover;

    let root = PathBuf::from(root).canonicalize()?;
    let project = config::load(&root);
    let mut cfg = config::effective(&project, None, None);
    cfg.exclude.extend(exclude.iter().cloned());

    let discovery = discover::discover_files_with_config(&root, &cfg)?;

    let include_dirs: Vec<PathBuf> = include.iter().map(PathBuf::from).collect();
    let mut files: Vec<String> = discovery
        .files
        .iter()
        .map(|p| discover::relative_path(p, &root, &include_dirs))
        .collect();

    // Add files from include dirs
    if !include_dirs.is_empty() {
        let mut seen: std::collections::HashSet<PathBuf> = discovery
            .files
            .iter()
            .filter_map(|p| p.canonicalize().ok())
            .collect();
        let extra = discover::discover_additional_files(
            &include_dirs,
            if exclude.is_empty() {
                None
            } else {
                Some(exclude)
            },
            &mut seen,
        );
        for f in &extra {
            files.push(discover::relative_path(f, &root, &include_dirs));
        }
    }

    Ok(serde_json::json!({"files": files, "count": files.len()}))
}

// ============================================================================
// status
// ============================================================================

pub(crate) async fn status_impl(root: Option<&str>, db: Option<&str>, dims: u32) -> Result<serde_json::Value> {
    let db = db.context("db path required")?;
    let sp = PathBuf::from(db);
    if !tokio::fs::try_exists(&sp).await.unwrap_or(false) {
        return Ok(serde_json::json!({"exists": false}));
    }

    let storage = cached_storage(&sp, dims).await?;

    // Lazy migration: backfill missing metadata for legacy indexes
    // This ensures status bar shows upd/speed info without needing to open Index Status dialog
    match storage.backfill_metadata().await {
        Ok(count) if count > 0 => {
            tracing::info!("status: auto-fixed {} metadata field(s)", count);
        }
        Ok(_) => {} // No backfill needed
        Err(e) => {
            tracing::warn!("status: metadata backfill failed: {}", e);
        }
    }

    // If indexing was interrupted (SIGKILL, crash, etc.), the progress keys can
    // get stuck. Self-heal by clearing progress on status check (daemon singleton
    // enforced by port lock; no PID files needed).
    // Guard: skip self-heal when this db is actively being indexed to avoid a
    // race where status clears progress set by the concurrent run_index call.
    {
        let active = active_indexes().lock().await;
        if !active.contains(&sp.to_string_lossy().to_string()) {
            if storage.get_indexing_in_progress().await.unwrap_or(false) {
                let _ = storage.clear_indexing_progress().await;
            }
        }
    }
    // Try cached counts first (< 1ms) — eliminates 2-20s table scans
    let (chunks, files, chunks_corrupted, files_corrupted) = if let Some((cached_files, cached_chunks)) = storage.get_cached_counts().await {
        // Cache hit — instant response
        (cached_chunks, cached_files, false, false)
    } else {
        // Cache miss or stale — do full scan and update cache
        let (chunks_val, chunks_corrupted_val) = match storage.count_chunks().await {
            Ok(c) => (c, false),
            Err(e) if crate::storage::is_corruption_error(&e) => {
                tracing::warn!("status: corruption detected in count_chunks: {e:#}");
                (0, true)
            }
            Err(_) => (0, false),
        };
        let (files_val, files_corrupted_val) = match storage.get_indexed_files().await {
            Ok(f) => (f.len(), false),
            Err(e) if crate::storage::is_corruption_error(&e) => {
                tracing::warn!("status: corruption detected in get_indexed_files: {e:#}");
                (0, true)
            }
            Err(_) => (0, false),
        };

        // Update cache with fresh counts (if no corruption)
        if !chunks_corrupted_val && !files_corrupted_val {
            storage.update_cached_counts(files_val, chunks_val).await;
        }

        (chunks_val, files_val, chunks_corrupted_val, files_corrupted_val)
    };
    let tier = storage.get_tier().await.unwrap_or(None);

    let last_duration_ms = storage.get_last_index_duration_ms().await.unwrap_or(None);
    let last_files_count = storage.get_last_index_files_count().await.unwrap_or(None);
    let last_indexed = storage.get_last_index_timestamp().await.unwrap_or(None);
    let last_updated = storage
        .get_last_update_timestamp()
        .await
        .unwrap_or(None)
        .or_else(|| last_indexed.clone());
    let last_watched = storage.get_last_watched_timestamp().await.unwrap_or(None);

    let files_per_sec = match (last_duration_ms, last_files_count) {
        (Some(ms), Some(count)) if ms > 0 => Some((count as f64) / (ms as f64 / 1000.0)),
        _ => None,
    };

    let indexing_in_progress = storage.get_indexing_in_progress().await.unwrap_or(false);
    let indexing_started_at = storage.get_indexing_start_time().await.unwrap_or(None);
    let indexing_phase = storage.get_indexing_phase().await.unwrap_or(None);
    let (scanning_done, scanning_total) = storage
        .get_phase_progress("scanning")
        .await
        .unwrap_or((0, 0));
    let (chunking_done, chunking_total) = storage
        .get_phase_progress("chunking")
        .await
        .unwrap_or((0, 0));
    let (embedding_done, embedding_total) = storage
        .get_phase_progress("embedding")
        .await
        .unwrap_or((0, 0));

    // Check for index corruption by verifying lance table directories exist
    let mut corrupted = files_corrupted || chunks_corrupted;
    let mut corruption_errors: Vec<String> = Vec::new();
    if files_corrupted {
        corruption_errors.push("Arrow RecordBatch error in get_indexed_files".into());
    }
    if chunks_corrupted {
        corruption_errors.push("Arrow RecordBatch error in count_chunks".into());
    }

    if chunks > 0 || files > 0 {
        // If we have data, verify the lance tables are intact
        let chunks_table = sp.join("chunks.lance");
        let config_table = sp.join("config.lance");

        if !tokio::fs::metadata(&chunks_table)
            .await
            .map(|m| m.is_dir())
            .unwrap_or(false)
        {
            corrupted = true;
            corruption_errors.push("Missing chunks.lance table directory".into());
        } else {
            // Check for data files in chunks.lance/data/
            let data_dir = chunks_table.join("data");
            if tokio::fs::metadata(&data_dir)
                .await
                .map(|m| m.is_dir())
                .unwrap_or(false)
            {
                // Verify at least one .lance file exists
                let has_data = {
                    let mut found = false;
                    if let Ok(mut dir) = tokio::fs::read_dir(&data_dir).await {
                        while let Ok(Some(entry)) = dir.next_entry().await {
                            if entry.path().extension().map_or(false, |ext| ext == "lance") {
                                found = true;
                                break;
                            }
                        }
                    }
                    found
                };
                if !has_data {
                    corrupted = true;
                    corruption_errors.push("No data files found in chunks.lance/data/".into());
                }
            }
        }

        if !tokio::fs::metadata(&config_table)
            .await
            .map(|m| m.is_dir())
            .unwrap_or(false)
        {
            corrupted = true;
            corruption_errors.push("Missing config.lance table directory".into());
        } else {
            // Check for data files in config.lance/data/
            let data_dir = config_table.join("data");
            if tokio::fs::metadata(&data_dir)
                .await
                .map(|m| m.is_dir())
                .unwrap_or(false)
            {
                let has_data = {
                    let mut found = false;
                    if let Ok(mut dir) = tokio::fs::read_dir(&data_dir).await {
                        while let Ok(Some(entry)) = dir.next_entry().await {
                            if entry.path().extension().map_or(false, |ext| ext == "lance") {
                                found = true;
                                break;
                            }
                        }
                    }
                    found
                };
                if !has_data {
                    corrupted = true;
                    corruption_errors.push("No data files found in config.lance/data/".into());
                }
            }
        }
    }

    // Auto-recover from corruption detected via Arrow/LanceDB errors
    let mut rebuilding = false;
    if corrupted {
        // Dedup guard: don't spawn recovery if indexing is already in progress
        let already_indexing = storage.get_indexing_in_progress().await.unwrap_or(false);
        if !already_indexing {
            if let Some(project_root) = root {
                let root_owned = project_root.to_string();
                let tier_str = storage
                    .get_tier()
                    .await
                    .unwrap_or(None)
                    .unwrap_or_else(|| "budget".to_string());
                tracing::warn!("status_impl: auto-recovering corrupted index for {root_owned}");
                if let Ok(true) = crate::storage::clear_corrupted_index(&sp) {
                    invalidate_storage_cache(&sp).await;
                    rebuilding = true;
                    let tier_owned = tier_str.clone();
                    tokio::spawn(async move {
                        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
                        if let Err(e) = run_index_background(&root_owned, &tier_owned, dims).await {
                            tracing::warn!("status_impl: background reindex after corruption recovery failed: {e:#}");
                        }
                    });
                }
            } else {
                tracing::warn!("status_impl: corruption detected but no project root available for auto-recovery — run /index manually");
            }
        }
    }

    Ok(serde_json::json!({
        "exists": true,
        "indexed": files > 0 || chunks > 0,
        "files": files,
        "chunks": chunks,
        "tier": tier,
        "lastIndexed": last_indexed,
        "lastUpdated": last_updated,
        "lastWatched": last_watched,
        "lastIndexDurationMs": last_duration_ms,
        "lastIndexFilesCount": last_files_count,
        "filesPerSec": files_per_sec,
        "indexingInProgress": indexing_in_progress,
        "indexingStartedAt": indexing_started_at,
        "indexingPhase": indexing_phase,
        "scanningDone": scanning_done,
        "scanningTotal": scanning_total,
        "chunkingDone": chunking_done,
        "chunkingTotal": chunking_total,
        "embeddingDone": embedding_done,
        "embeddingTotal": embedding_total,
        "corrupted": corrupted,
        "corruptionErrors": corruption_errors,
        "rebuilding": rebuilding,
    }))
}

// ============================================================================
// discover_links
// ============================================================================

pub(crate) fn discover_links_impl(root: &str) -> Result<serde_json::Value> {
    use crate::config;
    use crate::discover;
    use crate::storage;

    let root = PathBuf::from(root).canonicalize()?;
    let project = config::load(&root);
    let cfg = config::effective(&project, None, None);

    // Use discover_files_with_config to get skipped_repos, matching CLI behavior
    let discovery = discover::discover_files_with_config(&root, &cfg)?;

    let mut links = Vec::new();
    for repo in &discovery.skipped_repos {
        let name = repo
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("unknown")
            .to_string();

        // Skip repos marked with skip: true in .opencode-index.yaml
        if project.linked.get(&name).map(|l| l.skip).unwrap_or(false) {
            continue;
        }

        let id = storage::git_project_id(repo);
        let db = storage::storage_path(repo);
        links.push(serde_json::json!({
            "path": repo.to_str().unwrap_or(""),
            "projectId": id,
            "name": name,
            "dbPath": db.to_str().unwrap_or(""),
        }));
    }

    // Also detect git submodules as linked projects
    let submodules = discover::discover_submodules(&root);
    for (sub_path, name) in &submodules {
        // Skip if already found as a symlink-based link
        if links
            .iter()
            .any(|l| l["path"].as_str() == Some(sub_path.to_str().unwrap_or("")))
        {
            continue;
        }
        // Skip repos marked with skip: true in .opencode-index.yaml
        if project.linked.get(name).map(|l| l.skip).unwrap_or(false) {
            continue;
        }
        let id = storage::git_project_id(sub_path);
        let db = storage::storage_path(sub_path);
        links.push(serde_json::json!({
            "path": sub_path.to_str().unwrap_or(""),
            "projectId": id,
            "name": name,
            "dbPath": db.to_str().unwrap_or(""),
            "submodule": true,
        }));
    }

    // Also detect nested git repos that aren't registered submodules
    // (directories with .git files instead of .git directories)
    let nested = discover::discover_nested_git_repos(&root);
    for (repo_path, name) in &nested {
        if links
            .iter()
            .any(|l| l["path"].as_str() == Some(repo_path.to_str().unwrap_or("")))
        {
            continue;
        }
        if project.linked.get(name).map(|l| l.skip).unwrap_or(false) {
            continue;
        }
        let id = storage::git_project_id(repo_path);
        let db = storage::storage_path(repo_path);
        links.push(serde_json::json!({
            "path": repo_path.to_str().unwrap_or(""),
            "projectId": id,
            "name": name,
            "dbPath": db.to_str().unwrap_or(""),
            "nested_repo": true,
        }));
    }

    Ok(serde_json::json!({
        "rootProjectId": storage::git_project_id(&root),
        "links": links,
    }))
}

// ============================================================================
// health — comprehensive check (all logic here, TS just forwards)
// ============================================================================

async fn health_impl(
    root: &str,
    db: Option<&str>,
    dims: u32,
    shared: &str,
    project_id: &str,
) -> Result<serde_json::Value> {
    let root_path = PathBuf::from(root);
    let storage_path = db
        .map(PathBuf::from)
        .unwrap_or_else(|| crate::storage::storage_path(&root_path));
    let exists = tokio::fs::try_exists(&storage_path).await.unwrap_or(false);

    let mut result = serde_json::json!({
        "healthy": true,
        "root": root,
        "indexExists": exists,
        "dbPath": storage_path.to_str(),
        "errors": [],
    });

    // Index status
    if exists {
        if let Ok(storage) = cached_storage(&storage_path, dims).await {
            // Lazy migration: backfill missing metadata for legacy indexes
            match storage.backfill_metadata().await {
                Ok(count) if count > 0 => {
                    tracing::info!("health check: auto-fixed {} metadata field(s)", count);
                }
                Ok(_) => {} // No backfill needed
                Err(e) => {
                    tracing::warn!("health check: metadata backfill failed: {}", e);
                }
            }

            let chunks = storage.count_chunks().await.unwrap_or(0);
            let files = storage.get_file_count().await.unwrap_or(0);
            let tier = storage.get_tier().await.unwrap_or(None);
            result["files"] = serde_json::json!(files);
            result["chunks"] = serde_json::json!(chunks);
            result["tier"] = serde_json::json!(tier);

            let last_duration_ms = storage.get_last_index_duration_ms().await.unwrap_or(None);
            let last_files_count = storage.get_last_index_files_count().await.unwrap_or(None);
            let last_indexed = storage.get_last_index_timestamp().await.unwrap_or(None);
            let last_updated = storage
                .get_last_update_timestamp()
                .await
                .unwrap_or(None)
                .or_else(|| last_indexed.clone());
            let last_watched = storage.get_last_watched_timestamp().await.unwrap_or(None);
            let files_per_sec = match (last_duration_ms, last_files_count) {
                (Some(ms), Some(count)) if ms > 0 => Some((count as f64) / (ms as f64 / 1000.0)),
                _ => None,
            };

            result["lastIndexed"] = serde_json::json!(last_indexed);
            result["lastUpdated"] = serde_json::json!(last_updated);
            result["lastWatched"] = serde_json::json!(last_watched);
            result["lastIndexDurationMs"] = serde_json::json!(last_duration_ms);
            result["lastIndexFilesCount"] = serde_json::json!(last_files_count);
            result["filesPerSec"] = serde_json::json!(files_per_sec);
        }
    }

    // Index integrity
    if exists {
        let chunks_table = storage_path.join("chunks.lance");
        let config_table = storage_path.join("config.lance");
        let mut errors: Vec<String> = Vec::new();
        if !tokio::fs::metadata(&chunks_table)
            .await
            .map(|m| m.is_dir())
            .unwrap_or(false)
        {
            errors.push("Missing chunks.lance table".into());
        }
        if !tokio::fs::metadata(&config_table)
            .await
            .map(|m| m.is_dir())
            .unwrap_or(false)
        {
            errors.push("Missing config.lance table".into());
        }
        if !errors.is_empty() {
            result["healthy"] = serde_json::json!(false);
            result["errors"] = serde_json::json!(errors);
        }
    }

    // Linked projects
    let root_owned = root.to_string();
    if let Ok(Ok(links)) =
        tokio::task::spawn_blocking(move || discover_links_impl(&root_owned)).await
    {
        if let Some(arr) = links["links"].as_array() {
            let mut linked: Vec<serde_json::Value> = Vec::new();
            for link in arr {
                let mut entry = link.clone();
                let link_db = link["dbPath"].as_str().unwrap_or("");
                let link_path = PathBuf::from(link_db);
                if tokio::fs::try_exists(&link_path).await.unwrap_or(false) {
                    if let Ok(s) = cached_storage(&link_path, dims).await {
                        let files = s.get_file_count().await.unwrap_or(0);
                        let chunks = s.count_chunks().await.unwrap_or(0);
                        entry["indexed"] = serde_json::json!(files > 0 || chunks > 0);
                        entry["files"] = serde_json::json!(files);
                        entry["chunks"] = serde_json::json!(chunks);
                    } else {
                        entry["indexed"] = serde_json::json!(false);
                    }
                } else {
                    entry["indexed"] = serde_json::json!(false);
                }
                linked.push(entry);
            }
            result["linkedProjects"] = serde_json::json!(linked);
        }
    }

    // Global memory index
    if !shared.is_empty() {
        let global_db = PathBuf::from(shared)
            .join("memories")
            .join("global")
            .join(".lancedb");
        result["globalIndex"] = serde_json::json!({
            "exists": tokio::fs::try_exists(&global_db).await.unwrap_or(false),
            "path": global_db.to_str(),
        });

        // Memory dirs
        if !project_id.is_empty() {
            let project_mem = PathBuf::from(shared)
                .join("projects")
                .join(project_id)
                .join("memories");
            let global_mem = PathBuf::from(shared).join("memories").join("global");
            result["memoryDirs"] = serde_json::json!({
                "project": project_mem.to_str(),
                "global": global_mem.to_str(),
            });
        }
    }

    Ok(result)
}

// ============================================================================
// watcher_status — check if a watcher is running for a project
// ============================================================================

/// Check watcher status with TUI connection count.
/// `watcher_active` is checked by the caller from state.watchers to handle the
/// race window between the initial watcher-data lock and this second lock.
pub(crate) fn watcher_status_with_connections(
    root: &str,
    db: Option<&str>,
    connection_count: usize,
    watcher_active: bool,
) -> serde_json::Value {
    let root_path = PathBuf::from(root);
    let storage_path = db
        .map(PathBuf::from)
        .unwrap_or_else(|| crate::storage::storage_path(&root_path));

    serde_json::json!({
        "watcherActive": watcher_active,
        "internal": watcher_active,
        "watcherPid": null,
        "indexerActive": false,
        "indexerPid": null,
        "dbPath": storage_path.to_str(),
        "connectionCount": connection_count,
    })
}
