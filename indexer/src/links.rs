//! Link discovery cache: avoids re-running `git ls-files` on every search.
//!
//! This module caches discovered linked projects on disk and in memory,
//! invalidating when symlinks or repo structure changes.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Instant;

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use tokio::sync::Mutex;

use crate::daemon::{
    active_indexes, cached_storage, storage_cache,
    CachedStorage,
};
use crate::handlers::{discover_links_impl, run_index_impl};

const MAX_LINK_CACHE_SIZE: usize = 50;

// ---------------------------------------------------------------------------
// Link discovery cache: avoids re-running `git ls-files` on every search.
// Populated lazily on first search, expires after 5 minutes.
// ---------------------------------------------------------------------------

pub fn env_duration_ms(key: &str, default: std::time::Duration) -> std::time::Duration {
    let Ok(value) = std::env::var(key) else {
        return default;
    };
    let Ok(ms) = value.trim().parse::<u64>() else {
        return default;
    };
    if ms == 0 {
        return default;
    }
    std::time::Duration::from_millis(ms)
}

#[derive(Clone)]
pub(crate) struct Link {
    pub(crate) repo: PathBuf,
    pub(crate) db: PathBuf,
    pub(crate) project_id: String,
    pub(crate) mount: String,
    pub(crate) name: String,
}

// Persisted link structure for .links.json
#[derive(Debug, Clone, Serialize, Deserialize)]
struct PersistedLinks {
    version: u32,
    updated_at: i64,
    project_root: String,
    links: Vec<PersistedLink>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct PersistedLink {
    path: String,
    project_id: String,
    name: String,
    db_path: String,
}

const LINKS_CACHE_VERSION: u32 = 1;

/// Returns path to the links cache file in the project's storage directory.
fn links_cache_path(root: &Path) -> PathBuf {
    use crate::storage;
    let storage_dir = storage::storage_path(root);
    storage_dir.parent().unwrap_or(root).join(".links.json")
}

/// Load links from the persisted cache file.
/// Returns None if file doesn't exist, is invalid, or has wrong version.
/// BLOCKING: Must run in spawn_blocking context.
fn load_links_from_file(root: &Path) -> Option<Vec<Link>> {
    let cache_path = links_cache_path(root);

    let content = std::fs::read_to_string(&cache_path).ok()?;
    let persisted: PersistedLinks = serde_json::from_str(&content).ok()?;

    // Validate version
    if persisted.version != LINKS_CACHE_VERSION {
        tracing::debug!("links cache version mismatch, invalidating");
        return None;
    }

    // Validate root matches
    let root_str = root.to_string_lossy();
    if persisted.project_root != root_str {
        tracing::debug!("links cache root mismatch, invalidating");
        return None;
    }

    // Convert persisted links to Link structs
    let links = persisted
        .links
        .iter()
        .filter_map(|l| {
            Some(Link {
                repo: PathBuf::from(&l.path),
                db: PathBuf::from(&l.db_path),
                project_id: l.project_id.clone(),
                mount: String::new(), // mount is derived from link name
                name: l.name.clone(),
            })
        })
        .collect();

    tracing::debug!(
        "loaded {} links from cache for {}",
        persisted.links.len(),
        root_str
    );
    Some(links)
}

/// Save discovered links to the cache file.
/// BLOCKING: Must run in spawn_blocking context.
fn save_links_to_file(root: &Path, links: &[Link]) -> Result<()> {
    let cache_path = links_cache_path(root);

    // Ensure parent directory exists
    if let Some(parent) = cache_path.parent() {
        std::fs::create_dir_all(parent).context("failed to create links cache directory")?;
    }

    let persisted = PersistedLinks {
        version: LINKS_CACHE_VERSION,
        updated_at: std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs() as i64,
        project_root: root.to_string_lossy().to_string(),
        links: links
            .iter()
            .map(|l| PersistedLink {
                path: l.repo.to_string_lossy().to_string(),
                project_id: l.project_id.clone(),
                name: l.name.clone(),
                db_path: l.db.to_string_lossy().to_string(),
            })
            .collect(),
    };

    let json =
        serde_json::to_string_pretty(&persisted).context("failed to serialize links cache")?;

    std::fs::write(&cache_path, json).context("failed to write links cache")?;

    tracing::debug!(
        "saved {} links to cache for {}",
        links.len(),
        root.to_string_lossy()
    );
    Ok(())
}

/// Invalidate the links cache file by deleting it.
/// Called when symlinks or repo structure changes.
/// ASYNC: Uses tokio::fs internally.
pub async fn invalidate_links_cache(root: &Path) {
    let cache_path = links_cache_path(root);
    if tokio::fs::try_exists(&cache_path).await.unwrap_or(false) {
        if let Err(e) = tokio::fs::remove_file(&cache_path).await {
            tracing::warn!("failed to invalidate links cache: {}", e);
        } else {
            tracing::info!("invalidated links cache for {}", root.to_string_lossy());
        }
    }

    // Also clear in-memory cache
    if let Ok(mut cache) = link_cache().try_write() {
        cache.remove(root);
    }
}

/// Check if a path change should invalidate the links cache.
/// Returns true if the path is a symlink or in a symlink-related directory.
pub(crate) fn should_invalidate_links_cache(path: &Path) -> bool {
    // Pure string-based check - no stat syscalls
    // Symlinks are typically in .opencode-links/ or node_modules with special names
    if let Some(path_str) = path.to_str() {
        // Direct .opencode-links directory
        if path_str.contains(".opencode-links") {
            return true;
        }
        // Common symlink locations in monorepos
        if path_str.contains("/node_modules/") && path_str.contains("@") {
            return true;
        }
        // Repository collection directories (common patterns for multi-repo workspaces)
        if path_str.contains("/repositories/") || path_str.contains("/repositories-ubuntu/") {
            return true;
        }
    }
    false
}

/// Cached links with LRU tracking
struct CachedLinks {
    links: Vec<Link>,
    last_access: Instant,
}

fn link_cache() -> &'static tokio::sync::RwLock<HashMap<PathBuf, CachedLinks>> {
    static CACHE: std::sync::OnceLock<tokio::sync::RwLock<HashMap<PathBuf, CachedLinks>>> =
        std::sync::OnceLock::new();
    CACHE.get_or_init(|| tokio::sync::RwLock::new(HashMap::new()))
}

/// Returns linked repos using file-based cache.
/// Uses `try_read`/`try_write` to avoid blocking search on lock contention.
/// Cache is invalidated by deleting `.links.json` when symlinks change.
pub(crate) fn cached_discover_links(root: &str) -> Vec<Link> {
    let root_path = match PathBuf::from(root).canonicalize() {
        Ok(p) => p,
        Err(_) => return vec![],
    };

    // Fast path: check in-memory cache and update access time
    match link_cache().try_read() {
        Ok(r) => {
            if let Some(cached) = r.get(&root_path) {
                let links = cached.links.clone();
                drop(r);

                // Update last_access with write lock
                if let Ok(mut w) = link_cache().try_write() {
                    if let Some(entry) = w.get_mut(&root_path) {
                        entry.last_access = Instant::now();
                    }
                }
                return links;
            }
        }
        Err(_) => {
            tracing::debug!("link cache contended on read, will try file");
        }
    }

    // TTL check: invalidate the file cache if it is older than the configured threshold.
    // Default: 1 hour. Override via OPENCODE_INDEXER_LINKS_CACHE_TTL_SECS env var.
    // This catches stale caches where new symlinks were added after the last save.
    // BLOCKING: This function is called from spawn_blocking, std::fs is OK here.
    let ttl_secs: u64 = std::env::var("OPENCODE_INDEXER_LINKS_CACHE_TTL_SECS")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(3600);
    let cache_path = links_cache_path(&root_path);
    if let Ok(meta) = std::fs::metadata(&cache_path) {
        if let Ok(modified) = meta.modified() {
            let age = std::time::SystemTime::now()
                .duration_since(modified)
                .unwrap_or_default();
            if age.as_secs() > ttl_secs {
                tracing::info!(
                    "links cache TTL expired ({}/{}s) for {}, invalidating",
                    age.as_secs(),
                    ttl_secs,
                    root_path.display()
                );
                let _ = std::fs::remove_file(&cache_path);
                // Also evict from in-memory cache so we don't serve the stale copy
                if let Ok(mut cache) = link_cache().try_write() {
                    cache.remove(&root_path);
                }
            }
        }
    }

    // Medium path: try loading from file
    tracing::debug!("checking links cache file for {}", root_path.display());
    if let Some(links) = load_links_from_file(&root_path) {
        // Update in-memory cache with LRU eviction
        if let Ok(mut cache) = link_cache().try_write() {
            // LRU eviction: remove oldest entry if cache is full
            if cache.len() >= MAX_LINK_CACHE_SIZE && !cache.contains_key(&root_path) {
                if let Some(oldest_key) = cache
                    .iter()
                    .min_by_key(|(_, cached)| cached.last_access)
                    .map(|(k, _)| k.clone())
                {
                    tracing::debug!(
                        "link cache full ({}), evicting oldest entry: {}",
                        MAX_LINK_CACHE_SIZE,
                        oldest_key.display()
                    );
                    cache.remove(&oldest_key);
                }
            }
            cache.insert(
                root_path,
                CachedLinks {
                    links: links.clone(),
                    last_access: Instant::now(),
                },
            );
        }
        return links;
    }

    // Slow path: discover, save to file, and cache in memory
    let result = match discover_links_impl(root) {
        Ok(v) => v,
        Err(_) => return vec![],
    };

    let links_result = result["links"].as_array().cloned().unwrap_or_default();
    let links: Vec<Link> = links_result
        .iter()
        .filter_map(|l| {
            let repo = l["path"].as_str().map(PathBuf::from)?;
            let db = l["dbPath"].as_str().map(PathBuf::from)?;
            let project_id = l["projectId"].as_str().unwrap_or("").to_string();
            let mount = l["mount"].as_str().unwrap_or("").to_string();
            let name = l["name"].as_str().unwrap_or("linked").to_string();
            Some(Link {
                repo,
                db,
                project_id,
                mount,
                name,
            })
        })
        .collect();

    // Save to file (best effort, don't fail if it errors)
    // BLOCKING: This function is called from spawn_blocking, std::fs is OK here.
    tracing::info!(
        "saving {} links to file for {}",
        links.len(),
        root_path.display()
    );
    if let Err(e) = save_links_to_file(&root_path, &links) {
        tracing::warn!("failed to save links cache: {}", e);
    } else {
        tracing::info!(
            "links cache saved successfully to {}",
            links_cache_path(&root_path).display()
        );
    }

    // Update in-memory cache with LRU eviction
    match link_cache().try_write() {
        Ok(mut cache) => {
            // LRU eviction: remove oldest entry if cache is full
            if cache.len() >= MAX_LINK_CACHE_SIZE && !cache.contains_key(&root_path) {
                if let Some(oldest_key) = cache
                    .iter()
                    .min_by_key(|(_, cached)| cached.last_access)
                    .map(|(k, _)| k.clone())
                {
                    tracing::debug!(
                        "link cache full ({}), evicting oldest entry: {}",
                        MAX_LINK_CACHE_SIZE,
                        oldest_key.display()
                    );
                    cache.remove(&oldest_key);
                }
            }
            cache.insert(
                root_path,
                CachedLinks {
                    links: links.clone(),
                    last_access: Instant::now(),
                },
            );
        }
        Err(_) => {
            tracing::debug!("link cache contended on write, skipping in-memory cache update");
        }
    }

    links
}

fn link_index_inflight() -> &'static Mutex<HashSet<PathBuf>> {
    static SET: std::sync::OnceLock<Mutex<HashSet<PathBuf>>> =
        std::sync::OnceLock::new();
    SET.get_or_init(|| Mutex::new(HashSet::new()))
}

fn link_index_cooldown() -> &'static Mutex<std::collections::HashMap<PathBuf, std::time::Instant>> {
    static MAP: std::sync::OnceLock<Mutex<std::collections::HashMap<PathBuf, std::time::Instant>>> =
        std::sync::OnceLock::new();
    MAP.get_or_init(|| Mutex::new(std::collections::HashMap::new()))
}

/// Adaptive semaphore for linked project indexing.
/// Total budget of 4 permits (default) limits concurrent linked-project indexing.
/// Override via OPENCODE_INDEXER_LINK_CONCURRENCY env var.
fn link_index_semaphore() -> &'static tokio::sync::Semaphore {
    static SEM: std::sync::OnceLock<tokio::sync::Semaphore> = std::sync::OnceLock::new();
    SEM.get_or_init(|| {
        let permits = std::env::var("OPENCODE_INDEXER_LINK_CONCURRENCY")
            .ok()
            .and_then(|v| v.parse::<usize>().ok())
            .unwrap_or(4);
        tokio::sync::Semaphore::new(permits)
    })
}

/// Determine how many semaphore permits a linked project should consume
/// based on its file count. Smaller projects use fewer permits so more
/// can index concurrently; larger projects use more to throttle load.
fn link_index_weight(files: usize) -> u32 {
    if files < 200 {
        1 // small: up to 8 concurrent
    } else if files < 1000 {
        2 // medium: up to 4 concurrent
    } else {
        4 // large: up to 2 concurrent
    }
}

/// Check whether a linked project needs initial indexing.
/// Returns true if the LanceDB directory is missing or contains zero chunks.
pub(crate) async fn needs_initial_index(db: &Path) -> bool {
    if !tokio::fs::try_exists(db).await.unwrap_or(false) {
        return true;
    }
    // Check lance data file as a quick existence proxy (avoid full Storage open for hot path)
    if !db.join("chunks.lance").exists() {
        return true;
    }
    // Open storage to verify row count; treat errors as "needs index"
    match cached_storage(db, 0).await {
        Ok(s) => s.count_chunks().await.unwrap_or(0) == 0,
        Err(_) => true,
    }
}

pub(crate) async fn ensure_link_index(link: Link, tier: &str, dims: u32, parent_root: &str) {
    // Cooldown: skip if this link was checked within the last 60 seconds
    {
        let cooldown = link_index_cooldown().lock().await;
        if let Some(last) = cooldown.get(&link.db) {
            if last.elapsed() < std::time::Duration::from_secs(60) {
                return;
            }
        }
    }

    // Check skip in parent project config
    let parent_path = PathBuf::from(parent_root);
    let project = crate::config::load(&parent_path);
    if project
        .linked
        .get(&link.name)
        .map(|l| l.skip)
        .unwrap_or(false)
    {
        return;
    }

    // De-dupe by DB path
    let mut set = link_index_inflight().lock().await;
    if set.contains(&link.db) {
        return;
    }
    set.insert(link.db.clone());
    drop(set);

    // Discover file count to determine adaptive weight.
    // Invalidate cache first so weight reflects current filesystem state
    // (cache is event-driven via watcher notifications, not TTL-based).
    let repo_for_discover = link.repo.clone();
    let name_for_discover = link.name.clone();
    let parent_path_for_cfg = PathBuf::from(parent_root);
    let weight = tokio::task::spawn_blocking(move || {
        crate::discover::invalidate_discovery_cache(&repo_for_discover);
        let parent_project = crate::config::load(&parent_path_for_cfg);
        let linked_project = crate::config::load(&repo_for_discover);
        let cfg = crate::config::effective(
            &parent_project,
            Some(&name_for_discover),
            Some(&linked_project),
        );
        match crate::discover::discover_files_with_config(&repo_for_discover, &cfg) {
            Ok(result) => link_index_weight(result.files.len()),
            Err(_) => 2, // default to medium weight on discovery failure
        }
    })
    .await
    .unwrap_or(2);

    // Check if DB already has chunks - skip if already indexed
    let needs_index = if !tokio::fs::try_exists(&link.db).await.unwrap_or(false) {
        true
    } else {
        match cached_storage(&link.db, dims).await {
            Ok(s) => s.count_chunks().await.unwrap_or(0) == 0,
            Err(_) => true,
        }
    };

    if !needs_index {
        tracing::info!(
            "skipping already-indexed linked project: {} (has chunks)",
            link.repo.display()
        );
        link_index_cooldown()
            .lock()
            .await
            .insert(link.db.clone(), std::time::Instant::now());
        link_index_inflight().lock().await.remove(&link.db);
        return;
    }

    tracing::info!(
        "auto-indexing linked project in background: {} (weight={}/8)",
        link.repo.display(),
        weight
    );

    let tier = tier.to_string();
    let parent_root_owned = parent_root.to_string();
    tokio::spawn(async move {
        let Ok(_permit) = link_index_semaphore().acquire_many(weight).await else {
            link_index_inflight().lock().await.remove(&link.db);
            return;
        };

        // Load parent project config to get linked project's exclude/include patterns
        let parent_path = PathBuf::from(&parent_root_owned);
        let parent_project = crate::config::load(&parent_path);

        // Load linked project's own config (if it has .opencode-index.yaml)
        let linked_project = crate::config::load(&link.repo);

        // Get effective config: merge parent's linked[name] with linked project's own config
        let effective_cfg =
            crate::config::effective(&parent_project, Some(&link.name), Some(&linked_project));

        let root = link.repo.to_string_lossy().to_string();
        tracing::info!(
            "linked index start: {} (weight={}/8, exclude: {:?}, include: {:?})",
            root,
            weight,
            effective_cfg.exclude,
            effective_cfg.include
        );
        let r = run_index_impl(
            &root,
            None,
            &tier,
            dims,
            false,
            &effective_cfg.exclude,
            &effective_cfg.include,
        )
        .await;
        if let Err(e) = r {
            tracing::warn!("linked index failed: {}: {e}", root);
        }
        tracing::info!("linked index done: {} (weight={}/8)", root, weight);

        link_index_cooldown()
            .lock()
            .await
            .insert(link.db.clone(), std::time::Instant::now());
        link_index_inflight().lock().await.remove(&link.db);
    });
}

/// Trigger a background reindex for auto-recovery from corruption.
pub(crate) async fn run_index_background(root: &str, tier: &str, dims: u32) -> anyhow::Result<()> {
    use crate::config;
    use crate::discover;
    use crate::model_client;
    use crate::storage;

    let started = std::time::Instant::now();
    let root = PathBuf::from(root).canonicalize()?;
    let storage_path = storage::storage_path(&root);

    // Load config
    let project = config::load(&root);
    let cfg = config::effective(&project, Some(tier), None);

    // Discover files (blocking operation, run in thread pool)
    let root_clone = root.clone();
    let cfg_clone = cfg.clone();
    let discovery = tokio::task::spawn_blocking(move || {
        discover::discover_files_with_config(&root_clone, &cfg_clone)
    })
    .await??;

    if discovery.files.is_empty() {
        tracing::info!("auto-recovery: no files to index for {}", root.display());
        return Ok(());
    }

    tracing::info!(
        "auto-recovery: indexing {} files in {}",
        discovery.files.len(),
        root.display()
    );

    // Register this db path as actively indexing so status_impl won't self-heal it away.
    let key = storage_path.to_string_lossy().to_string();
    active_indexes().lock().await.insert(key.clone());
    struct Guard(String);
    impl Drop for Guard {
        fn drop(&mut self) {
            let k = self.0.clone();
            tokio::spawn(async move {
                active_indexes().lock().await.remove(&k);
            });
        }
    }
    let _guard = Guard(key);

    // Open fresh storage (will create new .lancedb)
    let storage = storage::Storage::open(&storage_path, dims).await?;
    storage.set_tier(tier).await?;
    storage.set_dimensions(dims).await?;
    storage.set_indexing_in_progress(true).await?;
    storage
        .set_indexing_start_time(&chrono::Utc::now().to_rfc3339())
        .await?;
    storage.set_indexing_phase("embedding").await?;
    storage
        .set_phase_progress("embedding", 0, discovery.files.len() as i64)
        .await?;

    // Run indexing (simplified - just index all files)
    let mut client = model_client::client().await?;
    let embed = tokio::sync::Semaphore::new(1);

    // Create WriteQueue for auto-recovery indexing
    let storage_arc = Arc::new(storage);
    let write_queue = crate::storage::WriteQueue::new(storage_arc.clone(), 32);

    let mut done = 0i64;
    let total = discovery.files.len() as i64;
    for file in &discovery.files {
        let file_path = root.join(file);
        if !tokio::fs::try_exists(&file_path).await.unwrap_or(false) {
            continue;
        }

        match crate::cli::update_file_partial_pub(
            &root,
            &[],
            &[],
            &storage_path,
            &*storage_arc,
            &mut client,
            &file_path,
            tier,
            dims,
            "int8",
            None,
            &embed,
            false,
            false,
            &write_queue,
        )
        .await
        {
            Ok(_) => {}
            Err(e) => {
                tracing::warn!("auto-recovery: failed to index {}: {}", file.display(), e);
            }
        }
        done += 1;
        if done % 10 == 0 || done == total {
            let _ = storage_arc
                .set_phase_progress("embedding", done, total)
                .await;
        }
    }

    // Wait for all writes to complete
    let _ = write_queue.shutdown().await;
    let _ = storage_arc.clear_indexing_progress().await;

    // Set metadata after indexing completes (duration, files, timestamps)
    let elapsed = started.elapsed();
    let _ = storage_arc
        .set_last_index_duration_ms((elapsed.as_millis() as i64).max(0))
        .await;
    let file_count = storage_arc.count_files().await.unwrap_or(0);
    let _ = storage_arc
        .set_last_index_files_count(file_count as i64)
        .await;
    let now = chrono::Utc::now().to_rfc3339();
    let _ = storage_arc.set_last_index_timestamp(&now).await;
    let _ = storage_arc.set_last_update_timestamp(&now).await;

    // Update cache with new storage
    let storage = storage_arc;
    let key = (storage_path.to_string_lossy().to_string(), dims);
    {
        let mut w = storage_cache().write().await;
        w.insert(
            key,
            CachedStorage {
                storage,
                last_access: Instant::now(),
            },
        );
    }

    tracing::info!(
        "auto-recovery: indexing complete for {} ({} files)",
        root.display(),
        file_count
    );
    Ok(())
}
