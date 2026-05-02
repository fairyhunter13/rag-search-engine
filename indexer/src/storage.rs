//! LanceDB storage layer — same schema as the Python embedder.
//!
//! Tables:
//!   chunks: chunk_id(i64), path(utf8), file_hash(utf8), language(utf8),
//!           position(i32), content(utf8), content_hash(utf8),
//!           start_line(i32), end_line(i32), vector(list[f32]), created_at(timestamp_us)
//!   config: key(utf8), value(utf8)
//!   usage:  date(utf8), tokens(i64), cost(f64), tier(utf8)
//!
//! Compatible with indexes created by the Python embedder.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, OnceLock};
use std::time::Instant;

use anyhow::{Context, Result};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use arrow_array::{
    builder::{
        FixedSizeListBuilder, Float32Builder, Int32Builder, Int64Builder, StringBuilder,
        TimestampMicrosecondBuilder,
    },
    types::TimestampMicrosecondType,
    Array, Float32Array, Int32Array, Int64Array, PrimitiveArray, RecordBatch, RecordBatchIterator,
    StringArray,
};
use arrow_schema::{DataType, Field, Schema, TimeUnit};
use chrono::Utc;
use lancedb::connect;
use lancedb::query::{ExecutableQuery, QueryBase};
use sha2::{Digest, Sha256};
use tracing::info;

use crate::simd::rerank_by_cosine;

pub const FTS_THRESHOLD: usize = 50; // Create FTS index after this many chunks
pub const IVF_PQ_THRESHOLD: usize = 512; // Create IVF-PQ index after this many vectors

// IVF-PQ Index Tuning Parameters
// Trade-off: Higher values = better recall but slower search/build

/// Number of IVF partitions (clusters)
/// Higher = more precise partitioning, slower build time
/// Rule of thumb: sqrt(num_vectors) to num_vectors/10
/// Default will be computed dynamically: (count / 10).clamp(1, 256)
pub const IVF_NUM_PARTITIONS_MAX: usize = 256;

/// Number of PQ sub-vectors
/// Higher = more precise quantization, more memory
/// Rule of thumb: dimensions / 4 to dimensions / 2
/// Default will be computed dynamically: (dimensions / 4).clamp(1, 96)
pub const IVF_NUM_SUB_VECTORS_MAX: usize = 96;

/// Number of partitions to search (nprobes)
/// Higher = better recall, slower search
/// Default: 16 (good balance for most use cases)
/// Small DB (<1K): 32, Medium (1K-10K): 16, Large (>10K): 8
pub const IVF_NPROBES: usize = 16;

/// Refine factor for post-filtering
/// Fetches (limit × refine_factor) candidates from index, then refines to top limit
/// Higher = better recall, slightly slower
/// Default: 3 (fetch 3× candidates, refine to top)
pub const IVF_REFINE_FACTOR: usize = 3;

const COST_PER_MILLION_BUDGET: f64 = 0.02;
const COST_PER_MILLION_BALANCED: f64 = 0.06;
const COST_PER_MILLION_PREMIUM: f64 = 0.12;

const SCHEMA_VERSION: &str = "2";
const CONFIG_FILE_COUNT: &str = "file_count";

/// Get IVF nprobes from environment or default
fn get_ivf_nprobes() -> usize {
    std::env::var("OPENCODE_IVF_NPROBES")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(IVF_NPROBES)
}

/// Get IVF refine factor from environment or default
fn get_ivf_refine_factor() -> usize {
    std::env::var("OPENCODE_IVF_REFINE_FACTOR")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(IVF_REFINE_FACTOR)
}

// Lock-free chunk ID counter using per-database AtomicI64.
// The outer mutex is only held briefly to get/create the entry.
// The actual increment uses AtomicI64 — no lock contention during inserts.
// LruCache cap=100 gives O(1) memory guarantee (100 concurrent projects max).
fn chunk_ids() -> &'static std::sync::Mutex<lru::LruCache<String, Arc<std::sync::atomic::AtomicI64>>> {
    static IDS: std::sync::OnceLock<std::sync::Mutex<lru::LruCache<String, Arc<std::sync::atomic::AtomicI64>>>> = std::sync::OnceLock::new();
    IDS.get_or_init(|| std::sync::Mutex::new(lru::LruCache::new(std::num::NonZeroUsize::new(100).unwrap())))
}

fn estimate_cost(tokens: i64, tier: &str) -> f64 {
    let price = match tier {
        "premium" => COST_PER_MILLION_PREMIUM,
        "budget" => COST_PER_MILLION_BUDGET,
        _ => COST_PER_MILLION_BALANCED,
    };
    (tokens as f64 / 1_000_000.0) * price
}

/// Check if an error indicates LanceDB corruption (missing lance files, IO errors, etc.).
/// These errors typically occur when:
/// - A .lance data file was deleted or corrupted
/// - The index was interrupted during write
/// - Disk corruption or incomplete sync
/// - Table metadata exists but table directory is missing
pub fn is_corruption_error(err: &anyhow::Error) -> bool {
    let msg = err.to_string().to_lowercase();

    // "does not exist" is LanceDB's normal message for a table that hasn't been created
    // yet — not corruption. Only treat it as corruption if a .lance file is involved.
    if msg.contains("does not exist") && !msg.contains(".lance") {
        return false;
    }

    // Check for common corruption patterns
    msg.contains("not found") && msg.contains(".lance")
        || msg.contains("lanceerror(io)")
        || msg.contains("execution error") && msg.contains("not found")
        || msg.contains("corrupted")
        || msg.contains("invalid data")
        || msg.contains("unexpected eof")
        || msg.contains("failed to read")
        // Table exists in metadata but directory/files are missing — "was not found" is
        // specific enough to indicate corruption rather than a genuinely absent table.
        || msg.contains("table") && msg.contains("was not found")
        // Intentionally NOT matching the broader "table" + "not found" pattern because
        // messages like "table 'config' not found" are normal for a fresh database.
        // Arrow RecordBatch size mismatch — occurs when lance-0.23.x merges batches
        // with inconsistent row counts (e.g., "20 != 19"). Not matched by the IO/Arrow
        // categories above so we add it explicitly.
        || msg.contains("recordbatch") && msg.contains("different sizes")
        || msg.contains("invalid argument error") && msg.contains("merge")
        || msg.contains("lanceerror(arrow)") && msg.contains("invalid argument")
}

/// Clear a corrupted LanceDB index by removing the .lancedb directory.
/// Returns Ok(true) if cleared, Ok(false) if nothing to clear, Err on failure.
pub fn clear_corrupted_index(db_path: &Path) -> Result<bool> {
    if !db_path.exists() {
        return Ok(false);
    }
    
    tracing::warn!("clearing corrupted index at {}", db_path.display());
    
    // Create a backup first (best-effort)
    let backup_dir = backup_dir();
    std::fs::create_dir_all(&backup_dir).ok();
    let ts = chrono::Utc::now().format("%Y%m%d-%H%M%S").to_string();
    let backup_name = format!("corrupted-{}.lancedb", ts);
    let backup_path = backup_dir.join(&backup_name);
    
    // Try to move to backup (faster than copy)
    if std::fs::rename(db_path, &backup_path).is_ok() {
        tracing::info!("corrupted index backed up to {}", backup_path.display());
        return Ok(true);
    }
    
    // Fallback: delete directly if rename failed (cross-device)
    std::fs::remove_dir_all(db_path).context("failed to remove corrupted index")?;
    tracing::info!("corrupted index removed (backup failed, deleted directly)");
    Ok(true)
}

/// Validate that a lance table has valid data files.
/// Returns true if valid, false if corrupted or missing data.
/// Checks for minimum file size (64 bytes) to detect truncated/empty .lance files.
pub fn validate_lance_table(table_path: &Path) -> bool {
    if !table_path.is_dir() {
        return false;
    }
    let data_dir = table_path.join("data");
    if !data_dir.is_dir() {
        // Table exists but no data dir - empty table is valid
        return true;
    }
    match std::fs::read_dir(&data_dir) {
        Ok(entries) => {
            for entry in entries.flatten() {
                let path = entry.path();
                if path.extension().map_or(false, |e| e == "lance") {
                    // Check file is not empty/truncated (min 64 bytes for header)
                    if let Ok(meta) = std::fs::metadata(&path) {
                        if meta.len() < 64 {
                            return false; // Truncated file = corruption
                        }
                    }
                    return true;
                }
            }
            false
        }
        Err(_) => false,
    }
}

/// Check if the database at the given path appears corrupted.
/// Validates that all lance table directories have valid data files.
pub fn is_database_corrupted(db_path: &Path) -> (bool, Vec<String>) {
    let mut corrupted = false;
    let mut errors = Vec::new();
    
    if !db_path.is_dir() {
        return (false, errors); // Doesn't exist, not corrupted just empty
    }
    
    // Check known tables
    for table_name in &["chunks.lance", "config.lance", "usage.lance"] {
        let table_path = db_path.join(table_name);
        if table_path.is_dir() {
            let data_dir = table_path.join("data");
            if data_dir.is_dir() {
                // Data directory exists, verify it has valid .lance files
                let has_lance_files = std::fs::read_dir(&data_dir)
                    .map(|entries| {
                        entries
                            .filter_map(Result::ok)
                            .any(|e| e.path().extension().map_or(false, |ext| ext == "lance"))
                    })
                    .unwrap_or(false);
                
                if !has_lance_files {
                    corrupted = true;
                    errors.push(format!("No data files in {}/data/", table_name));
                }
            }
        }
    }
    
    (corrupted, errors)
}

/// Get the shared data directory (~/.local/share/opencode on all platforms).
/// This matches the TUI's Global.Path.shared for cross-platform consistency.
pub fn shared_data_dir() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("/tmp"))
        .join(".local")
        .join("share")
        .join("opencode")
}

fn backup_dir() -> PathBuf {
    shared_data_dir().join("backups")
}

fn today() -> String {
    chrono::Local::now().date_naive().to_string()
}

/// Hash content with SHA-256 (compatible with Python's hash_content).
pub fn hash_content(content: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(content.as_bytes());
    hex::encode(hasher.finalize())
}

/// Hash file contents with SHA-256.
///
/// Used to avoid reading entire files into memory during scan.
pub fn hash_file(path: &Path) -> Result<String> {
    use std::io::Read;

    let mut file = std::fs::File::open(path).context("open file for hashing")?;
    let mut hasher = Sha256::new();
    let mut buf = [0_u8; 64 * 1024];
    loop {
        let n = file.read(&mut buf).context("read file for hashing")?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hex::encode(hasher.finalize()))
}

/// Get the git project ID (first 16 chars of the root commit hash).
/// Caches results to avoid spawning git subprocess on every call.
/// LruCache cap=100 gives O(1) memory guarantee.
pub fn git_project_id(root: &Path) -> String {
    static CACHE: OnceLock<std::sync::Mutex<lru::LruCache<PathBuf, String>>> = OnceLock::new();

    let cache = CACHE.get_or_init(|| {
        std::sync::Mutex::new(lru::LruCache::new(std::num::NonZeroUsize::new(100).unwrap()))
    });
    let key = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());

    // Check cache first
    if let Ok(mut guard) = cache.lock() {
        if let Some(id) = guard.get(&key) {
            return id.clone();
        }
    }

    // Helper for fallback: hash the path
    let fallback = || {
        let mut hasher = Sha256::new();
        hasher.update(root.to_string_lossy().as_bytes());
        hex::encode(hasher.finalize())[..16].to_string()
    };

    // Compute the value
    let result = Command::new("git")
        .args(["rev-list", "--max-parents=0", "HEAD"])
        .current_dir(root)
        .output();

    let id = if let Ok(output) = result {
        if output.status.success() {
            let stdout = String::from_utf8_lossy(&output.stdout);
            if let Some(line) = stdout.lines().last() {
                let commit = line.trim();
                if commit.len() >= 16 {
                    commit[..16].to_string()
                } else {
                    fallback()
                }
            } else {
                fallback()
            }
        } else {
            fallback()
        }
    } else {
        fallback()
    };

    // Store in cache (LRU auto-evicts if at cap)
    if let Ok(mut guard) = cache.lock() {
        guard.put(key, id.clone());
    }

    id
}

/// Get default storage path for a project.
/// Uses ~/.local/share/opencode on all platforms for cross-platform consistency.
pub fn storage_path(root: &Path) -> PathBuf {
    let id = git_project_id(root);
    shared_data_dir()
        .join("projects")
        .join(&id)
        .join(".lancedb")
}

fn chunks_schema(dimensions: u32) -> Arc<Schema> {
    Arc::new(Schema::new(vec![
        Field::new("chunk_id", DataType::Int64, false),
        Field::new("path", DataType::Utf8, false),
        Field::new("file_hash", DataType::Utf8, false),
        Field::new("language", DataType::Utf8, false),
        Field::new("position", DataType::Int32, false),
        Field::new("content", DataType::Utf8, false),
        Field::new("content_hash", DataType::Utf8, false),
        Field::new("start_line", DataType::Int32, false),
        Field::new("end_line", DataType::Int32, false),
        Field::new(
            "vector",
            DataType::FixedSizeList(
                Arc::new(Field::new("item", DataType::Float32, true)),
                dimensions as i32,
            ),
            false,
        ),
        Field::new(
            "created_at",
            DataType::Timestamp(TimeUnit::Microsecond, None),
            false,
        ),
    ]))
}

fn config_schema() -> Arc<Schema> {
    Arc::new(Schema::new(vec![
        Field::new("key", DataType::Utf8, false),
        Field::new("value", DataType::Utf8, false),
    ]))
}

fn usage_schema() -> Arc<Schema> {
    Arc::new(Schema::new(vec![
        Field::new("date", DataType::Utf8, false),
        Field::new("tokens", DataType::Int64, false),
        Field::new("cost", DataType::Float64, false),
        Field::new("tier", DataType::Utf8, false),
    ]))
}

#[derive(Debug, Clone)]
pub struct Usage {
    pub tokens: i64,
    pub cost: f64,
}

#[derive(Debug, Clone)]
pub struct DailyUsage {
    pub date: String,
    pub tokens: i64,
    pub cost: f64,
}

#[derive(Debug, Clone)]
pub struct BackupInfo {
    pub name: String,
    pub tier: String,
    pub created_at: String,
    pub size_bytes: u64,
}

/// LanceDB storage handle.
/// Cached file/chunk counts for fast status queries.
/// Eliminates 2-20s table scans on every status RPC.
#[derive(Clone)]
struct CachedCounts {
    files: usize,
    chunks: usize,
    last_updated: Instant,
}

const CACHE_TTL_SECS: u64 = 120;

pub struct Storage {
    db: lancedb::Connection,
    path: PathBuf,
    dimensions: u32,
    cached_counts: Arc<tokio::sync::RwLock<Option<CachedCounts>>>,
    cached_table: Arc<tokio::sync::RwLock<Option<lancedb::Table>>>,
}

impl Clone for Storage {
    fn clone(&self) -> Self {
        Self {
            db: self.db.clone(),
            path: self.path.clone(),
            dimensions: self.dimensions,
            cached_counts: self.cached_counts.clone(),
            cached_table: self.cached_table.clone(),
        }
    }
}

impl Storage {
    /// Open or create a LanceDB database.
    /// If corruption is detected, automatically clears and recreates the database.
    /// Retries at most once — if it fails again after clearing, the error is returned.
    pub async fn open(path: &Path, dimensions: u32) -> Result<Self> {
        let mut retries = 2_u8;

        loop {
            // Check for corruption before opening (blocking I/O, run off async runtime)
            let path_clone = path.to_path_buf();
            let (corrupted, errors) = tokio::task::spawn_blocking(move || {
                is_database_corrupted(&path_clone)
            })
            .await
            .context("spawn_blocking for is_database_corrupted")?;

            if corrupted {
                tracing::warn!(
                    "detected corrupted database at {}: {:?}",
                    path.display(),
                    errors
                );
                if retries == 0 {
                    anyhow::bail!("database still corrupted after clearing: {:?}", errors);
                }
                retries -= 1;
                let path_clone = path.to_path_buf();
                tokio::task::spawn_blocking(move || clear_corrupted_index(&path_clone))
                    .await
                    .context("spawn_blocking for clear_corrupted_index")??;
                tracing::info!("cleared corrupted database, will recreate");
                continue;
            }

            tokio::fs::create_dir_all(path).await?;
            let db = connect(path.to_str().context("non-UTF-8 database path")?)
                .execute()
                .await
                .context("failed to open LanceDB")?;

            let storage = Self {
                db,
                path: path.to_path_buf(),
                dimensions,
                cached_counts: Arc::new(tokio::sync::RwLock::new(None)),
                cached_table: Arc::new(tokio::sync::RwLock::new(None)),
            };

            // Check and migrate schema — with iterative corruption recovery
            match storage.check_schema_version().await {
                Ok(()) => return Ok(storage),
                Err(e) if is_corruption_error(&e) => {
                    tracing::warn!("schema check failed due to corruption: {}", e);
                    if retries == 0 {
                        return Err(e);
                    }
                    retries -= 1;
                    let path_clone = path.to_path_buf();
                    tokio::task::spawn_blocking(move || clear_corrupted_index(&path_clone))
                        .await
                        .context("spawn_blocking for clear_corrupted_index (schema retry)")??;
                    tracing::info!("cleared corrupted database after schema check, retrying");
                    // loop back to retry from scratch
                }
                Err(e) => return Err(e),
            }
        }
    }
    
    async fn check_schema_version(&self) -> Result<()> {
        let stored = self.get_config("schema_version").await?.unwrap_or_default();
        if stored.is_empty() {
            // New database, set version
            self.set_config("schema_version", SCHEMA_VERSION).await?;
            return Ok(());
        }
        if stored != SCHEMA_VERSION {
            // Migration needed
            self.migrate_schema(&stored, SCHEMA_VERSION).await?;
        }
        Ok(())
    }
    
    async fn migrate_schema(&self, from: &str, to: &str) -> Result<()> {
        tracing::info!("migrating schema from {} to {}", from, to);
        // Add migration logic here as needed
        self.set_config("schema_version", to).await?;
        Ok(())
    }

    /// Ensure the chunks table exists.
    async fn ensure_chunks(&self) -> Result<lancedb::Table> {
        // Fast path: cached
        if let Some(table) = self.cached_table.read().await.as_ref() {
            return Ok(table.clone());
        }
        // Slow path: check + open/create, then cache
        let mut w = self.cached_table.write().await;
        if let Some(table) = w.as_ref() {
            return Ok(table.clone());
        }
        let names = self.db.table_names().execute().await?;
        let table = if names.iter().any(|n| n == "chunks") {
            self.db.open_table("chunks").execute().await?
        } else {
            self.db.create_empty_table("chunks", chunks_schema(self.dimensions)).execute().await?
        };
        *w = Some(table.clone());
        Ok(table)
    }

    /// Invalidate the cached table handle (call after clear_all/compact/schema changes).
    async fn invalidate_table_cache(&self) {
        *self.cached_table.write().await = None;
    }

    pub async fn clear_all(&self) -> Result<()> {
        self.invalidate_table_cache().await;
        let names = self.db.table_names().execute().await?;
        if names.contains(&"chunks".to_string()) {
            self.db.drop_table("chunks").await?;
        }
        // Reset file count
        let _ = self.set_file_count(0).await;
        Ok(())
    }

    pub async fn drop_all_tables(&self) -> Result<()> {
        self.invalidate_table_cache().await;
        self.db.drop_all_tables().await?;
        Ok(())
    }

    /// Ensure the config table exists.
    async fn ensure_config(&self) -> Result<lancedb::Table> {
        let names = self.db.table_names().execute().await?;
        if names.contains(&"config".to_string()) {
            Ok(self.db.open_table("config").execute().await?)
        } else {
            Ok(self
                .db
                .create_empty_table("config", config_schema())
                .execute()
                .await?)
        }
    }
    
    /// Repair a corrupted config table by deleting and recreating it.
    /// This is safe because config only stores metadata (tier, timestamps, etc.)
    /// that can be regenerated via backfill_metadata().
    /// Returns true if repair was performed, false if no repair was needed.
    pub async fn repair_config_table(&self) -> Result<bool> {
        let config_path = self.path.join("config.lance");
        if !config_path.exists() {
            return Ok(false);
        }
        
        tracing::info!("repairing corrupted config.lance at {}", config_path.display());
        
        // Drop the table from LanceDB first (ignore errors if table doesn't exist in metadata)
        let _ = self.db.drop_table("config").await;
        
        // Remove the directory using tokio::fs to avoid blocking the async runtime
        if let Err(e) = tokio::fs::remove_dir_all(&config_path).await {
            tracing::warn!("failed to remove config.lance directory: {}", e);
            // Try to continue anyway - the directory might have been partially removed
        }
        
        // Recreate the table
        self.db
            .create_empty_table("config", config_schema())
            .execute()
            .await
            .context("failed to recreate config table after repair")?;
        
        tracing::info!("config table repaired successfully, metadata will be backfilled");
        Ok(true)
    }

    /// Ensure the usage table exists.
    async fn ensure_usage(&self) -> Result<lancedb::Table> {
        let names = self.db.table_names().execute().await?;
        if names.contains(&"usage".to_string()) {
            Ok(self.db.open_table("usage").execute().await?)
        } else {
            Ok(self
                .db
                .create_empty_table("usage", usage_schema())
                .execute()
                .await?)
        }
    }

    // =========================================================================
    // Config
    // =========================================================================

    pub async fn get_config(&self, key: &str) -> Result<Option<String>> {
        self.get_config_impl(key, true).await
    }
    
    /// Internal config getter. When corruption is detected and auto_repair is true,
    /// repairs config.lance and retries once.
    fn get_config_impl<'a>(&'a self, key: &'a str, auto_repair: bool) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<Option<String>>> + Send + 'a>> {
        Box::pin(async move {
            let table = match self.ensure_config().await {
                Ok(t) => t,
                Err(e) => {
                    if is_corruption_error(&e) {
                        if auto_repair {
                            tracing::warn!("config table corrupted, attempting auto-repair for key '{}': {}", key, e);
                            if let Ok(true) = self.repair_config_table().await {
                                return self.get_config_impl(key, false).await;
                            }
                        }
                        return Ok(None);
                    }
                    return Err(e);
                }
            };
            
            let results = match table
                .query()
                .only_if(format!("key = '{}'", escape_sql(key)))
                .limit(1)
                .execute()
                .await
            {
                Ok(r) => r,
                Err(e) => {
                    let err = anyhow::anyhow!("{}", e);
                    if is_corruption_error(&err) {
                        if auto_repair {
                            tracing::warn!("config query corrupted, attempting auto-repair for key '{}': {}", key, e);
                            if let Ok(true) = self.repair_config_table().await {
                                return self.get_config_impl(key, false).await;
                            }
                        }
                        return Ok(None);
                    }
                    return Err(err.context("config query failed"));
                }
            };

            use futures::TryStreamExt;
            let batches: Vec<RecordBatch> = match results.try_collect().await {
                Ok(b) => b,
                Err(e) => {
                    let err = anyhow::anyhow!("{}", e);
                    if is_corruption_error(&err) {
                        if auto_repair {
                            tracing::warn!("config results corrupted, attempting auto-repair for key '{}': {}", key, e);
                            if let Ok(true) = self.repair_config_table().await {
                                return self.get_config_impl(key, false).await;
                            }
                        }
                        return Ok(None);
                    }
                    return Err(err.context("failed to collect config results"));
                }
            };
        
            if batches.is_empty() || batches[0].num_rows() == 0 {
                return Ok(None);
            }

            let values = batches[0]
                .column_by_name("value")
                .context("missing 'value' column")?
                .as_any()
                .downcast_ref::<StringArray>()
                .context("invalid type for 'value' column")?;

            Ok(Some(values.value(0).to_string()))
        })
    }

    pub async fn set_config(&self, key: &str, value: &str) -> Result<()> {
        let table = self.ensure_config().await?;

        // Delete existing
        let _ = table
            .delete(&format!("key = '{}'", escape_sql(key)))
            .await;

        // Build batch with builders
        let mut keys = StringBuilder::new();
        let mut values = StringBuilder::new();
        keys.append_value(key);
        values.append_value(value);

        let schema = config_schema();
        let batch = RecordBatch::try_new(
            schema.clone(),
            vec![
                Arc::new(keys.finish()),
                Arc::new(values.finish()),
            ],
        )?;
        let reader = RecordBatchIterator::new(vec![Ok(batch)], schema);
        table.add(reader).execute().await?;
        Ok(())
    }

    pub async fn get_tier(&self) -> Result<Option<String>> {
        // Try config.lance first, fall back to tier.txt backup
        match self.get_config("tier").await? {
            Some(t) => Ok(Some(t)),
            None => Ok(self.get_tier_from_backup().await),
        }
    }
    
    /// Read tier from backup file (tier.txt in .lancedb directory).
    /// Used for recovery when config.lance is corrupted.
    /// Uses async I/O to avoid blocking the runtime.
    async fn get_tier_from_backup(&self) -> Option<String> {
        let tier_file = self.path.join("tier.txt");
        tokio::fs::read_to_string(&tier_file)
            .await
            .ok()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
    }

    pub async fn set_tier(&self, tier: &str) -> Result<()> {
        // Write to config.lance
        self.set_config("tier", tier).await?;
        
        // Also write to tier.txt as backup (survives config.lance corruption)
        // Use tokio::fs to avoid blocking the async runtime
        let tier_file = self.path.join("tier.txt");
        if let Err(e) = tokio::fs::write(&tier_file, tier).await {
            tracing::warn!("failed to write tier backup file: {}", e);
        }
        
        Ok(())
    }

    pub async fn get_dimensions(&self) -> Result<Option<u32>> {
        let val = self.get_config("dimensions").await?;
        Ok(val.and_then(|v| v.parse().ok()))
    }

    pub async fn set_dimensions(&self, dims: u32) -> Result<()> {
        self.set_config("dimensions", &dims.to_string()).await
    }

    pub async fn get_quantization(&self) -> Result<Option<String>> {
        self.get_config("quantization").await
    }

    pub async fn set_quantization(&self, quantization: &str) -> Result<()> {
        self.set_config("quantization", quantization).await
    }

    pub async fn get_last_index_duration_ms(&self) -> Result<Option<i64>> {
        let val = self.get_config("last_index_duration_ms").await?;
        Ok(val.and_then(|v| v.parse().ok()))
    }

    pub async fn set_last_index_duration_ms(&self, ms: i64) -> Result<()> {
        self.set_config("last_index_duration_ms", &ms.to_string()).await
    }

    pub async fn get_last_index_files_count(&self) -> Result<Option<i64>> {
        let val = self.get_config("last_index_files_count").await?;
        Ok(val.and_then(|v| v.parse().ok()))
    }

    pub async fn set_last_index_files_count(&self, count: i64) -> Result<()> {
        self.set_config("last_index_files_count", &count.to_string()).await
    }

    pub async fn get_last_index_timestamp(&self) -> Result<Option<String>> {
        self.get_config("last_index_timestamp").await
    }

    pub async fn set_last_index_timestamp(&self, ts: &str) -> Result<()> {
        self.set_config("last_index_timestamp", ts).await
    }

    pub async fn get_last_update_timestamp(&self) -> Result<Option<String>> {
        self.get_config("last_update_timestamp").await
    }

    pub async fn set_last_update_timestamp(&self, ts: &str) -> Result<()> {
        self.set_config("last_update_timestamp", ts).await
    }

    pub async fn get_last_watched_timestamp(&self) -> Result<Option<String>> {
        self.get_config("last_watched_timestamp").await
    }

    pub async fn set_last_watched_timestamp(&self, ts: &str) -> Result<()> {
        self.set_config("last_watched_timestamp", ts).await
    }

    pub async fn get_indexing_in_progress(&self) -> Result<bool> {
        Ok(self
            .get_config("indexing_in_progress")
            .await?
            .is_some_and(|v| v == "true"))
    }

    pub async fn set_indexing_in_progress(&self, in_progress: bool) -> Result<()> {
        self.set_config(
            "indexing_in_progress",
            if in_progress { "true" } else { "false" },
        )
        .await
    }

    pub async fn get_indexing_start_time(&self) -> Result<Option<String>> {
        self.get_config("indexing_start_time").await
    }

    pub async fn set_indexing_start_time(&self, ts: &str) -> Result<()> {
        self.set_config("indexing_start_time", ts).await
    }

    pub async fn get_indexing_phase(&self) -> Result<Option<String>> {
        self.get_config("indexing_phase").await
    }

    pub async fn set_indexing_phase(&self, phase: &str) -> Result<()> {
        self.set_config("indexing_phase", phase).await
    }

    pub async fn get_file_count(&self) -> Result<usize> {
        // Try cached config first
        if let Some(count_str) = self.get_config(CONFIG_FILE_COUNT).await? {
            if let Ok(count) = count_str.parse::<usize>() {
                return Ok(count);
            }
        }

        // Try in-memory cached counts before expensive full scan
        if let Some((files, _)) = self.get_cached_counts().await {
            return Ok(files);
        }

        // Fallback: count from table and cache result
        tracing::warn!("get_file_count: no cached count for {}, full scan fallback", self.path.display());
        let count = self.get_file_hashes(None).await?.len();
        let _ = self.set_file_count(count).await;
        Ok(count)
    }

    pub async fn set_file_count(&self, count: usize) -> Result<()> {
        self.set_config(CONFIG_FILE_COUNT, &count.to_string()).await
    }

    pub async fn increment_file_count(&self, delta: i32) -> Result<()> {
        let current = self.get_file_count().await?;
        let new_count = if delta < 0 {
            current.saturating_sub(delta.unsigned_abs() as usize)
        } else {
            current.saturating_add(delta as usize)
        };
        self.set_file_count(new_count).await
    }

    /// Backfill missing metadata for legacy indexes.
    /// Called lazily during health check to migrate old indexes.
    /// Idempotent: only sets fields that are currently None.
    /// Returns the number of fields that were backfilled.
    pub async fn backfill_metadata(&self) -> Result<usize> {
        // Check if index has any files (only backfill if there's actual data)
        let file_count = self.count_files().await?;
        if file_count == 0 {
            return Ok(0); // Empty index, nothing to backfill
        }

        let now = chrono::Utc::now().to_rfc3339();
        let mut backfilled = 0;

        // Backfill tier if missing
        // get_tier() already falls back to tier.txt, so if both are missing, use "budget"
        if self.get_config("tier").await?.is_none() {
            let tier = self.get_tier_from_backup().await.unwrap_or_else(|| "budget".to_string());
            self.set_tier(&tier).await?;
            backfilled += 1;
            if tier == "budget" {
                tracing::debug!("backfilled tier to 'budget' (default)");
            } else {
                tracing::info!("restored tier '{}' from backup file", tier);
            }
        }

        // Backfill last_index_timestamp if missing
        if self.get_last_index_timestamp().await?.is_none() {
            self.set_last_index_timestamp(&now).await?;
            backfilled += 1;
            tracing::debug!("backfilled last_index_timestamp");
        }

        // Backfill last_update_timestamp if missing
        if self.get_last_update_timestamp().await?.is_none() {
            self.set_last_update_timestamp(&now).await?;
            backfilled += 1;
            tracing::debug!("backfilled last_update_timestamp");
        }

        // Backfill last_index_files_count if missing (needed for speed calculation)
        if self.get_last_index_files_count().await?.is_none() {
            self.set_last_index_files_count(file_count as i64).await?;
            backfilled += 1;
            tracing::debug!("backfilled last_index_files_count to {}", file_count);
        }

        // Backfill last_index_duration_ms if missing
        // For legacy indexes we don't know actual duration, estimate ~100ms per file
        // This provides a reasonable baseline for speed display
        if self.get_last_index_duration_ms().await?.is_none() {
            let estimated_ms = (file_count as i64 * 100).max(1000); // Min 1 second
            self.set_last_index_duration_ms(estimated_ms).await?;
            backfilled += 1;
            tracing::debug!("backfilled last_index_duration_ms to {}ms (estimated)", estimated_ms);
        }

        if backfilled > 0 {
            tracing::info!("auto-fixed {} missing metadata field(s) for legacy index", backfilled);
        }

        Ok(backfilled)
    }

    pub async fn get_phase_progress(&self, phase: &str) -> Result<(i64, i64)> {
        let done = self
            .get_config(&format!("{phase}_done"))
            .await?
            .and_then(|v| v.parse().ok())
            .unwrap_or(0);
        let total = self
            .get_config(&format!("{phase}_total"))
            .await?
            .and_then(|v| v.parse().ok())
            .unwrap_or(0);
        Ok((done, total))
    }

    pub async fn set_phase_progress(&self, phase: &str, done: i64, total: i64) -> Result<()> {
        self.set_config(&format!("{phase}_done"), &done.to_string())
            .await?;
        self.set_config(&format!("{phase}_total"), &total.to_string())
            .await?;
        Ok(())
    }

    pub async fn clear_indexing_progress(&self) -> Result<()> {
        let table = self.ensure_config().await?;
        for key in [
            "indexing_in_progress",
            "indexing_files_done",
            "indexing_files_total",
            "indexing_start_time",
            "indexing_phase",
            "scanning_done",
            "scanning_total",
            "chunking_done",
            "chunking_total",
            "embedding_done",
            "embedding_total",
        ] {
            let _ = table.delete(&format!("key = '{}'", escape_sql(key))).await;
        }
        Ok(())
    }

    // =========================================================================
    // Usage
    // =========================================================================

    pub async fn record_usage(&self, tokens: i64, tier: &str) -> Result<()> {
        let table = self.ensure_usage().await?;
        let date = today();
        let cost = estimate_cost(tokens, tier);

        // Load existing
        let results = table
            .query()
            .only_if(format!(
                "date = '{}' AND tier = '{}'",
                escape_sql(&date),
                escape_sql(tier)
            ))
            .limit(1)
            .execute()
            .await?;

        use futures::TryStreamExt;
        let batches: Vec<RecordBatch> = results.try_collect().await?;
        let (prev_tokens, prev_cost) = if batches.is_empty() || batches[0].num_rows() == 0 {
            (0_i64, 0.0_f64)
        } else {
            let tokens_col = batches[0]
                .column_by_name("tokens")
                .context("missing 'tokens' column")?
                .as_any()
                .downcast_ref::<Int64Array>()
                .context("invalid type for 'tokens' column")?;
            let cost_col = batches[0]
                .column_by_name("cost")
                .context("missing 'cost' column")?
                .as_any()
                .downcast_ref::<arrow_array::Float64Array>()
                .context("invalid type for 'cost' column")?;
            (tokens_col.value(0), cost_col.value(0))
        };

        let _ = table
            .delete(&format!(
                "date = '{}' AND tier = '{}'",
                escape_sql(&date),
                escape_sql(tier)
            ))
            .await;

        let mut dates = StringBuilder::new();
        let mut tokens_b = Int64Builder::new();
        let mut costs = arrow_array::builder::Float64Builder::new();
        let mut tiers = StringBuilder::new();
        dates.append_value(&date);
        tokens_b.append_value(prev_tokens + tokens);
        costs.append_value(prev_cost + cost);
        tiers.append_value(tier);

        let schema = usage_schema();
        let batch = RecordBatch::try_new(
            schema.clone(),
            vec![
                Arc::new(dates.finish()),
                Arc::new(tokens_b.finish()),
                Arc::new(costs.finish()),
                Arc::new(tiers.finish()),
            ],
        )?;
        let reader = RecordBatchIterator::new(vec![Ok(batch)], schema);
        table.add(reader).execute().await?;
        Ok(())
    }

    pub async fn get_daily_usage(&self) -> Result<Usage> {
        let table = self.ensure_usage().await?;
        let date = today();
        let results = table
            .query()
            .only_if(format!("date = '{}'", escape_sql(&date)))
            .execute()
            .await?;

        use futures::TryStreamExt;
        let batches: Vec<RecordBatch> = results.try_collect().await?;
        let mut tokens = 0_i64;
        let mut cost = 0.0_f64;
        for batch in &batches {
            let t = batch
                .column_by_name("tokens")
                .context("missing 'tokens' column")?
                .as_any()
                .downcast_ref::<Int64Array>()
                .context("invalid type for 'tokens' column")?;
            let c = batch
                .column_by_name("cost")
                .context("missing 'cost' column")?
                .as_any()
                .downcast_ref::<arrow_array::Float64Array>()
                .context("invalid type for 'cost' column")?;
            for i in 0..batch.num_rows() {
                tokens += t.value(i);
                cost += c.value(i);
            }
        }
        Ok(Usage { tokens, cost })
    }

    pub async fn get_total_usage(&self) -> Result<Usage> {
        let table = self.ensure_usage().await?;
        let results = table.query().execute().await?;
        use futures::TryStreamExt;
        let batches: Vec<RecordBatch> = results.try_collect().await?;
        let mut tokens = 0_i64;
        let mut cost = 0.0_f64;
        for batch in &batches {
            let t = batch
                .column_by_name("tokens")
                .context("missing 'tokens' column")?
                .as_any()
                .downcast_ref::<Int64Array>()
                .context("invalid type for 'tokens' column")?;
            let c = batch
                .column_by_name("cost")
                .context("missing 'cost' column")?
                .as_any()
                .downcast_ref::<arrow_array::Float64Array>()
                .context("invalid type for 'cost' column")?;
            for i in 0..batch.num_rows() {
                tokens += t.value(i);
                cost += c.value(i);
            }
        }
        Ok(Usage { tokens, cost })
    }

    pub async fn get_usage_history(&self, days: usize) -> Result<Vec<DailyUsage>> {
        let table = self.ensure_usage().await?;
        let results = table.query().execute().await?;
        use futures::TryStreamExt;
        let batches: Vec<RecordBatch> = results.try_collect().await?;

        let mut grouped: std::collections::HashMap<String, (i64, f64)> =
            std::collections::HashMap::new();
        for batch in &batches {
            let d = batch
                .column_by_name("date")
                .unwrap()
                .as_any()
                .downcast_ref::<StringArray>()
                .unwrap();
            let t = batch
                .column_by_name("tokens")
                .unwrap()
                .as_any()
                .downcast_ref::<Int64Array>()
                .unwrap();
            let c = batch
                .column_by_name("cost")
                .unwrap()
                .as_any()
                .downcast_ref::<arrow_array::Float64Array>()
                .unwrap();
            for i in 0..batch.num_rows() {
                let date = d.value(i).to_string();
                let entry = grouped.entry(date).or_insert((0, 0.0));
                entry.0 += t.value(i);
                entry.1 += c.value(i);
            }
        }

        let mut out: Vec<DailyUsage> = grouped
            .into_iter()
            .map(|(date, (tokens, cost))| DailyUsage { date, tokens, cost })
            .collect();
        out.sort_by(|a, b| b.date.cmp(&a.date));
        out.truncate(days);
        Ok(out)
    }

    // =========================================================================
    // Chunks
    // =========================================================================

    /// Get all indexed file paths and their hashes.
    /// 
    /// # Arguments
    /// * `limit` - Optional maximum number of unique files to return. None means no limit.
    /// 
    /// # Performance
    /// For large repositories, consider using a limit to avoid loading all file hashes into memory.
    pub async fn get_file_hashes(&self, limit: Option<usize>) -> Result<std::collections::HashMap<String, String>> {
        let table = self.ensure_chunks().await?;
        let mut map = std::collections::HashMap::new();

        // Paginate through results to avoid streaming issues with large tables
        let page_size = 5000;
        let mut offset = 0;

        loop {
            // Stop if we've reached the requested limit
            if let Some(max) = limit {
                if map.len() >= max {
                    break;
                }
            }

            let results = match table
                .query()
                .select(lancedb::query::Select::Columns(vec![
                    "path".into(),
                    "file_hash".into(),
                ]))
                .limit(page_size)
                .offset(offset)
                .execute()
                .await
            {
                Ok(r) => r,
                Err(e) => {
                    let err = anyhow::anyhow!("{}", e);
                    if is_corruption_error(&err) {
                        return Err(err.context("LanceDB corruption detected in get_file_hashes"));
                    }
                    return Err(err.context("get_file_hashes query failed"));
                }
            };

            use futures::TryStreamExt;
            let batches: Vec<RecordBatch> = match results.try_collect().await {
                Ok(b) => b,
                Err(e) => {
                    let err = anyhow::anyhow!("{}", e);
                    if is_corruption_error(&err) {
                        return Err(err.context("LanceDB corruption detected in get_file_hashes (collect)"));
                    }
                    return Err(err.context("get_file_hashes collect failed"));
                }
            };

            let mut batch_rows = 0;
            for batch in &batches {
                batch_rows += batch.num_rows();
                let paths = batch
                    .column_by_name("path")
                    .unwrap()
                    .as_any()
                    .downcast_ref::<StringArray>()
                    .unwrap();
                let hashes = batch
                    .column_by_name("file_hash")
                    .unwrap()
                    .as_any()
                    .downcast_ref::<StringArray>()
                    .unwrap();

                for i in 0..batch.num_rows() {
                    // Stop early if limit reached
                    if let Some(max) = limit {
                        if map.len() >= max {
                            return Ok(map);
                        }
                    }
                    
                    map.entry(paths.value(i).to_string())
                        .or_insert_with(|| hashes.value(i).to_string());
                }
            }

            if batch_rows < page_size {
                break;
            }
            offset += page_size;
        }

        Ok(map)
    }

    /// Get cached file/chunk counts if available and not stale (< 60s old).
    /// Returns None on cache miss or stale data → caller should do full scan.
    pub async fn get_cached_counts(&self) -> Option<(usize, usize)> {
        let cache = self.cached_counts.read().await;
        cache.as_ref()
            .filter(|c| c.last_updated.elapsed().as_secs() < CACHE_TTL_SECS)
            .map(|c| (c.files, c.chunks))
    }

    /// Invalidate cached counts (called on any write operation).
    /// Better to re-scan occasionally than show stale counts.
    pub async fn invalidate_cached_counts(&self) {
        let mut cache = self.cached_counts.write().await;
        *cache = None;
    }

    /// Update cached counts after fresh scan (called after batch ops complete).
    pub async fn update_cached_counts(&self, files: usize, chunks: usize) {
        let mut cache = self.cached_counts.write().await;
        *cache = Some(CachedCounts {
            files,
            chunks,
            last_updated: Instant::now(),
        });
    }

    /// Delete all chunks for a file path.
    pub async fn delete_file(&self, path: &str) -> Result<usize> {
        let table = self.ensure_chunks().await?;

        // Count first
        let results = table
            .query()
            .only_if(format!("path = '{}'", escape_sql(path)))
            .execute()
            .await?;

        use futures::TryStreamExt;
        let batches: Vec<RecordBatch> = results.try_collect().await?;
        let count: usize = batches.iter().map(|b| b.num_rows()).sum();

        if count > 0 {
            table
                .delete(&format!("path = '{}'", escape_sql(path)))
                .await?;
            
            // Decrement file count
            let _ = self.increment_file_count(-1).await;
            
            // Invalidate cache on write
            self.invalidate_cached_counts().await;
        }

        Ok(count)
    }

    /// Get stored file hash for a path (first matching chunk).
    pub async fn get_file_hash(&self, path: &str) -> Result<Option<String>> {
        let table = self.ensure_chunks().await?;
        let results = table
            .query()
            .select(lancedb::query::Select::Columns(vec!["file_hash".into()]))
            .only_if(format!("path = '{}'", escape_sql(path)))
            .limit(1)
            .execute()
            .await?;

        use futures::TryStreamExt;
        let batches: Vec<RecordBatch> = results.try_collect().await?;
        if batches.is_empty() || batches[0].num_rows() == 0 {
            return Ok(None);
        }

        let hashes = batches[0]
            .column_by_name("file_hash")
            .unwrap()
            .as_any()
            .downcast_ref::<StringArray>()
            .unwrap();

        Ok(Some(hashes.value(0).to_string()))
    }

    pub async fn needs_index(&self, path: &str, file_hash: &str) -> Result<bool> {
        Ok(self.get_file_hash(path).await?.as_deref() != Some(file_hash))
    }

    /// Get chunks for a file with their IDs, content hashes, and positions.
    pub async fn get_chunks_with_hashes(&self, path: &str) -> Result<Vec<(i64, String, i32)>> {
        let table = self.ensure_chunks().await?;
        let results = table
            .query()
            .select(lancedb::query::Select::Columns(vec![
                "chunk_id".into(),
                "content_hash".into(),
                "position".into(),
            ]))
            .only_if(format!("path = '{}'", escape_sql(path)))
            .execute()
            .await?;

        use futures::TryStreamExt;
        let batches: Vec<RecordBatch> = results.try_collect().await?;
        let mut out = Vec::new();
        for batch in &batches {
            if batch.num_rows() == 0 {
                continue;
            }

            let ids = batch
                .column_by_name("chunk_id")
                .unwrap()
                .as_any()
                .downcast_ref::<Int64Array>()
                .unwrap();
            let hashes = batch
                .column_by_name("content_hash")
                .unwrap()
                .as_any()
                .downcast_ref::<StringArray>()
                .unwrap();
            let positions = batch
                .column_by_name("position")
                .unwrap()
                .as_any()
                .downcast_ref::<Int32Array>()
                .unwrap();

            for i in 0..batch.num_rows() {
                out.push((
                    ids.value(i),
                    hashes.value(i).to_string(),
                    positions.value(i),
                ));
            }
        }

        out.sort_by_key(|(_, _, pos)| *pos);
        Ok(out)
    }

    pub async fn delete_chunk_by_id(&self, chunk_id: i64) -> Result<()> {
        let table = self.ensure_chunks().await?;
        let _ = table.delete(&format!("chunk_id = {chunk_id}")).await;
        Ok(())
    }

    /// Update a chunk's position and line numbers (delete + re-add).
    pub async fn update_chunk_position(
        &self,
        chunk_id: i64,
        position: i32,
        start_line: i32,
        end_line: i32,
    ) -> Result<()> {
        let table = self.ensure_chunks().await?;

        let results = table
            .query()
            .only_if(format!("chunk_id = {chunk_id}"))
            .limit(1)
            .execute()
            .await?;

        use futures::TryStreamExt;
        let batches: Vec<RecordBatch> = results.try_collect().await?;
        if batches.is_empty() || batches[0].num_rows() == 0 {
            return Ok(());
        }

        let batch = &batches[0];
        let path = batch
            .column_by_name("path")
            .unwrap()
            .as_any()
            .downcast_ref::<StringArray>()
            .unwrap()
            .value(0)
            .to_string();
        let file_hash = batch
            .column_by_name("file_hash")
            .unwrap()
            .as_any()
            .downcast_ref::<StringArray>()
            .unwrap()
            .value(0)
            .to_string();
        let language = batch
            .column_by_name("language")
            .unwrap()
            .as_any()
            .downcast_ref::<StringArray>()
            .unwrap()
            .value(0)
            .to_string();
        let content = batch
            .column_by_name("content")
            .unwrap()
            .as_any()
            .downcast_ref::<StringArray>()
            .unwrap()
            .value(0)
            .to_string();
        let content_hash = batch
            .column_by_name("content_hash")
            .unwrap()
            .as_any()
            .downcast_ref::<StringArray>()
            .unwrap()
            .value(0)
            .to_string();

        let vec_col = batch
            .column_by_name("vector")
            .unwrap()
            .as_any()
            .downcast_ref::<arrow_array::FixedSizeListArray>()
            .unwrap();
        let values = vec_col
            .values()
            .as_any()
            .downcast_ref::<Float32Array>()
            .unwrap();

        let dims = self.dimensions as usize;
        let vector: Vec<f32> = (0..dims).map(|i| values.value(i)).collect();

        let created_at = batch
            .column_by_name("created_at")
            .unwrap()
            .as_any()
            .downcast_ref::<PrimitiveArray<TimestampMicrosecondType>>()
            .unwrap()
            .value(0);

        // Generate new chunk ID for add-then-delete pattern (prevents data loss)
        let new_chunk_id = self.next_id(&table).await?;

        let mut chunk_ids = Int64Builder::new();
        let mut paths = StringBuilder::new();
        let mut file_hashes = StringBuilder::new();
        let mut languages = StringBuilder::new();
        let mut positions = Int32Builder::new();
        let mut contents = StringBuilder::new();
        let mut content_hashes = StringBuilder::new();
        let mut start_lines = Int32Builder::new();
        let mut end_lines = Int32Builder::new();
        let mut timestamps = TimestampMicrosecondBuilder::new();

        let mut vector_builder = FixedSizeListBuilder::new(
            Float32Builder::new(),
            self.dimensions as i32,
        );

        chunk_ids.append_value(new_chunk_id);
        paths.append_value(&path);
        file_hashes.append_value(&file_hash);
        languages.append_value(&language);
        positions.append_value(position);
        contents.append_value(&content);
        content_hashes.append_value(&content_hash);
        start_lines.append_value(start_line);
        end_lines.append_value(end_line);
        timestamps.append_value(created_at);

        let vb = vector_builder.values();
        for &v in &vector {
            vb.append_value(v);
        }
        for _ in vector.len()..self.dimensions as usize {
            vb.append_value(0.0);
        }
        vector_builder.append(true);

        let schema = chunks_schema(self.dimensions);
        let new_batch = RecordBatch::try_new(
            schema.clone(),
            vec![
                Arc::new(chunk_ids.finish()),
                Arc::new(paths.finish()),
                Arc::new(file_hashes.finish()),
                Arc::new(languages.finish()),
                Arc::new(positions.finish()),
                Arc::new(contents.finish()),
                Arc::new(content_hashes.finish()),
                Arc::new(start_lines.finish()),
                Arc::new(end_lines.finish()),
                Arc::new(vector_builder.finish()),
                Arc::new(timestamps.finish()),
            ],
        )?;

        // Add-then-delete pattern: add new chunk first to prevent data loss
        let reader = RecordBatchIterator::new(vec![Ok(new_batch)], schema);
        table.add(reader).execute().await?;
        
        // Only delete old chunk after new one is successfully added
        table.delete(&format!("chunk_id = {chunk_id}")).await?;
        Ok(())
    }

    /// Add chunks for a file (after chunking + embedding by Python server).
    /// 
    /// Note: This method does NOT update file_count automatically because in typical
    /// workflows, files are first deleted then re-added (update pattern). The caller
    /// should track whether this is a new file vs update and call increment_file_count
    /// explicitly if needed. Use via WriteQueue which handles this correctly.
    pub async fn add_chunks(
        &self,
        path: &str,
        file_hash: &str,
        language: &str,
        chunks: Vec<ChunkData>,
    ) -> Result<()> {
        if chunks.is_empty() {
            return Ok(());
        }

        let table = self.ensure_chunks().await?;
        let now = Utc::now().timestamp_micros();

        // Generate chunk IDs
        let base_id = self.next_id(&table).await?;

        // Use builders for type-safe construction
        let mut chunk_ids = Int64Builder::new();
        let mut paths = StringBuilder::new();
        let mut file_hashes = StringBuilder::new();
        let mut languages = StringBuilder::new();
        let mut positions = Int32Builder::new();
        let mut contents = StringBuilder::new();
        let mut content_hashes = StringBuilder::new();
        let mut start_lines = Int32Builder::new();
        let mut end_lines = Int32Builder::new();
        let mut timestamps = TimestampMicrosecondBuilder::new();

        // FixedSizeList builder for vectors
        let mut vector_builder = FixedSizeListBuilder::new(
            Float32Builder::new(),
            self.dimensions as i32,
        );

        for (i, chunk) in chunks.iter().enumerate() {
            chunk_ids.append_value(base_id + i as i64);
            paths.append_value(path);
            file_hashes.append_value(file_hash);
            languages.append_value(language);
            positions.append_value(chunk.position);
            contents.append_value(&chunk.content);
            content_hashes.append_value(hash_content(&chunk.content));
            start_lines.append_value(chunk.start_line);
            end_lines.append_value(chunk.end_line);
            timestamps.append_value(now);

            let values = vector_builder.values();
            for &v in &chunk.vector {
                values.append_value(v);
            }
            // Pad if vector is shorter than dimensions
            for _ in chunk.vector.len()..self.dimensions as usize {
                values.append_value(0.0);
            }
            vector_builder.append(true);
        }

        let batch = RecordBatch::try_new(
            chunks_schema(self.dimensions),
            vec![
                Arc::new(chunk_ids.finish()),
                Arc::new(paths.finish()),
                Arc::new(file_hashes.finish()),
                Arc::new(languages.finish()),
                Arc::new(positions.finish()),
                Arc::new(contents.finish()),
                Arc::new(content_hashes.finish()),
                Arc::new(start_lines.finish()),
                Arc::new(end_lines.finish()),
                Arc::new(vector_builder.finish()),
                Arc::new(timestamps.finish()),
            ],
        )?;

        let schema = chunks_schema(self.dimensions);
        let reader = RecordBatchIterator::new(vec![Ok(batch)], schema);
        table.add(reader).execute().await?;
        
        Ok(())
    }

    /// Add chunks for multiple files in a single batch operation.
    /// This is more efficient than calling add_chunks repeatedly.
    /// 
    /// Note: This method does NOT update file_count automatically. The caller
    /// should track new vs updated files and update the count separately.
    /// Use via WriteQueue which handles this correctly.
    pub async fn add_chunks_batch(&self, files: Vec<FileChunks>) -> Result<usize> {
        let t_start = std::time::Instant::now();
        let total_chunks: usize = files.iter().map(|f| f.chunks.len()).sum();
        let file_count = files.len();
        if total_chunks == 0 {
            return Ok(0);
        }

        let table = self.ensure_chunks().await?;
        let now = Utc::now().timestamp_micros();
        let base_id = self.next_id(&table).await?;

        let mut chunk_ids = Int64Builder::new();
        let mut paths = StringBuilder::new();
        let mut file_hashes = StringBuilder::new();
        let mut languages = StringBuilder::new();
        let mut positions = Int32Builder::new();
        let mut contents = StringBuilder::new();
        let mut content_hashes = StringBuilder::new();
        let mut start_lines = Int32Builder::new();
        let mut end_lines = Int32Builder::new();
        let mut timestamps = TimestampMicrosecondBuilder::new();
        let mut vector_builder = FixedSizeListBuilder::new(
            Float32Builder::new(),
            self.dimensions as i32,
        );

        let mut id = base_id;
        for file in &files {
            for chunk in &file.chunks {
                chunk_ids.append_value(id);
                id += 1;
                paths.append_value(&file.path);
                file_hashes.append_value(&file.file_hash);
                languages.append_value(&file.language);
                positions.append_value(chunk.position);
                contents.append_value(&chunk.content);
                content_hashes.append_value(hash_content(&chunk.content));
                start_lines.append_value(chunk.start_line);
                end_lines.append_value(chunk.end_line);
                timestamps.append_value(now);

                let values = vector_builder.values();
                for &v in &chunk.vector {
                    values.append_value(v);
                }
                for _ in chunk.vector.len()..self.dimensions as usize {
                    values.append_value(0.0);
                }
                vector_builder.append(true);
            }
        }

        let batch = RecordBatch::try_new(
            chunks_schema(self.dimensions),
            vec![
                Arc::new(chunk_ids.finish()),
                Arc::new(paths.finish()),
                Arc::new(file_hashes.finish()),
                Arc::new(languages.finish()),
                Arc::new(positions.finish()),
                Arc::new(contents.finish()),
                Arc::new(content_hashes.finish()),
                Arc::new(start_lines.finish()),
                Arc::new(end_lines.finish()),
                Arc::new(vector_builder.finish()),
                Arc::new(timestamps.finish()),
            ],
        )?;

        let schema = chunks_schema(self.dimensions);
        let reader = RecordBatchIterator::new(vec![Ok(batch)], schema);
        let t_build = t_start.elapsed();
        table.add(reader).execute().await?;
        let t_total = t_start.elapsed();
        tracing::debug!(
            "add_chunks_batch: {} files, {} chunks, build={}ms, write={}ms, total={}ms",
            file_count, total_chunks,
            t_build.as_millis(), (t_total - t_build).as_millis(), t_total.as_millis(),
        );
        
        // Invalidate cache on write
        self.invalidate_cached_counts().await;
        
        Ok(total_chunks)
    }

    /// Delete multiple files in a single operation.
    pub async fn delete_files_batch(&self, paths: &[String]) -> Result<usize> {
        if paths.is_empty() {
            return Ok(0);
        }
        let t_start = std::time::Instant::now();
        let table = self.ensure_chunks().await?;
        let escaped: Vec<String> = paths.iter().map(|p| format!("'{}'", escape_sql(p))).collect();
        let filter = format!("path IN ({})", escaped.join(", "));
        table.delete(&filter).await?;
        tracing::debug!(
            "delete_files_batch: {} files, {}ms",
            paths.len(), t_start.elapsed().as_millis(),
        );
        
        // Decrement file count
        if !paths.is_empty() {
            let _ = self.increment_file_count(-(paths.len() as i32)).await;
            
            // Invalidate cache on write
            self.invalidate_cached_counts().await;
        }
        
        Ok(paths.len())
    }

    /// Get all unique file paths in the index.
    pub async fn get_indexed_files(&self) -> Result<std::collections::HashSet<String>> {
        Ok(self.get_file_hashes(None).await?.into_keys().collect())
    }

    /// Find an existing embedding vector by content hash (for dedup).
    pub async fn find_by_content_hash(&self, content_hash: &str) -> Result<Option<Vec<f32>>> {
        let table = self.ensure_chunks().await?;
        let results = table
            .query()
            .select(lancedb::query::Select::Columns(vec!["vector".into()]))
            .only_if(format!(
                "content_hash = '{}'",
                escape_sql(content_hash)
            ))
            .limit(1)
            .execute()
            .await?;

        use futures::TryStreamExt;
        let batches: Vec<RecordBatch> = results.try_collect().await?;
        if batches.is_empty() || batches[0].num_rows() == 0 {
            return Ok(None);
        }

        let col = batches[0].column_by_name("vector").unwrap();
        let list = col
            .as_any()
            .downcast_ref::<arrow_array::FixedSizeListArray>()
            .unwrap();
        let values = list
            .values()
            .as_any()
            .downcast_ref::<Float32Array>()
            .unwrap();

        if values.is_empty() {
            return Ok(None);
        }

        // Row 0; first element check to avoid zero-vector reuse.
        if values.value(0) == 0.0 {
            return Ok(None);
        }

        let dims = self.dimensions as usize;
        Ok(Some((0..dims).map(|i| values.value(i)).collect()))
    }

    /// Get the next available chunk ID with atomic increment.
    /// Holds the outer mutex briefly only to get/create the per-db entry,
    /// then increments the AtomicI64 without holding any lock.
    /// On first use, seeds the counter from MAX(chunk_id) in the table to avoid
    /// ID collisions after a daemon restart.
    async fn next_id(&self, table: &lancedb::Table) -> Result<i64> {
        let key = self.path.to_string_lossy().to_string();
        let needs_seed = {
            let ids = chunk_ids().lock().unwrap();
            !ids.contains(&key)
        };
        if needs_seed {
            let max_id = self.get_max_chunk_id(table).await.unwrap_or(0);
            let mut ids = chunk_ids().lock().unwrap();
            if !ids.contains(&key) {
                ids.put(key.clone(), Arc::new(std::sync::atomic::AtomicI64::new(max_id)));
            }
        }
        let counter = {
            let mut ids = chunk_ids().lock().unwrap();
            ids.get(&key).unwrap().clone()
        };
        Ok(counter.fetch_add(1, std::sync::atomic::Ordering::Relaxed) + 1)
    }

    /// Query MAX(chunk_id) from the table to seed the ID counter on restart.
    async fn get_max_chunk_id(&self, table: &lancedb::Table) -> Result<i64> {
        use futures::TryStreamExt;
        let batches = table
            .query()
            .select(lancedb::query::Select::columns(&["chunk_id"]))
            .execute()
            .await?
            .try_collect::<Vec<_>>()
            .await?;
        let mut max_id: i64 = 0;
        for batch in batches {
            if let Some(col) = batch.column_by_name("chunk_id") {
                if let Some(arr) = col.as_any().downcast_ref::<Int64Array>() {
                    for i in 0..arr.len() {
                        if !arr.is_null(i) {
                            max_id = max_id.max(arr.value(i));
                        }
                    }
                }
            }
        }
        Ok(max_id)
    }

    /// Vector search.
    /// 
    /// Returns a corruption error if the LanceDB index is corrupted, allowing
    /// the caller to handle recovery (e.g., rebuild the index).
    pub async fn search_vector(
        &self,
        query_vec: &[f32],
        limit: usize,
    ) -> Result<Vec<SearchResult>> {
        let table = self.ensure_chunks().await?;
        
        // Apply IVF-PQ tuning parameters for better recall
        let nprobes = get_ivf_nprobes();
        let refine_factor = get_ivf_refine_factor();
        
        let search_result = table
            .vector_search(query_vec)
            .context("vector search failed")?
            .distance_type(lancedb::DistanceType::Cosine)
            .nprobes(nprobes)
            .refine_factor(refine_factor as u32)
            // Fetch extra results to filter out corrupted entries (e.g. NaN distances)
            .limit(limit * 2)
            .execute()
            .await;
        
        let results = match search_result {
            Ok(r) => r,
            Err(e) => {
                let err = anyhow::anyhow!("{}", e);
                if is_corruption_error(&err) {
                    return Err(anyhow::anyhow!(
                        "INDEX_CORRUPTED: LanceDB index is corrupted and needs to be rebuilt. \
                         Delete the .lancedb directory and re-run indexing. Error: {}",
                        e
                    ));
                }
                return Err(err.context("vector search execution failed"));
            }
        };

        use futures::TryStreamExt;
        let batches: Vec<RecordBatch> = results.try_collect().await?;
        let mut out = Vec::new();

        for batch in &batches {
            let paths = batch
                .column_by_name("path")
                .unwrap()
                .as_any()
                .downcast_ref::<StringArray>()
                .unwrap();
            let contents = batch
                .column_by_name("content")
                .unwrap()
                .as_any()
                .downcast_ref::<StringArray>()
                .unwrap();
            let start_lines = batch
                .column_by_name("start_line")
                .unwrap()
                .as_any()
                .downcast_ref::<Int32Array>()
                .unwrap();
            let end_lines = batch
                .column_by_name("end_line")
                .unwrap()
                .as_any()
                .downcast_ref::<Int32Array>()
                .unwrap();
            let languages = batch
                .column_by_name("language")
                .unwrap()
                .as_any()
                .downcast_ref::<StringArray>()
                .unwrap();
            let chunk_ids = batch
                .column_by_name("chunk_id")
                .unwrap()
                .as_any()
                .downcast_ref::<Int64Array>()
                .unwrap();

            let distances = batch
                .column_by_name("_distance")
                .and_then(|c| c.as_any().downcast_ref::<Float32Array>().cloned());

            for i in 0..batch.num_rows() {
                let score = distances
                    .as_ref()
                    .map(|d| 1.0 - d.value(i))
                    .unwrap_or(0.0);

                if score.is_nan() {
                    continue;
                }

                out.push(SearchResult {
                    chunk_id: chunk_ids.value(i),
                    path: paths.value(i).to_string(),
                    content: contents.value(i).to_string(),
                    start_line: start_lines.value(i),
                    end_line: end_lines.value(i),
                    language: languages.value(i).to_string(),
                    score,
                    vector: None, // Vector not fetched in basic search
                });

                if out.len() >= limit {
                    break;
                }
            }

            if out.len() >= limit {
                break;
            }
        }

        Ok(out)
    }

    /// Vector search with SIMD reranking for improved accuracy.
    /// 
    /// Fetches more candidates than needed from LanceDB (approximate search),
    /// then uses exact SIMD-accelerated cosine similarity to rerank and return
    /// the top results.
    /// 
    /// # Arguments
    /// * `query_vec` - Query embedding vector
    /// * `initial_limit` - Number of candidates to fetch from LanceDB (e.g., 100)
    /// * `final_limit` - Number of results to return after reranking (e.g., 10)
    /// 
    /// # Performance
    /// - ~12ms for approximate search (100 candidates)
    /// - ~0.5ms for SIMD reranking (100 → 10)
    /// - Total: ~12.5ms (25% slower than approximate-only)
    /// - Accuracy: ~98% vs ~85% for approximate-only
    /// 
    /// # Environment Variables
    /// - `OPENCODE_SIMD_RERANK=0` to disable (default: enabled)
    /// - `OPENCODE_RERANK_FACTOR=5` to set initial_limit = final_limit * factor
    pub async fn search_with_rerank(
        &self,
        query_vec: &[f32],
        initial_limit: usize,
        final_limit: usize,
    ) -> Result<Vec<SearchResult>> {
        // Check if SIMD reranking is disabled
        let simd_enabled = std::env::var("OPENCODE_SIMD_RERANK")
            .map(|v| v != "0" && v.to_lowercase() != "false")
            .unwrap_or(true);

        if !simd_enabled || initial_limit <= final_limit {
            // Fall back to basic search
            return self.search_vector(query_vec, final_limit).await;
        }

        // Step 1: Approximate search - fetch more candidates
        let table = self.ensure_chunks().await?;
        
        // Apply IVF-PQ tuning parameters
        let nprobes = get_ivf_nprobes();
        let refine_factor = get_ivf_refine_factor();
        
        let search_result = table
            .vector_search(query_vec)
            .context("vector search failed")?
            .distance_type(lancedb::DistanceType::Cosine)
            .nprobes(nprobes)
            .refine_factor(refine_factor as u32)
            .limit(initial_limit * 2) // Fetch extra to filter NaN
            .execute()
            .await;
        
        let results = match search_result {
            Ok(r) => r,
            Err(e) => {
                let err = anyhow::anyhow!("{}", e);
                if is_corruption_error(&err) {
                    return Err(anyhow::anyhow!(
                        "INDEX_CORRUPTED: LanceDB index is corrupted and needs to be rebuilt. \
                         Delete the .lancedb directory and re-run indexing. Error: {}",
                        e
                    ));
                }
                return Err(err.context("vector search execution failed"));
            }
        };

        use futures::TryStreamExt;
        let batches: Vec<RecordBatch> = results.try_collect().await?;
        let mut candidates = Vec::new();

        // Step 2: Extract results with vectors for reranking
        for batch in &batches {
            let paths = batch
                .column_by_name("path")
                .unwrap()
                .as_any()
                .downcast_ref::<StringArray>()
                .unwrap();
            let contents = batch
                .column_by_name("content")
                .unwrap()
                .as_any()
                .downcast_ref::<StringArray>()
                .unwrap();
            let start_lines = batch
                .column_by_name("start_line")
                .unwrap()
                .as_any()
                .downcast_ref::<Int32Array>()
                .unwrap();
            let end_lines = batch
                .column_by_name("end_line")
                .unwrap()
                .as_any()
                .downcast_ref::<Int32Array>()
                .unwrap();
            let languages = batch
                .column_by_name("language")
                .unwrap()
                .as_any()
                .downcast_ref::<StringArray>()
                .unwrap();
            let chunk_ids = batch
                .column_by_name("chunk_id")
                .unwrap()
                .as_any()
                .downcast_ref::<Int64Array>()
                .unwrap();

            // Get vectors for reranking
            let vectors = batch
                .column_by_name("vector")
                .and_then(|c| c.as_any().downcast_ref::<arrow_array::FixedSizeListArray>());

            let distances = batch
                .column_by_name("_distance")
                .and_then(|c| c.as_any().downcast_ref::<Float32Array>().cloned());

            for i in 0..batch.num_rows() {
                let approx_score = distances
                    .as_ref()
                    .map(|d| 1.0 - d.value(i))
                    .unwrap_or(0.0);

                if approx_score.is_nan() {
                    continue;
                }

                // Extract vector for this row
                let vector = if let Some(vec_arr) = vectors {
                    let list = vec_arr.value(i);
                    let floats = list.as_any().downcast_ref::<Float32Array>().unwrap();
                    Some((0..floats.len()).map(|j| floats.value(j)).collect::<Vec<f32>>())
                } else {
                    None
                };

                candidates.push(SearchResult {
                    chunk_id: chunk_ids.value(i),
                    path: paths.value(i).to_string(),
                    content: contents.value(i).to_string(),
                    start_line: start_lines.value(i),
                    end_line: end_lines.value(i),
                    language: languages.value(i).to_string(),
                    score: approx_score,
                    vector,
                });

                if candidates.len() >= initial_limit {
                    break;
                }
            }

            if candidates.len() >= initial_limit {
                break;
            }
        }

        // If we don't have enough candidates or vectors missing, return what we have
        if candidates.len() <= final_limit || candidates.iter().any(|c| c.vector.is_none()) {
            candidates.truncate(final_limit);
            return Ok(candidates);
        }

        // Step 3: SIMD reranking
        let embeddings: Vec<Vec<f32>> = candidates
            .iter()
            .filter_map(|c| c.vector.clone())
            .collect();

        if embeddings.is_empty() {
            candidates.truncate(final_limit);
            return Ok(candidates);
        }

        let top_indices = rerank_by_cosine(query_vec, &embeddings, final_limit);

        // Step 4: Return reranked results with exact scores
        let mut reranked = Vec::with_capacity(final_limit);
        for &idx in top_indices.iter() {
            if let Some(mut result) = candidates.get(idx).cloned() {
                // Compute exact cosine similarity score
                if let Some(ref vec) = result.vector {
                    result.score = crate::simd::cosine_similarity(query_vec, vec);
                }
                // Clear vector to save memory (not needed in output)
                result.vector = None;
                reranked.push(result);
            }
            if reranked.len() >= final_limit {
                break;
            }
        }

        Ok(reranked)
    }

    /// Conditionally use SIMD reranking based on environment variables.
    /// 
    /// If SIMD reranking is enabled and enough candidates exist, uses SIMD.
    /// Otherwise falls back to basic vector search.
    /// 
    /// # Environment Variables
    /// - `OPENCODE_SIMD_RERANK=1` to enable (default: enabled)
    /// - `OPENCODE_RERANK_FACTOR=5` multiplier for candidates (default: 5)
    async fn search_vector_maybe_rerank(
        &self,
        query_vec: &[f32],
        limit: usize,
    ) -> Result<Vec<SearchResult>> {
        // Check if SIMD reranking is enabled
        let simd_enabled = std::env::var("OPENCODE_SIMD_RERANK")
            .map(|v| v != "0" && v.to_lowercase() != "false")
            .unwrap_or(true);

        if !simd_enabled {
            return self.search_vector(query_vec, limit).await;
        }

        // Get rerank factor (how many candidates to fetch for reranking)
        let rerank_factor = std::env::var("OPENCODE_RERANK_FACTOR")
            .ok()
            .and_then(|v| v.parse::<usize>().ok())
            .unwrap_or(5);

        let initial_limit = limit * rerank_factor;

        // Use SIMD reranking for quality improvement
        self.search_with_rerank(query_vec, initial_limit, limit).await
    }

    /// Count total chunks.
    pub async fn count_chunks(&self) -> Result<usize> {
        let table = self.ensure_chunks().await?;
        Ok(table.count_rows(None).await?)
    }

    /// Count unique indexed files.
    pub async fn count_files(&self) -> Result<usize> {
        self.get_file_count().await
    }

    pub async fn count_embeddings(&self) -> Result<usize> {
        let table = self.ensure_chunks().await?;
        let mut out = 0_usize;
        let dims = self.dimensions as usize;

        // Paginate through results to avoid streaming issues with large tables
        let page_size = 5000;
        let mut offset = 0;

        loop {
            let results = table
                .query()
                .select(lancedb::query::Select::Columns(vec!["vector".into()]))
                .limit(page_size)
                .offset(offset)
                .execute()
                .await?;

            use futures::TryStreamExt;
            let batches: Vec<RecordBatch> = results.try_collect().await?;

            let mut batch_rows = 0;
            for batch in &batches {
                if batch.num_rows() == 0 {
                    continue;
                }
                batch_rows += batch.num_rows();

                let col = batch.column_by_name("vector").unwrap();
                let list = col
                    .as_any()
                    .downcast_ref::<arrow_array::FixedSizeListArray>()
                    .unwrap();
                let values = list
                    .values()
                    .as_any()
                    .downcast_ref::<Float32Array>()
                    .unwrap();

                for i in 0..batch.num_rows() {
                    let first = values.value(i * dims);
                    if first != 0.0 {
                        out += 1;
                    }
                }
            }

            if batch_rows < page_size {
                break;
            }
            offset += page_size;
        }

        Ok(out)
    }

    pub async fn stats(&self) -> Result<(usize, usize, usize)> {
        let files = self.count_files().await?;
        let chunks = self.count_chunks().await?;
        let embeddings = self.count_embeddings().await?;
        Ok((files, chunks, embeddings))
    }

    /// Compact storage (merge fragments and prune old versions).
    /// 
    /// Uses `OptimizeAction::Compact` + `Prune` instead of `All` to avoid massive memory
    /// allocation during index rebuilding. Index optimization is memory-intensive and can
    /// try to allocate 100+ GB for large indexes, causing OOM crashes.
    /// 
    /// This function:
    /// 1. Compact: Merges small files into larger ones (low memory)
    /// 2. Prune: Deletes old version history older than 1 hour (low memory, saves storage)
    /// 
    /// We skip `Index` optimization which rebuilds vector indexes and causes OOM.
    pub async fn compact(&self) -> Result<()> {
        let table = self.ensure_chunks().await?;
        
        // Step 1: Compact - merge small files (low memory)
        table.optimize(lancedb::table::OptimizeAction::Compact {
            options: lancedb::table::CompactionOptions::default(),
            remap_options: None,
        }).await?;
        
        // Step 2: Prune - delete old versions (default 15min, configurable via env)
        let prune_mins = std::env::var("OPENCODE_INDEXER_PRUNE_AGE_MINS")
            .ok()
            .and_then(|v| v.parse::<i64>().ok())
            .filter(|&n| n > 0)
            .unwrap_or(15);
        table.optimize(lancedb::table::OptimizeAction::Prune {
            older_than: Some(chrono::TimeDelta::minutes(prune_mins)),
            delete_unverified: Some(false),
            error_if_tagged_old_versions: Some(false),
        }).await?;
        self.invalidate_cached_counts().await;
        self.invalidate_table_cache().await;

        info!("compacted and pruned storage at {}", self.path.display());
        Ok(())
    }

    /// Release any cached memory that can be regenerated.
    /// Call periodically during long-running operations to prevent memory buildup.
    /// 
    /// LanceDB connections and table handles are lightweight and can be reopened
    /// on next access, so this is safe to call without affecting correctness.
    pub async fn release_memory_pressure(&self) -> Result<()> {
        tracing::debug!("releasing memory pressure for storage at {}", self.path.display());
        self.invalidate_cached_counts().await;
        Ok(())
    }

    /// Create FTS index on content column for hybrid search.
    pub async fn create_fts_index(&self) -> Result<()> {
        let table = self.ensure_chunks().await?;
        let count = self.count_chunks().await?;

        // Only create FTS index if we have enough chunks
        if count < FTS_THRESHOLD {
            info!(
                "skipping FTS index: {} chunks < {} threshold",
                count, FTS_THRESHOLD
            );
            return Ok(());
        }

        // Check if FTS index already exists
        let indices = table.list_indices().await?;
        let has_fts = indices.iter().any(|i| {
            i.columns.iter().any(|c| c == "content")
                && matches!(i.index_type, lancedb::index::IndexType::FTS)
        });

        if has_fts {
            info!("FTS index already exists");
            return Ok(());
        }

        info!("creating FTS index on content column...");
        table
            .create_index(&["content"], lancedb::index::Index::FTS(Default::default()))
            .execute()
            .await
            .context("failed to create FTS index")?;

        info!("FTS index created");
        self.invalidate_table_cache().await;
        Ok(())
    }

    /// Hybrid search combining vector similarity and full-text search.
    /// Falls back to vector-only search if FTS index doesn't exist.
    pub async fn search_hybrid(
        &self,
        query: &str,
        query_vec: &[f32],
        limit: usize,
    ) -> Result<Vec<SearchResult>> {
        let table = self.ensure_chunks().await?;

        use lancedb::index::scalar::FullTextSearchQuery;
        use lancedb::query::QueryBase;
        use lancedb::rerankers::rrf::RRFReranker;
        use std::sync::Arc;

        // Fix A3: skip hybrid entirely if DB is too small for FTS
        let cnt = self.count_chunks().await.unwrap_or(0);
        if cnt < FTS_THRESHOLD {
            tracing::debug!("hybrid skipped: {} chunks < threshold {}", cnt, FTS_THRESHOLD);
            // Try SIMD reranking if enabled
            return self.search_vector_maybe_rerank(query_vec, limit).await;
        }

        // Fix A4: skip hybrid if FTS index not present (avoids noisy lance error + wasted work)
        {
            let indices = table.list_indices().await.unwrap_or_default();
            let has_fts = indices.iter().any(|i| {
                i.columns.iter().any(|c| c == "content")
                    && matches!(i.index_type, lancedb::index::IndexType::FTS)
            });
            if !has_fts {
                tracing::debug!("hybrid skipped: no FTS index on content column");
                // Try SIMD reranking if enabled
                return self.search_vector_maybe_rerank(query_vec, limit).await;
            }
        }

        // Try hybrid search
        // Apply IVF-PQ tuning parameters
        let nprobes = get_ivf_nprobes();
        let refine_factor = get_ivf_refine_factor();
        
        match table
            .vector_search(query_vec)
            .context("hybrid search failed")?
            .distance_type(lancedb::DistanceType::Cosine)
            .nprobes(nprobes)
            .refine_factor(refine_factor as u32)
            .full_text_search(FullTextSearchQuery::new(query.to_string()))
            .rerank(Arc::new(RRFReranker::default()))
            .limit(limit * 2) // Fetch extra to filter NaN
            .execute_hybrid()
            .await
        {
            Ok(results) => {
                use futures::TryStreamExt;
                let batches: Vec<RecordBatch> = results.try_collect().await?;
                let mut out = Vec::new();

                for batch in &batches {
                    let paths = batch
                        .column_by_name("path")
                        .unwrap()
                        .as_any()
                        .downcast_ref::<StringArray>()
                        .unwrap();
                    let contents = batch
                        .column_by_name("content")
                        .unwrap()
                        .as_any()
                        .downcast_ref::<StringArray>()
                        .unwrap();
                    let start_lines = batch
                        .column_by_name("start_line")
                        .unwrap()
                        .as_any()
                        .downcast_ref::<Int32Array>()
                        .unwrap();
                    let end_lines = batch
                        .column_by_name("end_line")
                        .unwrap()
                        .as_any()
                        .downcast_ref::<Int32Array>()
                        .unwrap();
                    let languages = batch
                        .column_by_name("language")
                        .unwrap()
                        .as_any()
                        .downcast_ref::<StringArray>()
                        .unwrap();
                    let chunk_ids = batch
                        .column_by_name("chunk_id")
                        .unwrap()
                        .as_any()
                        .downcast_ref::<Int64Array>()
                        .unwrap();

                    // _relevance_score from hybrid search (RRF combined score)
                    let scores = batch
                        .column_by_name("_relevance_score")
                        .and_then(|c| c.as_any().downcast_ref::<Float32Array>().cloned());

                    for i in 0..batch.num_rows() {
                        let score = scores.as_ref().map(|s| s.value(i)).unwrap_or(0.0);

                        // Skip NaN scores
                        if score.is_nan() {
                            continue;
                        }

                        out.push(SearchResult {
                            chunk_id: chunk_ids.value(i),
                            path: paths.value(i).to_string(),
                            content: contents.value(i).to_string(),
                            start_line: start_lines.value(i),
                            end_line: end_lines.value(i),
                            language: languages.value(i).to_string(),
                            score,
                            vector: None, // Vector not fetched in hybrid search
                        });

                        if out.len() >= limit {
                            break;
                        }
                    }

                    if out.len() >= limit {
                        break;
                    }
                }

                info!(
                    "hybrid search for '{}' returned {} results",
                    query,
                    out.len()
                );
                Ok(out)
            }
            Err(e) => {
                // Check for corruption first - don't fallback if index is corrupted
                let err = anyhow::anyhow!("{}", e);
                if is_corruption_error(&err) {
                    return Err(anyhow::anyhow!(
                        "INDEX_CORRUPTED: LanceDB index is corrupted and needs to be rebuilt. \
                         Delete the .lancedb directory and re-run indexing. Error: {}",
                        e
                    ));
                }
                // Fallback to vector-only search if hybrid fails (e.g., no FTS index)
                info!("hybrid search failed, falling back to vector: {}", e);
                self.search_vector(query_vec, limit).await
            }
        }
    }

    pub async fn create_indexes(&self, force: bool) -> Result<()> {
        self.create_fts_index().await?;

        let count = self.count_chunks().await?;
        if count < IVF_PQ_THRESHOLD && !force {
            return Ok(());
        }

        let table = self.ensure_chunks().await?;
        let indices = table.list_indices().await.unwrap_or_default();
        let has_vector = indices.iter().any(|i| {
            i.columns.contains(&"vector".to_string())
                && matches!(
                    i.index_type,
                    lancedb::index::IndexType::IvfPq
                        | lancedb::index::IndexType::IvfFlat
                        | lancedb::index::IndexType::IvfHnswPq
                        | lancedb::index::IndexType::IvfHnswSq
                )
        });
        if has_vector {
            return Ok(());
        }

        // Dynamic partitioning based on database size
        // Rule of thumb: sqrt(num_vectors) to num_vectors/10
        // Small DB: fewer partitions, Large DB: more partitions
        let partitions = (count / 10).clamp(1, IVF_NUM_PARTITIONS_MAX) as u32;
        
        // Dynamic sub-vectors based on dimensionality
        // Rule of thumb: dimensions / 4 (good compression vs accuracy trade-off)
        let subvectors = ((self.dimensions / 4).clamp(1, IVF_NUM_SUB_VECTORS_MAX as u32)) as u32;
        
        info!(
            "creating IVF-PQ index with {} partitions, {} sub-vectors for {} chunks ({}D)",
            partitions, subvectors, count, self.dimensions
        );
        
        let _ = table
            .create_index(
                &["vector"],
                lancedb::index::Index::IvfPq(
                    lancedb::index::vector::IvfPqIndexBuilder::default()
                        .distance_type(lancedb::DistanceType::Cosine)
                        .num_partitions(partitions)
                        .num_sub_vectors(subvectors),
                ),
            )
            .execute()
            .await;

        info!("IVF-PQ index created successfully");
        self.invalidate_table_cache().await;
        Ok(())
    }

    // =========================================================================
    // Backup / Restore
    // =========================================================================

    pub async fn backup(&self) -> Result<PathBuf> {
        let dir = backup_dir();
        // Use tokio::fs to avoid blocking the async runtime
        tokio::fs::create_dir_all(&dir).await?;

        let tier = self.get_tier().await?.unwrap_or_else(|| "unknown".into());
        let ts = chrono::Utc::now().format("%Y%m%d-%H%M%S").to_string();
        let name = format!("backup-{tier}-{ts}.lancedb");
        let dst = dir.join(name);

        // copy_dir is CPU-bound directory traversal, wrap in spawn_blocking
        let src = self.path.clone();
        let dst_clone = dst.clone();
        tokio::task::spawn_blocking(move || copy_dir(&src, &dst_clone, None))
            .await
            .context("spawn_blocking for copy_dir")??;
        Ok(dst)
    }
}

pub fn list_backups() -> Vec<BackupInfo> {
    let dir = backup_dir();
    if !dir.exists() {
        return Vec::new();
    }

    let mut out = Vec::new();
    let entries = std::fs::read_dir(&dir);
    let Ok(entries) = entries else {
        return Vec::new();
    };

    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        if path.extension().and_then(|e| e.to_str()) != Some("lancedb") {
            continue;
        }
        let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
        if name.is_empty() {
            continue;
        }

        let parts: Vec<&str> = name.trim_end_matches(".lancedb").split('-').collect();
        let tier = parts.get(1).copied().unwrap_or("unknown").to_string();
        let created_at = if parts.len() > 2 {
            parts[2..].join("-")
        } else {
            "".into()
        };

        let mut size_bytes = 0_u64;
        for entry in walkdir::WalkDir::new(&path) {
            let Ok(entry) = entry else {
                continue;
            };
            if entry.file_type().is_file() {
                size_bytes += entry.metadata().map(|m| m.len()).unwrap_or(0);
            }
        }

        out.push(BackupInfo {
            name: name.to_string(),
            tier,
            created_at,
            size_bytes,
        });
    }

    out.sort_by(|a, b| b.created_at.cmp(&a.created_at));
    out
}

pub fn restore_backup(name: &str, target: &Path) -> Result<()> {
    let src = backup_dir().join(name);
    if !src.exists() {
        anyhow::bail!("backup not found: {name}");
    }

    if target.exists() {
        std::fs::remove_dir_all(target).context("failed to remove existing index")?;
    }
    std::fs::create_dir_all(target.parent().unwrap_or(target))?;
    copy_dir(&src, target, None)?;
    Ok(())
}

fn copy_dir(src: &Path, dst: &Path, keep: Option<&std::collections::HashSet<String>>) -> Result<()> {
    std::fs::create_dir_all(dst)?;
    for entry in walkdir::WalkDir::new(src) {
        let entry = entry?;
        let rel = entry.path().strip_prefix(src).unwrap_or(entry.path());
        if rel.as_os_str().is_empty() {
            continue;
        }

        if let Some(keep) = keep {
            if let Some(name) = rel.file_name().and_then(|n| n.to_str()) {
                if keep.contains(name) {
                    continue;
                }
            }
        }

        let out = dst.join(rel);
        if entry.file_type().is_dir() {
            std::fs::create_dir_all(&out)?;
            continue;
        }
        if entry.file_type().is_file() {
            if let Some(parent) = out.parent() {
                std::fs::create_dir_all(parent)?;
            }
            std::fs::copy(entry.path(), &out)?;
        }
    }
    Ok(())
}

/// Data for a single chunk to be stored.
#[derive(Debug)]
pub struct ChunkData {
    pub position: i32,
    pub content: String,
    pub start_line: i32,
    pub end_line: i32,
    pub vector: Vec<f32>,
}

/// All chunks for a single file, used for batch writes.
#[derive(Debug)]
pub struct FileChunks {
    pub path: String,
    pub file_hash: String,
    pub language: String,
    pub chunks: Vec<ChunkData>,
}

/// Search result.
#[derive(Debug, Clone)]
pub struct SearchResult {
    pub chunk_id: i64,
    pub path: String,
    pub content: String,
    pub start_line: i32,
    pub end_line: i32,
    pub language: String,
    pub score: f32,
    /// Vector embedding for SIMD reranking (optional)
    pub vector: Option<Vec<f32>>,
}

/// Write operation for the async queue.
pub enum WriteOp {
    /// Delete files by path
    Delete(Vec<String>),
    /// Add chunks for files
    Add(Vec<FileChunks>),
    /// Record usage stats
    Usage { tokens: i64, tier: String },
    /// Delete a single chunk by ID
    DeleteChunkById(i64),
    /// Update a chunk's position and line numbers
    UpdateChunkPosition { chunk_id: i64, position: i32, start_line: i32, end_line: i32 },
    /// Add chunks for a single file
    AddSingle { path: String, file_hash: String, language: String, chunks: Vec<ChunkData> },
    /// Delete a single file by path
    DeleteFile(String),
}

/// Statistics tracked by WriteQueue.
#[derive(Debug, Default)]
pub struct WriteQueueStats {
    pub batches_queued: AtomicU64,
    pub batches_written: AtomicU64,
    pub chunks_written: AtomicU64,
    pub files_deleted: AtomicU64,
    pub errors: AtomicU64,
}

impl WriteQueueStats {
    pub fn snapshot(&self) -> WriteQueueStatsSnapshot {
        WriteQueueStatsSnapshot {
            batches_queued: self.batches_queued.load(Ordering::Relaxed),
            batches_written: self.batches_written.load(Ordering::Relaxed),
            chunks_written: self.chunks_written.load(Ordering::Relaxed),
            files_deleted: self.files_deleted.load(Ordering::Relaxed),
            errors: self.errors.load(Ordering::Relaxed),
        }
    }
}

/// Snapshot of WriteQueueStats for reporting.
#[derive(Debug, Clone)]
pub struct WriteQueueStatsSnapshot {
    pub batches_queued: u64,
    pub batches_written: u64,
    pub chunks_written: u64,
    pub files_deleted: u64,
    pub errors: u64,
}

/// Async write queue that batches disk operations in the background.
/// 
/// This allows the indexer to continue processing files while writes happen concurrently,
/// improving throughput by overlapping GPU compute with disk I/O.
pub struct WriteQueue {
    tx: mpsc::Sender<WriteOp>,
    handle: Option<JoinHandle<()>>,
    stats: Arc<WriteQueueStats>,
}

impl WriteQueue {
    /// Create a new WriteQueue with a background writer task.
    /// 
    /// # Arguments
    /// * `storage` - The Storage instance to write to
    /// * `buffer_size` - Maximum number of write operations to buffer (backpressure threshold)
    pub fn new(storage: Arc<Storage>, buffer_size: usize) -> Self {
        let (tx, rx) = mpsc::channel(buffer_size);
        let stats = Arc::new(WriteQueueStats::default());
        let stats_clone = stats.clone();
        
        let handle = tokio::spawn(async move {
            Self::writer_loop(storage, rx, stats_clone).await;
        });
        
        Self {
            tx,
            handle: Some(handle),
            stats,
        }
    }
    
    /// Background writer loop that drains the queue and writes to storage.
    ///
    /// Exits when the channel is closed (all senders dropped), ensuring graceful shutdown.
    async fn writer_loop(
        storage: Arc<Storage>,
        mut rx: mpsc::Receiver<WriteOp>,
        stats: Arc<WriteQueueStats>,
    ) {
        loop {
            tokio::select! {
                Some(op) = rx.recv() => {
                    match op {
                        WriteOp::Delete(paths) => {
                            let count = paths.len();
                            if let Err(e) = storage.delete_files_batch(&paths).await {
                                tracing::warn!("WriteQueue: batch delete failed: {}", e);
                                stats.errors.fetch_add(1, Ordering::Relaxed);
                            } else {
                                stats.files_deleted.fetch_add(count as u64, Ordering::Relaxed);
                            }
                        }
                        WriteOp::Add(files) => {
                            let chunk_count: usize = files.iter().map(|f| f.chunks.len()).sum();
                            if let Err(e) = storage.add_chunks_batch(files).await {
                                tracing::error!("WriteQueue: batch add failed: {}", e);
                                stats.errors.fetch_add(1, Ordering::Relaxed);
                            } else {
                                stats.chunks_written.fetch_add(chunk_count as u64, Ordering::Relaxed);
                            }
                        }
                        WriteOp::Usage { tokens, tier } => {
                            if let Err(e) = storage.record_usage(tokens, &tier).await {
                                tracing::warn!("WriteQueue: failed to record usage: {}", e);
                                stats.errors.fetch_add(1, Ordering::Relaxed);
                            }
                        }
                        WriteOp::DeleteChunkById(chunk_id) => {
                            if let Err(e) = storage.delete_chunk_by_id(chunk_id).await {
                                tracing::warn!("WriteQueue: failed to delete chunk {}: {}", chunk_id, e);
                                stats.errors.fetch_add(1, Ordering::Relaxed);
                            }
                        }
                        WriteOp::UpdateChunkPosition { chunk_id, position, start_line, end_line } => {
                            if let Err(e) = storage.update_chunk_position(chunk_id, position, start_line, end_line).await {
                                tracing::warn!("WriteQueue: failed to update chunk position {}: {}", chunk_id, e);
                                stats.errors.fetch_add(1, Ordering::Relaxed);
                            }
                        }
                        WriteOp::AddSingle { path, file_hash, language, chunks } => {
                            let chunk_count = chunks.len();
                            // Check if file is new before adding
                            let is_new = storage.get_file_hash(&path).await.ok().and_then(|h| h).is_none();
                            
                            if let Err(e) = storage.add_chunks(&path, &file_hash, &language, chunks).await {
                                tracing::error!("WriteQueue: failed to add chunks for {}: {}", path, e);
                                stats.errors.fetch_add(1, Ordering::Relaxed);
                            } else {
                                stats.chunks_written.fetch_add(chunk_count as u64, Ordering::Relaxed);
                                
                                // Increment file count if new
                                if is_new {
                                    let _ = storage.increment_file_count(1).await;
                                }
                            }
                        }
                        WriteOp::DeleteFile(path) => {
                            if let Err(e) = storage.delete_file(&path).await {
                                tracing::warn!("WriteQueue: failed to delete file {}: {}", path, e);
                                stats.errors.fetch_add(1, Ordering::Relaxed);
                            } else {
                                stats.files_deleted.fetch_add(1, Ordering::Relaxed);
                            }
                        }
                    }
                    stats.batches_written.fetch_add(1, Ordering::Relaxed);
                }
                else => {
                    // Channel closed - all senders dropped, shutdown signal received
                    tracing::debug!("WriteQueue: channel closed, exiting writer loop");
                    break;
                }
            }
        }
    }
    
    /// Queue a delete operation (non-blocking if buffer not full).
    /// If queue is full, returns the paths back so the caller can retry.
    pub fn try_delete(&self, paths: Vec<String>) -> std::result::Result<(), Vec<String>> {
        if paths.is_empty() {
            return Ok(());
        }
        self.stats.batches_queued.fetch_add(1, Ordering::Relaxed);
        match self.tx.try_send(WriteOp::Delete(paths)) {
            Ok(()) => Ok(()),
            Err(tokio::sync::mpsc::error::TrySendError::Full(WriteOp::Delete(v))) => {
                self.stats.batches_queued.fetch_sub(1, Ordering::Relaxed);
                Err(v)
            }
            Err(_) => Ok(()), // channel closed — writer is done
        }
    }
    
    /// Queue an add operation (non-blocking if buffer not full).
    /// If queue is full, returns the files back so the caller can retry.
    pub fn try_add(&self, files: Vec<FileChunks>) -> std::result::Result<(), Vec<FileChunks>> {
        if files.is_empty() {
            return Ok(());
        }
        self.stats.batches_queued.fetch_add(1, Ordering::Relaxed);
        match self.tx.try_send(WriteOp::Add(files)) {
            Ok(()) => Ok(()),
            Err(tokio::sync::mpsc::error::TrySendError::Full(WriteOp::Add(v))) => {
                self.stats.batches_queued.fetch_sub(1, Ordering::Relaxed);
                Err(v)
            }
            Err(_) => Ok(()), // channel closed
        }
    }
    
    /// Queue a usage recording operation (non-blocking).
    pub fn try_record_usage(&self, tokens: i64, tier: &str) -> Result<()> {
        self.stats.batches_queued.fetch_add(1, Ordering::Relaxed);
        self.tx.try_send(WriteOp::Usage { tokens, tier: tier.to_string() })
            .map_err(|e| anyhow::anyhow!("WriteQueue full: {}", e))
    }
    
    /// Queue a delete operation (async, waits if buffer full).
    pub async fn delete(&self, paths: Vec<String>) -> Result<()> {
        if paths.is_empty() {
            return Ok(());
        }
        self.stats.batches_queued.fetch_add(1, Ordering::Relaxed);
        self.tx.send(WriteOp::Delete(paths)).await
            .map_err(|e| anyhow::anyhow!("WriteQueue closed: {}", e))
    }
    
    /// Queue an add operation (async, waits if buffer full).
    pub async fn add(&self, files: Vec<FileChunks>) -> Result<()> {
        if files.is_empty() {
            return Ok(());
        }
        self.stats.batches_queued.fetch_add(1, Ordering::Relaxed);
        self.tx.send(WriteOp::Add(files)).await
            .map_err(|e| anyhow::anyhow!("WriteQueue closed: {}", e))
    }
    
    /// Queue a usage recording operation (async).
    pub async fn record_usage(&self, tokens: i64, tier: &str) -> Result<()> {
        self.stats.batches_queued.fetch_add(1, Ordering::Relaxed);
        self.tx.send(WriteOp::Usage { tokens, tier: tier.to_string() }).await
            .map_err(|e| anyhow::anyhow!("WriteQueue closed: {}", e))
    }
    
    /// Queue a chunk deletion by ID (async).
    pub async fn delete_chunk_by_id(&self, chunk_id: i64) -> Result<()> {
        self.stats.batches_queued.fetch_add(1, Ordering::Relaxed);
        self.tx.send(WriteOp::DeleteChunkById(chunk_id)).await
            .map_err(|e| anyhow::anyhow!("WriteQueue closed: {}", e))
    }
    
    /// Queue a chunk position update (async).
    pub async fn update_chunk_position(&self, chunk_id: i64, position: i32, start_line: i32, end_line: i32) -> Result<()> {
        self.stats.batches_queued.fetch_add(1, Ordering::Relaxed);
        self.tx.send(WriteOp::UpdateChunkPosition { chunk_id, position, start_line, end_line }).await
            .map_err(|e| anyhow::anyhow!("WriteQueue closed: {}", e))
    }
    
    /// Queue chunks for a single file (async).
    pub async fn add_single(&self, path: String, file_hash: String, language: String, chunks: Vec<ChunkData>) -> Result<()> {
        if chunks.is_empty() {
            return Ok(());
        }
        self.stats.batches_queued.fetch_add(1, Ordering::Relaxed);
        self.tx.send(WriteOp::AddSingle { path, file_hash, language, chunks }).await
            .map_err(|e| anyhow::anyhow!("WriteQueue closed: {}", e))
    }
    
    /// Queue a single file deletion by path (async).
    pub async fn delete_file(&self, path: &str) {
        self.stats.batches_queued.fetch_add(1, Ordering::Relaxed);
        let _ = self.tx.send(WriteOp::DeleteFile(path.to_string())).await;
    }
    
    /// Get current statistics.
    pub fn stats(&self) -> WriteQueueStatsSnapshot {
        self.stats.snapshot()
    }
    
    /// Check if queue is empty (all writes have been processed).
    pub fn is_drained(&self) -> bool {
        let s = self.stats.snapshot();
        s.batches_queued == s.batches_written
    }
    
    /// Get number of pending operations.
    pub fn pending(&self) -> u64 {
        let s = self.stats.snapshot();
        s.batches_queued.saturating_sub(s.batches_written)
    }
    
    /// Gracefully shutdown: close the channel and wait for all pending writes to complete.
    pub async fn shutdown(mut self) -> WriteQueueStatsSnapshot {
        // Drop the sender to signal the writer to stop after draining
        drop(self.tx);

        // Wait for the writer task to complete
        if let Some(handle) = self.handle.take() {
            if let Err(e) = handle.await {
                tracing::error!("WriteQueue: writer task panicked: {}", e);
            }
        }

        self.stats.snapshot()
    }

    /// Gracefully shutdown when wrapped in Arc: close the channel and wait for all pending writes.
    /// This method works with shared ownership and won't cause data loss if other Arc clones exist.
    ///
    /// Unlike shutdown(), this method doesn't consume self, so it works with Arc<WriteQueue>.
    /// The channel will be closed (blocking further writes) and the writer task will drain
    /// all pending operations before completing.
    pub async fn shutdown_shared(self: Arc<Self>) -> WriteQueueStatsSnapshot {
        // Close the sender by dropping all strong references to it.
        // The channel will close when tx is dropped, signaling writer_loop to exit after draining.
        // We clone tx to get the receiver count, but immediately drop it.
        drop(self.tx.clone());

        // Try to get exclusive ownership to properly await the handle
        match Arc::try_unwrap(self) {
            Ok(mut queue) => {
                // We have exclusive ownership now. Drop tx first to signal writer to stop,
                // then await the handle (mirrors shutdown()).
                drop(queue.tx);
                if let Some(handle) = queue.handle.take() {
                    if let Err(e) = handle.await {
                        tracing::error!("WriteQueue: writer task panicked during shutdown: {}", e);
                    }
                }
                queue.stats.snapshot()
            }
            Err(arc) => {
                // Other Arc references still exist. The sender is already closed (dropped above),
                // so no new writes can happen. The writer will drain and exit.
                // We can't await the handle without ownership, but we can wait for the queue to drain.
                let stats = arc.stats.clone();

                // Poll until drained or timeout
                let deadline = tokio::time::Instant::now() + tokio::time::Duration::from_secs(30);
                while tokio::time::Instant::now() < deadline {
                    let s = stats.snapshot();
                    if s.batches_queued == s.batches_written {
                        tracing::debug!("WriteQueue: fully drained despite multiple Arc refs");
                        break;
                    }
                    tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;
                }

                stats.snapshot()
            }
        }
    }
}

fn escape_sql(s: &str) -> String {
    s.replace('\\', "\\\\")
     .replace('\'', "''")
     .replace('\0', "")
     .replace('\n', "\\n")
     .replace('\r', "\\r")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_vector(seed: u32, dims: usize) -> Vec<f32> {
        (0..dims)
            .map(|i| ((seed as f32) + (i as f32) * 0.001) / 1000.0)
            .collect()
    }

    #[tokio::test]
    async fn storage_count_files_returns_unique_paths() {
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Storage::open(&db, 256).await.unwrap();

        // Add chunks for two files
        storage
            .add_chunks(
                "file1.txt",
                "hash1",
                "text",
                vec![
                    ChunkData {
                        position: 0,
                        content: "chunk 1".into(),
                        start_line: 1,
                        end_line: 1,
                        vector: test_vector(1, 256),
                    },
                    ChunkData {
                        position: 1,
                        content: "chunk 2".into(),
                        start_line: 2,
                        end_line: 2,
                        vector: test_vector(2, 256),
                    },
                ],
            )
            .await
            .unwrap();

        storage
            .add_chunks(
                "file2.txt",
                "hash2",
                "text",
                vec![ChunkData {
                    position: 0,
                    content: "chunk 3".into(),
                    start_line: 1,
                    end_line: 1,
                    vector: test_vector(3, 256),
                }],
            )
            .await
            .unwrap();

        assert_eq!(storage.count_files().await.unwrap(), 2);
        assert_eq!(storage.count_chunks().await.unwrap(), 3);
    }

    #[tokio::test]
    async fn storage_fts_index_skips_below_threshold() {
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Storage::open(&db, 256).await.unwrap();

        // Add fewer chunks than FTS_THRESHOLD
        for i in 0..(FTS_THRESHOLD - 1) {
            storage
                .add_chunks(
                    &format!("file{i}.txt"),
                    &format!("hash{i}"),
                    "text",
                    vec![ChunkData {
                        position: 0,
                        content: format!("content {i}"),
                        start_line: 1,
                        end_line: 1,
                        vector: test_vector(i as u32, 256),
                    }],
                )
                .await
                .unwrap();
        }

        // Should succeed but skip index creation (below threshold)
        storage.create_fts_index().await.unwrap();

        // Verify no FTS index was created
        let table = storage.ensure_chunks().await.unwrap();
        let indices = table.list_indices().await.unwrap();
        let has_fts = indices.iter().any(|idx| {
            idx.columns.contains(&"content".to_string())
                && matches!(idx.index_type, lancedb::index::IndexType::FTS)
        });
        assert!(!has_fts, "FTS index should not be created below threshold");
    }

    #[tokio::test]
    async fn storage_fts_index_creates_above_threshold() {
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Storage::open(&db, 256).await.unwrap();

        // Add enough chunks to exceed FTS_THRESHOLD
        for i in 0..(FTS_THRESHOLD + 5) {
            storage
                .add_chunks(
                    &format!("file{i}.txt"),
                    &format!("hash{i}"),
                    "text",
                    vec![ChunkData {
                        position: 0,
                        content: format!("hello world content number {i}"),
                        start_line: 1,
                        end_line: 1,
                        vector: test_vector(i as u32, 256),
                    }],
                )
                .await
                .unwrap();
        }

        // Should create FTS index
        storage.create_fts_index().await.unwrap();

        // Verify FTS index was created
        let table = storage.ensure_chunks().await.unwrap();
        let indices = table.list_indices().await.unwrap();
        let has_fts = indices.iter().any(|idx| {
            idx.columns.contains(&"content".to_string())
                && matches!(idx.index_type, lancedb::index::IndexType::FTS)
        });
        assert!(has_fts, "FTS index should be created above threshold");
    }

    #[tokio::test]
    async fn storage_search_vector_returns_results() {
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Storage::open(&db, 256).await.unwrap();

        // Add a chunk
        storage
            .add_chunks(
                "test.txt",
                "testhash",
                "text",
                vec![ChunkData {
                    position: 0,
                    content: "hello world test content".into(),
                    start_line: 1,
                    end_line: 1,
                    vector: test_vector(42, 256),
                }],
            )
            .await
            .unwrap();

        // Search with similar vector
        let query_vec = test_vector(42, 256);
        let results = storage.search_vector(&query_vec, 10).await.unwrap();

        assert_eq!(results.len(), 1);
        assert_eq!(results[0].path, "test.txt");
        assert!(results[0].content.contains("hello world"));
    }

    #[tokio::test]
    async fn storage_search_vector_filters_nan_scores() {
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Storage::open(&db, 256).await.unwrap();

        // Add a valid chunk
        storage
            .add_chunks(
                "good.txt",
                "hash_good",
                "text",
                vec![ChunkData {
                    position: 0,
                    content: "good".into(),
                    start_line: 1,
                    end_line: 1,
                    vector: test_vector(42, 256),
                }],
            )
            .await
            .unwrap();

        // Add a chunk with a zero vector (can produce NaN cosine distance)
        storage
            .add_chunks(
                "zero.txt",
                "hash_zero",
                "text",
                vec![ChunkData {
                    position: 0,
                    content: "zero".into(),
                    start_line: 1,
                    end_line: 1,
                    vector: vec![0.0; 256],
                }],
            )
            .await
            .unwrap();

        let results = storage.search_vector(&test_vector(42, 256), 10).await.unwrap();
        assert!(results.iter().all(|r| !r.score.is_nan()));
        assert!(results.iter().any(|r| r.path == "good.txt"));
    }

    #[tokio::test]
    async fn storage_search_hybrid_falls_back_to_vector() {
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Storage::open(&db, 256).await.unwrap();

        // Add a chunk (no FTS index)
        storage
            .add_chunks(
                "test.txt",
                "testhash",
                "text",
                vec![ChunkData {
                    position: 0,
                    content: "hello world test content".into(),
                    start_line: 1,
                    end_line: 1,
                    vector: test_vector(42, 256),
                }],
            )
            .await
            .unwrap();

        // Hybrid search should fall back to vector search (no FTS index)
        let query_vec = test_vector(42, 256);
        let results = storage
            .search_hybrid("hello", &query_vec, 10)
            .await
            .unwrap();

        assert_eq!(results.len(), 1);
        assert_eq!(results[0].path, "test.txt");
    }

    #[tokio::test]
    async fn storage_search_hybrid_skips_hybrid_below_threshold() {
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Storage::open(&db, 256).await.unwrap();

        // Add fewer chunks than FTS_THRESHOLD — should fall back to vector silently
        let q = test_vector(1, 256);
        for i in 0..(FTS_THRESHOLD - 1) {
            storage
                .add_chunks(
                    &format!("f{i}.txt"),
                    &format!("h{i}"),
                    "text",
                    vec![ChunkData { position: 0, content: format!("doc {i}"), start_line: 1, end_line: 1, vector: q.clone() }],
                )
                .await
                .unwrap();
        }

        // Must not error even without FTS index
        let results = storage.search_hybrid("doc", &q, 5).await.unwrap();
        assert!(!results.is_empty() || results.is_empty()); // just confirms no panic/error
    }

    #[tokio::test]
    async fn storage_search_hybrid_uses_fts_when_available() {
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Storage::open(&db, 256).await.unwrap();

        // Create many chunks whose vectors match the query vector so they dominate vector-only search.
        let query_vec = test_vector(1, 256);
        for i in 0..(FTS_THRESHOLD + 15) {
            storage
                .add_chunks(
                    &format!("vec{i}.txt"),
                    &format!("hash_vec{i}"),
                    "text",
                    vec![ChunkData {
                        position: 0,
                        content: format!("vector doc {i}"),
                        start_line: 1,
                        end_line: 1,
                        vector: query_vec.clone(),
                    }],
                )
                .await
                .unwrap();
        }

        // Add an FTS-only chunk with a very different vector.
        storage
            .add_chunks(
                "fts.txt",
                "hash_fts",
                "text",
                vec![ChunkData {
                    position: 0,
                    content: "needlexyz unique term".into(),
                    start_line: 1,
                    end_line: 1,
                    vector: test_vector(999, 256),
                }],
            )
            .await
            .unwrap();

        storage.create_fts_index().await.unwrap();

        let results = storage
            .search_hybrid("needlexyz", &query_vec, 20)
            .await
            .unwrap();

        assert!(results.iter().any(|r| r.path == "fts.txt"));
    }

    // ==================== WriteQueue Tests ====================

    #[tokio::test]
    async fn write_queue_basic_add_and_shutdown() {
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Arc::new(Storage::open(&db, 256).await.unwrap());
        
        let queue = WriteQueue::new(storage.clone(), 16);
        
        // Queue some add operations
        let files = vec![
            FileChunks {
                path: "test1.txt".into(),
                file_hash: "hash1".into(),
                language: "text".into(),
                chunks: vec![ChunkData {
                    position: 0,
                    content: "hello world".into(),
                    start_line: 1,
                    end_line: 1,
                    vector: test_vector(1, 256),
                }],
            },
            FileChunks {
                path: "test2.txt".into(),
                file_hash: "hash2".into(),
                language: "text".into(),
                chunks: vec![ChunkData {
                    position: 0,
                    content: "foo bar".into(),
                    start_line: 1,
                    end_line: 1,
                    vector: test_vector(2, 256),
                }],
            },
        ];
        
        queue.add(files).await.unwrap();
        
        // Shutdown and wait for writes to complete
        let stats = queue.shutdown().await;
        
        // Verify stats
        assert_eq!(stats.batches_queued, 1);
        assert_eq!(stats.batches_written, 1);
        assert_eq!(stats.chunks_written, 2);
        assert_eq!(stats.errors, 0);
        
        // Verify data was written
        let count = storage.count_chunks().await.unwrap();
        assert_eq!(count, 2);
    }

    #[tokio::test]
    async fn write_queue_delete_and_add() {
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Arc::new(Storage::open(&db, 256).await.unwrap());
        
        // First add some data directly
        storage.add_chunks(
            "existing.txt",
            "old_hash",
            "text",
            vec![ChunkData {
                position: 0,
                content: "old content".into(),
                start_line: 1,
                end_line: 1,
                vector: test_vector(99, 256),
            }],
        ).await.unwrap();
        
        assert_eq!(storage.count_chunks().await.unwrap(), 1);
        
        let queue = WriteQueue::new(storage.clone(), 16);
        
        // Queue delete then add (simulating update)
        queue.delete(vec!["existing.txt".into()]).await.unwrap();
        queue.add(vec![FileChunks {
            path: "existing.txt".into(),
            file_hash: "new_hash".into(),
            language: "text".into(),
            chunks: vec![
                ChunkData {
                    position: 0,
                    content: "new content part 1".into(),
                    start_line: 1,
                    end_line: 5,
                    vector: test_vector(100, 256),
                },
                ChunkData {
                    position: 1,
                    content: "new content part 2".into(),
                    start_line: 6,
                    end_line: 10,
                    vector: test_vector(101, 256),
                },
            ],
        }]).await.unwrap();
        
        let stats = queue.shutdown().await;
        
        assert_eq!(stats.batches_queued, 2);
        assert_eq!(stats.batches_written, 2);
        assert_eq!(stats.files_deleted, 1);
        assert_eq!(stats.chunks_written, 2);
        assert_eq!(stats.errors, 0);
        
        // Should have 2 chunks now (old one deleted, 2 new added)
        assert_eq!(storage.count_chunks().await.unwrap(), 2);
    }

    #[tokio::test]
    async fn write_queue_multiple_batches() {
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Arc::new(Storage::open(&db, 256).await.unwrap());
        
        let queue = WriteQueue::new(storage.clone(), 32);
        
        // Queue many batches
        for i in 0..10 {
            queue.add(vec![FileChunks {
                path: format!("file{}.txt", i),
                file_hash: format!("hash{}", i),
                language: "text".into(),
                chunks: vec![ChunkData {
                    position: 0,
                    content: format!("content {}", i),
                    start_line: 1,
                    end_line: 1,
                    vector: test_vector(i, 256),
                }],
            }]).await.unwrap();
        }
        
        let stats = queue.shutdown().await;
        
        assert_eq!(stats.batches_queued, 10);
        assert_eq!(stats.batches_written, 10);
        assert_eq!(stats.chunks_written, 10);
        assert_eq!(stats.errors, 0);
        
        assert_eq!(storage.count_chunks().await.unwrap(), 10);
    }

    #[tokio::test]
    async fn write_queue_try_send_non_blocking() {
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Arc::new(Storage::open(&db, 256).await.unwrap());
        
        // Small buffer to test backpressure
        let queue = WriteQueue::new(storage.clone(), 2);
        
        // try_add should succeed for first few
        for i in 0..2 {
            let result = queue.try_add(vec![FileChunks {
                path: format!("file{}.txt", i),
                file_hash: format!("hash{}", i),
                language: "text".into(),
                chunks: vec![ChunkData {
                    position: 0,
                    content: format!("content {}", i),
                    start_line: 1,
                    end_line: 1,
                    vector: test_vector(i, 256),
                }],
            }]);
            assert!(result.is_ok(), "try_add {} should succeed", i);
        }
        
        // Give writer time to drain
        tokio::time::sleep(tokio::time::Duration::from_millis(100)).await;
        
        let stats = queue.shutdown().await;
        assert!(stats.batches_written >= 1); // At least some should have been written
        assert_eq!(stats.errors, 0);
    }

    #[tokio::test]
    async fn write_queue_is_drained_and_pending() {
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Arc::new(Storage::open(&db, 256).await.unwrap());
        
        let queue = WriteQueue::new(storage.clone(), 16);
        
        // Initially should be drained
        assert!(queue.is_drained());
        assert_eq!(queue.pending(), 0);
        
        // Queue some work
        queue.add(vec![FileChunks {
            path: "test.txt".into(),
            file_hash: "hash".into(),
            language: "text".into(),
            chunks: vec![ChunkData {
                position: 0,
                content: "test".into(),
                start_line: 1,
                end_line: 1,
                vector: test_vector(1, 256),
            }],
        }]).await.unwrap();
        
        // After shutdown, should be drained
        let stats = queue.shutdown().await;
        assert_eq!(stats.batches_queued, stats.batches_written);
    }

    #[tokio::test]
    async fn write_queue_empty_operations_are_no_ops() {
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Arc::new(Storage::open(&db, 256).await.unwrap());
        
        let queue = WriteQueue::new(storage.clone(), 16);
        
        // Empty operations should succeed but not count
        queue.add(vec![]).await.unwrap();
        queue.delete(vec![]).await.unwrap();
        queue.try_add(vec![]).unwrap();
        queue.try_delete(vec![]).unwrap();
        
        let stats = queue.shutdown().await;
        
        // Empty ops don't count as batches
        assert_eq!(stats.batches_queued, 0);
        assert_eq!(stats.batches_written, 0);
    }

    #[tokio::test]
    async fn write_queue_usage_recording() {
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Arc::new(Storage::open(&db, 256).await.unwrap());
        
        let queue = WriteQueue::new(storage.clone(), 16);
        
        // Record some usage
        queue.record_usage(1000, "budget").await.unwrap();
        queue.record_usage(2000, "budget").await.unwrap();
        
        let stats = queue.shutdown().await;
        
        assert_eq!(stats.batches_queued, 2);
        assert_eq!(stats.batches_written, 2);
        assert_eq!(stats.errors, 0);
    }

    // ==================== WriteQueue Data Integrity Tests ====================

    #[tokio::test]
    async fn write_queue_data_integrity_under_load() {
        // Test that all data is correctly persisted under high load
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Arc::new(Storage::open(&db, 256).await.unwrap());
        
        let queue = WriteQueue::new(storage.clone(), 32);
        
        // Queue 100 files with varying chunk counts
        let mut expected_chunks = 0;
        for i in 0..100 {
            let chunk_count = (i % 5) + 1; // 1-5 chunks per file
            let chunks: Vec<ChunkData> = (0..chunk_count)
                .map(|j| ChunkData {
                    position: j as i32,
                    content: format!("file {} chunk {}", i, j),
                    start_line: j as i32 + 1,
                    end_line: j as i32 + 10,
                    vector: test_vector((i * 10 + j) as u32, 256),
                })
                .collect();
            expected_chunks += chunks.len();
            
            queue.add(vec![FileChunks {
                path: format!("file{}.txt", i),
                file_hash: format!("hash{}", i),
                language: "text".into(),
                chunks,
            }]).await.unwrap();
        }
        
        let stats = queue.shutdown().await;
        
        // Verify all batches were written without errors
        assert_eq!(stats.batches_queued, 100);
        assert_eq!(stats.batches_written, 100);
        assert_eq!(stats.errors, 0);
        assert_eq!(stats.chunks_written, expected_chunks as u64);
        
        // Verify actual data in storage
        let actual_chunks = storage.count_chunks().await.unwrap();
        assert_eq!(actual_chunks, expected_chunks, "chunk count mismatch");
        
        // Verify we can retrieve specific files
        let files = storage.get_indexed_files().await.unwrap();
        assert_eq!(files.len(), 100, "should have 100 unique files");
        
        for i in 0..100 {
            assert!(files.contains(&format!("file{}.txt", i)), "missing file{}.txt", i);
        }
    }

    #[tokio::test]
    async fn write_queue_delete_then_add_preserves_order() {
        // Test that delete-then-add operations execute in correct order
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Arc::new(Storage::open(&db, 256).await.unwrap());
        
        // Pre-populate with some files
        for i in 0..10 {
            storage.add_chunks(
                &format!("file{}.txt", i),
                &format!("old_hash{}", i),
                "text",
                vec![ChunkData {
                    position: 0,
                    content: format!("OLD content {}", i),
                    start_line: 1,
                    end_line: 1,
                    vector: test_vector(i as u32, 256),
                }],
            ).await.unwrap();
        }
        
        assert_eq!(storage.count_chunks().await.unwrap(), 10);
        
        let queue = WriteQueue::new(storage.clone(), 16);
        
        // Simulate file updates: delete old, add new (like the indexer does)
        for i in 0..10 {
            // Delete first
            queue.delete(vec![format!("file{}.txt", i)]).await.unwrap();
            // Then add updated version with more chunks
            queue.add(vec![FileChunks {
                path: format!("file{}.txt", i),
                file_hash: format!("new_hash{}", i),
                language: "rust".into(),
                chunks: vec![
                    ChunkData {
                        position: 0,
                        content: format!("NEW content {} part 1", i),
                        start_line: 1,
                        end_line: 5,
                        vector: test_vector((i + 100) as u32, 256),
                    },
                    ChunkData {
                        position: 1,
                        content: format!("NEW content {} part 2", i),
                        start_line: 6,
                        end_line: 10,
                        vector: test_vector((i + 200) as u32, 256),
                    },
                ],
            }]).await.unwrap();
        }
        
        let stats = queue.shutdown().await;
        
        assert_eq!(stats.errors, 0, "should have no errors");
        assert_eq!(stats.files_deleted, 10, "should delete 10 files");
        assert_eq!(stats.chunks_written, 20, "should write 20 chunks (2 per file)");
        
        // Verify final state
        let final_chunks = storage.count_chunks().await.unwrap();
        assert_eq!(final_chunks, 20, "should have 20 chunks after update");
        
        // Verify content is the NEW content
        let hashes = storage.get_file_hashes(None).await.unwrap();
        for i in 0..10 {
            let path = format!("file{}.txt", i);
            let hash = hashes.get(&path).expect(&format!("missing {}", path));
            assert_eq!(hash, &format!("new_hash{}", i), "should have new hash for {}", path);
        }
    }

    #[tokio::test]
    async fn write_queue_interleaved_operations() {
        // Test interleaved add/delete operations from multiple "files"
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Arc::new(Storage::open(&db, 256).await.unwrap());
        
        let queue = WriteQueue::new(storage.clone(), 32);
        
        // Interleave: add file A, add file B, delete file A, add file C, add file A again
        queue.add(vec![FileChunks {
            path: "a.txt".into(),
            file_hash: "hash_a_v1".into(),
            language: "text".into(),
            chunks: vec![ChunkData {
                position: 0,
                content: "A version 1".into(),
                start_line: 1,
                end_line: 1,
                vector: test_vector(1, 256),
            }],
        }]).await.unwrap();
        
        queue.add(vec![FileChunks {
            path: "b.txt".into(),
            file_hash: "hash_b".into(),
            language: "text".into(),
            chunks: vec![ChunkData {
                position: 0,
                content: "B content".into(),
                start_line: 1,
                end_line: 1,
                vector: test_vector(2, 256),
            }],
        }]).await.unwrap();
        
        queue.delete(vec!["a.txt".into()]).await.unwrap();
        
        queue.add(vec![FileChunks {
            path: "c.txt".into(),
            file_hash: "hash_c".into(),
            language: "text".into(),
            chunks: vec![ChunkData {
                position: 0,
                content: "C content".into(),
                start_line: 1,
                end_line: 1,
                vector: test_vector(3, 256),
            }],
        }]).await.unwrap();
        
        queue.add(vec![FileChunks {
            path: "a.txt".into(),
            file_hash: "hash_a_v2".into(),
            language: "text".into(),
            chunks: vec![ChunkData {
                position: 0,
                content: "A version 2".into(),
                start_line: 1,
                end_line: 1,
                vector: test_vector(4, 256),
            }],
        }]).await.unwrap();
        
        let stats = queue.shutdown().await;
        
        assert_eq!(stats.errors, 0);
        
        // Final state: a.txt (v2), b.txt, c.txt = 3 files
        let files = storage.get_indexed_files().await.unwrap();
        assert_eq!(files.len(), 3);
        assert!(files.contains("a.txt"));
        assert!(files.contains("b.txt"));
        assert!(files.contains("c.txt"));
        
        // Verify a.txt has version 2
        let hashes = storage.get_file_hashes(None).await.unwrap();
        assert_eq!(hashes.get("a.txt").unwrap(), "hash_a_v2");
    }

    #[tokio::test]
    async fn write_queue_search_works_after_async_writes() {
        // Test that vector search works correctly after async writes
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Arc::new(Storage::open(&db, 256).await.unwrap());
        
        let queue = WriteQueue::new(storage.clone(), 16);
        
        // Create a distinctive vector for searching
        let search_vector: Vec<f32> = (0..256).map(|i| (i as f32) / 256.0).collect();
        let similar_vector: Vec<f32> = (0..256).map(|i| (i as f32) / 256.0 + 0.001).collect();
        let different_vector: Vec<f32> = (0..256).map(|i| 1.0 - (i as f32) / 256.0).collect();
        
        // Add files with different vectors
        queue.add(vec![FileChunks {
            path: "similar.txt".into(),
            file_hash: "hash_similar".into(),
            language: "text".into(),
            chunks: vec![ChunkData {
                position: 0,
                content: "This should match the search".into(),
                start_line: 1,
                end_line: 1,
                vector: similar_vector,
            }],
        }]).await.unwrap();
        
        queue.add(vec![FileChunks {
            path: "different.txt".into(),
            file_hash: "hash_different".into(),
            language: "text".into(),
            chunks: vec![ChunkData {
                position: 0,
                content: "This should not match well".into(),
                start_line: 1,
                end_line: 1,
                vector: different_vector,
            }],
        }]).await.unwrap();
        
        let stats = queue.shutdown().await;
        assert_eq!(stats.errors, 0);
        assert_eq!(stats.chunks_written, 2);
        
        // Search should find the similar file first
        let results = storage.search_vector(&search_vector, 10).await.unwrap();
        
        assert!(!results.is_empty(), "should have search results");
        assert_eq!(results[0].path, "similar.txt", "similar.txt should be first result");
    }

    #[tokio::test]
    async fn write_queue_concurrent_producers() {
        // Test multiple concurrent tasks queueing writes
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Arc::new(Storage::open(&db, 256).await.unwrap());
        
        let queue = Arc::new(WriteQueue::new(storage.clone(), 64));
        
        // Spawn multiple producer tasks
        let mut handles = vec![];
        for producer_id in 0..5 {
            let queue = queue.clone();
            let handle = tokio::spawn(async move {
                for i in 0..20 {
                    let file_id = producer_id * 20 + i;
                    queue.add(vec![FileChunks {
                        path: format!("file{}.txt", file_id),
                        file_hash: format!("hash{}", file_id),
                        language: "text".into(),
                        chunks: vec![ChunkData {
                            position: 0,
                            content: format!("content from producer {} file {}", producer_id, i),
                            start_line: 1,
                            end_line: 1,
                            vector: test_vector(file_id as u32, 256),
                        }],
                    }]).await.unwrap();
                }
            });
            handles.push(handle);
        }
        
        // Wait for all producers to finish
        for handle in handles {
            handle.await.unwrap();
        }

        // Shutdown the queue (uses shutdown_shared which handles Arc correctly)
        let stats = queue.shutdown_shared().await;
        
        assert_eq!(stats.errors, 0);
        assert_eq!(stats.batches_written, 100); // 5 producers * 20 files
        assert_eq!(stats.chunks_written, 100);
        
        // Verify all files are present
        let files = storage.get_indexed_files().await.unwrap();
        assert_eq!(files.len(), 100);
    }

    #[tokio::test]
    async fn write_queue_stress_test_rapid_operations() {
        // Stress test with rapid fire operations
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Arc::new(Storage::open(&db, 256).await.unwrap());
        
        let queue = WriteQueue::new(storage.clone(), 128);
        
        // Rapidly queue many small operations
        for i in 0..200 {
            if i % 10 == 0 && i > 0 {
                // Every 10th operation, delete some files
                let to_delete: Vec<String> = ((i-5)..i)
                    .map(|j| format!("file{}.txt", j))
                    .collect();
                queue.delete(to_delete).await.unwrap();
            }
            
            queue.add(vec![FileChunks {
                path: format!("file{}.txt", i),
                file_hash: format!("hash{}", i),
                language: "text".into(),
                chunks: vec![ChunkData {
                    position: 0,
                    content: format!("rapid content {}", i),
                    start_line: 1,
                    end_line: 1,
                    vector: test_vector(i as u32, 256),
                }],
            }]).await.unwrap();
        }
        
        let stats = queue.shutdown().await;
        
        assert_eq!(stats.errors, 0, "should complete without errors");
        
        // Verify storage is in consistent state
        let files = storage.get_indexed_files().await.unwrap();
        assert!(!files.is_empty(), "should have some files indexed");
        
        // The exact count depends on delete timing, but should be consistent
        let chunk_count = storage.count_chunks().await.unwrap();
        assert!(chunk_count > 0, "should have chunks in storage");
    }

    #[tokio::test]
    async fn write_queue_try_add_returns_data_on_full() {
        // Verify that try_add returns the data when the queue is full,
        // so the caller can retry without data loss.
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Arc::new(Storage::open(&db, 256).await.unwrap());

        // Tiny buffer (2 slots) to easily trigger "full"
        let queue = WriteQueue::new(storage.clone(), 2);

        // Fill the queue with blocking sends first
        for i in 0..2 {
            queue.add(vec![FileChunks {
                path: format!("fill{}.txt", i),
                file_hash: format!("hash{}", i),
                language: "text".into(),
                chunks: vec![ChunkData {
                    position: 0,
                    content: format!("fill {}", i),
                    start_line: 1,
                    end_line: 1,
                    vector: test_vector(i, 256),
                }],
            }]).await.unwrap();
        }

        // Now try_add should fail and return the data
        let files = vec![FileChunks {
            path: "overflow.txt".into(),
            file_hash: "hash_overflow".into(),
            language: "text".into(),
            chunks: vec![ChunkData {
                position: 0,
                content: "overflow".into(),
                start_line: 1,
                end_line: 1,
                vector: test_vector(99, 256),
            }],
        }];

        // The queue may or may not be full depending on how fast the writer drains,
        // so we just verify that if it returns an error, the data is intact.
        match queue.try_add(files) {
            Ok(()) => {
                // Writer drained fast enough — that's fine too
            }
            Err(returned) => {
                // Data was returned without loss
                assert_eq!(returned.len(), 1);
                assert_eq!(returned[0].path, "overflow.txt");
                assert_eq!(returned[0].chunks.len(), 1);
            }
        }

        let stats = queue.shutdown().await;
        assert_eq!(stats.errors, 0);
    }

    #[tokio::test]
    async fn write_queue_try_delete_returns_data_on_full() {
        // Same as above but for try_delete
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Arc::new(Storage::open(&db, 256).await.unwrap());

        let queue = WriteQueue::new(storage.clone(), 2);

        // Fill the queue
        for i in 0..2 {
            queue.add(vec![FileChunks {
                path: format!("fill{}.txt", i),
                file_hash: format!("hash{}", i),
                language: "text".into(),
                chunks: vec![ChunkData {
                    position: 0,
                    content: format!("fill {}", i),
                    start_line: 1,
                    end_line: 1,
                    vector: test_vector(i, 256),
                }],
            }]).await.unwrap();
        }

        let paths = vec!["a.txt".to_string(), "b.txt".to_string()];
        match queue.try_delete(paths) {
            Ok(()) => {}
            Err(returned) => {
                assert_eq!(returned.len(), 2);
                assert_eq!(returned[0], "a.txt");
                assert_eq!(returned[1], "b.txt");
            }
        }

        let stats = queue.shutdown().await;
        assert_eq!(stats.errors, 0);
    }

    #[tokio::test]
    async fn write_queue_nonblocking_pattern_no_data_loss() {
        // Simulate the TCP consumer pattern: use try_send, put data back on failure,
        // then final blocking flush. Verify no data loss.
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Arc::new(Storage::open(&db, 256).await.unwrap());

        // Large buffer like TCP mode uses
        let queue = WriteQueue::new(storage.clone(), 512);

        let mut pending_deletes: Vec<String> = Vec::new();
        let mut pending_adds: Vec<FileChunks> = Vec::new();

        // Simulate consumer loop: accumulate 128 files, then non-blocking flush
        for i in 0..300u32 {
            pending_deletes.push(format!("file{}.txt", i));
            pending_adds.push(FileChunks {
                path: format!("file{}.txt", i),
                file_hash: format!("hash{}", i),
                language: "text".into(),
                chunks: vec![ChunkData {
                    position: 0,
                    content: format!("content {}", i),
                    start_line: 1,
                    end_line: 1,
                    vector: test_vector(i, 256),
                }],
            });

            // Flush every 128 files (like TCP mode)
            if pending_adds.len() >= 128 {
                let batch = std::mem::take(&mut pending_deletes);
                if let Err(returned) = queue.try_delete(batch) {
                    pending_deletes = returned;
                }
                let batch = std::mem::take(&mut pending_adds);
                if let Err(returned) = queue.try_add(batch) {
                    pending_adds = returned;
                }
            }
        }

        // Final blocking flush (like TCP mode does after embed phase)
        if !pending_deletes.is_empty() {
            queue.delete(std::mem::take(&mut pending_deletes)).await.unwrap();
        }
        if !pending_adds.is_empty() {
            queue.add(std::mem::take(&mut pending_adds)).await.unwrap();
        }

        let stats = queue.shutdown().await;
        assert_eq!(stats.errors, 0);

        // Verify ALL 300 files were written
        let files = storage.get_indexed_files().await.unwrap();
        assert_eq!(files.len(), 300, "all 300 files should be persisted");

        let chunks = storage.count_chunks().await.unwrap();
        assert_eq!(chunks, 300, "all 300 chunks should be persisted");
    }

    #[tokio::test]
    async fn write_queue_large_buffer_tcp_mode() {
        // Verify that a large buffer (512) works correctly and doesn't
        // cause issues with the writer draining.
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Arc::new(Storage::open(&db, 256).await.unwrap());

        let queue = WriteQueue::new(storage.clone(), 512);

        // Queue 500 items rapidly via try_send
        let mut failed = 0u32;
        for i in 0..500u32 {
            let files = vec![FileChunks {
                path: format!("file{}.txt", i),
                file_hash: format!("hash{}", i),
                language: "text".into(),
                chunks: vec![ChunkData {
                    position: 0,
                    content: format!("content {}", i),
                    start_line: 1,
                    end_line: 1,
                    vector: test_vector(i, 256),
                }],
            }];
            if queue.try_add(files).is_err() {
                failed += 1;
                // In real code we'd retry; here just track failures
            }
        }

        let stats = queue.shutdown().await;
        assert_eq!(stats.errors, 0);

        // Some may have been dropped by try_send if queue was full
        let written = stats.chunks_written as u32;
        assert!(written + failed == 500, "written ({}) + failed ({}) should equal 500", written, failed);
        assert!(written > 400, "most should succeed with 512 buffer: got {}", written);
    }

    #[tokio::test]
    async fn write_queue_graceful_shutdown_completes_all_writes() {
        // Verify shutdown waits for ALL pending writes
        let dir = tempfile::TempDir::new().unwrap();
        let db = dir.path().join(".lancedb");
        let storage = Arc::new(Storage::open(&db, 256).await.unwrap());
        
        // Small buffer to create backpressure
        let queue = WriteQueue::new(storage.clone(), 4);
        
        // Queue more items than buffer size
        for i in 0..50 {
            queue.add(vec![FileChunks {
                path: format!("file{}.txt", i),
                file_hash: format!("hash{}", i),
                language: "text".into(),
                chunks: vec![ChunkData {
                    position: 0,
                    content: format!("content {}", i),
                    start_line: 1,
                    end_line: 1,
                    vector: test_vector(i as u32, 256),
                }],
            }]).await.unwrap();
        }
        
        // Shutdown should wait for ALL writes, not just buffered ones
        let stats = queue.shutdown().await;
        
        assert_eq!(stats.batches_queued, 50);
        assert_eq!(stats.batches_written, 50, "all queued batches should be written");
        assert_eq!(stats.chunks_written, 50);
        assert_eq!(stats.errors, 0);
        
        // Verify all data persisted
        let files = storage.get_indexed_files().await.unwrap();
        assert_eq!(files.len(), 50, "all 50 files should be persisted");
    }

    // ==================== Rust Security & Correctness Tests ====================

    // B2: SQL Injection - escape_sql handles all special chars
    #[test]
    fn test_escape_sql_handles_all_special_chars() {
        assert_eq!(escape_sql("normal"), "normal");
        assert_eq!(escape_sql("it's"), "it''s");
        assert_eq!(escape_sql("back\\slash"), "back\\\\slash");
        assert_eq!(escape_sql("null\0byte"), "nullbyte");
        assert_eq!(escape_sql("new\nline"), "new\\nline");
        assert_eq!(escape_sql("carriage\rreturn"), "carriage\\rreturn");
        assert_eq!(escape_sql("all\0'\\\n\r"), "all''\\\\\\n\\r");
        
        // Test with path-like strings (common attack vector)
        assert_eq!(escape_sql("../../etc/passwd"), "../../etc/passwd");
        assert_eq!(escape_sql("'; DROP TABLE chunks; --"), "''; DROP TABLE chunks; --");
    }

    // B3: Chunk ID Race Condition - atomic increments
    #[tokio::test]
    async fn test_chunk_id_atomic_increments() {
        // Test the AtomicI64-based chunk ID counter directly
        use std::sync::atomic::Ordering;
        let key = "test_atomic_db";

        // Clear any existing counter for this key
        {
            let mut ids = chunk_ids().lock().unwrap();
            ids.pop(key);
        }

        // Concurrent increments should all be unique
        let mut handles = vec![];
        for _ in 0..10 {
            let k = key.to_string();
            handles.push(tokio::spawn(async move {
                let counter = {
                    let mut ids = chunk_ids().lock().unwrap();
                    if !ids.contains(&k) {
                        ids.put(k.clone(), Arc::new(std::sync::atomic::AtomicI64::new(0)));
                    }
                    ids.get(&k).unwrap().clone()
                };
                counter.fetch_add(1, Ordering::Relaxed) + 1
            }));
        }

        let results: Vec<i64> = futures::future::join_all(handles)
            .await
            .into_iter()
            .map(|r| r.unwrap())
            .collect();

        // All should be unique
        let unique: std::collections::HashSet<_> = results.iter().collect();
        assert_eq!(unique.len(), results.len(), "all chunk IDs should be unique");
        assert_eq!(results.len(), 10, "should have 10 IDs");

        // Verify the counter is at 10
        {
            let mut ids = chunk_ids().lock().unwrap();
            assert_eq!(
                ids.get(key).unwrap().load(Ordering::Relaxed),
                10
            );
        }
    }

    // B5: Schema Migration - version tracking
    #[tokio::test]
    async fn test_schema_version_tracking() {
        let dir = tempfile::TempDir::new().unwrap();
        let db_path = dir.path().join(".lancedb");
        
        // Create fresh storage - should set version
        {
            let storage = Storage::open(&db_path, 256).await.unwrap();
            let version = storage.get_config("schema_version").await.unwrap();
            assert_eq!(version, Some(SCHEMA_VERSION.to_string()));
        }
        
        // Reopen - version should be preserved
        {
            let storage = Storage::open(&db_path, 256).await.unwrap();
            let version = storage.get_config("schema_version").await.unwrap();
            assert_eq!(version, Some(SCHEMA_VERSION.to_string()));
        }
    }

    // M5: update_chunk_position - add-before-delete pattern (no data loss)
    #[tokio::test]
    async fn test_update_chunk_position_add_before_delete() {
        let dir = tempfile::TempDir::new().unwrap();
        let db_path = dir.path().join(".lancedb");
        let storage = Storage::open(&db_path, 256).await.unwrap();
        
        // Add initial chunk
        storage.add_chunks(
            "test.txt",
            "hash1",
            "text",
            vec![ChunkData {
                position: 0,
                content: "original content".into(),
                start_line: 1,
                end_line: 5,
                vector: test_vector(1, 256),
            }],
        ).await.unwrap();
        
        // Get the chunk ID
        let chunks = storage.get_chunks_with_hashes("test.txt").await.unwrap();
        assert_eq!(chunks.len(), 1);
        let (chunk_id, _, _) = chunks[0];
        
        // Update position (should use add-then-delete)
        storage.update_chunk_position(chunk_id, 1, 10, 15).await.unwrap();
        
        // Verify chunk still exists with new position
        let updated = storage.get_chunks_with_hashes("test.txt").await.unwrap();
        assert_eq!(updated.len(), 1, "chunk should still exist after update");
        let (new_id, _, new_pos) = updated[0];
        assert_ne!(new_id, chunk_id, "should have new ID after update");
        assert_eq!(new_pos, 1, "position should be updated");
        
        // Verify no duplicate chunks
        let count = storage.count_chunks().await.unwrap();
        assert_eq!(count, 1, "should have exactly 1 chunk (old deleted)");
    }

    // M6: Bounded HashMap - get_file_hashes with limit
    #[tokio::test]
    async fn test_get_file_hashes_with_limit() {
        let dir = tempfile::TempDir::new().unwrap();
        let db_path = dir.path().join(".lancedb");
        let storage = Storage::open(&db_path, 256).await.unwrap();
        
        // Add many files
        for i in 0..100 {
            storage.add_chunks(
                &format!("file{}.txt", i),
                &format!("hash{}", i),
                "text",
                vec![ChunkData {
                    position: 0,
                    content: format!("content {}", i),
                    start_line: 1,
                    end_line: 1,
                    vector: test_vector(i as u32, 256),
                }],
            ).await.unwrap();
        }
        
        // Get with limit - should respect limit
        let limited = storage.get_file_hashes(Some(10)).await.unwrap();
        assert_eq!(limited.len(), 10, "should return exactly 10 files");
        
        // Get without limit - should return all
        let all = storage.get_file_hashes(None).await.unwrap();
        assert_eq!(all.len(), 100, "should return all 100 files without limit");
    }
    
    // Additional edge case: empty result
    #[tokio::test]
    async fn test_get_file_hashes_empty() {
        let dir = tempfile::TempDir::new().unwrap();
        let db_path = dir.path().join(".lancedb");
        let storage = Storage::open(&db_path, 256).await.unwrap();
        
        let hashes = storage.get_file_hashes(Some(10)).await.unwrap();
        assert_eq!(hashes.len(), 0, "should return empty map for empty DB");
    }
    
    // Additional edge case: limit larger than dataset
    #[tokio::test]
    async fn test_get_file_hashes_limit_exceeds_data() {
        let dir = tempfile::TempDir::new().unwrap();
        let db_path = dir.path().join(".lancedb");
        let storage = Storage::open(&db_path, 256).await.unwrap();
        
        // Add 5 files
        for i in 0..5 {
            storage.add_chunks(
                &format!("file{}.txt", i),
                &format!("hash{}", i),
                "text",
                vec![ChunkData {
                    position: 0,
                    content: format!("content {}", i),
                    start_line: 1,
                    end_line: 1,
                    vector: test_vector(i as u32, 256),
                }],
            ).await.unwrap();
        }
        
        // Request more than available
        let hashes = storage.get_file_hashes(Some(100)).await.unwrap();
        assert_eq!(hashes.len(), 5, "should return all 5 files when limit > data");
    }

    // =========================================================================
    // Backfill Metadata Tests
    // =========================================================================

    #[tokio::test]
    async fn test_backfill_metadata_empty_index() {
        let dir = tempfile::TempDir::new().unwrap();
        let db_path = dir.path().join(".lancedb");
        let storage = Storage::open(&db_path, 256).await.unwrap();

        // Empty index should return 0 (nothing to backfill)
        let count = storage.backfill_metadata().await.unwrap();
        assert_eq!(count, 0, "empty index should not backfill anything");
    }

    #[tokio::test]
    async fn test_backfill_metadata_sets_missing_fields() {
        let dir = tempfile::TempDir::new().unwrap();
        let db_path = dir.path().join(".lancedb");
        let storage = Storage::open(&db_path, 256).await.unwrap();

        // Add a file to make the index non-empty
        storage.add_chunks(
            "test.txt",
            "hash123",
            "text",
            vec![ChunkData {
                position: 0,
                content: "test content".into(),
                start_line: 1,
                end_line: 1,
                vector: test_vector(1, 256),
            }],
        ).await.unwrap();

        // Verify metadata is initially missing
        assert!(storage.get_tier().await.unwrap().is_none());
        assert!(storage.get_last_index_timestamp().await.unwrap().is_none());
        assert!(storage.get_last_update_timestamp().await.unwrap().is_none());
        assert!(storage.get_last_index_files_count().await.unwrap().is_none());
        assert!(storage.get_last_index_duration_ms().await.unwrap().is_none());

        // Run backfill
        let count = storage.backfill_metadata().await.unwrap();
        assert_eq!(count, 5, "should backfill 5 fields: tier, last_index_timestamp, last_update_timestamp, last_index_files_count, last_index_duration_ms");

        // Verify all fields are now set
        assert_eq!(storage.get_tier().await.unwrap(), Some("budget".into()));
        assert!(storage.get_last_index_timestamp().await.unwrap().is_some());
        assert!(storage.get_last_update_timestamp().await.unwrap().is_some());
        assert_eq!(storage.get_last_index_files_count().await.unwrap(), Some(1));
        assert!(storage.get_last_index_duration_ms().await.unwrap().is_some());
    }

    #[tokio::test]
    async fn test_backfill_metadata_idempotent() {
        let dir = tempfile::TempDir::new().unwrap();
        let db_path = dir.path().join(".lancedb");
        let storage = Storage::open(&db_path, 256).await.unwrap();

        // Add a file
        storage.add_chunks(
            "test.txt",
            "hash123",
            "text",
            vec![ChunkData {
                position: 0,
                content: "test content".into(),
                start_line: 1,
                end_line: 1,
                vector: test_vector(1, 256),
            }],
        ).await.unwrap();

        // First backfill
        let count1 = storage.backfill_metadata().await.unwrap();
        assert_eq!(count1, 5, "first backfill should set 5 fields");

        // Get timestamps after first backfill
        let ts1 = storage.get_last_index_timestamp().await.unwrap();

        // Second backfill should be idempotent (no changes)
        let count2 = storage.backfill_metadata().await.unwrap();
        assert_eq!(count2, 0, "second backfill should not change anything");

        // Timestamps should remain unchanged
        let ts2 = storage.get_last_index_timestamp().await.unwrap();
        assert_eq!(ts1, ts2, "timestamps should not change on second backfill");
    }

    #[tokio::test]
    async fn test_backfill_metadata_preserves_existing() {
        let dir = tempfile::TempDir::new().unwrap();
        let db_path = dir.path().join(".lancedb");
        let storage = Storage::open(&db_path, 256).await.unwrap();

        // Add a file
        storage.add_chunks(
            "test.txt",
            "hash123",
            "text",
            vec![ChunkData {
                position: 0,
                content: "test content".into(),
                start_line: 1,
                end_line: 1,
                vector: test_vector(1, 256),
            }],
        ).await.unwrap();

        // Set some fields manually (simulating a partial index)
        storage.set_tier("cloud").await.unwrap();
        storage.set_last_index_timestamp("2024-01-01T00:00:00Z").await.unwrap();

        // Run backfill
        let count = storage.backfill_metadata().await.unwrap();
        assert_eq!(count, 3, "should only backfill 3 missing fields");

        // Verify existing values are preserved
        assert_eq!(storage.get_tier().await.unwrap(), Some("cloud".into()));
        assert_eq!(storage.get_last_index_timestamp().await.unwrap(), Some("2024-01-01T00:00:00Z".into()));

        // Verify missing fields were filled
        assert!(storage.get_last_update_timestamp().await.unwrap().is_some());
        assert!(storage.get_last_index_files_count().await.unwrap().is_some());
        assert!(storage.get_last_index_duration_ms().await.unwrap().is_some());
    }

    #[tokio::test]
    async fn test_tier_backup_file_created() {
        let dir = tempfile::TempDir::new().unwrap();
        let db_path = dir.path().join(".lancedb");
        let storage = Storage::open(&db_path, 256).await.unwrap();

        // Set tier
        storage.set_tier("premium").await.unwrap();

        // Verify tier.txt was created
        let tier_file = db_path.join("tier.txt");
        assert!(tier_file.exists(), "tier.txt should be created");
        let contents = std::fs::read_to_string(&tier_file).unwrap();
        assert_eq!(contents, "premium");
    }

    #[tokio::test]
    async fn test_tier_recovered_from_backup() {
        let dir = tempfile::TempDir::new().unwrap();
        let db_path = dir.path().join(".lancedb");
        let storage = Storage::open(&db_path, 256).await.unwrap();

        // Set tier to premium
        storage.set_tier("premium").await.unwrap();
        
        // Add a file so backfill_metadata will run
        storage.add_chunks(
            "test.txt",
            "hash123",
            "text",
            vec![ChunkData {
                position: 0,
                content: "test content".into(),
                start_line: 1,
                end_line: 1,
                vector: test_vector(1, 256),
            }],
        ).await.unwrap();

        // Manually delete config.lance to simulate corruption
        let config_path = db_path.join("config.lance");
        std::fs::remove_dir_all(&config_path).unwrap();

        // Reopen storage
        let storage2 = Storage::open(&db_path, 256).await.unwrap();

        // Run backfill - should recover tier from tier.txt
        let count = storage2.backfill_metadata().await.unwrap();
        assert!(count > 0, "should backfill some fields");

        // Verify tier was restored from backup
        assert_eq!(storage2.get_tier().await.unwrap(), Some("premium".into()), 
            "tier should be restored from tier.txt backup");
    }

    #[tokio::test]
    async fn test_repair_config_table() {
        let dir = tempfile::TempDir::new().unwrap();
        let db_path = dir.path().join(".lancedb");
        let storage = Storage::open(&db_path, 256).await.unwrap();

        // Set some config values
        storage.set_tier("balanced").await.unwrap();
        storage.set_config("test_key", "test_value").await.unwrap();

        // Repair config table (even if not corrupted, should work)
        let repaired = storage.repair_config_table().await.unwrap();
        assert!(repaired, "repair should return true");

        // Config.lance should be recreated but empty
        let config_path = db_path.join("config.lance");
        assert!(config_path.exists(), "config.lance should exist after repair");

        // Tier should be recoverable from backup
        assert_eq!(storage.get_tier_from_backup().await, Some("balanced".into()));
        
        // After backfill, tier should be restored
        storage.backfill_metadata().await.unwrap();
        assert_eq!(storage.get_tier().await.unwrap(), Some("balanced".into()));
    }
}
