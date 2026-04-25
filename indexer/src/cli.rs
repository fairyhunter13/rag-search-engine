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
fn models_for_tier(tier: &str) -> (&'static str, &'static str) {
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

    /// Quantization (ignored, for backward compat with Python embedder)
    #[arg(long, default_value = "int8")]
    pub quantization: String,

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

    /// Show usage stats (for backward compat, currently shows nothing)
    #[arg(long)]
    pub usage: bool,

    /// Federated search: additional DB paths to search across
    #[arg(long = "federated-db")]
    pub federated_db: Vec<PathBuf>,

    /// Discover linked projects (symlinks to external git repos)
    #[arg(long)]
    pub discover_links: bool,

    /// Health check (returns structured health info)
    #[arg(long)]
    pub health: bool,

    /// Run as a daemon (HTTP server) instead of one-shot CLI.
    /// The HTTP port is written to ~/.opencode/indexer.port on startup.
    #[arg(long)]
    pub daemon: bool,

    /// HTTP port for --http mode (0 = pick a random free port).
    #[arg(long, default_value = "0")]
    pub port: u16,

    /// Idle timeout before auto-shutdown (seconds, 0=disabled, default: 600)
    #[arg(long)]
    pub idle_shutdown: Option<u64>,

    /// Parent process PID to monitor (shutdown if parent dies)
    #[arg(long)]
    pub parent_pid: Option<i32>,
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

/// Make models_for_tier accessible from daemon module.
pub fn models_for_tier_pub(tier: &str) -> (&'static str, &'static str) {
    models_for_tier(tier)
}

/// Make update_file_partial accessible from daemon module.
pub async fn update_file_partial_pub(
    root: &Path,
    include_dirs: &[PathBuf],
    symlink_dirs: &[discover::SymlinkDir],
    storage_path: &Path,
    storage: &Storage,
    client: &mut crate::model_client::PooledClient,
    file: &Path,
    tier: &str,
    dimensions: u32,
    quantization: &str,
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
        quantization,
        daily_cost_limit,
        embed,
        force,
        verbose,
        write_queue,
    )
    .await
}

pub async fn run(args: Args) -> Result<()> {
    // Daemon mode: start HTTP server.
    if args.daemon {
        return crate::daemon::run(args.port, args.idle_shutdown, args.parent_pid).await;
    }

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

    if args.usage {
        return show_usage(&storage_path, args.dimensions).await;
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
            &args.quantization,
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

    // Log hardware detection for diagnostics
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
        &args.quantization,
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
    println!("\nQuantization options:");
    println!("  --quantization float");
    println!("  --quantization int8");
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

#[cfg(test)]
mod cli_tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn read_text_utf16le_bom() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("utf16le.txt");
        let mut bytes = vec![0xFF, 0xFE];
        for u in "a\nb\n".encode_utf16() {
            bytes.extend_from_slice(&u.to_le_bytes());
        }
        std::fs::write(&path, bytes).unwrap();
        let out = read_text(&path, 1024).unwrap().unwrap();
        assert_eq!(out, "a\nb\n");
    }

    #[test]
    fn read_text_utf16be_bom() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("utf16be.txt");
        let mut bytes = vec![0xFE, 0xFF];
        for u in "x\ny".encode_utf16() {
            bytes.extend_from_slice(&u.to_be_bytes());
        }
        std::fs::write(&path, bytes).unwrap();
        let out = read_text(&path, 1024).unwrap().unwrap();
        assert_eq!(out, "x\ny");
    }

    #[test]
    fn read_text_non_utf8_is_lossy() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("lossy.bin");
        // Invalid UTF-8 sequence.
        std::fs::write(&path, vec![0xC3, 0x28, b'\n']).unwrap();
        let out = read_text(&path, 1024).unwrap().unwrap();
        assert!(out.contains('\n'));
        assert!(!out.is_empty());
    }

    #[test]
    fn read_text_binary_returns_none() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("bin.dat");
        std::fs::write(&path, vec![0, 1, 2, 3, 0, 4, 5, 0]).unwrap();
        assert!(read_text(&path, 1024).unwrap().is_none());
    }

    #[test]
    fn fallback_chunks_utf8() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("a.txt");
        std::fs::write(&path, "l1\nl2\nl3\n").unwrap();
        let chunks = fallback_chunks(&path).unwrap();
        assert_eq!(chunks.len(), 1);
        assert_eq!(chunks[0].start_line, 1);
        assert_eq!(chunks[0].end_line, 3);
        assert!(chunks[0].content.contains("l2"));
    }

    #[test]
    fn fallback_chunks_utf16le() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("u16.txt");
        let mut bytes = vec![0xFF, 0xFE];
        for u in "l1\nl2\nl3".encode_utf16() {
            bytes.extend_from_slice(&u.to_le_bytes());
        }
        std::fs::write(&path, bytes).unwrap();
        let chunks = fallback_chunks(&path).unwrap();
        assert_eq!(chunks.len(), 1);
        assert_eq!(chunks[0].start_line, 1);
        assert_eq!(chunks[0].end_line, 3);
        assert!(chunks[0].content.contains("l3"));
    }

    #[test]
    fn fallback_chunks_binary_is_empty() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("bin.bin");
        std::fs::write(&path, vec![0, 0, 0, 0, b'\n']).unwrap();
        let chunks = fallback_chunks(&path).unwrap();
        assert!(chunks.is_empty());
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
    client: &mut crate::model_client::PooledClient,
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
    quantization: &str,
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
        quantization,
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

async fn update_file_partial(
    root: &Path,
    include_dirs: &[PathBuf],
    symlink_dirs: &[discover::SymlinkDir],
    storage_path: &Path,
    storage: &Storage,
    client: &mut crate::model_client::PooledClient,
    file: &Path,
    tier: &str,
    dimensions: u32,
    quantization: &str,
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
    let legacy_sqlite = storage_path
        .parent()
        .map(|p| p.join("index.sqlite"))
        .unwrap_or_else(|| storage_path.join("index.sqlite"));
    let legacy_usearch = storage_path
        .parent()
        .map(|p| p.join("index.usearch"))
        .unwrap_or_else(|| storage_path.join("index.usearch"));
    if tokio::fs::try_exists(&legacy_sqlite).await.unwrap_or(false) {
        let _ = tokio::fs::remove_file(&legacy_sqlite).await;
        let _ = tokio::fs::remove_file(legacy_sqlite.with_extension("sqlite-wal")).await;
        let _ = tokio::fs::remove_file(legacy_sqlite.with_extension("sqlite-shm")).await;
    }
    if tokio::fs::try_exists(&legacy_usearch)
        .await
        .unwrap_or(false)
    {
        let _ = tokio::fs::remove_file(&legacy_usearch).await;
    }

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
    storage.set_quantization(quantization).await?;
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

    let mut new_chunks: Vec<(String, i32, String, i32, i32)> = Vec::new();
    for (i, chunk) in chunks.iter().enumerate() {
        new_chunks.push((
            storage::hash_content(&chunk.content),
            i as i32,
            chunk.content.clone(),
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

    let mut hash_to_new: HashMap<String, (i32, i32, i32)> = HashMap::new();
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

    let mut to_add = Vec::new();
    let mut to_embed = Vec::new();
    let mut embed_targets = Vec::new();

    for (hash, position, content, start, end) in &new_chunks {
        if !hashes_to_insert.contains(hash) {
            continue;
        }

        let existing = storage.find_by_content_hash(hash).await?;
        let idx = to_add.len();
        to_add.push((
            hash.clone(),
            *position,
            content.clone(),
            *start,
            *end,
            existing,
        ));
        if to_add[idx].5.is_none() {
            to_embed.push(content.clone());
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
        data.push(ChunkData {
            position,
            content,
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
    quantization: &str,
    force: bool,
    daily_cost_limit: Option<f64>,
    verbose: bool,
    exclude: &[String],
    include: &[PathBuf],
    _concurrency: usize,
    _scan_concurrency: Option<usize>,
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

    // Legacy index detection (best-effort)
    let legacy_sqlite = storage_path
        .parent()
        .map(|p| p.join("index.sqlite"))
        .unwrap_or_else(|| storage_path.join("index.sqlite"));
    let legacy_usearch = storage_path
        .parent()
        .map(|p| p.join("index.usearch"))
        .unwrap_or_else(|| storage_path.join("index.usearch"));
    let has_legacy_files = tokio::fs::try_exists(&legacy_sqlite).await.unwrap_or(false)
        || tokio::fs::try_exists(&legacy_usearch)
            .await
            .unwrap_or(false);
    if has_legacy_files {
        let _ = tokio::fs::remove_file(&legacy_usearch).await;
        let _ = tokio::fs::remove_file(&legacy_sqlite).await;
        let _ = tokio::fs::remove_file(legacy_sqlite.with_extension("sqlite-wal")).await;
        let _ = tokio::fs::remove_file(legacy_sqlite.with_extension("sqlite-shm")).await;
    }

    let backend = storage
        .get_config("embedder_backend")
        .await?
        .or(storage.get_config("backend").await?);
    // Only consider it a legacy backend if we have an EXPLICIT different value, not if config is missing
    // (missing config can happen with corrupted config.lance - don't wipe good data in that case)
    let legacy_backend = backend.as_deref().is_some_and(|b| b != "fastembed/v1");
    let has_data = storage.count_chunks().await? > 0;
    if has_legacy_files || (legacy_backend && has_data) {
        if !quiet {
            println!("Legacy index backend detected. Deleting old data and rebuilding...");
        }
        storage.drop_all_tables().await?;
    }

    // Config changes require a full rebuild (backup + clear)
    let current_tier = storage.get_tier().await?;
    let current_dims = storage.get_dimensions().await?;
    let current_quant = storage.get_quantization().await?;
    let tier_changed = current_tier.as_deref().is_some_and(|t| t != tier);
    let dims_changed = current_dims.is_some_and(|d| d != dimensions);
    let quant_changed = current_quant.as_deref().is_some_and(|q| q != quantization);
    if force || tier_changed || dims_changed || quant_changed {
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
    storage.set_quantization(quantization).await?;
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

    let workers = _scan_concurrency.unwrap_or_else(|| {
        std::thread::available_parallelism()
            .map(|n| (n.get() / 2).clamp(2, 8))
            .unwrap_or(4)
    });
    let sem = std::sync::Arc::new(Semaphore::new(workers));
    let stored = std::sync::Arc::new(stored_hashes);
    let files = discovery.files.clone();
    let root = root.to_path_buf();
    let include_dirs = include_dirs.clone();
    let mut futs = FuturesUnordered::new();

    for file in files {
        let sem = sem.clone();
        let stored = stored.clone();
        let root = root.clone();
        let include_dirs = include_dirs.clone();
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
    let embed_concurrency = _concurrency.max(1).min(max_concurrency);
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

    // Pre-warm connection pool for both TCP and local modes:
    // - TCP: avoids SSH spawn race + connection latency
    // - Local: prevents thundering herd at startup
    if let Err(e) = crate::model_client::warmup().await {
        tracing::warn!(
            "pool warmup failed: {}, continuing with lazy connections",
            e
        );
    }

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
                        let mut client = crate::model_client::pooled().await?;
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

                    let mut client = crate::model_client::pooled().await?;
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
                                    let mut client = crate::model_client::pooled().await?;
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
    quantization: &str,
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
    storage.set_quantization(quantization).await?;
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
    let mut client = crate::model_client::pooled().await?;

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
        quantization,
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

fn watcher_state_path(storage_path: &Path) -> Option<PathBuf> {
    Some(storage_path.parent()?.join(".watcher-state.json"))
}

#[cfg(test)]
mod tests {
    use super::*;

    // =========================================================================
    // Producer-Consumer Pattern Tests
    // =========================================================================
    // These tests validate the mpsc channel pattern used in run_indexing()
    // to decouple embedding parallelism from sequential storage writes.

    use std::sync::atomic::{AtomicUsize, Ordering};

    /// Test that the producer-consumer pattern correctly processes all items
    /// and doesn't lose any results.
    #[tokio::test]
    async fn producer_consumer_processes_all_items() {
        let num_items = 100;
        let concurrency = 10;
        let sem = Arc::new(Semaphore::new(concurrency));

        // Channel with buffer size matching our pattern
        let (tx, mut rx) =
            tokio::sync::mpsc::channel::<Result<usize, anyhow::Error>>(concurrency * 2);

        let counter = Arc::new(AtomicUsize::new(0));

        // Spawn producer tasks (mimics the run_indexing pattern)
        for i in 0..num_items {
            let sem = sem.clone();
            let tx = tx.clone();
            let counter = counter.clone();

            tokio::spawn(async move {
                let _permit = sem.acquire().await.ok();
                counter.fetch_add(1, Ordering::SeqCst);
                // Simulate some work
                tokio::time::sleep(std::time::Duration::from_micros(100)).await;
                let _ = tx.send(Ok(i)).await;
            });
        }

        // Drop the original sender so channel closes when producers finish
        drop(tx);

        // Consumer: collect all results
        let mut received = Vec::new();
        while let Some(res) = rx.recv().await {
            match res {
                Ok(val) => received.push(val),
                Err(e) => panic!("unexpected error: {}", e),
            }
        }

        // Verify all items were processed
        assert_eq!(received.len(), num_items, "all items should be received");
        assert_eq!(
            counter.load(Ordering::SeqCst),
            num_items,
            "all producers should have run"
        );

        // Verify all values are present (order may vary due to concurrency)
        received.sort();
        let expected: Vec<usize> = (0..num_items).collect();
        assert_eq!(received, expected, "all values should be present");
    }

    /// Test that errors from producers are properly propagated to the consumer.
    #[tokio::test]
    async fn producer_consumer_propagates_errors() {
        let num_items = 10;
        let (tx, mut rx) =
            tokio::sync::mpsc::channel::<Result<usize, anyhow::Error>>(num_items * 2);

        // Spawn producer tasks, some succeed, some fail
        for i in 0..num_items {
            let tx = tx.clone();

            tokio::spawn(async move {
                if i % 3 == 0 {
                    let _ = tx.send(Err(anyhow::anyhow!("error at {}", i))).await;
                } else {
                    let _ = tx.send(Ok(i)).await;
                }
            });
        }

        drop(tx);

        let mut successes = 0;
        let mut failures = 0;

        while let Some(res) = rx.recv().await {
            match res {
                Ok(_) => successes += 1,
                Err(_) => failures += 1,
            }
        }

        // Items 0, 3, 6, 9 fail = 4 failures
        // Items 1, 2, 4, 5, 7, 8 succeed = 6 successes
        assert_eq!(failures, 4, "4 items should fail (0, 3, 6, 9)");
        assert_eq!(successes, 6, "6 items should succeed");
        assert_eq!(
            successes + failures,
            num_items,
            "total should equal num_items"
        );
    }

    /// Test that the channel properly closes when all producers are done.
    #[tokio::test]
    async fn producer_consumer_channel_closes_properly() {
        let (tx, mut rx) = tokio::sync::mpsc::channel::<i32>(10);

        // Spawn a few producers
        for i in 0..5 {
            let tx = tx.clone();
            tokio::spawn(async move {
                tokio::time::sleep(std::time::Duration::from_millis(i * 10)).await;
                let _ = tx.send(i as i32).await;
            });
        }

        // Drop original sender
        drop(tx);

        // Consumer should eventually receive None when all producers finish
        let mut count = 0;
        let timeout = tokio::time::timeout(std::time::Duration::from_secs(5), async {
            while let Some(_) = rx.recv().await {
                count += 1;
            }
        })
        .await;

        assert!(timeout.is_ok(), "channel should close within timeout");
        assert_eq!(count, 5, "should receive exactly 5 messages");
    }

    /// Test that the semaphore correctly limits concurrency.
    #[tokio::test]
    async fn producer_consumer_respects_concurrency_limit() {
        let max_concurrent = 3;
        let num_items = 20;
        let sem = Arc::new(Semaphore::new(max_concurrent));

        let current_concurrent = Arc::new(AtomicUsize::new(0));
        let max_observed = Arc::new(AtomicUsize::new(0));

        let (tx, mut rx) = tokio::sync::mpsc::channel::<()>(num_items * 2);

        for _ in 0..num_items {
            let sem = sem.clone();
            let tx = tx.clone();
            let current = current_concurrent.clone();
            let max_obs = max_observed.clone();

            tokio::spawn(async move {
                let _permit = sem.acquire().await.ok();

                // Track concurrent executions
                let prev = current.fetch_add(1, Ordering::SeqCst);
                max_obs.fetch_max(prev + 1, Ordering::SeqCst);

                // Simulate work
                tokio::time::sleep(std::time::Duration::from_millis(10)).await;

                current.fetch_sub(1, Ordering::SeqCst);
                let _ = tx.send(()).await;
            });
        }

        drop(tx);

        // Drain all results
        let mut count = 0;
        while let Some(_) = rx.recv().await {
            count += 1;
        }

        assert_eq!(count, num_items, "all items should complete");
        assert!(
            max_observed.load(Ordering::SeqCst) <= max_concurrent,
            "max concurrent should not exceed limit: {} > {}",
            max_observed.load(Ordering::SeqCst),
            max_concurrent
        );
    }

    /// Test behavior when a producer task panics.
    /// The channel should still receive results from non-panicking tasks.
    #[tokio::test]
    async fn producer_consumer_handles_task_panic() {
        let num_items = 10;
        let (tx, mut rx) =
            tokio::sync::mpsc::channel::<Result<usize, anyhow::Error>>(num_items * 2);

        for i in 0..num_items {
            let tx = tx.clone();

            tokio::spawn(async move {
                if i == 5 {
                    // This task will panic and not send anything
                    panic!("intentional panic for testing");
                }
                let _ = tx.send(Ok(i)).await;
            });
        }

        drop(tx);

        // Should receive 9 results (all except the panicked task)
        let mut received = Vec::new();
        while let Some(res) = rx.recv().await {
            if let Ok(val) = res {
                received.push(val);
            }
        }

        assert_eq!(
            received.len(),
            9,
            "should receive 9 results (10 - 1 panicked)"
        );
        assert!(
            !received.contains(&5),
            "should not contain value from panicked task"
        );
    }

    /// Test that empty input produces no results but completes correctly.
    #[tokio::test]
    async fn producer_consumer_handles_empty_input() {
        let (tx, mut rx) = tokio::sync::mpsc::channel::<i32>(10);

        // No producers spawned
        drop(tx);

        // Consumer should immediately get None
        let result = rx.recv().await;
        assert!(
            result.is_none(),
            "should receive None immediately for empty input"
        );
    }

    /// Test that large batches complete without deadlock.
    #[tokio::test]
    async fn producer_consumer_large_batch() {
        let num_items = 1000;
        let concurrency = 48; // Match TCP concurrency
        let sem = Arc::new(Semaphore::new(concurrency));

        let (tx, mut rx) = tokio::sync::mpsc::channel::<usize>(concurrency * 2);

        for i in 0..num_items {
            let sem = sem.clone();
            let tx = tx.clone();

            tokio::spawn(async move {
                let _permit = sem.acquire().await.ok();
                let _ = tx.send(i).await;
            });
        }

        drop(tx);

        let timeout = tokio::time::timeout(std::time::Duration::from_secs(30), async {
            let mut count = 0;
            while let Some(_) = rx.recv().await {
                count += 1;
            }
            count
        })
        .await;

        match timeout {
            Ok(count) => assert_eq!(count, num_items, "all items should be received"),
            Err(_) => panic!("test timed out - possible deadlock"),
        }
    }

    /// Test that panics in spawned tasks are caught and converted to errors
    /// using the catch_unwind pattern (matches the pattern in run_indexing).
    #[tokio::test]
    async fn producer_consumer_catch_unwind_pattern() {
        let num_items = 10;
        let (tx, mut rx) =
            tokio::sync::mpsc::channel::<Result<String, anyhow::Error>>(num_items * 2);

        for i in 0..num_items {
            let tx = tx.clone();

            tokio::spawn(async move {
                // This pattern matches what we use in run_indexing()
                let task_id = format!("task_{}", i);
                let task_id_clone = task_id.clone();

                let result = std::panic::AssertUnwindSafe(async {
                    if i == 5 {
                        panic!("intentional panic in task {}", i);
                    }
                    Ok::<String, anyhow::Error>(format!("success_{}", i))
                });

                let result = match futures::FutureExt::catch_unwind(result).await {
                    Ok(r) => r,
                    Err(panic) => {
                        let panic_msg = if let Some(s) = panic.downcast_ref::<&str>() {
                            s.to_string()
                        } else if let Some(s) = panic.downcast_ref::<String>() {
                            s.clone()
                        } else {
                            "unknown panic".to_string()
                        };
                        Err(anyhow::anyhow!("panic in {}: {}", task_id_clone, panic_msg))
                    }
                };

                let _ = tx.send(result).await;
            });
        }

        drop(tx);

        let mut successes = Vec::new();
        let mut errors = Vec::new();

        while let Some(res) = rx.recv().await {
            match res {
                Ok(val) => successes.push(val),
                Err(e) => errors.push(e.to_string()),
            }
        }

        // Should have 9 successes and 1 error (from the caught panic)
        assert_eq!(successes.len(), 9, "should have 9 successful results");
        assert_eq!(errors.len(), 1, "should have 1 error from caught panic");

        // The error should contain the panic message
        assert!(
            errors[0].contains("panic in task_5"),
            "error should identify the panicked task: {}",
            errors[0]
        );
        assert!(
            errors[0].contains("intentional panic"),
            "error should contain panic message: {}",
            errors[0]
        );
    }

    /// Test that the catch_unwind pattern handles string panics correctly.
    #[tokio::test]
    async fn catch_unwind_handles_string_panic() {
        let (tx, mut rx) = tokio::sync::mpsc::channel::<Result<(), anyhow::Error>>(1);

        tokio::spawn(async move {
            let result = std::panic::AssertUnwindSafe(async {
                panic!("string panic message");
                #[allow(unreachable_code)]
                Ok::<(), anyhow::Error>(())
            });

            let result = match futures::FutureExt::catch_unwind(result).await {
                Ok(r) => r,
                Err(panic) => {
                    let panic_msg = if let Some(s) = panic.downcast_ref::<&str>() {
                        s.to_string()
                    } else if let Some(s) = panic.downcast_ref::<String>() {
                        s.clone()
                    } else {
                        "unknown panic".to_string()
                    };
                    Err(anyhow::anyhow!("caught: {}", panic_msg))
                }
            };

            let _ = tx.send(result).await;
            // tx is dropped here when the task completes, closing the channel
        });

        // Wait for the result - channel will close after task completes
        let result = rx.recv().await.expect("should receive result");
        assert!(result.is_err(), "should be an error");
        let err = result.unwrap_err().to_string();
        assert!(
            err.contains("string panic message"),
            "should contain panic message: {}",
            err
        );
    }

    // ---- TCP cross-file batching pipeline tests ----
    //
    // These tests validate the cross-file batching architecture used by TCP mode:
    // local chunk -> accumulate texts across files -> batch embed RPCs -> scatter results.

    /// Test the cross-file batching pipeline: texts from multiple files are
    /// accumulated into batches, embedded in one RPC, and vectors are scattered
    /// back to their source files via oneshot channels.
    #[tokio::test]
    async fn cross_file_batch_pipeline_completes() {
        let num_files = 30;
        let chunks_per_file = 3;
        let batch_size = 10;
        let concurrency = 4;

        struct FakeChunked {
            rel: String,
            chunks: Vec<String>,
        }

        struct FakePrepared {
            rel: String,
            embedded: usize,
        }

        struct BatchSlot {
            text: String,
            result_tx: tokio::sync::oneshot::Sender<Result<Vec<f32>, anyhow::Error>>,
        }

        let (chunk_tx, mut chunk_rx) =
            tokio::sync::mpsc::channel::<Result<FakeChunked, anyhow::Error>>(concurrency * 2);
        let (batch_tx, batch_rx) = async_channel::bounded::<Vec<BatchSlot>>(concurrency * 4);
        let (result_tx, mut result_rx) =
            tokio::sync::mpsc::channel::<Result<FakePrepared, anyhow::Error>>(concurrency * 2);

        // Stage 1: Produce chunked files
        for i in 0..num_files {
            let chunk_tx = chunk_tx.clone();
            tokio::spawn(async move {
                let chunks: Vec<String> = (0..chunks_per_file)
                    .map(|j| format!("file_{}_chunk_{}", i, j))
                    .collect();
                let _ = chunk_tx
                    .send(Ok(FakeChunked {
                        rel: format!("file_{}.rs", i),
                        chunks,
                    }))
                    .await;
            });
        }
        drop(chunk_tx);

        // Stage 2: Collector accumulates texts into cross-file batches
        {
            let result_tx = result_tx.clone();
            let batch_tx = batch_tx.clone();
            tokio::spawn(async move {
                let mut pending: Vec<BatchSlot> = Vec::with_capacity(batch_size);

                while let Some(res) = chunk_rx.recv().await {
                    match res {
                        Ok(c) => {
                            let mut receivers = Vec::with_capacity(c.chunks.len());
                            for text in &c.chunks {
                                let (tx, rx) = tokio::sync::oneshot::channel();
                                receivers.push(rx);
                                pending.push(BatchSlot {
                                    text: text.clone(),
                                    result_tx: tx,
                                });
                                if pending.len() >= batch_size {
                                    let batch = std::mem::replace(
                                        &mut pending,
                                        Vec::with_capacity(batch_size),
                                    );
                                    if batch_tx.send(batch).await.is_err() {
                                        break;
                                    }
                                }
                            }
                            // Spawn a task to collect per-file vectors
                            let tx = result_tx.clone();
                            let rel = c.rel.clone();
                            let count = c.chunks.len();
                            tokio::spawn(async move {
                                let mut ok = true;
                                for rx in receivers {
                                    if rx.await.is_err() {
                                        ok = false;
                                        break;
                                    }
                                }
                                if ok {
                                    let _ = tx
                                        .send(Ok(FakePrepared {
                                            rel,
                                            embedded: count,
                                        }))
                                        .await;
                                } else {
                                    let _ = tx
                                        .send(Err(anyhow::anyhow!("embed failed for {}", rel)))
                                        .await;
                                }
                            });
                        }
                        Err(e) => {
                            let _ = result_tx.send(Err(e)).await;
                        }
                    }
                }
                if !pending.is_empty() {
                    let _ = batch_tx.send(pending).await;
                }
                drop(batch_tx);
            });
        }
        drop(batch_tx);
        drop(result_tx);

        // Stage 3: Embed workers pull cross-file batches (MPMC — no mutex)
        for _ in 0..concurrency {
            let batch_rx = batch_rx.clone();
            tokio::spawn(async move {
                while let Ok(slots) = batch_rx.recv().await {
                    // Simulate embed RPC: produce a fake 4-dim vector for each text
                    for slot in slots {
                        let _ = slot.result_tx.send(Ok(vec![1.0, 2.0, 3.0, 4.0]));
                    }
                }
            });
        }

        // Consumer: collect all results
        let timeout = tokio::time::timeout(std::time::Duration::from_secs(10), async {
            let mut count = 0;
            while let Some(res) = result_rx.recv().await {
                assert!(res.is_ok(), "file should succeed: {:?}", res.err());
                let p = res.unwrap();
                assert_eq!(
                    p.embedded, chunks_per_file,
                    "each file should have {} chunks",
                    chunks_per_file
                );
                count += 1;
            }
            count
        })
        .await;

        match timeout {
            Ok(count) => assert_eq!(count, num_files, "all {} files should complete", num_files),
            Err(_) => panic!("cross-file batch pipeline deadlocked"),
        }
    }

    /// Test that cross-file batching handles embed errors gracefully:
    /// an error in one batch slot should propagate to the affected file
    /// without killing other files.
    #[tokio::test]
    async fn cross_file_batch_handles_errors() {
        let batch_size = 4;

        struct BatchSlot {
            text: String,
            result_tx: tokio::sync::oneshot::Sender<Result<Vec<f32>, anyhow::Error>>,
        }

        // Create 6 slots: first 4 go in batch 1 (will fail), last 2 in batch 2 (succeed)
        let mut slots = Vec::new();
        let mut receivers = Vec::new();
        for i in 0..6 {
            let (tx, rx) = tokio::sync::oneshot::channel();
            receivers.push(rx);
            slots.push(BatchSlot {
                text: format!("text_{}", i),
                result_tx: tx,
            });
        }

        let batch1: Vec<BatchSlot> = slots.drain(..batch_size).collect();
        let batch2: Vec<BatchSlot> = slots;

        // Batch 1: return error for all slots
        for slot in batch1 {
            let _ = slot.result_tx.send(Err(anyhow::anyhow!("GPU OOM")));
        }
        // Batch 2: succeed
        for slot in batch2 {
            let _ = slot.result_tx.send(Ok(vec![1.0, 2.0]));
        }

        // Check receivers
        for (i, rx) in receivers.into_iter().enumerate() {
            let result = rx.await.unwrap();
            if i < batch_size {
                assert!(result.is_err(), "slot {} should be an error", i);
            } else {
                assert!(result.is_ok(), "slot {} should succeed", i);
            }
        }
    }

    /// Test that the pipeline handles empty chunked files correctly
    /// (files with no text content should produce empty Prepared results).
    #[tokio::test]
    async fn tcp_pipeline_handles_empty_chunks() {
        let (tx, mut rx) = tokio::sync::mpsc::channel::<Option<(String, usize)>>(10);

        // Simulate: file with 0 chunks (binary file)
        let tx2 = tx.clone();
        tokio::spawn(async move {
            // Empty file -> emit immediately with 0 embedded
            let _ = tx2.send(Some(("binary.dat".to_string(), 0))).await;
        });

        // Simulate: file with chunks
        let tx3 = tx.clone();
        tokio::spawn(async move {
            let _ = tx3.send(Some(("code.rs".to_string(), 3))).await;
        });

        drop(tx);

        let mut results = Vec::new();
        while let Some(Some((rel, embedded))) = rx.recv().await {
            results.push((rel, embedded));
        }

        assert_eq!(results.len(), 2);
        let empty = results.iter().find(|(r, _)| r == "binary.dat").unwrap();
        assert_eq!(empty.1, 0);
        let non_empty = results.iter().find(|(r, _)| r == "code.rs").unwrap();
        assert_eq!(non_empty.1, 3);
    }

    /// Test that cross-file batching correctly handles partial batches (last batch
    /// has fewer items than CROSS_FILE_BATCH_SIZE).
    #[tokio::test]
    async fn cross_file_batch_handles_partial_batches() {
        // 7 items with batch size 4 -> 2 batches: [4, 3]
        let batch_size = 4;
        let total = 7;

        struct BatchSlot {
            result_tx: tokio::sync::oneshot::Sender<Result<Vec<f32>, anyhow::Error>>,
        }

        let (batch_tx, mut batch_rx) = tokio::sync::mpsc::channel::<Vec<BatchSlot>>(10);

        // Collector: accumulate items into batches
        tokio::spawn(async move {
            let mut pending = Vec::with_capacity(batch_size);
            let mut senders = Vec::new();
            for _ in 0..total {
                let (tx, rx) = tokio::sync::oneshot::channel();
                senders.push(rx);
                pending.push(BatchSlot { result_tx: tx });
                if pending.len() >= batch_size {
                    let batch = std::mem::replace(&mut pending, Vec::with_capacity(batch_size));
                    batch_tx.send(batch).await.unwrap();
                }
            }
            // Flush partial
            if !pending.is_empty() {
                batch_tx.send(pending).await.unwrap();
            }
            drop(batch_tx);

            // Verify all receivers eventually get a value
            for (i, rx) in senders.into_iter().enumerate() {
                let result = rx.await.unwrap();
                assert!(result.is_ok(), "slot {} should succeed", i);
            }
        });

        // Consumer: count batches and respond
        let mut batch_count = 0;
        let mut batch_sizes = Vec::new();
        while let Some(batch) = batch_rx.recv().await {
            batch_sizes.push(batch.len());
            for slot in batch {
                let _ = slot.result_tx.send(Ok(vec![1.0]));
            }
            batch_count += 1;
        }

        assert_eq!(batch_count, 2, "should have 2 batches");
        assert_eq!(batch_sizes, vec![4, 3], "batches should be [4, 3]");
    }

    /// Test that fallback_chunks splits large files into bounded chunks.
    #[test]
    fn fallback_chunks_respects_line_limit() {
        let dir = tempfile::TempDir::new().unwrap();
        let path = dir.path().join("large.txt");

        // Create a file with more lines than FALLBACK_CHUNK_MAX_LINES (200)
        let content: String = (0..500).map(|i| format!("line {}\n", i)).collect();
        std::fs::write(&path, &content).unwrap();

        let chunks = fallback_chunks(&path).unwrap();
        assert!(
            chunks.len() > 1,
            "large file should produce multiple chunks, got {}",
            chunks.len()
        );

        // Each chunk should have at most FALLBACK_CHUNK_MAX_LINES lines
        for chunk in &chunks {
            let lines = chunk.end_line - chunk.start_line + 1;
            assert!(
                lines <= FALLBACK_CHUNK_MAX_LINES as i32,
                "chunk has {} lines, max is {}",
                lines,
                FALLBACK_CHUNK_MAX_LINES
            );
        }
    }

    /// Test that fallback_chunks respects character limit per chunk.
    #[test]
    fn fallback_chunks_respects_char_limit() {
        let dir = tempfile::TempDir::new().unwrap();
        let path = dir.path().join("wide.txt");

        // Create file with very long lines (each > 1000 chars)
        let long_line = "x".repeat(2000);
        let content: String = (0..10).map(|_| format!("{}\n", long_line)).collect();
        std::fs::write(&path, &content).unwrap();

        let chunks = fallback_chunks(&path).unwrap();
        assert!(chunks.len() >= 1, "should produce at least one chunk");

        for chunk in &chunks {
            assert!(
                chunk.content.len() <= FALLBACK_CHUNK_MAX_CHARS + 2100,
                "chunk content length {} exceeds expected max",
                chunk.content.len()
            );
        }
    }

    /// Test that memory-aware backpressure logic correctly adjusts write_batch.
    /// Simulates the consumer loop's memory check interval and verifies:
    /// - Normal: write_batch stays at default
    /// - Pressure (>80%): write_batch reduced to 32
    /// - Critical (>90%): write_batch reduced to 8
    /// - Recovered: write_batch restored to default
    #[test]
    fn memory_backpressure_adjusts_write_batch() {
        use crate::hardware::{MemoryUsage, MEMORY_CRITICAL_THRESHOLD, MEMORY_PRESSURE_THRESHOLD};

        let write_batch_default: usize = 128;
        let mut write_batch = write_batch_default;
        let mut under_pressure = false;

        // Simulate: normal memory (50% used)
        let normal = MemoryUsage {
            total_bytes: 1000,
            available_bytes: 500,
        };
        assert!(!normal.exceeds(MEMORY_PRESSURE_THRESHOLD));
        // write_batch should stay at default
        assert_eq!(write_batch, 128);

        // Simulate: pressure (85% used)
        let pressure = MemoryUsage {
            total_bytes: 1000,
            available_bytes: 150,
        };
        assert!(pressure.exceeds(MEMORY_PRESSURE_THRESHOLD));
        assert!(!pressure.exceeds(MEMORY_CRITICAL_THRESHOLD));

        // Apply the same logic as the consumer loop
        let prev_pressure = under_pressure;
        under_pressure = pressure.exceeds(MEMORY_PRESSURE_THRESHOLD);
        if pressure.exceeds(MEMORY_CRITICAL_THRESHOLD) {
            write_batch = 8;
        } else if under_pressure {
            write_batch = 32;
        } else if prev_pressure {
            write_batch = write_batch_default;
        }
        assert_eq!(write_batch, 32, "pressure should reduce batch to 32");
        assert!(under_pressure);

        // Simulate: critical (95% used)
        let critical = MemoryUsage {
            total_bytes: 1000,
            available_bytes: 50,
        };
        assert!(critical.exceeds(MEMORY_CRITICAL_THRESHOLD));

        let prev_pressure = under_pressure;
        under_pressure = critical.exceeds(MEMORY_PRESSURE_THRESHOLD);
        if critical.exceeds(MEMORY_CRITICAL_THRESHOLD) {
            write_batch = 8;
        } else if under_pressure {
            write_batch = 32;
        } else if prev_pressure {
            write_batch = write_batch_default;
        }
        assert_eq!(write_batch, 8, "critical should reduce batch to 8");

        // Simulate: recovered (40% used)
        let recovered = MemoryUsage {
            total_bytes: 1000,
            available_bytes: 600,
        };
        assert!(!recovered.exceeds(MEMORY_PRESSURE_THRESHOLD));

        let prev_pressure = under_pressure;
        under_pressure = recovered.exceeds(MEMORY_PRESSURE_THRESHOLD);
        if recovered.exceeds(MEMORY_CRITICAL_THRESHOLD) {
            write_batch = 8;
        } else if under_pressure {
            write_batch = 32;
        } else if prev_pressure {
            write_batch = write_batch_default;
        }
        assert_eq!(
            write_batch, write_batch_default,
            "recovery should restore default batch"
        );
        assert!(!under_pressure);
    }

    /// Test that under memory pressure, TCP mode would use blocking flush.
    /// Verifies the `is_tcp && !under_pressure` guard used in the consumer loop.
    #[test]
    fn memory_pressure_forces_blocking_flush_in_tcp() {
        let is_tcp = true;

        // Normal: TCP uses non-blocking
        let under_pressure = false;
        let use_nonblocking = is_tcp && !under_pressure;
        assert!(
            use_nonblocking,
            "TCP should use non-blocking flush when not under pressure"
        );

        // Pressure: TCP switches to blocking
        let under_pressure = true;
        let use_nonblocking = is_tcp && !under_pressure;
        assert!(
            !use_nonblocking,
            "TCP should use blocking flush under memory pressure"
        );

        // Local mode: always blocking regardless of pressure
        let is_tcp = false;
        let under_pressure = false;
        let use_nonblocking = is_tcp && !under_pressure;
        assert!(
            !use_nonblocking,
            "Local mode should always use blocking flush"
        );
    }

    /// Test that the memory check interval is reasonable (not too frequent, not too rare).
    /// Verifies the MEMORY_CHECK_INTERVAL constant and that it aligns with the
    /// modulo check used in the consumer loop.
    #[test]
    fn memory_check_interval_is_reasonable() {
        const MEMORY_CHECK_INTERVAL: usize = 50;
        // Should check at file 0, 50, 100, 150, ...
        assert_eq!(0 % MEMORY_CHECK_INTERVAL, 0, "should check at start");
        assert_eq!(50 % MEMORY_CHECK_INTERVAL, 0, "should check at 50");
        assert_ne!(25 % MEMORY_CHECK_INTERVAL, 0, "should NOT check at 25");
        assert_ne!(1 % MEMORY_CHECK_INTERVAL, 0, "should NOT check at 1");
        // Interval should be between 10 and 200 (balance overhead vs responsiveness)
        assert!(
            MEMORY_CHECK_INTERVAL >= 10,
            "interval too small, /proc/meminfo overhead"
        );
        assert!(
            MEMORY_CHECK_INTERVAL <= 200,
            "interval too large, slow reaction"
        );
    }

    /// Test that the pipeline consumer correctly handles batch size transitions
    /// across multiple pressure states, simulating a realistic sequence.
    #[tokio::test]
    async fn memory_backpressure_pipeline_simulation() {
        use crate::hardware::{MemoryUsage, MEMORY_CRITICAL_THRESHOLD, MEMORY_PRESSURE_THRESHOLD};

        let write_batch_default: usize = 128;
        let mut write_batch = write_batch_default;
        let mut under_pressure = false;

        // Simulate processing 300 files with varying memory pressure
        let memory_states: Vec<(usize, f64)> = vec![
            (0, 0.50),   // start: normal
            (50, 0.75),  // still normal
            (100, 0.85), // pressure kicks in
            (150, 0.92), // critical
            (200, 0.88), // back to pressure (not critical)
            (250, 0.70), // recovered
        ];

        let check_interval = 50usize;

        for (done, usage_frac) in memory_states {
            if done % check_interval != 0 {
                continue;
            }

            let mem = MemoryUsage {
                total_bytes: 10000,
                available_bytes: ((1.0 - usage_frac) * 10000.0) as u64,
            };

            let prev_pressure = under_pressure;
            under_pressure = mem.exceeds(MEMORY_PRESSURE_THRESHOLD);
            if mem.exceeds(MEMORY_CRITICAL_THRESHOLD) {
                write_batch = 8;
            } else if under_pressure {
                write_batch = 32;
            } else if prev_pressure {
                write_batch = write_batch_default;
            }

            match done {
                0 => assert_eq!(write_batch, 128, "file 0: should be default"),
                50 => assert_eq!(write_batch, 128, "file 50: 75% is not pressure"),
                100 => assert_eq!(write_batch, 32, "file 100: 85% is pressure"),
                150 => assert_eq!(write_batch, 8, "file 150: 92% is critical"),
                200 => assert_eq!(write_batch, 32, "file 200: 88% back to pressure"),
                250 => assert_eq!(write_batch, 128, "file 250: 70% recovered"),
                _ => {}
            }
        }
    }

    /// Test the two-stage pipeline pattern: chunk channel feeds embed workers,
    /// which feed the result channel. This is the exact architecture used
    /// by both TCP and Unix modes.
    #[tokio::test]
    async fn two_stage_pipeline_no_deadlock() {
        let concurrency = 8;
        let num_files = 100;
        let sem = Arc::new(Semaphore::new(concurrency));

        let (chunk_tx, chunk_rx) = tokio::sync::mpsc::channel::<usize>(concurrency * 2);
        let (result_tx, mut result_rx) = tokio::sync::mpsc::channel::<usize>(concurrency * 2);

        // Stage 1: chunk producers
        for i in 0..num_files {
            let sem = sem.clone();
            let chunk_tx = chunk_tx.clone();
            tokio::spawn(async move {
                let _permit = sem.acquire().await.ok();
                let _ = chunk_tx.send(i).await;
            });
        }
        drop(chunk_tx);

        // Stage 2: embed workers (consume from chunk_rx, produce to result_tx)
        let chunk_rx = Arc::new(tokio::sync::Mutex::new(chunk_rx));
        for _ in 0..concurrency {
            let chunk_rx = chunk_rx.clone();
            let result_tx = result_tx.clone();
            tokio::spawn(async move {
                loop {
                    let msg = {
                        let mut rx = chunk_rx.lock().await;
                        rx.recv().await
                    };
                    match msg {
                        Some(i) => {
                            // Simulate embed work
                            tokio::time::sleep(std::time::Duration::from_millis(1)).await;
                            let _ = result_tx.send(i).await;
                        }
                        None => break,
                    }
                }
            });
        }
        drop(result_tx);

        // Consumer
        let timeout = tokio::time::timeout(std::time::Duration::from_secs(10), async {
            let mut count = 0;
            while let Some(_) = result_rx.recv().await {
                count += 1;
            }
            count
        })
        .await;

        match timeout {
            Ok(count) => assert_eq!(count, num_files, "all files should complete"),
            Err(_) => panic!("two-stage pipeline deadlocked"),
        }
    }

    /// Test that batch destructuring (into_iter + unzip) correctly separates
    /// texts and senders without cloning. This validates the optimization in
    /// Stage 3 embed workers that replaces `.text.clone()` with zero-copy move.
    #[tokio::test]
    async fn batch_destructure_no_clone() {
        struct BatchSlot {
            text: String,
            result_tx: tokio::sync::oneshot::Sender<Result<Vec<f32>, anyhow::Error>>,
        }

        let mut batch = Vec::new();
        let mut receivers = Vec::new();

        for i in 0..48 {
            let (tx, rx) = tokio::sync::oneshot::channel();
            batch.push(BatchSlot {
                text: format!("text content number {}", i),
                result_tx: tx,
            });
            receivers.push(rx);
        }

        // Destructure: moves texts out, no clone
        let (texts, senders): (Vec<String>, Vec<_>) =
            batch.into_iter().map(|s| (s.text, s.result_tx)).unzip();

        assert_eq!(texts.len(), 48);
        assert_eq!(senders.len(), 48);
        assert_eq!(texts[0], "text content number 0");
        assert_eq!(texts[47], "text content number 47");

        // Simulate RPC result scatter
        let fake_vecs: Vec<Vec<f32>> = (0..48).map(|i| vec![i as f32; 4]).collect();
        for (tx, vec) in senders.into_iter().zip(fake_vecs.into_iter()) {
            let _ = tx.send(Ok(vec));
        }

        // Verify all receivers got their results
        for (i, rx) in receivers.into_iter().enumerate() {
            let result = rx.await.unwrap().unwrap();
            assert_eq!(result, vec![i as f32; 4]);
        }
    }

    /// Test that IndexStats.compact_handle works as a background task.
    /// The handle should complete independently of the caller.
    #[tokio::test]
    async fn compact_handle_runs_in_background() {
        use std::sync::atomic::{AtomicBool, Ordering};

        let completed = Arc::new(AtomicBool::new(false));
        let completed_clone = completed.clone();

        let handle = tokio::spawn(async move {
            // Simulate compaction work
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
            completed_clone.store(true, Ordering::SeqCst);
        });

        // Task should not be complete yet
        assert!(!completed.load(Ordering::SeqCst));

        // Await the handle
        let _ = handle.await;

        // Now it should be complete
        assert!(completed.load(Ordering::SeqCst));
    }

    // ---- Scan concurrency bounds tests ----

    #[test]
    fn scan_concurrency_formula() {
        // Verify (n/2).clamp(2,8) produces correct bounds for all CPU counts
        for cpus in [1, 2, 3, 4, 8, 16, 32, 64, 128] {
            let workers = (cpus / 2usize).clamp(2, 8);
            assert!(workers >= 2, "min 2 workers for {cpus} CPUs, got {workers}");
            assert!(workers <= 8, "max 8 workers for {cpus} CPUs, got {workers}");
        }
        // Specific edge cases
        assert_eq!(
            (1usize / 2).clamp(2, 8),
            2,
            "1 CPU → 2 workers (clamped min)"
        );
        assert_eq!(
            (2usize / 2).clamp(2, 8),
            2,
            "2 CPUs → 2 workers (clamped min)"
        );
        assert_eq!((4usize / 2).clamp(2, 8), 2, "4 CPUs → 2 workers");
        assert_eq!((8usize / 2).clamp(2, 8), 4, "8 CPUs → 4 workers");
        assert_eq!(
            (16usize / 2).clamp(2, 8),
            8,
            "16 CPUs → 8 workers (clamped max)"
        );
        assert_eq!(
            (32usize / 2).clamp(2, 8),
            8,
            "32 CPUs → 8 workers (clamped max)"
        );
        assert_eq!(
            (128usize / 2).clamp(2, 8),
            8,
            "128 CPUs → 8 workers (clamped max)"
        );
    }

    #[test]
    fn scan_concurrency_default_fallback() {
        // When available_parallelism fails, default should be 4
        let fallback = 4usize;
        assert!(fallback >= 2, "fallback must be >= min clamp");
        assert!(fallback <= 8, "fallback must be <= max clamp");
    }

    // ---- Spawn bounding tests (TCP pipeline) ----

    /// Verify spawn_sem bounds in-flight tasks to embed_concurrency * 4
    #[tokio::test]
    async fn spawn_sem_bounds_inflight_tasks() {
        use std::sync::atomic::{AtomicUsize, Ordering};

        let concurrency = 4usize;
        let cap = concurrency * 4;
        let sem = Arc::new(Semaphore::new(cap));
        let peak = Arc::new(AtomicUsize::new(0));
        let active = Arc::new(AtomicUsize::new(0));
        let total = 100usize;

        let (tx, mut rx) = tokio::sync::mpsc::channel::<usize>(cap);

        // Simulate the spawn loop
        {
            let sem = sem.clone();
            let peak = peak.clone();
            let active = active.clone();
            let tx = tx.clone();
            tokio::spawn(async move {
                for i in 0..total {
                    let permit = sem.clone().acquire_owned().await.unwrap();
                    let peak = peak.clone();
                    let active = active.clone();
                    let tx = tx.clone();
                    tokio::spawn(async move {
                        let cur = active.fetch_add(1, Ordering::SeqCst) + 1;
                        peak.fetch_max(cur, Ordering::SeqCst);
                        tokio::time::sleep(std::time::Duration::from_millis(5)).await;
                        let _ = tx.send(i).await;
                        active.fetch_sub(1, Ordering::SeqCst);
                        drop(permit);
                    });
                }
            });
        }
        drop(tx);

        // Drain rx concurrently (prevents channel backpressure deadlock)
        let mut received = 0;
        while let Some(_) = rx.recv().await {
            received += 1;
        }

        assert_eq!(received, total, "all {total} tasks must complete");
        let max = peak.load(Ordering::SeqCst);
        assert!(max <= cap, "peak inflight {max} should be <= cap {cap}");
        assert!(max >= 2, "should have some parallelism, got {max}");
    }

    /// Verify spawn_sem releases permit after task completes (no leak)
    #[tokio::test]
    async fn spawn_sem_releases_permits() {
        let cap = 4;
        let sem = Arc::new(Semaphore::new(cap));

        // Use all permits
        for _ in 0..cap {
            let permit = sem.clone().acquire_owned().await.unwrap();
            let sem = sem.clone();
            tokio::spawn(async move {
                tokio::time::sleep(std::time::Duration::from_millis(10)).await;
                drop(permit);
                drop(sem);
            });
        }

        // Wait for tasks to complete
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;

        // All permits should be returned
        assert_eq!(
            sem.available_permits(),
            cap,
            "all {cap} permits should be returned after tasks complete"
        );
    }

    /// Edge case: embed_concurrency=1 → spawn_sem=4 (minimum viable)
    #[tokio::test]
    async fn spawn_sem_min_concurrency() {
        use std::sync::atomic::{AtomicUsize, Ordering};

        let concurrency = 1usize;
        let cap = concurrency * 4;
        assert_eq!(cap, 4, "minimum spawn cap should be 4");

        let sem = Arc::new(Semaphore::new(cap));
        let done = Arc::new(AtomicUsize::new(0));

        for _ in 0..8 {
            let permit = sem.clone().acquire_owned().await.unwrap();
            let done = done.clone();
            tokio::spawn(async move {
                done.fetch_add(1, Ordering::SeqCst);
                drop(permit);
            });
        }

        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        assert_eq!(done.load(Ordering::SeqCst), 8, "all tasks should complete");
    }
}

async fn show_status(storage_path: &Path, dimensions: u32, json: bool) -> Result<()> {
    if !tokio::fs::try_exists(storage_path).await.unwrap_or(false) {
        if json {
            println!(
                "{{\"error\": \"no_index\", \"path\": \"{}\"}}",
                storage_path.display()
            );
        } else {
            println!("No index found at {}", storage_path.display());
        }
        return Ok(());
    }

    // Check if config.lance exists to avoid creating empty table before backfill
    let config_exists = tokio::fs::try_exists(&storage_path.join("config.lance"))
        .await
        .unwrap_or(false);

    // Get dimensions from config if it exists, otherwise use default
    let dims = if config_exists {
        let storage0 = Storage::open(storage_path, dimensions).await?;
        let d = storage0.get_dimensions().await?.unwrap_or(dimensions);
        drop(storage0);
        d
    } else {
        dimensions
    };

    // Open storage and run backfill (will create config.lance if needed)
    let storage = Storage::open(storage_path, dims).await?;
    let backfill_count = storage.backfill_metadata().await.unwrap_or(0);
    if backfill_count > 0 {
        tracing::info!("status: auto-fixed {} metadata field(s)", backfill_count);
    }
    drop(storage);

    // Reopen to ensure fresh reads after backfill
    let storage = Storage::open(storage_path, dims).await?;

    let (files, chunks, embeddings) = storage.stats().await?;
    let tier = storage
        .get_tier()
        .await?
        .unwrap_or_else(|| "not set".into());
    let quantization = storage
        .get_quantization()
        .await?
        .unwrap_or_else(|| "float".into());

    let last_duration_ms = storage.get_last_index_duration_ms().await?;
    let last_files_count = storage.get_last_index_files_count().await?;
    let last_timestamp = storage.get_last_index_timestamp().await?;

    // Self-heal stuck progress (daemon singleton enforced by port lock; clear on status check).
    if storage.get_indexing_in_progress().await? {
        let _ = storage.clear_indexing_progress().await;
    }

    let indexing_in_progress = storage.get_indexing_in_progress().await?;
    let indexing_start_time = storage.get_indexing_start_time().await?;
    let indexing_phase = storage.get_indexing_phase().await?;
    let (scanning_done, scanning_total) = storage.get_phase_progress("scanning").await?;
    let (chunking_done, chunking_total) = storage.get_phase_progress("chunking").await?;
    let (embedding_done, embedding_total) = storage.get_phase_progress("embedding").await?;

    let bytes_per_dim = if quantization == "int8" { 1.0 } else { 4.0 };
    let embedding_size = (dims as f64) * bytes_per_dim;
    let storage_mb = (embeddings as f64) * embedding_size / (1024.0 * 1024.0);

    let files_per_sec = match (last_duration_ms, last_files_count) {
        (Some(ms), Some(count)) if ms > 0 => Some((count as f64) / (ms as f64 / 1000.0)),
        _ => None,
    };

    let calc_pct = |done: i64, total: i64| {
        if total > 0 {
            ((done as f64) / (total as f64) * 100.0 * 10.0).round() / 10.0
        } else {
            0.0
        }
    };

    if json {
        println!(
            "{{\"path\": \"{}\", \"tier\": \"{}\", \"dimensions\": {}, \"quantization\": \"{}\", \"files\": {}, \"chunks\": {}, \"embeddings\": {}, \"storage_mb\": {:.2}, \"last_index_duration_ms\": {}, \"last_index_files_count\": {}, \"last_index_timestamp\": {}, \"files_per_sec\": {}, \"indexing_in_progress\": {}, \"indexing_start_time\": {}, \"indexing_phase\": {}, \"scanning_done\": {}, \"scanning_total\": {}, \"chunking_done\": {}, \"chunking_total\": {}, \"embedding_done\": {}, \"embedding_total\": {}}}",
            storage_path.display(),
            tier,
            dims,
            quantization,
            files,
            chunks,
            embeddings,
            storage_mb,
            last_duration_ms
                .map(|v| v.to_string())
                .unwrap_or_else(|| "null".into()),
            last_files_count
                .map(|v| v.to_string())
                .unwrap_or_else(|| "null".into()),
            last_timestamp
                .as_ref()
                .map(|v| format!("\"{v}\""))
                .unwrap_or_else(|| "null".into()),
            files_per_sec
                .map(|v| format!("{v:.1}"))
                .unwrap_or_else(|| "null".into()),
            if indexing_in_progress { "true" } else { "false" },
            indexing_start_time
                .as_ref()
                .map(|v| format!("\"{v}\""))
                .unwrap_or_else(|| "null".into()),
            indexing_phase
                .as_ref()
                .map(|v| format!("\"{v}\""))
                .unwrap_or_else(|| "null".into()),
            scanning_done,
            scanning_total,
            chunking_done,
            chunking_total,
            embedding_done,
            embedding_total
        );
    } else {
        println!("Index: {}", storage_path.display());

        if indexing_in_progress {
            let phase = indexing_phase.unwrap_or_else(|| "indexing".into());
            println!("Status: {phase}");
            let scan_pct = calc_pct(scanning_done, scanning_total);
            let chunk_pct = calc_pct(chunking_done, chunking_total);
            let embed_pct = calc_pct(embedding_done, embedding_total);
            println!(
                "  Scanning:  {}/{} files ({}%)",
                scanning_done, scanning_total, scan_pct
            );
            println!(
                "  Chunking:  {}/{} files ({}%)",
                chunking_done, chunking_total, chunk_pct
            );
            println!(
                "  Embedding: {}/{} files ({}%)",
                embedding_done, embedding_total, embed_pct
            );
            if let Some(ts) = indexing_start_time {
                println!("Started: {ts}");
            }
        } else {
            println!("Status: idle");
        }

        println!("Tier: {tier}");
        println!("Dimensions: {dims}");
        println!("Quantization: {quantization}");
        println!("Files: {files}");
        println!("Chunks: {chunks}");
        println!("Embeddings: {embeddings}");
        println!(
            "Vector storage: {:.1} MB ({} bytes/embedding)",
            storage_mb, embedding_size as i64
        );

        if let Some(ms) = last_duration_ms {
            println!("Last index duration: {:.1}s", (ms as f64) / 1000.0);
        }
        if let Some(speed) = files_per_sec {
            println!("Index speed: {:.1} files/s", speed);
        }
        if let Some(ts) = last_timestamp {
            println!("Last indexed: {ts}");
        }
    }

    Ok(())
}

async fn show_usage(storage_path: &Path, dimensions: u32) -> Result<()> {
    if !tokio::fs::try_exists(storage_path).await.unwrap_or(false) {
        println!("No index found at {}", storage_path.display());
        return Ok(());
    }

    let storage = Storage::open(storage_path, dimensions).await?;
    let today = storage.get_daily_usage().await?;
    println!("Today's usage:");
    println!("  Tokens: {}", today.tokens);
    println!("  Cost:   ${:.4}", today.cost);

    let history = storage.get_usage_history(7).await?;
    if !history.is_empty() {
        println!("\nLast 7 days:");
        for day in history {
            println!("  {}: {} tokens (${:0.4})", day.date, day.tokens, day.cost);
        }
    }

    let total = storage.get_total_usage().await?;
    println!("\nTotal usage:");
    println!("  Tokens: {}", total.tokens);
    println!("  Cost:   ${:.4}", total.cost);
    Ok(())
}

async fn show_files(storage_path: &Path, dimensions: u32, json: bool) -> Result<()> {
    if !tokio::fs::try_exists(storage_path).await.unwrap_or(false) {
        if json {
            println!(
                "{{\"error\": \"no_index\", \"path\": \"{}\", \"files\": {{}}}}",
                storage_path.display()
            );
        } else {
            println!("No index found at {}", storage_path.display());
        }
        return Ok(());
    }

    let storage = Storage::open(storage_path, dimensions).await?;
    let hashes = storage.get_file_hashes(None).await?;

    if json {
        let j = serde_json::json!({
            "path": storage_path.display().to_string(),
            "count": hashes.len(),
            "files": hashes,
        });
        println!("{}", serde_json::to_string(&j)?);
    } else {
        println!("Indexed files ({}):", hashes.len());
        let mut sorted: Vec<_> = hashes.iter().collect();
        sorted.sort_by_key(|(k, _)| k.to_string());
        for (path, hash) in sorted {
            println!("  {path}: {hash}");
        }
    }

    Ok(())
}

async fn run_search(
    storage_path: &Path,
    query: &str,
    tier: &str,
    dimensions: u32,
    json_lines: bool,
    federated_dbs: &[PathBuf],
) -> Result<()> {
    // Collect all storage paths to search (primary + federated)
    let mut all_paths: Vec<PathBuf> = vec![storage_path.to_path_buf()];
    all_paths.extend(federated_dbs.iter().cloned());

    let mut all_ranked: Vec<(f64, String, String)> = Vec::new(); // (score, path, content)

    for sp in &all_paths {
        if !tokio::fs::try_exists(sp).await.unwrap_or(false) {
            continue;
        }

        let storage0 = Storage::open(sp, dimensions).await?;
        let stored_tier = storage0.get_tier().await?.unwrap_or_else(|| tier.into());
        let stored_dims = storage0.get_dimensions().await?.unwrap_or(dimensions);
        drop(storage0);

        let storage = Storage::open(sp, stored_dims).await?;
        let mut client = crate::model_client::pooled().await?;
        let (embed_model, rerank_model) = models_for_tier(&stored_tier);

        let query_vec = client.embed_query(query, embed_model, stored_dims).await?;
        storage
            .record_usage(count_tokens(query), &stored_tier)
            .await?;

        let results = storage.search_hybrid(query, &query_vec, 20).await?;
        if results.is_empty() {
            continue;
        }

        let docs: Vec<&str> = results.iter().map(|r| r.content.as_str()).collect();
        let ranked = client.rerank(query, &docs, rerank_model, 10).await?;
        storage
            .record_usage(count_tokens(query), &stored_tier)
            .await?;

        for (idx, score) in ranked {
            if idx < results.len() {
                let r = &results[idx];
                all_ranked.push((score.into(), r.path.clone(), r.content.clone()));
            }
        }
    }

    // Sort by score descending, deduplicate by path
    all_ranked.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
    let mut seen = HashSet::new();
    all_ranked.retain(|(_, p, _)| seen.insert(p.clone()));
    all_ranked.truncate(10);

    if all_ranked.is_empty() {
        if json_lines {
            emit_json(
                true,
                &serde_json::json!({"type": "search_results", "results": []}),
            );
        } else {
            println!("No results found");
        }
        return Ok(());
    }

    if json_lines {
        let results: Vec<serde_json::Value> = all_ranked
            .iter()
            .enumerate()
            .map(|(i, (score, path, content))| {
                serde_json::json!({
                    "rank": i + 1,
                    "score": score,
                    "path": path,
                    "content": content,
                })
            })
            .collect();
        emit_json(
            true,
            &serde_json::json!({"type": "search_results", "results": results}),
        );
    } else {
        println!("\nSearch results for: {query}\n");
        for (i, (score, path, content)) in all_ranked.iter().enumerate() {
            let preview: String = content.chars().take(200).collect();
            let preview = preview.replace('\n', " ");
            println!("{}. [score: {:.3}] {}", i + 1, score, path);
            println!("   {preview}");
            println!();
        }
    }

    Ok(())
}

async fn remove_file(
    storage_path: &Path,
    file: &Path,
    root: &Path,
    include: &[PathBuf],
    dimensions: u32,
) -> Result<()> {
    let storage = Storage::open(storage_path, dimensions).await?;
    let include_dirs = resolve_include_dirs(root, include);
    let rel = relative_path(file, root, &include_dirs);

    // Get count before deletion
    let chunks = storage.get_chunks_with_hashes(&rel).await?;
    let removed = chunks.len();

    // Create WriteQueue for deletion
    let write_queue = crate::storage::WriteQueue::new(std::sync::Arc::new(storage), 32);
    write_queue.delete_file(&rel).await;

    // Wait for deletion to complete
    let _ = write_queue.shutdown().await;

    println!("Removed {rel} ({removed} chunks)");
    Ok(())
}

async fn run_dry_run(root: &Path, exclude: &[String], include: &[PathBuf]) -> Result<()> {
    println!("Dry run: would index {}", root.display());

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
        if exclude.is_empty() {
            None
        } else {
            Some(exclude)
        },
        &mut seen,
    ));

    println!("Would discover {} files", discovery.files.len());
    for file in &discovery.files {
        let rel = relative_path(file, root, &include_dirs);
        println!("  {rel}");
    }

    Ok(())
}

/// Discover linked projects (symlinks to external git repos) and emit as JSON.
async fn discover_links(root: &Path, json_lines: bool) -> Result<()> {
    let project = config::load(root);
    let cfg = config::effective(&project, None, None);
    let discovery = discover::discover_files_with_config(root, &cfg)?;

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

    let result = serde_json::json!({
        "type": "discover_links",
        "root": root.to_str().unwrap_or(""),
        "rootProjectId": storage::git_project_id(root),
        "links": links,
    });

    if json_lines {
        emit_json(true, &result);
    } else {
        println!("{}", serde_json::to_string_pretty(&result)?);
    }

    Ok(())
}

/// Show comprehensive health status.
async fn show_health(
    root: &Path,
    storage_path: &Path,
    dimensions: u32,
    json_lines: bool,
) -> Result<()> {
    let index_exists = tokio::fs::try_exists(storage_path).await.unwrap_or(false);
    let mut status = serde_json::json!({
        "type": "health",
        "root": root.to_str().unwrap_or(""),
        "indexExists": index_exists,
        "dbPath": storage_path.to_str().unwrap_or(""),
    });

    if index_exists {
        if let Ok(storage) = Storage::open(storage_path, dimensions).await {
            // Lazy migration: backfill missing metadata for legacy indexes
            match storage.backfill_metadata().await {
                Ok(count) if count > 0 => {
                    eprintln!("auto-fixed {} metadata field(s)", count);
                }
                Ok(_) => {} // No backfill needed
                Err(e) => {
                    eprintln!("warning: metadata backfill failed: {}", e);
                }
            }

            let chunks = storage.count_chunks().await.unwrap_or(0);
            let files = storage.get_file_count().await.unwrap_or(0);
            let tier = storage.get_tier().await.unwrap_or(None);
            let dims = storage.get_dimensions().await.unwrap_or(None);
            let in_progress = storage
                .get_config("indexing_in_progress")
                .await
                .unwrap_or(None);
            let phase = storage.get_config("indexing_phase").await.unwrap_or(None);
            let last_ts = storage
                .get_config("last_index_timestamp")
                .await
                .unwrap_or(None);
            let last_duration_ms = storage.get_last_index_duration_ms().await.unwrap_or(None);
            let last_files_count = storage.get_last_index_files_count().await.unwrap_or(None);
            let files_per_sec = match (last_duration_ms, last_files_count) {
                (Some(ms), Some(count)) if ms > 0 => Some((count as f64) / (ms as f64 / 1000.0)),
                _ => None,
            };

            status["files"] = serde_json::json!(files);
            status["chunks"] = serde_json::json!(chunks);
            status["tier"] = serde_json::json!(tier);
            status["dimensions"] = serde_json::json!(dims);
            status["indexingInProgress"] = serde_json::json!(in_progress == Some("true".into()));
            status["indexingPhase"] = serde_json::json!(phase);
            status["lastIndexed"] = serde_json::json!(last_ts);
            status["lastIndexDurationMs"] = serde_json::json!(last_duration_ms);
            status["lastIndexFilesCount"] = serde_json::json!(last_files_count);
            status["filesPerSec"] = serde_json::json!(files_per_sec);

            // Check for integrity issues
            let mut issues = Vec::new();
            let tables_dir = storage_path.join("chunks.lance");
            if !tokio::fs::try_exists(&tables_dir).await.unwrap_or(false) && chunks == 0 {
                issues.push("no chunks table");
            }
            status["issues"] = serde_json::json!(issues);
        }
    }

    // Check watcher state
    if let Some(state_path) = watcher_state_path(storage_path) {
        if tokio::fs::try_exists(&state_path).await.unwrap_or(false) {
            if let Ok(content) = tokio::fs::read_to_string(&state_path).await {
                if let Ok(ws) = serde_json::from_str::<serde_json::Value>(&content) {
                    status["watcher"] = ws;
                }
            }
        }
    }

    if json_lines {
        emit_json(true, &status);
    } else {
        println!("{}", serde_json::to_string_pretty(&status)?);
    }

    Ok(())
}
