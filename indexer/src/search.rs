//! Search implementation for the indexer daemon.
//!
//! Provides federated search across multiple project databases with adaptive
//! hybrid reranking. Strategy adapts based on number of federated projects:
//! - Few projects (≤5): Two-stage (per-project rerank + global rerank) for best quality
//! - Many projects (>5): Vector-only + global rerank for speed (skip per-project rerank)

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant};

use anyhow::Result;
use tokio::sync::RwLock;

use crate::daemon::cached_storage;

// ============================================================================
// search (with federated support) — ADAPTIVE HYBRID RERANKING
//
// Strategy adapts based on number of federated projects:
// - Few projects (≤5): Two-stage (per-project rerank + global rerank) for best quality
// - Many projects (>5): Vector-only + global rerank for speed (skip per-project rerank)
//
// This balances quality vs latency for large federated searches.
// ============================================================================

/// Number of results to keep per project in stage 1 (before global rerank)
const STAGE1_TOP_K: u32 = 15;
/// Maximum results to feed into global rerank (prevents context overflow)
const GLOBAL_RERANK_MAX: usize = 100;
/// Final number of results to return
const FINAL_TOP_K: u32 = 10;
/// Threshold: skip per-project rerank if more than this many projects (for speed)
const SKIP_STAGE1_RERANK_THRESHOLD: usize = 5;
/// Results per project when skipping stage 1 rerank (use vector score only)
const VECTOR_ONLY_TOP_K: usize = 10;
/// Warning threshold for too many federated DBs
const FEDERATED_DB_WARNING_THRESHOLD: usize = 50;

/// Semaphore for limiting concurrent federated searches.
/// Configurable via OPENCODE_FEDERATED_CONCURRENCY environment variable.
/// Defaults to min(num_cpus, 4) to balance parallelism with resource usage.
pub(crate) fn federated_search_semaphore() -> &'static tokio::sync::Semaphore {
    static SEM: std::sync::OnceLock<tokio::sync::Semaphore> = std::sync::OnceLock::new();
    SEM.get_or_init(|| {
        let limit = std::env::var("OPENCODE_FEDERATED_CONCURRENCY")
            .ok()
            .and_then(|s| s.parse::<usize>().ok())
            .unwrap_or_else(|| num_cpus::get().min(4));

        tracing::debug!("federated search concurrency limit: {}", limit);
        tokio::sync::Semaphore::new(limit)
    })
}

// ============================================================================
// Query Cache - LRU cache for repeated search queries
// ============================================================================

/// Cached search result with metadata
#[derive(Clone)]
pub(crate) struct QueryCacheEntry {
    results: serde_json::Value,
    timestamp: Instant,
}

/// Cache key for a search query
#[derive(Hash, Eq, PartialEq, Clone)]
pub(crate) struct QueryCacheKey {
    query: String,
    db_path: String,
    tier: String,
    dims: u32,
    federated: Vec<String>,
}

impl QueryCacheKey {
    fn new(
        query: &str,
        db_path: &str,
        tier: &str,
        dims: u32,
        federated: &[String],
    ) -> Self {
        Self {
            query: query.to_lowercase().trim().to_string(), // normalize query
            db_path: db_path.to_string(),
            tier: tier.to_string(),
            dims,
            federated: federated.to_vec(),
        }
    }
}

/// Global LRU cache for query results
pub(crate) fn query_cache() -> &'static RwLock<lru::LruCache<QueryCacheKey, QueryCacheEntry>> {
    static CACHE: OnceLock<RwLock<lru::LruCache<QueryCacheKey, QueryCacheEntry>>> = OnceLock::new();
    CACHE.get_or_init(|| {
        let capacity = std::env::var("OPENCODE_QUERY_CACHE_SIZE")
            .ok()
            .and_then(|s| s.parse::<usize>().ok())
            .unwrap_or(50);

        let cap = std::num::NonZeroUsize::new(capacity).unwrap_or(std::num::NonZeroUsize::new(50).unwrap());
        tracing::debug!("query cache size: {}", capacity);
        RwLock::new(lru::LruCache::new(cap))
    })
}

/// Get TTL for cached query results (default 60 seconds)
fn query_cache_ttl() -> Duration {
    let ttl_secs = std::env::var("OPENCODE_QUERY_CACHE_TTL_SECS")
        .ok()
        .and_then(|s| s.parse::<u64>().ok())
        .unwrap_or(30);
    Duration::from_secs(ttl_secs)
}

/// Invalidate all cache entries for a specific DB path
pub(crate) async fn invalidate_query_cache(db_path: &Path) {
    let db_str = db_path.to_string_lossy().to_string();
    let mut cache = query_cache().write().await;

    // Collect keys to remove (can't modify while iterating)
    let keys_to_remove: Vec<QueryCacheKey> = cache
        .iter()
        .filter(|(k, _)| k.db_path == db_str)
        .map(|(k, _)| k.clone())
        .collect();

    for key in keys_to_remove {
        cache.pop(&key);
    }

    if !cache.is_empty() {
        tracing::debug!("invalidated query cache for: {}", db_str);
    }
}

pub(crate) async fn search_impl(
    root: &str,
    db: Option<&str>,
    query: &str,
    tier: &str,
    dims: u32,
    federated: &[String],
    mounts: &HashMap<String, String>,
) -> Result<serde_json::Value> {
    use crate::model_client;
    use crate::storage;
    use std::collections::HashSet;

    let root = PathBuf::from(root);
    let storage_path = db
        .map(PathBuf::from)
        .unwrap_or_else(|| storage::storage_path(&root));

    // Check cache before expensive search
    let cache_key = QueryCacheKey::new(
        query,
        &storage_path.to_string_lossy(),
        tier,
        dims,
        federated,
    );

    {
        let mut cache = query_cache().write().await;
        if let Some(entry) = cache.get(&cache_key) {
            let age = entry.timestamp.elapsed();
            if age < query_cache_ttl() {
                tracing::debug!(
                    "query cache hit: query='{}' age={:.2}s",
                    query,
                    age.as_secs_f64()
                );
                return Ok(entry.results.clone());
            } else {
                // Expired - remove it
                cache.pop(&cache_key);
                tracing::debug!("query cache expired: query='{}'", query);
            }
        }
    }

    let mut all_paths: Vec<PathBuf> = vec![storage_path.clone()];
    all_paths.extend(federated.iter().map(PathBuf::from));

    let num_projects = all_paths.len();

    // Warn if too many federated DBs (potential resource issue)
    if num_projects > FEDERATED_DB_WARNING_THRESHOLD {
        tracing::warn!(
            "federated search across {} DBs may be slow/resource-intensive (threshold: {})",
            num_projects,
            FEDERATED_DB_WARNING_THRESHOLD
        );
    }

    // Adaptive strategy: skip per-project rerank for many projects (speed optimization)
    let use_vector_only = num_projects > SKIP_STAGE1_RERANK_THRESHOLD;

    // ========================================================================
    // STAGE 1: Parallel per-project search (bounded concurrency)
    // - Few projects: vector search + per-project rerank (quality)
    // - Many projects: vector search only (speed)
    // - Concurrency limited by semaphore to prevent CPU/memory spikes
    // ========================================================================
    let sem = federated_search_semaphore();
    let query_arc: Arc<str> = Arc::from(query);
    let tier_arc: Arc<str> = Arc::from(tier);
    let search_tasks: Vec<_> = all_paths
        .into_iter()
        .map(|sp| {
            let query = Arc::clone(&query_arc);
            let tier = Arc::clone(&tier_arc);
            let key = sp.to_string_lossy().to_string();
            let prefix = mounts
                .get(&key)
                .map(|s| s.trim_end_matches('/').to_string());

            tokio::spawn(async move {
                // Acquire semaphore permit to limit concurrency
                let _permit = sem.acquire().await.ok();

                if use_vector_only {
                    // Fast path: vector search only, no per-project rerank
                    search_single_db_vector_only(sp, &*query, &*tier, dims, prefix).await
                } else {
                    // Quality path: vector search + per-project rerank
                    search_single_db_stage1(sp, &*query, &*tier, dims, prefix).await
                }
            })
        })
        .collect();

    let results = futures::future::join_all(search_tasks).await;

    // Collect stage 1 results from all projects
    // Memory is bounded by: num_projects * max(STAGE1_TOP_K, VECTOR_ONLY_TOP_K) results
    // With 100 projects × 15 results = 1500 results max (~15MB for large content)
    // This is acceptable for search quality - global rerank will select the best
    let mut stage1_results: Vec<(f64, String, String)> =
        Vec::with_capacity(num_projects * std::cmp::max(STAGE1_TOP_K as usize, VECTOR_ONLY_TOP_K));
    for result in results {
        match result {
            Ok(Ok(ranked)) => stage1_results.extend(ranked),
            Ok(Err(e)) => {
                tracing::warn!("federated search error: {}", e);
            }
            Err(e) => {
                tracing::warn!("federated search task panicked: {}", e);
            }
        }
    }

    // Deduplicate by path (keep highest score)
    // Sort by score descending so we keep the highest-scoring duplicate
    stage1_results.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));

    // Dedup without allocating String clones - use indices instead
    let mut seen_indices = HashSet::new();
    stage1_results.retain(|(_, p, _)| {
        // Hash the string slice directly without cloning
        let hash = {
            use std::collections::hash_map::DefaultHasher;
            use std::hash::{Hash, Hasher};
            let mut hasher = DefaultHasher::new();
            p.hash(&mut hasher);
            hasher.finish()
        };
        seen_indices.insert(hash)
    });

    // ========================================================================
    // STAGE 2: Global rerank for fair cross-project comparison
    // Always do global rerank when multiple projects (scores must be comparable)
    // ========================================================================
    let final_results = if num_projects > 1 && stage1_results.len() > FINAL_TOP_K as usize {
        // Limit input to global rerank to prevent context overflow
        stage1_results.truncate(GLOBAL_RERANK_MAX);

        // Get rerank model from primary project tier
        let (_, rerank_model) = crate::cli::models_for_tier(tier);

        // Global rerank on combined results
        let docs: Vec<&str> = stage1_results.iter().map(|(_, _, c)| c.as_str()).collect();
        let mut client = model_client::client().await?;
        let global_ranked = client
            .rerank(query, &docs, rerank_model, FINAL_TOP_K)
            .await?;

        // Build final results with globally comparable scores
        let mut final_ranked: Vec<(f64, String, String)> = Vec::new();
        for (idx, score) in global_ranked {
            if idx < stage1_results.len() {
                let (_, path, content) = &stage1_results[idx];
                final_ranked.push((score.into(), path.clone(), content.clone()));
            }
        }
        final_ranked
    } else {
        // Single project or few results: skip global rerank
        stage1_results.truncate(FINAL_TOP_K as usize);
        stage1_results
    };

    let results: Vec<serde_json::Value> = final_results.iter().enumerate().map(|(i, (score, path, content))| {
        serde_json::json!({"rank": i+1, "score": score, "path": path, "content": content})
    }).collect();

    let result_json = serde_json::json!({"results": results});

    // Cache successful results
    {
        let mut cache = query_cache().write().await;
        cache.put(
            cache_key,
            QueryCacheEntry {
                results: result_json.clone(),
                timestamp: Instant::now(),
            },
        );
        tracing::debug!("query cache stored: query='{}'", query);
    }

    Ok(result_json)
}

/// Stage 1: Search a single database with first-pass rerank.
/// Returns top STAGE1_TOP_K results per project for global reranking.
async fn search_single_db_stage1(
    sp: PathBuf,
    query: &str,
    tier: &str,
    dims: u32,
    prefix: Option<String>,
) -> Result<Vec<(f64, String, String)>> {
    use crate::model_client;

    if !tokio::fs::try_exists(&sp).await.unwrap_or(false) {
        return Ok(Vec::new());
    }

    let s0 = cached_storage(&sp, dims).await?;
    let stored_tier = s0.get_tier().await?.unwrap_or_else(|| tier.into());
    let stored_dims = s0.get_dimensions().await?.unwrap_or(dims);

    let storage = cached_storage(&sp, stored_dims).await?;
    let mut client = model_client::client().await?;
    let (embed_model, rerank_model) = crate::cli::models_for_tier(&stored_tier);

    // Vector search: get top 20 candidates
    let qvec = client.embed_query(query, embed_model, stored_dims).await?;
    let mut results = storage.search_hybrid(query, &qvec, 20).await?;
    if results.is_empty() {
        return Ok(Vec::new());
    }

    // First-pass rerank: filter to top STAGE1_TOP_K for this project
    let docs: Vec<&str> = results.iter().map(|r| r.content.as_str()).collect();
    let ranked = client
        .rerank(query, &docs, rerank_model, STAGE1_TOP_K)
        .await?;

    let mut ranked_results = Vec::new();
    for (idx, score) in ranked {
        if idx < results.len() {
            // Move path when no prefix (avoids clone in common case)
            let p = if let Some(ref pre) = prefix {
                format!("{}/{}", pre, results[idx].path)
            } else {
                std::mem::take(&mut results[idx].path)
            };
            // Move content to avoid clone
            let c = std::mem::take(&mut results[idx].content);
            ranked_results.push((score.into(), p, c));
        }
    }

    Ok(ranked_results)
}

/// Fast path: Vector search only, no per-project rerank.
/// Used when many federated projects to reduce model server calls.
/// Returns top VECTOR_ONLY_TOP_K results per project using vector similarity scores.
async fn search_single_db_vector_only(
    sp: PathBuf,
    query: &str,
    tier: &str,
    dims: u32,
    prefix: Option<String>,
) -> Result<Vec<(f64, String, String)>> {
    use crate::model_client;

    if !tokio::fs::try_exists(&sp).await.unwrap_or(false) {
        return Ok(Vec::new());
    }

    let s0 = cached_storage(&sp, dims).await?;
    let stored_tier = s0.get_tier().await?.unwrap_or_else(|| tier.into());
    let stored_dims = s0.get_dimensions().await?.unwrap_or(dims);

    let storage = cached_storage(&sp, stored_dims).await?;
    let mut client = model_client::client().await?;
    let (embed_model, _) = crate::cli::models_for_tier(&stored_tier);

    // Vector search only - no rerank (speed optimization for many projects)
    let qvec = client.embed_query(query, embed_model, stored_dims).await?;
    let results = storage
        .search_hybrid(query, &qvec, VECTOR_ONLY_TOP_K)
        .await?;

    let mut ranked_results = Vec::new();
    for r in results {
        // Move path when no prefix (avoids clone in common case)
        let p = if let Some(ref pre) = prefix {
            format!("{}/{}", pre, r.path)
        } else {
            r.path
        };
        // Use vector similarity score (already normalized 0-1)
        // Move content to avoid clone
        ranked_results.push((r.score as f64, p, r.content));
    }

    Ok(ranked_results)
}

// ============================================================================
// search_memories — searches project + global memory indices, merges results
// ============================================================================

pub(crate) async fn search_memories_impl(
    shared: &str,
    project_id: &str,
    query: &str,
    limit: usize,
    tier: &str,
    dims: u32,
    federated_ids: &[String],
) -> Result<serde_json::Value> {
    let shared = PathBuf::from(shared);

    // Collect all memory DB paths to search
    let mut db_paths: Vec<(PathBuf, String)> = Vec::new(); // (db_path, scope)

    // Project memories
    let project_mem_db = shared
        .join("projects")
        .join(project_id)
        .join("memories")
        .join(".lancedb");
    db_paths.push((project_mem_db, "project".into()));

    // Global memories
    let global_mem_db = shared.join("memories").join("global").join(".lancedb");
    db_paths.push((global_mem_db, "global".into()));

    // Federated project memories
    for fid in federated_ids {
        let db = shared
            .join("projects")
            .join(fid)
            .join("memories")
            .join(".lancedb");
        db_paths.push((db, format!("linked:{fid}")));
    }

    // Search all indices, collect results
    let mut all: Vec<serde_json::Value> = Vec::new();

    for (db_path, scope) in &db_paths {
        if !tokio::fs::try_exists(db_path).await.unwrap_or(false) {
            continue;
        }

        let results = search_single_index(db_path, query, tier, dims, limit).await;
        match results {
            Ok(ranked) => {
                for (score, file_path, content) in ranked {
                    let filename = Path::new(&file_path)
                        .file_name()
                        .and_then(|n| n.to_str())
                        .unwrap_or(&file_path);
                    let id = filename.strip_suffix(".md").unwrap_or(filename);
                    let title = content
                        .lines()
                        .find(|l| l.starts_with("# "))
                        .map(|l| l[2..].trim().to_string())
                        .unwrap_or_else(|| id.to_string());
                    all.push(serde_json::json!({
                        "id": id,
                        "path": filename,
                        "title": title,
                        "content": content,
                        "score": score,
                        "scope": scope,
                    }));
                }
            }
            Err(e) => {
                tracing::debug!("memory search failed for {}: {e}", db_path.display());
            }
        }
    }

    // Sort by score descending, dedup by ID, truncate
    all.sort_by(|a, b| {
        let sa = a["score"].as_f64().unwrap_or(0.0);
        let sb = b["score"].as_f64().unwrap_or(0.0);
        sb.partial_cmp(&sa).unwrap_or(std::cmp::Ordering::Equal)
    });

    let mut seen = std::collections::HashSet::new();
    all.retain(|r| {
        let id = r["id"].as_str().unwrap_or("");
        seen.insert(id.to_string())
    });
    all.truncate(limit);

    Ok(serde_json::json!({"results": all}))
}

// ============================================================================
// search_activity
// ============================================================================

pub(crate) async fn search_activity_impl(
    shared: &str,
    project_id: &str,
    query: &str,
    limit: usize,
    tier: &str,
    dims: u32,
) -> Result<serde_json::Value> {
    let db_path = PathBuf::from(shared)
        .join("projects")
        .join(project_id)
        .join("activity")
        .join(".lancedb");

    if !tokio::fs::try_exists(&db_path).await.unwrap_or(false) {
        return Ok(serde_json::json!({"results": []}));
    }

    let ranked = search_single_index(&db_path, query, tier, dims, limit).await?;
    let results: Vec<serde_json::Value> = ranked
        .into_iter()
        .map(|(score, file_path, content)| {
            let filename = Path::new(&file_path)
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or(&file_path);
            let id = filename.strip_suffix(".md").unwrap_or(filename);
            let title = content
                .lines()
                .find(|l| l.starts_with("# "))
                .map(|l| l[2..].trim().to_string())
                .unwrap_or_else(|| id.to_string());
            serde_json::json!({
                "id": id,
                "path": filename,
                "title": title,
                "content": content,
                "score": score,
            })
        })
        .collect();

    Ok(serde_json::json!({"results": results}))
}

pub(crate) async fn search_skills_impl(
    shared: &str,
    query: &str,
    limit: usize,
    tier: &str,
    dims: u32,
) -> Result<serde_json::Value> {
    let db_path = PathBuf::from(shared).join("skills").join(".lancedb");

    if !tokio::fs::try_exists(&db_path).await.unwrap_or(false) {
        return Ok(serde_json::json!({"results": []}));
    }

    let ranked = search_single_index(&db_path, query, tier, dims, limit).await?;
    let results: Vec<serde_json::Value> = ranked
        .into_iter()
        .map(|(score, file_path, content)| {
            let filename = Path::new(&file_path)
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or(&file_path);
            let id = filename.strip_suffix(".md").unwrap_or(filename);
            let title = content
                .lines()
                .find(|l| l.starts_with("# "))
                .map(|l| l[2..].trim().to_string())
                .unwrap_or_else(|| id.to_string());
            serde_json::json!({
                "id": id,
                "path": filename,
                "title": title,
                "content": content,
                "score": score,
            })
        })
        .collect();

    Ok(serde_json::json!({"results": results}))
}

/// Search a single LanceDB index. Returns (score, path, content) triples.
async fn search_single_index(
    db_path: &Path,
    query: &str,
    tier: &str,
    dims: u32,
    limit: usize,
) -> Result<Vec<(f64, String, String)>> {
    use crate::model_client;

    let storage = cached_storage(db_path, dims).await?;
    let stored_tier = storage.get_tier().await?.unwrap_or_else(|| tier.into());
    let stored_dims = storage.get_dimensions().await?.unwrap_or(dims);

    // Reopen with correct dims if they differ
    let storage = if stored_dims != dims {
        cached_storage(db_path, stored_dims).await?
    } else {
        storage
    };

    let mut client = model_client::client().await?;
    let (embed_model, rerank_model) = crate::cli::models_for_tier(&stored_tier);

    let qvec = client.embed_query(query, embed_model, stored_dims).await?;
    let mut results = storage.search_hybrid(query, &qvec, 20).await?;
    if results.is_empty() {
        return Ok(Vec::new());
    }

    let docs: Vec<&str> = results.iter().map(|r| r.content.as_str()).collect();
    let ranked = client
        .rerank(query, &docs, rerank_model, limit as u32)
        .await?;

    let mut out = Vec::new();
    for (idx, score) in ranked {
        if idx < results.len() {
            // Move path and content to avoid clone
            out.push((
                score.into(),
                std::mem::take(&mut results[idx].path),
                std::mem::take(&mut results[idx].content),
            ));
        }
    }
    Ok(out)
}

/// Returns embed concurrency from env var OPENCODE_INDEXER_EMBED_CONCURRENCY (default 2).
/// Reduced from 3 to 2 to match the embedder's default worker capacity (1-2 workers),
/// preventing excessive queuing at the Python server.
pub(crate) fn embed_concurrency() -> usize {
    std::env::var("OPENCODE_INDEXER_EMBED_CONCURRENCY")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .unwrap_or(2)
}

/// Session-level set of DB paths where FTS index has been confirmed present.
/// Avoids calling list_indices() on every watcher batch.
pub(crate) fn fts_ensured() -> &'static tokio::sync::Mutex<std::collections::HashSet<std::path::PathBuf>> {
    static S: std::sync::OnceLock<
        tokio::sync::Mutex<std::collections::HashSet<std::path::PathBuf>>,
    > = std::sync::OnceLock::new();
    S.get_or_init(|| tokio::sync::Mutex::new(std::collections::HashSet::new()))
}
