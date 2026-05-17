//! CLI interface — same flags as the Python embedder for backward compatibility.
//!
//! The TypeScript layer (spawn.ts) parses stdout from this binary, so we must
//! produce output in the same format.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Instant;

use anyhow::{bail, Context, Result};
use clap::Parser;
use futures::stream::FuturesUnordered;
use futures::StreamExt;
use tokio::sync::Semaphore;

use crate::config;
use crate::discover::{self, detect_language, relative_path};
use crate::hardware::{self, HardwareInfo};
use crate::storage::{self, ChunkData, FileChunks, Storage, WriteQueue};

/// Tier -> (embed_model, rerank_model) mapping (must match Python's embeddings.py).
pub fn models_for_tier(tier: &str) -> (&'static str, &'static str) {
    match tier {
        "premium" => (
            "jinaai/jina-embeddings-v2-base-code",
            "Xenova/ms-marco-MiniLM-L-6-v2",
        ),
        "balanced" => (
            "jinaai/jina-embeddings-v2-base-en",
            "Xenova/ms-marco-MiniLM-L-6-v2",
        ),
        _ => (
            "jinaai/jina-embeddings-v2-small-en",
            "Xenova/ms-marco-MiniLM-L-6-v2",
        ),
    }
}

/// Build version string set at compile time by build.rs
/// Format: YYYY-MM-DD-<commit> (matching Python embedder)
const VERSION: &str = env!("OPENCODE_VERSION");

// ---------------------------------------------------------------------------
// Limits
// ---------------------------------------------------------------------------

// Inline content is sent inside msgpack RPC params. Keep this comfortably below
// the protocol MAX_PAYLOAD (256MB) to account for overhead and responses.
const MAX_INLINE_BYTES: u64 = 8 * 1024 * 1024;

// Max file size where we still try to use embedder-side chunking via file path.
// This avoids huge request payloads but still returns all chunks+vectors in one response.
const MAX_FILE_RPC_BYTES: u64 = 32 * 1024 * 1024;

// For "large" files, we avoid sending the file contents directly.
// We still cap what we'll attempt to index to avoid pathological cases.
const MAX_INDEX_BYTES: u64 = 512 * 1024 * 1024;

// When embedding passages, keep each RPC payload bounded.
const EMBED_PASSAGES_MAX_TEXTS: usize = 96;
const EMBED_PASSAGES_MAX_BYTES: usize = 8 * 1024 * 1024;

// Fallback chunking for very large files (used in remote/TCP mode).
const FALLBACK_CHUNK_MAX_CHARS: usize = 4000;
const FALLBACK_CHUNK_MAX_LINES: usize = 200;

#[derive(Parser)]
#[command(name = "opencode-indexer", about = "Fast file indexer for opencode", version = VERSION)]
pub struct Args {
    /// Project root to index
    #[arg(long, default_value = ".")]
    pub root: PathBuf,

    /// Storage path (LanceDB directory)
    #[arg(long)]
    pub db: Option<PathBuf>,

    /// Embedding tier
    #[arg(long, short, default_value = "budget")]
    pub tier: String,

    /// Embedding dimensions
    #[arg(long, short, default_value = "1024")]
    pub dimensions: u32,

    /// Show index status
    #[arg(long)]
    pub status: bool,

    /// Show available embedding tiers
    #[arg(long)]
    pub tiers: bool,

    /// List available backups
    #[arg(long)]
    pub backups: bool,

    /// Restore from backup name
    #[arg(long)]
    pub restore: Option<String>,

    /// Force re-index
    #[arg(long)]
    pub force: bool,

    /// Search the index
    #[arg(long)]
    pub search: Option<String>,

    /// Max daily spending (USD)
    #[arg(long)]
    pub daily_cost_limit: Option<f64>,

    /// Verbose output
    #[arg(long, short)]
    pub verbose: bool,

    /// Dry run (show what would be indexed without doing it)
    #[arg(long)]
    pub dry_run: bool,

    /// Output as JSON
    #[arg(long)]
    pub json: bool,

    /// Emit structured JSON-lines on stdout (one JSON object per line)
    /// for machine consumption by the TypeScript orchestrator.
    #[arg(long)]
    pub json_lines: bool,

    /// List indexed files
    #[arg(long)]
    pub files: bool,

    /// Exclude patterns
    #[arg(long, short)]
    pub exclude: Vec<String>,

    /// Additional directories to index
    #[arg(long, short)]
    pub include: Vec<PathBuf>,

    /// Max concurrent embedding jobs
    #[arg(long, short, default_value = "8")]
    pub concurrency: usize,

    /// Max concurrent file reads during scanning (default: auto)
    #[arg(long)]
    pub scan_concurrency: Option<usize>,

    /// Index a single file
    #[arg(long)]
    pub file: Option<PathBuf>,

    /// Remove a single file from the index
    #[arg(long)]
    pub remove: Option<PathBuf>,

    /// Federated search: additional DB paths to search across
    #[arg(long = "federated-db")]
    pub federated_db: Vec<PathBuf>,

    /// Discover linked projects (symlinks to external git repos)
    #[arg(long)]
    pub discover_links: bool,

    /// Health check (returns structured health info)
    #[arg(long)]
    pub health: bool,

    /// TCP port for daemon mode (0 = pick a random free port). If omitted, Unix socket is used.
    #[arg(long)]
    pub port: Option<u16>,

    /// Idle timeout before auto-shutdown (seconds, 0=disabled, default: 600)
    #[arg(long)]
    pub idle_shutdown: Option<u64>,

    /// Parent process PID to monitor (shutdown if parent dies)
    #[arg(long)]
    pub parent_pid: Option<i32>,

    /// Daemon mode (legacy, accepted for compatibility — no effect)
    #[arg(long, hide = true)]
    pub daemon: bool,
}

/// Emit a structured JSON-lines event to stdout.
fn emit_json(json_lines: bool, event: &serde_json::Value) {
    if !json_lines {
        return;
    }
    if let Ok(s) = serde_json::to_string(event) {
        println!("{s}");
    }
}

/// Conditionally print: if json_lines mode, emit JSON event; otherwise println.
macro_rules! jprintln {
    ($jl:expr, $event:expr, $($arg:tt)*) => {
        if $jl {
            emit_json(true, &$event);
        } else {
            println!($($arg)*);
        }
    };
}

/// Make update_file_partial accessible from daemon module.
pub async fn update_file_partial_pub(
    root: &Path,
    include_dirs: &[PathBuf],
    symlink_dirs: &[discover::SymlinkDir],
    storage_path: &Path,
    storage: &Storage,
    client: &mut crate::model_client::EmbedderClient,
    file: &Path,
    tier: &str,
    dimensions: u32,
    daily_cost_limit: Option<f64>,
    embed: &Semaphore,
    force: bool,
    verbose: bool,
    write_queue: &crate::storage::WriteQueue,
) -> Result<Option<FileUpdate>> {
    update_file_partial(
        root,
        include_dirs,
        symlink_dirs,
        storage_path,
        storage,
        client,
        file,
        tier,
        dimensions,
        daily_cost_limit,
        embed,
        force,
        verbose,
        write_queue,
    )
    .await
}

pub async fn run(args: Args) -> Result<()> {
    let root = args.root.canonicalize().context("invalid root path")?;

    let storage_path = match &args.db {
        Some(db) => {
            if db.extension().and_then(|e| e.to_str()) == Some("sqlite") {
                db.parent().context("invalid --db path")?.join(".lancedb")
            } else {
                if tokio::fs::try_exists(db).await.unwrap_or(false) {
                    let meta = tokio::fs::metadata(db).await.ok();
                    if meta.map(|m| m.is_file()).unwrap_or(false) {
                        bail!(
                            "--db must be a directory (or legacy .sqlite file), got file: {}",
                            db.display()
                        );
                    }
                }
                db.clone()
            }
        }
        None => storage::storage_path(&root),
    };

    if args.tiers {
        show_tiers();
        return Ok(());
    }

    if args.backups {
        // Use spawn_blocking to avoid blocking the async runtime with filesystem I/O
        tokio::task::spawn_blocking(show_backups)
            .await
            .context("spawn_blocking for show_backups")?;
        return Ok(());
    }

    if let Some(name) = &args.restore {
        println!("Restoring from backup: {name}");
        // Use spawn_blocking to avoid blocking the async runtime with filesystem I/O
        let name_clone = name.clone();
        let storage_path_clone = storage_path.clone();
        tokio::task::spawn_blocking(move || {
            storage::restore_backup(&name_clone, &storage_path_clone)
        })
        .await
        .context("spawn_blocking for restore_backup")??;
        println!("Restored to: {}", storage_path.display());
        return Ok(());
    }

    if args.status {
        return show_status(&storage_path, args.dimensions, args.json).await;
    }

    if args.files {
        return show_files(&storage_path, args.dimensions, args.json).await;
    }

    if args.discover_links {
        return discover_links(&root, args.json_lines).await;
    }

    if args.health {
        return show_health(&root, &storage_path, args.dimensions, args.json_lines).await;
    }

    if let Some(query) = &args.search {
        return run_search(
            &storage_path,
            query,
            &args.tier,
            args.dimensions,
            args.json_lines,
            &args.federated_db,
        )
        .await;
    }

    if let Some(path) = &args.remove {
        return remove_file(&storage_path, path, &root, &args.include, args.dimensions).await;
    }

    if let Some(file) = &args.file {
        return index_single_file(
            &root,
            &storage_path,
            file,
            &args.tier,
            args.dimensions,
            args.force,
            args.daily_cost_limit,
            &args.include,
            args.verbose,
        )
        .await;
    }

    // Dry run mode
    if args.dry_run {
        return run_dry_run(&root, &args.exclude, &args.include).await;
    }

    // ── Daemon mode ──────────────────────────────────────────────────────
    // Entered when --parent-pid is set (opencode spawn) or --daemon is passed.
    // Serves RPC over Unix abstract socket @opencode-indexer (or TCP when --port set).
    if args.parent_pid.is_some() || args.daemon {
        return crate::daemon::run(args.idle_shutdown, args.parent_pid, args.port).await;
    }

    // ── One-shot indexing mode ───────────────────────────────────────────
    if args.verbose {
        let hw = HardwareInfo::detect();
        println!("Hardware: {}", hw.description());
        println!(
            "Recommended embedding concurrency: {}",
            hw.embedding_concurrency()
        );
        println!("Recommended scan concurrency: {}", hw.scan_concurrency());
    }

    let start = Instant::now();
    let stats = run_indexing(
        &root,
        &storage_path,
        &args.tier,
        args.dimensions,
        args.force,
        args.daily_cost_limit,
        args.verbose,
        &args.exclude,
        &args.include,
        args.concurrency,
        args.scan_concurrency,
        false,
        args.json_lines,
    )
    .await?;

    let elapsed = start.elapsed();
    let files_per_sec = if elapsed.as_secs_f64() > 0.0 {
        stats.processed as f64 / elapsed.as_secs_f64()
    } else {
        0.0
    };

    jprintln!(
        args.json_lines,
        serde_json::json!({
            "type": "completed",
            "files": stats.processed,
            "modified": stats.modified,
            "embeddings": stats.embedded,
            "duration": elapsed.as_secs_f64(),
            "files_per_sec": files_per_sec,
        }),
        "Done. {} files processed, {} modified, {} new embeddings",
        stats.processed,
        stats.modified,
        stats.embedded
    );
    if !args.json_lines {
        println!(
            "Duration: {:.1}s ({:.1} files/s)",
            elapsed.as_secs_f64(),
            files_per_sec
        );
    }

    // Wait for background compaction to finish before exiting.
    // This ensures data integrity (all fragments are merged).
    if let Some(handle) = stats.compact_handle {
        let _ = handle.await;
    }

    Ok(())
}

fn show_tiers() {
    println!("Available embedding tiers:\n");
    println!("  premium  - best quality (local)");
    println!("  balanced - general usage (local)");
    println!("  budget   - fastest (local)");
    println!("\nFlexible dimensions (Matryoshka embeddings):");
    println!("  --dimensions 256");
    println!("  --dimensions 512");
    println!("  --dimensions 1024");
    println!("  --dimensions 2048");
}

fn show_backups() {
    let backups = storage::list_backups();
    if backups.is_empty() {
        println!("No backups found");
        return;
    }

    println!("Available backups:\n");
    for b in backups {
        println!(
            "  {}  tier={}  created={}  size={} KB",
            b.name,
            b.tier,
            b.created_at,
            b.size_bytes / 1024
        );
    }
    println!("\nRestore with: opencode-indexer --restore <backup-name>");
}

fn resolve_include_dirs(root: &Path, include: &[PathBuf]) -> Vec<PathBuf> {
    include
        .iter()
        .map(|p| {
            if p.is_absolute() {
                p.to_path_buf()
            } else {
                root.join(p)
            }
        })
        .collect()
}

fn count_tokens(text: &str) -> i64 {
    if text.is_empty() {
        return 0;
    }
    (text.len() as i64 / 4).max(1)
}

fn read_text(path: &Path, max_bytes: u64) -> Result<Option<String>> {
    let bytes = std::fs::read(path).context("read file")?;
    if bytes.len() as u64 > max_bytes {
        return Ok(None);
    }

    if bytes.starts_with(&[0xFF, 0xFE]) || bytes.starts_with(&[0xFE, 0xFF]) {
        let be = bytes.starts_with(&[0xFE, 0xFF]);
        let mut u16s = Vec::with_capacity(bytes.len() / 2);
        let mut i = 2;
        while i + 1 < bytes.len() {
            let val = if be {
                u16::from_be_bytes([bytes[i], bytes[i + 1]])
            } else {
                u16::from_le_bytes([bytes[i], bytes[i + 1]])
            };
            u16s.push(val);
            i += 2;
        }
        return Ok(Some(String::from_utf16_lossy(&u16s)));
    }

    let bytes = if bytes.starts_with(&[0xEF, 0xBB, 0xBF]) {
        bytes[3..].to_vec()
    } else {
        bytes
    };

    // Heuristic: NUL bytes in non-UTF16 files are usually binary.
    if bytes.iter().take(8192).any(|b| *b == 0) {
        return Ok(None);
    }

    match String::from_utf8(bytes) {
        Ok(s) => Ok(Some(s)),
        Err(e) => Ok(Some(String::from_utf8_lossy(&e.into_bytes()).into_owned())),
    }
}

fn fallback_chunks(path: &Path) -> Result<Vec<crate::model_client::ChunkMeta>> {
    use std::io::{BufRead, Read};

    let file = std::fs::File::open(path).context("open file")?;
    let mut reader = std::io::BufReader::new(file);

    #[derive(Clone, Copy)]
    enum Enc {
        Utf8,
        Utf16Le,
        Utf16Be,
    }

    let enc = {
        let buf = reader.fill_buf().context("peek")?;
        if buf.starts_with(&[0xFF, 0xFE]) {
            reader.consume(2);
            Enc::Utf16Le
        } else if buf.starts_with(&[0xFE, 0xFF]) {
            reader.consume(2);
            Enc::Utf16Be
        } else if buf.starts_with(&[0xEF, 0xBB, 0xBF]) {
            reader.consume(3);
            Enc::Utf8
        } else {
            Enc::Utf8
        }
    };

    let mut out = Vec::new();
    let mut chunk = String::new();
    let mut start = 1_i32;
    let mut line = 0_i32;
    let mut lines = 0_usize;

    let flush = |out: &mut Vec<crate::model_client::ChunkMeta>,
                 chunk: &mut String,
                 start: &mut i32,
                 line: i32,
                 lines: &mut usize,
                 path: &Path| {
        if chunk.is_empty() {
            return;
        }
        out.push(crate::model_client::ChunkMeta {
            content: std::mem::take(chunk),
            start_line: *start,
            end_line: line,
            chunk_type: "block".to_string(),
            language: detect_language(path).to_string(),
        });
        *start = line + 1;
        *lines = 0;
    };

    match enc {
        Enc::Utf8 => {
            let mut buf = Vec::new();
            loop {
                buf.clear();
                let n = reader.read_until(b'\n', &mut buf).context("read")?;
                if n == 0 {
                    break;
                }

                // Heuristic: treat NUL-heavy content as binary.
                if buf.iter().take(4096).any(|b| *b == 0) {
                    return Ok(Vec::new());
                }

                line += 1;
                lines += 1;
                chunk.push_str(&String::from_utf8_lossy(&buf));

                if chunk.len() < FALLBACK_CHUNK_MAX_CHARS && lines < FALLBACK_CHUNK_MAX_LINES {
                    continue;
                }
                flush(&mut out, &mut chunk, &mut start, line, &mut lines, path);
            }
        }
        Enc::Utf16Le | Enc::Utf16Be => {
            let be = matches!(enc, Enc::Utf16Be);
            let mut carry: Option<u8> = None;
            let mut bytes = vec![0_u8; 64 * 1024];
            let mut line_buf: Vec<u16> = Vec::new();

            loop {
                let n = reader.read(&mut bytes).context("read")?;
                if n == 0 {
                    break;
                }
                let mut slice = &bytes[..n];

                if let Some(c) = carry.take() {
                    if !slice.is_empty() {
                        let val = if be {
                            u16::from_be_bytes([c, slice[0]])
                        } else {
                            u16::from_le_bytes([c, slice[0]])
                        };
                        line_buf.push(val);
                        slice = &slice[1..];
                    } else {
                        carry = Some(c);
                        continue;
                    }
                }

                if slice.len() % 2 == 1 {
                    carry = Some(*slice.last().unwrap());
                    slice = &slice[..slice.len() - 1];
                }

                for pair in slice.chunks_exact(2) {
                    let val = if be {
                        u16::from_be_bytes([pair[0], pair[1]])
                    } else {
                        u16::from_le_bytes([pair[0], pair[1]])
                    };
                    line_buf.push(val);

                    if val != 0x000A {
                        continue;
                    }

                    line += 1;
                    lines += 1;
                    chunk.push_str(&String::from_utf16_lossy(&line_buf));
                    line_buf.clear();

                    if chunk.len() < FALLBACK_CHUNK_MAX_CHARS && lines < FALLBACK_CHUNK_MAX_LINES {
                        continue;
                    }
                    flush(&mut out, &mut chunk, &mut start, line, &mut lines, path);
                }
            }

            if !line_buf.is_empty() {
                line += 1;
                lines += 1;
                chunk.push_str(&String::from_utf16_lossy(&line_buf));
            }
        }
    }

    if !chunk.is_empty() {
        flush(&mut out, &mut chunk, &mut start, line, &mut lines, path);
    }

    Ok(out)
}

async fn embed_batched(
    client: &mut crate::model_client::EmbedderClient,
    texts: &[String],
    model: &str,
    dimensions: u32,
) -> Result<Vec<Vec<f32>>> {
    let mut out = Vec::with_capacity(texts.len());
    let mut i = 0;
    while i < texts.len() {
        let mut bytes = 0_usize;
        let mut n = 0_usize;
        while i + n < texts.len() {
            let add = texts[i + n].len();
            if n > 0 && (n >= EMBED_PASSAGES_MAX_TEXTS || bytes + add > EMBED_PASSAGES_MAX_BYTES) {
                break;
            }
            bytes += add;
            n += 1;
        }

        let vectors = client
            .embed_passages(&texts[i..i + n], model, dimensions)
            .await?;
        out.extend(vectors);
        i += n;
    }
    Ok(out)
}

pub struct IndexStats {
    pub processed: usize,
    pub modified: usize,
    pub embedded: usize,
    pub skipped_repos: Vec<PathBuf>,
    /// Non-git symlinked directories that should be watched with parent project.
    pub symlink_dirs: Vec<discover::SymlinkDir>,
    /// Background compaction task handle. Await this before process exit to
    /// ensure data integrity. Can be ignored if the process will keep running
    /// (e.g., watch mode compacts separately).
    pub compact_handle: Option<tokio::task::JoinHandle<()>>,
}

/// Public wrapper for daemon to call the indexing pipeline.
pub async fn run_indexing_pub(
    root: &Path,
    storage_path: &Path,
    tier: &str,
    dimensions: u32,
    force: bool,
    daily_cost_limit: Option<f64>,
    verbose: bool,
    exclude: &[String],
    include: &[PathBuf],
    concurrency: usize,
    scan_concurrency: Option<usize>,
    quiet: bool,
    json_lines: bool,
) -> Result<IndexStats> {
    run_indexing(
        root,
        storage_path,
        tier,
        dimensions,
        force,
        daily_cost_limit,
        verbose,
        exclude,
        include,
        concurrency,
        scan_concurrency,
        quiet,
        json_lines,
    )
    .await
}

pub struct FileUpdate {
    pub chunks: usize,
    pub kept: usize,
    pub deleted: usize,
    pub inserted: usize,
    pub embedded: usize,
}

async fn cleanup_legacy_indexes(storage_path: &Path) {
    let legacy_sqlite = storage_path
        .parent()
        .map(|p| p.join("index.sqlite"))
        .unwrap_or_else(|| storage_path.join("index.sqlite"));
    let legacy_usearch = storage_path
        .parent()
        .map(|p| p.join("index.usearch"))
        .unwrap_or_else(|| storage_path.join("index.usearch"));
    let _ = tokio::fs::remove_file(&legacy_usearch).await;
    let _ = tokio::fs::remove_file(&legacy_sqlite).await;
    let _ = tokio::fs::remove_file(legacy_sqlite.with_extension("sqlite-wal")).await;
    let _ = tokio::fs::remove_file(legacy_sqlite.with_extension("sqlite-shm")).await;
}

async fn update_file_partial(
    root: &Path,
    include_dirs: &[PathBuf],
    symlink_dirs: &[discover::SymlinkDir],
    storage_path: &Path,
    storage: &Storage,
    client: &mut crate::model_client::EmbedderClient,
    file: &Path,
    tier: &str,
    dimensions: u32,
    daily_cost_limit: Option<f64>,
    embed: &Semaphore,
    force: bool,
    verbose: bool,
    write_queue: &crate::storage::WriteQueue,
) -> Result<Option<FileUpdate>> {
    let rel = discover::relative_path_with_symlinks(file, root, include_dirs, symlink_dirs);

    let size = tokio::fs::metadata(file)
        .await
        .ok()
        .map(|m| m.len())
        .unwrap_or(0);
    if size > MAX_INDEX_BYTES {
        if verbose {
            println!(
                "Skipping huge file ({} MB): {}",
                size / 1024 / 1024,
                file.display()
            );
        }
        return Ok(None);
    }

    // Use spawn_blocking to avoid blocking the async runtime on file hashing
    let file_for_hash = file.to_path_buf();
    let file_hash = tokio::task::spawn_blocking(move || storage::hash_file(&file_for_hash))
        .await
        .map_err(|e| anyhow::anyhow!("hash task failed: {}", e))??;

    // Legacy purge (single-file update)
    cleanup_legacy_indexes(storage_path).await;

    let backend = storage
        .get_config("embedder_backend")
        .await?
        .or(storage.get_config("backend").await?);
    // Only drop tables if we have an EXPLICIT legacy backend value, not if config is missing
    // (missing config means we just need to set it, not wipe existing data)
    if backend.as_deref().is_some_and(|b| b != "fastembed/v1") {
        storage.drop_all_tables().await?;
    }

    let (embed_model, rerank_model) = models_for_tier(tier);
    storage.set_tier(tier).await?;
    storage.set_dimensions(dimensions).await?;
    storage
        .set_config("embedder_backend", "fastembed/v1")
        .await?;
    storage
        .set_config("embedder_embed_model", embed_model)
        .await?;
    storage
        .set_config("embedder_rerank_model", rerank_model)
        .await?;

    if !force && !storage.needs_index(&rel, &file_hash).await? {
        return Ok(None);
    }

    if let Some(limit) = daily_cost_limit {
        let usage = storage.get_daily_usage().await?;
        if usage.cost >= limit {
            println!("Daily cost limit reached");
            return Ok(None);
        }
    }

    let old_chunks_data = storage.get_chunks_with_hashes(&rel).await?;
    let mut old_hash_to_info: HashMap<String, (i64, i32)> = HashMap::new();
    for (chunk_id, content_hash, position) in &old_chunks_data {
        old_hash_to_info
            .entry(content_hash.clone())
            .or_insert((*chunk_id, *position));
    }
    let old_hashes: HashSet<String> = old_hash_to_info.keys().cloned().collect();

    let chunks = if size <= MAX_INLINE_BYTES {
        let Some(content) = read_text(file, MAX_INLINE_BYTES)? else {
            return Ok(None);
        };
        client.chunk(&content, &rel, tier).await?
    } else if !crate::model_client::is_remote_mode() && size <= MAX_FILE_RPC_BYTES {
        client
            .chunk_file(file.to_string_lossy().as_ref(), &rel, tier)
            .await?
    } else {
        fallback_chunks(file)?
    };
    if chunks.is_empty() {
        for (chunk_id, _, _) in old_chunks_data {
            write_queue.delete_chunk_by_id(chunk_id).await?;
        }
        return Ok(Some(FileUpdate {
            chunks: 0,
            kept: 0,
            deleted: old_hashes.len(),
            inserted: 0,
            embedded: 0,
        }));
    }

    // Fix 2: use Arc<String> so content is shared across new_chunks / to_add / to_embed
    // without triple-cloning the full string bytes.
    let mut new_chunks: Vec<(String, i32, std::sync::Arc<String>, i32, i32)> = Vec::new();
    for (i, chunk) in chunks.iter().enumerate() {
        new_chunks.push((
            storage::hash_content(&chunk.content),
            i as i32,
            std::sync::Arc::new(chunk.content.clone()),
            chunk.start_line,
            chunk.end_line,
        ));
    }

    let new_hashes: HashSet<String> = new_chunks.iter().map(|(h, _, _, _, _)| h.clone()).collect();
    let hashes_to_keep: HashSet<String> = old_hashes.intersection(&new_hashes).cloned().collect();
    let hashes_to_delete: HashSet<String> = old_hashes.difference(&new_hashes).cloned().collect();
    let hashes_to_insert: HashSet<String> = new_hashes.difference(&old_hashes).cloned().collect();

    for old_hash in &hashes_to_delete {
        if let Some((chunk_id, _)) = old_hash_to_info.get(old_hash) {
            write_queue.delete_chunk_by_id(*chunk_id).await?;
        }
    }

    // Fix 1: pre-allocate HashMap to avoid rehashing as entries are inserted.
    let mut hash_to_new: HashMap<String, (i32, i32, i32)> = HashMap::with_capacity(new_chunks.len());
    for (h, pos, _content, start, end) in &new_chunks {
        hash_to_new.insert(h.clone(), (*pos, *start, *end));
    }

    let mut matched_old_ids: HashSet<i64> = HashSet::new();
    for kept_hash in &hashes_to_keep {
        let Some((chunk_id, old_pos)) = old_hash_to_info.get(kept_hash) else {
            continue;
        };
        let Some((new_pos, start, end)) = hash_to_new.get(kept_hash) else {
            continue;
        };

        matched_old_ids.insert(*chunk_id);
        if *old_pos != *new_pos {
            write_queue
                .update_chunk_position(*chunk_id, *new_pos, *start, *end)
                .await?;
        }
    }

    for (chunk_id, content_hash, _) in &old_chunks_data {
        if hashes_to_keep.contains(content_hash) && !matched_old_ids.contains(chunk_id) {
            write_queue.delete_chunk_by_id(*chunk_id).await?;
        }
    }

    // Fix 3: pre-allocate with capacity; store Arc<String> in to_add so the
    // string bytes are shared rather than cloned a second time into the slot.
    let mut to_add = Vec::with_capacity(hashes_to_insert.len());
    let mut to_embed: Vec<String> = Vec::with_capacity(hashes_to_insert.len());
    let mut embed_targets: Vec<usize> = Vec::with_capacity(hashes_to_insert.len());

    for (hash, position, content, start, end) in &new_chunks {
        if !hashes_to_insert.contains(hash) {
            continue;
        }

        let existing = storage.find_by_content_hash(hash).await?;
        let idx = to_add.len();
        // Arc::clone is a cheap reference-count increment, not a string copy.
        to_add.push((
            hash.clone(),
            *position,
            std::sync::Arc::clone(content),
            *start,
            *end,
            existing,
        ));
        if to_add[idx].5.is_none() {
            // One String clone here is unavoidable: embed_batched takes &[String].
            to_embed.push((**content).clone());
            embed_targets.push(idx);
        }
    }

    let mut embedded = 0usize;
    if !to_embed.is_empty() {
        let limit_reached = if let Some(limit) = daily_cost_limit {
            let usage = storage.get_daily_usage().await?;
            usage.cost >= limit
        } else {
            false
        };

        if !limit_reached {
            let _permit = embed.acquire().await?;
            let vectors = embed_batched(client, &to_embed, embed_model, dimensions).await?;
            let tokens = to_embed.iter().map(|t| count_tokens(t)).sum();
            write_queue.record_usage(tokens, tier).await?;
            for (i, vec) in vectors.into_iter().enumerate() {
                let idx = embed_targets[i];
                to_add[idx].5 = Some(vec);
                embedded += 1;
            }
        }
    }

    let lang = detect_language(file);
    let mut data = Vec::new();
    for (_hash, position, content, start, end, vector) in to_add {
        let Some(vector) = vector else {
            continue;
        };
        // Arc::try_unwrap returns the inner String for free when this is the
        // last reference (common case); falls back to a clone only if another
        // Arc reference still exists.
        let content_str = std::sync::Arc::try_unwrap(content)
            .unwrap_or_else(|arc| (*arc).clone());
        data.push(ChunkData {
            position,
            content: content_str,
            start_line: start,
            end_line: end,
            vector,
        });
    }
    if !data.is_empty() {
        write_queue
            .add_single(rel.clone(), file_hash.clone(), lang.to_string(), data)
            .await?;
    }

    let kept = hashes_to_keep.len();
    let deleted = hashes_to_delete.len();
    let inserted = hashes_to_insert.len();
    let update = FileUpdate {
        chunks: chunks.len(),
        kept,
        deleted,
        inserted,
        embedded,
    };

    if verbose || embedded > 0 || deleted > 0 || inserted > 0 {
        println!(
            "  {} chunks: {} kept, {} deleted, {} inserted, {} embedded",
            update.chunks, update.kept, update.deleted, update.inserted, update.embedded
        );
    }

    Ok(Some(update))
}

async fn run_indexing(
    root: &Path,
    storage_path: &Path,
    tier: &str,
    dimensions: u32,
    force: bool,
    daily_cost_limit: Option<f64>,
    verbose: bool,
    exclude: &[String],
    include: &[PathBuf],
    concurrency: usize,
    scan_concurrency: Option<usize>,
    quiet: bool,
    json_lines: bool,
) -> Result<IndexStats> {
    if !quiet {
        println!("Indexing {}", root.display());
    }

    // Note: We no longer use PID file locks for indexing. Reasons:
    // 1. The daemon (primary indexing path) is already a singleton via port lock
    // 2. LanceDB handles concurrent writes internally
    // 3. PID files caused stale lock issues that blocked watcher_start
    let started = Instant::now();

    let storage = Storage::open(storage_path, dimensions).await?;

        // Legacy index cleanup (best-effort)
        cleanup_legacy_indexes(storage_path).await;

    let backend = storage
        .get_config("embedder_backend")
        .await?
        .or(storage.get_config("backend").await?);
    // Only consider it a legacy backend if we have an EXPLICIT different value, not if config is missing
    // (missing config can happen with corrupted config.lance - don't wipe good data in that case)
    let legacy_backend = backend.as_deref().is_some_and(|b| b != "fastembed/v1");
    let has_data = storage.count_chunks().await? > 0;
    if legacy_backend && has_data {
        if !quiet {
            println!("Legacy index backend detected. Deleting old data and rebuilding...");
        }
        storage.drop_all_tables().await?;
    }

    // Config changes require a full rebuild (backup + clear)
    let current_tier = storage.get_tier().await?;
    let current_dims = storage.get_dimensions().await?;
    let tier_changed = current_tier.as_deref().is_some_and(|t| t != tier);
    let dims_changed = current_dims.is_some_and(|d| d != dimensions);
    if force || tier_changed || dims_changed {
        let backup = storage.backup().await?;
        if !quiet {
            println!("Backup created: {}", backup.display());
        }
        storage.clear_all().await?;
    }

    // Persist config
    let (embed_model, rerank_model) = models_for_tier(tier);
    storage.set_tier(tier).await?;
    storage.set_dimensions(dimensions).await?;
    storage
        .set_config("embedder_backend", "fastembed/v1")
        .await?;
    storage
        .set_config("embedder_embed_model", embed_model)
        .await?;
    storage
        .set_config("embedder_rerank_model", rerank_model)
        .await?;

    // Check cost limit
    if let Some(limit) = daily_cost_limit {
        let usage = storage.get_daily_usage().await?;
        if usage.cost >= limit {
            if !quiet {
                println!(
                    "Daily cost limit reached: ${:.4} / ${:.2}",
                    usage.cost, limit
                );
            }
            return Ok(IndexStats {
                processed: 0,
                modified: 0,
                embedded: 0,
                skipped_repos: Vec::new(),
                symlink_dirs: Vec::new(),
                compact_handle: None,
            });
        }
    }

    // Progress tracking
    storage.set_indexing_in_progress(true).await?;
    storage
        .set_indexing_start_time(&chrono::Utc::now().to_rfc3339())
        .await?;
    storage.set_indexing_phase("scanning").await?;
    storage.set_phase_progress("scanning", 0, 0).await?;
    storage.set_phase_progress("chunking", 0, 0).await?;
    storage.set_phase_progress("embedding", 0, 0).await?;

    // Load config
    let project = config::load(root);
    let mut cfg = config::effective(&project, None, None);
    cfg.exclude.extend(exclude.iter().cloned());

    // Discover files (git ls-files + include override patterns)
    let mut discovery = discover::discover_files_with_config(root, &cfg)?;
    let include_dirs = resolve_include_dirs(root, include);
    let mut seen: HashSet<PathBuf> = discovery
        .files
        .iter()
        .filter_map(|p| p.canonicalize().ok())
        .collect();
    discovery.files.extend(discover::discover_additional_files(
        &include_dirs,
        if exclude.is_empty() {
            None
        } else {
            Some(exclude)
        },
        &mut seen,
    ));

    if !quiet {
        println!("Discovered {} files", discovery.files.len());
    }

    // Delete removed files (collect them first for WriteQueue)
    let current: HashSet<String> = discovery
        .files
        .iter()
        .map(|p| relative_path(p, root, &include_dirs))
        .collect();
    let paths_to_delete: Vec<String> = storage
        .get_indexed_files()
        .await?
        .into_iter()
        .filter(|path| !current.contains(path))
        .collect();

    // Scan hashes concurrently
    let stored_hashes = if force {
        HashMap::new()
    } else {
        storage.get_file_hashes(None).await?
    };

    let total = discovery.files.len();
    storage
        .set_phase_progress("scanning", 0, total as i64)
        .await?;

    let workers = scan_concurrency.unwrap_or_else(|| {
        std::thread::available_parallelism()
            .map(|n| (n.get() / 2).clamp(2, 8))
            .unwrap_or(4)
    });
    let sem = std::sync::Arc::new(Semaphore::new(workers));
    let stored = std::sync::Arc::new(stored_hashes);
    let files = discovery.files.clone();
    let root = root.to_path_buf();
    // Fix 4: wrap include_dirs in Arc before the loop so each task clones a
    // cheap pointer (8 bytes) instead of the full Vec<PathBuf> on every iteration.
    let include_dirs = std::sync::Arc::new(include_dirs);
    let mut futs = FuturesUnordered::new();

    for file in files {
        let sem = sem.clone();
        let stored = stored.clone();
        let root = root.clone();
        let include_dirs = std::sync::Arc::clone(&include_dirs);
        futs.push(async move {
            let _permit = sem.acquire_owned().await.ok();
            tokio::task::spawn_blocking(move || {
                let rel = relative_path(&file, &root, &include_dirs);

                let size = std::fs::metadata(&file).ok()?.len();
                if size > MAX_INDEX_BYTES {
                    return None;
                }

                let hash = storage::hash_file(&file).ok()?;
                let needs = stored.get(&rel) != Some(&hash);
                Some((file, rel, hash, needs, size))
            })
            .await
            .ok()
            .flatten()
        });
    }

    let mut scanned = 0usize;
    let mut unchanged = 0usize;
    let mut to_process: Vec<(PathBuf, String, String, u64)> = Vec::new();
    while let Some(res) = futs.next().await {
        scanned += 1;
        if scanned % 100 == 0 {
            let _ = storage
                .set_phase_progress("scanning", scanned as i64, total as i64)
                .await;
        }
        let Some((file, rel, hash, needs, size)) = res else {
            continue;
        };
        if !force && !needs {
            unchanged += 1;
            continue;
        }
        to_process.push((file, rel, hash, size));
    }
    storage
        .set_phase_progress("scanning", total as i64, total as i64)
        .await?;

    if !quiet {
        println!("Files to process: {} of {}", to_process.len(), total);
    }

    // Process files: chunk+embed concurrently, write to LanceDB sequentially.
    // LanceDB doesn't support concurrent writes to the same table, so we
    // pipeline: N workers run chunk+embed in parallel via the Python server,
    // then results are written to storage one at a time.
    //
    // Both modes use a two-stage pipeline (chunk -> batch embed):
    // - TCP mode: chunks ALL files locally with fallback_chunks() (no network),
    //   then sends only embed_passages() over TCP with cross-file batching.
    // - Local mode: chunks via Python server (fast local IPC), then
    //   embeds in batches for better GPU utilization.
    storage.set_indexing_phase("embedding").await?;
    storage
        .set_phase_progress("chunking", 0, to_process.len() as i64)
        .await?;
    storage
        .set_phase_progress("embedding", 0, to_process.len() as i64)
        .await?;

    // Prepared result from a concurrent chunk+embed task, ready for sequential storage write.
    struct Prepared {
        rel: String,
        hash: String,
        file: PathBuf,
        chunks: Vec<crate::model_client::ChunkMeta>,
        vectors: Vec<Option<Vec<f32>>>,
        embedded: usize,
    }

    // Use recommended concurrency based on connection mode:
    // - Local: lower concurrency (CPU-bound)
    // - TCP: higher concurrency to overcome network latency
    let max_concurrency = crate::model_client::recommended_concurrency();
    let embed_concurrency = concurrency.max(1).min(max_concurrency);
    let sem = Arc::new(Semaphore::new(embed_concurrency));
    let storage = Arc::new(storage);

    // Create async write queue for non-blocking disk I/O.
    // TCP mode uses a much larger buffer so writes never block the embed pipeline.
    // Local mode uses a smaller buffer since embedding is slower (CPU-bound).
    let write_queue_buffer = if crate::model_client::is_remote_mode() {
        512
    } else {
        64
    };
    let write_queue = WriteQueue::new(storage.clone(), write_queue_buffer);

    // Delete removed files using WriteQueue
    if !paths_to_delete.is_empty() {
        write_queue.delete(paths_to_delete).await?;
    }

    // Producer-consumer pattern: embeddings feed into a channel, writer drains it.
    // This decouples embedding from storage writes, allowing full parallelism.
    let (tx, mut rx) = tokio::sync::mpsc::channel::<Result<Option<Prepared>, anyhow::Error>>(
        embed_concurrency * 2,
    );

    let total_to_process = to_process.len();
    let is_tcp = crate::model_client::is_remote_mode();

    if is_tcp {
        // ======================================================================
        // TCP mode: cross-file batching pipeline
        // ======================================================================
        // Architecture: chunk locally, accumulate texts across files, send
        // large batches to the server to maximize GPU utilization per RPC.
        //
        //   Stage 1: Chunk all files locally with fallback_chunks() (pure Rust)
        //   Stage 2: Collector accumulates texts from multiple files
        //   Stage 3: N embed workers pull cross-file batches (up to 96 texts)
        //            from a shared queue, send one RPC each, scatter results
        //            back to their source files
        //   Stage 4: Completed files stream to the writer as Prepared results
        //
        // Key optimizations:
        //   - Cross-file batching: 880 RPCs (1-2 texts each) → ~40-80 RPCs (48-96 texts)
        //   - Pool pre-warmed: all connections established before pipeline starts
        //   - f32 capability cached: no double-RPCs probing f32 support
        //   - Results stream incrementally: writes overlap with embedding
        //   - Server fast-path: embed_passages_f32 bypasses server queue entirely

        struct Chunked {
            rel: String,
            hash: String,
            file: PathBuf,
            chunks: Vec<crate::model_client::ChunkMeta>,
        }

        // Cross-file batch item: one text to embed, with a slot to write the result back
        struct BatchSlot {
            text: String,
            result_tx: tokio::sync::oneshot::Sender<Result<Vec<f32>, anyhow::Error>>,
        }

        let (chunk_tx, mut chunk_rx) = tokio::sync::mpsc::channel::<
            Result<Option<Chunked>, anyhow::Error>,
        >(embed_concurrency * 2);
        // MPMC channel for batch distribution: all embed workers can recv() concurrently
        // without a mutex. This eliminates the hot-mutex bottleneck where 16 workers
        // competed for a single lock + 5ms timeout-under-lock.
        let (batch_tx, batch_rx) = async_channel::bounded::<Vec<BatchSlot>>(embed_concurrency * 4);

        // Optimal batch size for cross-file batching over TCP.
        // Larger batches = fewer RPCs = less network overhead.
        // 48 texts is a sweet spot: fills GPU sub-batch (ONNX batch=16, 3 sub-batches),
        // keeps individual RPC latency reasonable (~50-150ms).
        const CROSS_FILE_BATCH_SIZE: usize = 48;

        // Stage 1: Chunk all files locally in parallel (no network).
        // spawn_sem bounds in-flight tasks to avoid holding all file data in memory.
        let spawn_sem = Arc::new(Semaphore::new(embed_concurrency * 4));
        for (file, rel, hash, _size) in to_process.iter() {
            let spawn_permit = spawn_sem.clone().acquire_owned().await.unwrap();
            let sem = sem.clone();
            let file = file.clone();
            let rel = rel.clone();
            let hash = hash.clone();
            let chunk_tx = chunk_tx.clone();

            tokio::spawn(async move {
                let rel_clone = rel.clone();
                let result = std::panic::AssertUnwindSafe(async {
                    let _permit = sem.acquire().await.ok();
                    let chunks = fallback_chunks(&file)?;
                    Ok::<Option<Chunked>, anyhow::Error>(Some(Chunked {
                        rel,
                        hash,
                        file,
                        chunks,
                    }))
                });

                let result = match futures::FutureExt::catch_unwind(result).await {
                    Ok(r) => r,
                    Err(panic) => {
                        let msg = if let Some(s) = panic.downcast_ref::<&str>() {
                            s.to_string()
                        } else if let Some(s) = panic.downcast_ref::<String>() {
                            s.clone()
                        } else {
                            "unknown panic".to_string()
                        };
                        tracing::error!("PANIC in chunk task for {}: {}", rel_clone, msg);
                        Err(anyhow::anyhow!("panic in task: {}", msg))
                    }
                };

                let _ = chunk_tx.send(result).await;
                drop(spawn_permit);
            });
        }

        drop(chunk_tx);

        // Stage 2: Collector reads chunked files, submits texts to batch queue,
        // waits for all vectors via oneshot channels, then emits Prepared results.
        {
            let tx_embed = tx.clone();
            let batch_tx = batch_tx.clone();

            tokio::spawn(async move {
                // Accumulate texts across files into cross-file batches
                let mut pending: Vec<BatchSlot> = Vec::with_capacity(CROSS_FILE_BATCH_SIZE);

                while let Some(res) = chunk_rx.recv().await {
                    match res {
                        Ok(Some(c)) => {
                            if c.chunks.is_empty() {
                                let _ = tx_embed
                                    .send(Ok(Some(Prepared {
                                        rel: c.rel,
                                        hash: c.hash,
                                        file: c.file,
                                        chunks: Vec::new(),
                                        vectors: Vec::new(),
                                        embedded: 0,
                                    })))
                                    .await;
                                continue;
                            }

                            // Create oneshot channels for each chunk's vector
                            let mut receivers = Vec::with_capacity(c.chunks.len());

                            for chunk in &c.chunks {
                                let (result_tx, result_rx) = tokio::sync::oneshot::channel();
                                receivers.push(result_rx);
                                pending.push(BatchSlot {
                                    text: chunk.content.clone(),
                                    result_tx,
                                });

                                // Flush when batch is full
                                if pending.len() >= CROSS_FILE_BATCH_SIZE {
                                    let batch = std::mem::replace(
                                        &mut pending,
                                        Vec::with_capacity(CROSS_FILE_BATCH_SIZE),
                                    );
                                    if batch_tx.send(batch).await.is_err() {
                                        break;
                                    }
                                }
                            }

                            // Spawn a task to collect vectors and emit the Prepared result
                            let tx_file = tx_embed.clone();
                            tokio::spawn(async move {
                                let mut vectors: Vec<Option<Vec<f32>>> =
                                    Vec::with_capacity(receivers.len());
                                let mut error = None;

                                for rx in receivers {
                                    match rx.await {
                                        Ok(Ok(vec)) => vectors.push(Some(vec)),
                                        Ok(Err(e)) => {
                                            error = Some(e);
                                            break;
                                        }
                                        Err(_) => {
                                            error = Some(anyhow::anyhow!("embed channel closed"));
                                            break;
                                        }
                                    }
                                }

                                if let Some(e) = error {
                                    let _ = tx_file
                                        .send(Err(anyhow::anyhow!(
                                            "embed failed for {}: {}",
                                            c.rel,
                                            e
                                        )))
                                        .await;
                                } else {
                                    let embedded = c.chunks.len();
                                    let _ = tx_file
                                        .send(Ok(Some(Prepared {
                                            rel: c.rel,
                                            hash: c.hash,
                                            file: c.file,
                                            chunks: c.chunks,
                                            vectors,
                                            embedded,
                                        })))
                                        .await;
                                }
                            });
                        }
                        Ok(None) => {}
                        Err(e) => {
                            let _ = tx_embed.send(Err(e)).await;
                        }
                    }
                }

                // Flush remaining partial batch
                if !pending.is_empty() {
                    let _ = batch_tx.send(pending).await;
                }

                // Drop batch_tx to signal embed workers to stop
                drop(batch_tx);
            });
        }

        drop(batch_tx);

        // Stage 3: N embed workers pull cross-file batches and send RPCs.
        // Uses async_channel (MPMC) so all workers can recv() concurrently without
        // a mutex. Each worker blocks only on its own RPC, never on other workers.
        for _ in 0..embed_concurrency {
            let batch_rx = batch_rx.clone();
            let embed_model = embed_model.to_string();

            tokio::spawn(async move {
                // MPMC recv: multiple workers call recv() concurrently — no mutex needed.
                // Returns Err when all senders are dropped (pipeline complete).
                while let Ok(batch) = batch_rx.recv().await {
                    if batch.is_empty() {
                        continue;
                    }

                    // Destructure batch into texts + senders to avoid cloning.
                    // texts are consumed into the RPC, senders are kept for scattering results.
                    let (texts, senders): (Vec<String>, Vec<_>) =
                        batch.into_iter().map(|s| (s.text, s.result_tx)).unzip();

                    let t_rpc = std::time::Instant::now();
                    let count = texts.len();

                    let result = async {
                        let mut client = crate::model_client::client().await?;
                        client
                            .embed_passages(&texts, &embed_model, dimensions)
                            .await
                    }
                    .await;

                    let rpc_ms = t_rpc.elapsed().as_millis();
                    tracing::debug!(
                        "cross-file embed RPC: {} texts, {}ms ({:.1}ms/text)",
                        count,
                        rpc_ms,
                        rpc_ms as f64 / count as f64,
                    );

                    match result {
                        Ok(vecs) => {
                            // Scatter vectors back to their source files via oneshot channels
                            for (tx, vec) in senders.into_iter().zip(vecs.into_iter()) {
                                let _ = tx.send(Ok(vec));
                            }
                        }
                        Err(e) => {
                            // Send error to all senders in the batch
                            let msg = format!("{}", e);
                            for tx in senders {
                                let _ = tx.send(Err(anyhow::anyhow!("{}", msg)));
                            }
                        }
                    }
                }
            });
        }
    } else {
        // ======================================================================
        // Local mode: two-stage pipeline (chunk -> batch embed)
        // ======================================================================
        // Local IPC is fast, so separate chunk+embed stages allow cross-file
        // batching of embeddings for better GPU utilization.

        struct Chunked {
            rel: String,
            hash: String,
            file: PathBuf,
            chunks: Vec<crate::model_client::ChunkMeta>,
        }

        // MPMC channel for local mode: workers pull files concurrently without mutex
        let (chunk_tx, chunk_rx) =
            async_channel::bounded::<Result<Option<Chunked>, anyhow::Error>>(embed_concurrency * 2);

        for (file, rel, hash, size) in to_process.iter() {
            let sem = sem.clone();
            let file = file.clone();
            let rel = rel.clone();
            let hash = hash.clone();
            let size = *size;
            let tier = tier.to_string();
            let chunk_tx = chunk_tx.clone();

            tokio::spawn(async move {
                let rel_clone = rel.clone();
                let result = std::panic::AssertUnwindSafe(async {
                    let _permit = sem.acquire().await.ok();

                    let mut client = crate::model_client::client().await?;
                    let chunks = if size <= MAX_INLINE_BYTES {
                        let Some(content) = read_text(&file, MAX_INLINE_BYTES)? else {
                            return Ok::<Option<Chunked>, anyhow::Error>(None);
                        };
                        client.chunk(&content, &rel, &tier).await?
                    } else if size <= MAX_FILE_RPC_BYTES {
                        client
                            .chunk_file(file.to_string_lossy().as_ref(), &rel, &tier)
                            .await?
                    } else {
                        fallback_chunks(&file)?
                    };

                    Ok(Some(Chunked {
                        rel,
                        hash,
                        file,
                        chunks,
                    }))
                });

                let result = match futures::FutureExt::catch_unwind(result).await {
                    Ok(r) => r,
                    Err(panic) => {
                        let msg = if let Some(s) = panic.downcast_ref::<&str>() {
                            s.to_string()
                        } else if let Some(s) = panic.downcast_ref::<String>() {
                            s.clone()
                        } else {
                            "unknown panic".to_string()
                        };
                        tracing::error!("PANIC in chunk task for {}: {}", rel_clone, msg);
                        Err(anyhow::anyhow!("panic in task: {}", msg))
                    }
                };

                let _ = chunk_tx.send(result).await;
            });
        }

        drop(chunk_tx);

        // Parallel embedder workers for local mode (MPMC — no mutex needed)
        for _ in 0..embed_concurrency {
            let chunk_rx = chunk_rx.clone();
            let tx_embed = tx.clone();
            let embed_model = embed_model.to_string();

            tokio::spawn(async move {
                // MPMC recv: returns Err when all senders are dropped (pipeline complete)
                while let Ok(msg) = chunk_rx.recv().await {
                    match msg {
                        Ok(Some(c)) => {
                            if c.chunks.is_empty() {
                                let _ = tx_embed
                                    .send(Ok(Some(Prepared {
                                        rel: c.rel,
                                        hash: c.hash,
                                        file: c.file,
                                        chunks: Vec::new(),
                                        vectors: Vec::new(),
                                        embedded: 0,
                                    })))
                                    .await;
                                continue;
                            }

                            let mut vectors: Vec<Option<Vec<f32>>> = vec![None; c.chunks.len()];
                            let mut next = 0_usize;
                            let mut error = None;

                            while next < c.chunks.len() {
                                let lens: Vec<usize> =
                                    c.chunks[next..].iter().map(|ch| ch.content.len()).collect();
                                let mut batch_count = 0_usize;
                                let mut batch_bytes = 0_usize;

                                for len in &lens {
                                    if batch_count > 0
                                        && (batch_count >= EMBED_PASSAGES_MAX_TEXTS
                                            || batch_bytes + len > EMBED_PASSAGES_MAX_BYTES)
                                    {
                                        break;
                                    }
                                    batch_bytes += len;
                                    batch_count += 1;
                                }

                                if batch_count == 0 {
                                    break;
                                }

                                let texts: Vec<String> = c.chunks[next..next + batch_count]
                                    .iter()
                                    .map(|ch| ch.content.clone())
                                    .collect();

                                let result = async {
                                    let mut client = crate::model_client::client().await?;
                                    client
                                        .embed_passages(&texts, &embed_model, dimensions)
                                        .await
                                }
                                .await;

                                match result {
                                    Ok(vecs) => {
                                        for (i, vec) in vecs.into_iter().enumerate() {
                                            vectors[next + i] = Some(vec);
                                        }
                                        next += batch_count;
                                    }
                                    Err(e) => {
                                        error = Some(e);
                                        break;
                                    }
                                }
                            }

                            if let Some(e) = error {
                                let _ = tx_embed
                                    .send(Err(anyhow::anyhow!(
                                        "embed_passages failed for {}: {}",
                                        c.rel,
                                        e
                                    )))
                                    .await;
                            } else {
                                let embedded = c.chunks.len();
                                let _ = tx_embed
                                    .send(Ok(Some(Prepared {
                                        rel: c.rel,
                                        hash: c.hash,
                                        file: c.file,
                                        chunks: c.chunks,
                                        vectors,
                                        embedded,
                                    })))
                                    .await;
                            }
                        }
                        Ok(None) => {}
                        Err(e) => {
                            let _ = tx_embed.send(Err(e)).await;
                        }
                    }
                }
            });
        }
    }

    drop(tx);

    // Consumer: drain results and write to storage.
    //
    // Both modes use the same WriteQueue but with different tuning:
    //
    // TCP mode: Non-blocking writes via try_send with large buffer (512 slots).
    //   The consumer NEVER blocks waiting for writes. Large batches (128 files)
    //   reduce per-call LanceDB overhead. If the queue is momentarily full,
    //   we yield briefly and retry — but with 512 slots this is rare.
    //   Progress DB writes are deferred until the end.
    //
    // Local mode: Blocking writes with smaller buffer (64 slots).
    //   Embedding is CPU-bound and slower, so writes have time to drain.
    //   Progress DB writes every 50 files.

    let mut done = 0usize;
    let mut processed = unchanged;
    let mut modified = 0usize;
    let mut embedded = 0usize;
    let mut pending_tokens: i64 = 0;

    // TCP mode: larger batches (128) to reduce LanceDB per-call overhead
    // Local mode: smaller batches (32) for incremental progress
    let write_batch_default = if is_tcp { 128 } else { 32 };
    let mut write_batch = write_batch_default;
    let mut files_to_delete: Vec<String> = Vec::with_capacity(write_batch);
    let mut files_to_add: Vec<FileChunks> = Vec::with_capacity(write_batch);

    // Memory-aware backpressure: track whether we're under pressure to avoid
    // reading /proc/meminfo on every file (has measurable overhead).
    // Check every MEMORY_CHECK_INTERVAL files.
    const MEMORY_CHECK_INTERVAL: usize = 50;
    let mut under_pressure = false;

    // Helper: flush via blocking send (for local mode where backpressure is OK)
    macro_rules! flush_blocking {
        ($queue:expr, $deletes:expr, $adds:expr) => {{
            if !$deletes.is_empty() {
                if let Err(e) = $queue.delete(std::mem::take(&mut $deletes)).await {
                    tracing::warn!("batch delete queue failed: {}", e);
                }
            }
            if !$adds.is_empty() {
                if let Err(e) = $queue.add(std::mem::take(&mut $adds)).await {
                    tracing::error!("batch add queue failed: {}", e);
                }
            }
        }};
    }

    // Helper: flush via non-blocking try_send (for TCP mode — never stalls the pipeline).
    // If the queue is full, the data is returned and put back into the buffers for retry
    // on the next batch. With a 512-slot buffer this is extremely rare.
    macro_rules! flush_nonblocking {
        ($queue:expr, $deletes:expr, $adds:expr) => {{
            if !$deletes.is_empty() {
                let batch = std::mem::take(&mut $deletes);
                if let Err(returned) = $queue.try_delete(batch) {
                    tracing::debug!(
                        "write queue full on delete, will retry ({} paths)",
                        returned.len()
                    );
                    $deletes = returned;
                }
            }
            if !$adds.is_empty() {
                let batch = std::mem::take(&mut $adds);
                if let Err(returned) = $queue.try_add(batch) {
                    tracing::debug!(
                        "write queue full on add, will retry ({} files)",
                        returned.len()
                    );
                    $adds = returned;
                }
            }
        }};
    }

    while let Some(res) = rx.recv().await {
        done += 1;
        match res {
            Ok(Some(prep)) => {
                if prep.chunks.is_empty() {
                    files_to_delete.push(prep.rel.clone());
                } else {
                    let lang = detect_language(&prep.file);
                    let data: Vec<ChunkData> = prep
                        .chunks
                        .iter()
                        .enumerate()
                        .filter_map(|(i, c)| {
                            prep.vectors
                                .get(i)
                                .and_then(|v| v.as_ref())
                                .map(|vec| ChunkData {
                                    position: i as i32,
                                    content: c.content.clone(),
                                    start_line: c.start_line,
                                    end_line: c.end_line,
                                    vector: vec.clone(),
                                })
                        })
                        .collect();

                    if !data.is_empty() {
                        files_to_delete.push(prep.rel.clone());
                        files_to_add.push(FileChunks {
                            path: prep.rel.clone(),
                            file_hash: prep.hash.clone(),
                            language: lang.to_string(),
                            chunks: data,
                        });
                    }
                }

                pending_tokens += prep
                    .chunks
                    .iter()
                    .map(|c| count_tokens(&c.content))
                    .sum::<i64>();
                embedded += prep.embedded;
                modified += 1;
                processed += 1;

                // Periodic memory pressure check (every MEMORY_CHECK_INTERVAL files).
                // Reading /proc/meminfo has measurable cost, so we don't do it per-file.
                if done % MEMORY_CHECK_INTERVAL == 0 {
                    let prev_pressure = under_pressure;
                    under_pressure = hardware::memory_pressure();
                    if hardware::memory_critical() {
                        // Under critical pressure (>90%): flush aggressively (every 8 files)
                        write_batch = 8;
                        tracing::warn!(
                            "memory critical ({:.1}% used), reducing write batch to {}",
                            hardware::MemoryUsage::read()
                                .map(|m| m.usage_percent())
                                .unwrap_or(0.0),
                            write_batch,
                        );
                    } else if under_pressure {
                        // Under pressure (>80%): flush sooner (every 32 files)
                        write_batch = 32;
                        if !prev_pressure {
                            tracing::warn!(
                                "memory pressure ({:.1}% used), reducing write batch to {}",
                                hardware::MemoryUsage::read()
                                    .map(|m| m.usage_percent())
                                    .unwrap_or(0.0),
                                write_batch,
                            );
                        }
                    } else if prev_pressure {
                        // Pressure lifted — restore default batch size
                        write_batch = write_batch_default;
                        tracing::info!(
                            "memory pressure relieved, restoring write batch to {}",
                            write_batch
                        );
                    }
                }

                // Flush when batch is full
                if files_to_add.len() >= write_batch {
                    if is_tcp && !under_pressure {
                        // Non-blocking: fire-and-forget to the write queue
                        flush_nonblocking!(write_queue, files_to_delete, files_to_add);
                    } else {
                        // Blocking: wait for queue space.
                        // Used in local mode (always), and TCP mode under memory pressure
                        // to force the write queue to drain before accepting more data.
                        flush_blocking!(write_queue, files_to_delete, files_to_add);
                        if !is_tcp {
                            if let Err(e) = write_queue.record_usage(pending_tokens, tier).await {
                                tracing::warn!("failed to queue usage recording: {}", e);
                            }
                            pending_tokens = 0;
                        }
                    }
                }

                // Progress logging every 10 files
                if !quiet && processed % 10 == 0 {
                    tracing::info!(
                        "progress: {}/{} files processed ({} embedded)",
                        processed,
                        total,
                        embedded
                    );
                }
            }
            Ok(None) => {}
            Err(e) => {
                tracing::error!("file indexing error: {}", e);
            }
        }

        // Throttle progress DB writes to every 50 files.
        // Each set_phase_progress does 4 LanceDB ops (2 keys × delete+add).
        // TCP mode: skip entirely during embed phase (writer handles it at the end).
        if !is_tcp && (done % 50 == 0 || done == total_to_process) {
            let _ = storage
                .set_phase_progress("chunking", done as i64, total_to_process as i64)
                .await;
            let _ = storage
                .set_phase_progress("embedding", done as i64, total_to_process as i64)
                .await;
        }

        if !quiet {
            if json_lines {
                emit_json(
                    true,
                    &serde_json::json!({
                        "type": "progress",
                        "phase": "embedding",
                        "done": processed,
                        "total": total,
                        "embeddings": embedded,
                    }),
                );
            } else if verbose {
                println!(
                    "[{}/{}] Processed, {} embeddings",
                    processed, total, embedded
                );
            }
        }
    }

    // Flush remaining buffered results.
    // For TCP mode: use blocking send for the final flush to ensure no data loss.
    // The embed phase is done so blocking here is fine — we're just waiting for
    // the writer to catch up.
    if is_tcp {
        // Final flush uses blocking send to guarantee delivery
        flush_blocking!(write_queue, files_to_delete, files_to_add);
        if pending_tokens > 0 {
            if let Err(e) = write_queue.record_usage(pending_tokens, tier).await {
                tracing::warn!("failed to queue final usage recording: {}", e);
            }
        }
        // Write progress at the end (was skipped during embed phase)
        let _ = storage
            .set_phase_progress("chunking", done as i64, total_to_process as i64)
            .await;
        let _ = storage
            .set_phase_progress("embedding", done as i64, total_to_process as i64)
            .await;
    } else {
        flush_blocking!(write_queue, files_to_delete, files_to_add);
        if pending_tokens > 0 {
            if let Err(e) = write_queue.record_usage(pending_tokens, tier).await {
                tracing::warn!("failed to queue usage recording: {}", e);
            }
        }
    }

    // Shutdown write queue and wait for all pending writes to complete
    let write_stats = write_queue.shutdown().await;
    if write_stats.errors > 0 {
        tracing::warn!(
            "WriteQueue completed with {} errors (wrote {} chunks, deleted {} files)",
            write_stats.errors,
            write_stats.chunks_written,
            write_stats.files_deleted
        );
    } else {
        tracing::debug!(
            "WriteQueue completed: {} batches, {} chunks written, {} files deleted",
            write_stats.batches_written,
            write_stats.chunks_written,
            write_stats.files_deleted
        );
    }

    // Persist authoritative file count. The WriteQueue batch-add path does not
    // track per-file new/existing state, so we set it once after all writes
    // complete. `processed` = unchanged + successfully indexed = total files now.
    let _ = storage.set_file_count(processed).await;

    // Build FTS index (fast, blocks briefly)
    if let Err(e) = storage.create_indexes(false).await {
        tracing::warn!("failed to create indexes: {e}");
    }

    // Record indexing duration BEFORE compaction (compaction is I/O-heavy
    // and not part of the embed pipeline — measuring it separately avoids
    // inflating the files/s metric).
    let elapsed = started.elapsed();
    storage
        .set_last_index_duration_ms((elapsed.as_millis() as i64).max(0))
        .await?;
    storage.set_last_index_files_count(processed as i64).await?;

    let now = chrono::Utc::now().to_rfc3339();
    storage.set_last_index_timestamp(&now).await?;
    storage.set_last_update_timestamp(&now).await?;

    storage.clear_indexing_progress().await?;

    // Compact in background: spawn a task so the caller gets results immediately.
    // Compaction merges LanceDB fragments for smaller on-disk size and faster queries,
    // but takes 1-3+ minutes for large projects. By deferring it, the reported
    // indexing speed reflects the actual embed pipeline throughput.
    let compact_storage = storage.clone();
    let compact_handle = tokio::spawn(async move {
        let t0 = std::time::Instant::now();
        if let Err(e) = compact_storage.compact().await {
            tracing::warn!("background compaction failed: {}", e);
        } else {
            tracing::info!(
                "background compaction completed in {:.1}s",
                t0.elapsed().as_secs_f64()
            );
        }
    });

    Ok(IndexStats {
        processed,
        modified,
        embedded,
        skipped_repos: discovery.skipped_repos,
        symlink_dirs: discovery.symlink_dirs,
        compact_handle: Some(compact_handle),
    })
}

async fn index_single_file(
    root: &Path,
    storage_path: &Path,
    file: &Path,
    tier: &str,
    dimensions: u32,
    force: bool,
    daily_cost_limit: Option<f64>,
    include: &[PathBuf],
    verbose: bool,
) -> Result<()> {
    // Note: No PID lock needed - LanceDB handles concurrent writes internally

    let path = if file.is_absolute() {
        file.to_path_buf()
    } else {
        root.join(file)
    };
    let path = tokio::fs::canonicalize(&path)
        .await
        .context("invalid file path")?;
    let meta = tokio::fs::metadata(&path)
        .await
        .context("failed to read file metadata")?;
    if !meta.is_file() {
        bail!("not a file: {}", path.display());
    }

    let storage = Storage::open(storage_path, dimensions).await?;
    let (embed_model, rerank_model) = models_for_tier(tier);
    storage.set_tier(tier).await?;
    storage.set_dimensions(dimensions).await?;
    storage
        .set_config("embedder_backend", "fastembed/v1")
        .await?;
    storage
        .set_config("embedder_embed_model", embed_model)
        .await?;
    storage
        .set_config("embedder_rerank_model", rerank_model)
        .await?;

    let include_dirs = resolve_include_dirs(root, include);
    let rel = relative_path(&path, root, &include_dirs);
    let content = tokio::fs::read_to_string(&path)
        .await
        .context("failed to read file")?;
    let hash = storage::hash_content(&content);

    if !force && !storage.needs_index(&rel, &hash).await? {
        if verbose {
            println!("unchanged: {rel}");
        }
        println!("Indexed: {}", path.display());
        return Ok(());
    }

    if let Some(limit) = daily_cost_limit {
        let usage = storage.get_daily_usage().await?;
        if usage.cost >= limit {
            println!(
                "Daily cost limit reached: ${:.4} / ${:.2}",
                usage.cost, limit
            );
            println!("Indexed: {}", path.display());
            return Ok(());
        }
    }

    let embed = Semaphore::new(1);
    let mut client = crate::model_client::client().await?;

    // Create WriteQueue for this single file operation
    let write_queue = crate::storage::WriteQueue::new(std::sync::Arc::new(storage.clone()), 32);

    let _ = update_file_partial(
        root,
        &include_dirs,
        &[], // No symlink dirs for single file indexing
        storage_path,
        &storage,
        &mut client,
        &path,
        tier,
        dimensions,
        daily_cost_limit,
        &embed,
        force,
        verbose,
        &write_queue,
    )
    .await?;

    // Wait for all writes to complete
    let _ = write_queue.shutdown().await;

    // Update last_update_timestamp after successful single-file indexing
    let now = chrono::Utc::now().to_rfc3339();
    storage.set_last_update_timestamp(&now).await?;

    // If this is the first file indexed, also set last_index_timestamp
    if storage.get_last_index_timestamp().await?.is_none() {
        storage.set_last_index_timestamp(&now).await?;
    }

    println!("Indexed: {}", path.display());
    Ok(())
}

// ============================================================================
// CLI display helpers — these functions are called by run_cli and format output
// for human-readable or JSON display.
// ============================================================================

async fn show_status(storage_path: &Path, dimensions: u32, json: bool) -> Result<()> {
    if !tokio::fs::try_exists(storage_path).await.unwrap_or(false) {
        if json {
            println!("{}", serde_json::json!({"exists": false}));
        } else {
            println!("No index found at {}", storage_path.display());
        }
        return Ok(());
    }

    let storage = Storage::open(storage_path, dimensions).await?;

    // Backfill missing metadata for legacy indexes
    match storage.backfill_metadata().await {
        Ok(count) if count > 0 => {
            tracing::info!("status: auto-fixed {} metadata field(s)", count);
        }
        Ok(_) => {}
        Err(e) => {
            tracing::warn!("status: metadata backfill failed: {}", e);
        }
    }

    // Self-heal stuck progress
    if storage.get_indexing_in_progress().await.unwrap_or(false) {
        let _ = storage.clear_indexing_progress().await;
    }

    let (chunks, files) = if let Some((cached_files, cached_chunks)) = storage.get_cached_counts().await {
        (cached_chunks, cached_files)
    } else {
        let chunks = storage.count_chunks().await.unwrap_or(0);
        let files = storage.get_indexed_files().await.map(|f| f.len()).unwrap_or(0);
        storage.update_cached_counts(files, chunks).await;
        (chunks, files)
    };

    let tier = storage.get_tier().await.unwrap_or(None);
    let indexing_in_progress = storage.get_indexing_in_progress().await.unwrap_or(false);
    let last_indexed = storage.get_last_index_timestamp().await.unwrap_or(None);
    let last_updated = storage.get_last_update_timestamp().await.unwrap_or(None)
        .or_else(|| last_indexed.clone());

    if json {
        println!(
            "{}",
            serde_json::json!({
                "exists": true,
                "files": files,
                "chunks": chunks,
                "tier": tier,
                "dimensions": dimensions,
                "indexingInProgress": indexing_in_progress,
                "lastIndexed": last_indexed,
                "lastUpdated": last_updated,
            })
        );
    } else {
        println!("Files: {}", files);
        println!("Chunks: {}", chunks);
        println!("Tier: {}", tier.as_deref().unwrap_or("unknown"));
        println!("Dimensions: {}", dimensions);
        println!("Status: {}", if indexing_in_progress { "indexing" } else { "idle" });
        if let Some(ts) = &last_indexed {
            println!("Last indexed: {}", ts);
        }
        if let Some(ts) = &last_updated {
            println!("Last updated: {}", ts);
        }
    }

    Ok(())
}

async fn show_files(storage_path: &Path, dimensions: u32, json: bool) -> Result<()> {
    if !tokio::fs::try_exists(storage_path).await.unwrap_or(false) {
        if json {
            println!("{}", serde_json::json!({"files": []}));
        } else {
            println!("No index found at {}", storage_path.display());
        }
        return Ok(());
    }

    let storage = Storage::open(storage_path, dimensions).await?;
    let files = storage.get_indexed_files().await?;
    let mut sorted: Vec<String> = files.into_iter().collect();
    sorted.sort();

    if json {
        println!("{}", serde_json::json!({"files": sorted}));
    } else {
        for f in &sorted {
            println!("{}", f);
        }
        println!("{} files indexed", sorted.len());
    }

    Ok(())
}

async fn discover_links(root: &Path, json_lines: bool) -> Result<()> {
    use crate::config;
    use crate::discover;
    use crate::storage;

    let root = root.canonicalize()?;
    let project = config::load(&root);
    let cfg = config::effective(&project, None, None);

    let discovery = discover::discover_files_with_config(&root, &cfg)?;

    let mut links: Vec<serde_json::Value> = Vec::new();

    for repo in &discovery.skipped_repos {
        let name = repo
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("unknown")
            .to_string();
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

    let submodules = discover::discover_submodules(&root);
    for (sub_path, name) in &submodules {
        if links.iter().any(|l| l["path"].as_str() == Some(sub_path.to_str().unwrap_or(""))) {
            continue;
        }
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

    let nested = discover::discover_nested_git_repos(&root);
    for (repo_path, name) in &nested {
        if links.iter().any(|l| l["path"].as_str() == Some(repo_path.to_str().unwrap_or(""))) {
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

    let result = serde_json::json!({
        "rootProjectId": storage::git_project_id(&root),
        "links": links,
    });

    if json_lines {
        println!("{}", result);
    } else {
        println!("{}", serde_json::to_string_pretty(&result)?);
    }

    Ok(())
}

async fn show_health(
    root: &Path,
    storage_path: &Path,
    dimensions: u32,
    json_lines: bool,
) -> Result<()> {
    let exists = tokio::fs::try_exists(storage_path).await.unwrap_or(false);

    let mut result = serde_json::json!({
        "healthy": true,
        "root": root.to_str().unwrap_or(""),
        "indexExists": exists,
        "dbPath": storage_path.to_str(),
        "errors": [],
    });

    if exists {
        if let Ok(storage) = Storage::open(storage_path, dimensions).await {
            match storage.backfill_metadata().await {
                Ok(count) if count > 0 => {
                    tracing::info!("health check: auto-fixed {} metadata field(s)", count);
                }
                Ok(_) => {}
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

            let last_indexed = storage.get_last_index_timestamp().await.unwrap_or(None);
            let last_updated = storage.get_last_update_timestamp().await.unwrap_or(None)
                .or_else(|| last_indexed.clone());
            result["lastIndexed"] = serde_json::json!(last_indexed);
            result["lastUpdated"] = serde_json::json!(last_updated);
        }

        let chunks_table = storage_path.join("chunks.lance");
        let config_table = storage_path.join("config.lance");
        let mut errors: Vec<String> = Vec::new();
        if !tokio::fs::metadata(&chunks_table).await.map(|m| m.is_dir()).unwrap_or(false) {
            errors.push("Missing chunks.lance table".into());
        }
        if !tokio::fs::metadata(&config_table).await.map(|m| m.is_dir()).unwrap_or(false) {
            errors.push("Missing config.lance table".into());
        }
        if !errors.is_empty() {
            result["healthy"] = serde_json::json!(false);
            result["errors"] = serde_json::json!(errors);
        }
    }

    if json_lines {
        println!("{}", result);
    } else {
        println!("{}", serde_json::to_string_pretty(&result)?);
    }

    Ok(())
}

async fn run_search(
    storage_path: &Path,
    query: &str,
    tier: &str,
    dimensions: u32,
    json_lines: bool,
    federated_db: &[PathBuf],
) -> Result<()> {
    use crate::model_client;

    if !tokio::fs::try_exists(storage_path).await.unwrap_or(false) {
        if json_lines {
            println!("{}", serde_json::json!({"results": []}));
        } else {
            println!("No index found at {}", storage_path.display());
        }
        return Ok(());
    }

    let storage = Storage::open(storage_path, dimensions).await?;
    let stored_tier = storage.get_tier().await?.unwrap_or_else(|| tier.to_string());
    let stored_dims = storage.get_dimensions().await?.unwrap_or(dimensions);
    let storage = Storage::open(storage_path, stored_dims).await?;

    let mut client = model_client::client().await?;
    let (embed_model, rerank_model) = models_for_tier(&stored_tier);

    let qvec = client.embed_query(query, embed_model, stored_dims).await?;
    let candidates = storage.search_hybrid(query, &qvec, 20).await?;

    // Per-project rerank
    let rerank_limit = 10u32;
    let docs: Vec<&str> = candidates.iter().map(|r| r.content.as_str()).collect();
    let ranked = if docs.is_empty() {
        Vec::new()
    } else {
        client.rerank(query, &docs, rerank_model, rerank_limit).await?
    };

    // Also search federated DBs
    let mut all_results: Vec<(f64, String, String)> = ranked
        .iter()
        .filter_map(|(idx, score)| {
            candidates.get(*idx).map(|r| {
                (*score as f64, r.path.clone(), r.content.clone())
            })
        })
        .collect();

    for fed_path in federated_db {
        if !tokio::fs::try_exists(fed_path).await.unwrap_or(false) {
            continue;
        }
        if let Ok(fed) = Storage::open(fed_path, stored_dims).await {
            let fed_tier = fed.get_tier().await?.unwrap_or_else(|| stored_tier.clone());
            let fed_dims = fed.get_dimensions().await?.unwrap_or(stored_dims);
            let fed = Storage::open(fed_path, fed_dims).await?;
            let (fed_embed, _) = models_for_tier(&fed_tier);
            let fvec = client.embed_query(query, fed_embed, fed_dims).await?;
            let fed_candidates = fed.search_hybrid(query, &fvec, 10).await?;
            for r in fed_candidates {
                all_results.push((r.score as f64, r.path, r.content));
            }
        }
    }

    // Sort by score
    all_results.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
    all_results.truncate(10);

    if json_lines {
        let results: Vec<serde_json::Value> = all_results.iter().enumerate().map(|(i, (score, path, content))| {
            serde_json::json!({"rank": i + 1, "score": score, "path": path, "content": content})
        }).collect();
        println!("{}", serde_json::json!({"results": results}));
    } else {
        println!("Search results for: {}", query);
        println!();
        for (i, (score, path, content)) in all_results.iter().enumerate() {
            println!("{}. {} (score: {:.4})", i + 1, path, score);
            // Print first 2 lines of content as preview
            let preview: Vec<&str> = content.lines().take(2).collect();
            for line in preview {
                println!("   {}", line);
            }
            println!();
        }
    }

    Ok(())
}

async fn remove_file(
    storage_path: &Path,
    path: &Path,
    root: &Path,
    include: &[PathBuf],
    dimensions: u32,
) -> Result<()> {
    use crate::discover::relative_path;

    let include_dirs = resolve_include_dirs(root, include);
    let storage = Storage::open(storage_path, dimensions).await?;
    let rel = relative_path(path, root, &include_dirs);

    let chunks = storage.get_chunks_with_hashes(&rel).await?;
    let removed = chunks.len();

    let write_queue = WriteQueue::new(std::sync::Arc::new(storage), 32);
    write_queue.delete_file(&rel).await;
    let _ = write_queue.shutdown().await;

    println!("Removed {} chunk(s) for: {}", removed, rel);
    Ok(())
}

async fn run_dry_run(root: &Path, exclude: &[String], include: &[PathBuf]) -> Result<()> {
    let project = config::load(root);
    let mut cfg = config::effective(&project, None, None);
    cfg.exclude.extend(exclude.iter().cloned());

    let mut discovery = discover::discover_files_with_config(root, &cfg)?;
    let include_dirs = resolve_include_dirs(root, include);
    let mut seen: HashSet<PathBuf> = discovery
        .files
        .iter()
        .filter_map(|p| p.canonicalize().ok())
        .collect();
    discovery.files.extend(discover::discover_additional_files(
        &include_dirs,
        if exclude.is_empty() { None } else { Some(exclude) },
        &mut seen,
    ));

    println!("Dry run: {} files would be indexed", discovery.files.len());
    for f in &discovery.files {
        println!("  {}", f.display());
    }

    Ok(())
}

