//! Client for the Python model server (HTTP-only transport).
//!
//! All communication with the embedder uses the HTTP API on localhost:9998
//! (or the port set by `OPENCODE_EMBED_HTTP_PORT`).
//!
//! The daemon will auto-start the embedder if it's not already running.

use std::future::Future;
use std::time::Duration;

use anyhow::{bail, Context, Result};
use serde::Deserialize;
use serde_json::json;

/// Maximum allowed embedding dimension.
///
/// This is a safety limit to prevent OOM crashes from corrupted deserialization.
/// Common embedding dimensions are: 256, 384, 512, 768, 1024, 1536, 2048, 3072, 4096.
/// We use 16384 as a generous upper bound to accommodate future models.
const MAX_EMBEDDING_DIM: usize = 16384;

// RPC timeout: embeddings typically complete in <2s, chunking in <1s.
// 60s gives headroom for large batches while catching true hangs.
const RPC_TIMEOUT: Duration = Duration::from_secs(120);

/// Default HTTP port for the Python embedder's HTTP API.
pub const DEFAULT_HTTP_PORT: u16 = 9998;

fn decode_f32le_vec(bytes: &[u8], dimensions: usize) -> Result<Vec<f32>> {
    if dimensions == 0 || dimensions > MAX_EMBEDDING_DIM {
        anyhow::bail!(
            "invalid dimensions {}: expected 1-{} (possible deserialization corruption)",
            dimensions,
            MAX_EMBEDDING_DIM
        );
    }

    let expected = dimensions
        .checked_mul(4)
        .ok_or_else(|| anyhow::anyhow!("vector size overflow"))?;
    if bytes.len() != expected {
        anyhow::bail!(
            "invalid vector_f32 length: got {}, expected {} (dims={})",
            bytes.len(),
            expected,
            dimensions
        );
    }

    let mut out = vec![0f32; dimensions];
    for (i, chunk) in bytes.chunks_exact(4).enumerate() {
        out[i] = f32::from_le_bytes(chunk.try_into().unwrap());
    }
    Ok(out)
}

fn decode_f32le_mat(bytes: &[u8], dimensions: usize, count: usize) -> Result<Vec<Vec<f32>>> {
    if dimensions == 0 || dimensions > MAX_EMBEDDING_DIM {
        anyhow::bail!(
            "invalid dimensions {}: expected 1-{} (possible deserialization corruption)",
            dimensions,
            MAX_EMBEDDING_DIM
        );
    }

    let expected = count
        .checked_mul(dimensions)
        .and_then(|n| n.checked_mul(4))
        .ok_or_else(|| anyhow::anyhow!("vector size overflow"))?;
    if bytes.len() != expected {
        anyhow::bail!(
            "invalid vectors_f32 length: got {}, expected {} (count={}, dims={})",
            bytes.len(),
            expected,
            count,
            dimensions
        );
    }

    let step = dimensions * 4;
    let mut out = Vec::with_capacity(count);
    for i in 0..count {
        let start = i * step;
        let slice = &bytes[start..start + step];
        let mut vec = vec![0f32; dimensions];
        for (j, chunk) in slice.chunks_exact(4).enumerate() {
            vec[j] = f32::from_le_bytes(chunk.try_into().unwrap());
        }
        out.push(vec);
    }
    Ok(out)
}

/// Validate cross-field consistency for embedding responses.
fn validate_embed_passages_response(
    vectors_len: usize,
    dimensions: usize,
    count: usize,
    expected_dimensions: u32,
    method: &str,
) -> Result<()> {
    if dimensions == 0 || dimensions > MAX_EMBEDDING_DIM {
        tracing::error!(
            "{}: corrupted dimensions={} (expected ~{}), count={}, vectors_len={}",
            method, dimensions, expected_dimensions, count, vectors_len
        );
        bail!(
            "{}: invalid dimensions {} (expected 1-{}), possible deserialization corruption",
            method, dimensions, MAX_EMBEDDING_DIM
        );
    }

    let expected_len = count
        .checked_mul(dimensions)
        .and_then(|n| n.checked_mul(4));

    match expected_len {
        Some(expected) if vectors_len == expected => Ok(()),
        Some(expected) => {
            tracing::error!(
                "{}: cross-field validation failed - vectors_len={}, expected={} \
                (count={} * dimensions={} * 4), requested_dimensions={}. \
                This indicates ByteBuf deserialization read wrong number of bytes.",
                method, vectors_len, expected, count, dimensions, expected_dimensions
            );
            bail!(
                "{}: data integrity check failed - vectors_f32 length {} != expected {} \
                (count={} * dims={} * 4). Likely MessagePack framing/deserialization bug.",
                method, vectors_len, expected, count, dimensions
            )
        }
        None => {
            tracing::error!(
                "{}: overflow computing expected size (count={}, dimensions={})",
                method, count, dimensions
            );
            bail!("{}: size overflow with count={}, dimensions={}", method, count, dimensions)
        }
    }
}

/// Validate cross-field consistency for single vector embedding responses.
fn validate_embed_query_response(
    vector_len: usize,
    dimensions: usize,
    expected_dimensions: u32,
    method: &str,
) -> Result<()> {
    if dimensions == 0 || dimensions > MAX_EMBEDDING_DIM {
        tracing::error!(
            "{}: corrupted dimensions={} (expected ~{}), vector_len={}",
            method, dimensions, expected_dimensions, vector_len
        );
        bail!(
            "{}: invalid dimensions {} (expected 1-{}), possible deserialization corruption",
            method, dimensions, MAX_EMBEDDING_DIM
        );
    }

    let expected_len = dimensions.checked_mul(4);

    match expected_len {
        Some(expected) if vector_len == expected => Ok(()),
        Some(expected) => {
            tracing::error!(
                "{}: cross-field validation failed - vector_len={}, expected={} \
                (dimensions={} * 4), requested_dimensions={}. \
                This indicates ByteBuf deserialization read wrong number of bytes.",
                method, vector_len, expected, dimensions, expected_dimensions
            );
            bail!(
                "{}: data integrity check failed - vector_f32 length {} != expected {} \
                (dims={} * 4). Likely MessagePack framing/deserialization bug.",
                method, vector_len, expected, dimensions
            )
        }
        None => {
            tracing::error!("{}: overflow computing expected size (dimensions={})", method, dimensions);
            bail!("{}: size overflow with dimensions={}", method, dimensions)
        }
    }
}

// ============================================================================
// HTTP infrastructure
// ============================================================================

/// Base URL derived from OPENCODE_EMBED_HTTP_PORT (default 9998).
fn http_base_url() -> String {
    let port = std::env::var("OPENCODE_EMBED_HTTP_PORT")
        .ok()
        .and_then(|p| p.parse::<u16>().ok())
        .unwrap_or(DEFAULT_HTTP_PORT);
    format!("http://127.0.0.1:{}", port)
}

/// Singleton reqwest client (shared across all HTTP calls).
fn http_client() -> &'static reqwest::Client {
    static CLIENT: std::sync::OnceLock<reqwest::Client> = std::sync::OnceLock::new();
    CLIENT.get_or_init(|| {
        reqwest::Client::builder()
            .timeout(RPC_TIMEOUT)
            .build()
            .expect("failed to build HTTP client")
    })
}

use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};

/// Whether we've already confirmed the embedder is running this session.
static EMBEDDER_CHECKED: AtomicBool = AtomicBool::new(false);

/// PID of the embedder process we spawned (0 = not spawned by us).
static EMBEDDER_PID: AtomicU32 = AtomicU32::new(0);

/// Port file path for the embedder PID.
fn embedder_pid_path() -> Option<std::path::PathBuf> {
    dirs::home_dir().map(|h| h.join(".opencode").join("embedder.pid"))
}

/// Resolve the embedder binary path.
/// Checks: ~/.opencode/bin/opencode-embedder (GPU wrapper), then the PyInstaller binary.
fn embedder_binary() -> Option<std::path::PathBuf> {
    let home = dirs::home_dir()?;
    let candidates = [
        home.join(".opencode/bin/opencode-embedder"),
        home.join(".opencode/bin/opencode-embedder-dir/opencode-embedder"),
    ];
    candidates.into_iter().find(|p| p.exists())
}

/// Ensure the Python embedder is running. Called lazily on first use.
/// This is a singleton check — if already running (by us or externally), returns immediately.
pub async fn ensure_embedder() {
    // Fast path: already checked this session
    if EMBEDDER_CHECKED.load(Ordering::Relaxed) {
        return;
    }

    // Check if embedder is already healthy
    if http_health().await.unwrap_or(false) {
        EMBEDDER_CHECKED.store(true, Ordering::Relaxed);
        tracing::info!("embedder already running");
        return;
    }

    // Check if there's a stale PID file from a previous embedder
    if let Some(pid_path) = embedder_pid_path() {
        if let Ok(content) = tokio::fs::read_to_string(&pid_path).await {
            if let Ok(pid) = content.trim().parse::<u32>() {
                // Check if process is still alive
                let alive = unsafe { libc::kill(pid as i32, 0) == 0 };
                if alive {
                    // Process exists but not responding to health check — give it time
                    tracing::info!("embedder PID {} exists but not healthy yet, waiting", pid);
                    if wait_for_embedder(30).await {
                        EMBEDDER_CHECKED.store(true, Ordering::Relaxed);
                        return;
                    }
                    // Still not healthy after waiting, kill and respawn
                    tracing::warn!("embedder PID {} not healthy after 30s, killing", pid);
                    unsafe { libc::kill(pid as i32, libc::SIGTERM); }
                    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
                    unsafe { libc::kill(pid as i32, libc::SIGKILL); }
                }
                // Remove stale PID file
                let _ = tokio::fs::remove_file(&pid_path).await;
            }
        }
    }

    // Find and spawn the embedder binary
    let binary = match embedder_binary() {
        Some(b) => b,
        None => {
            tracing::warn!("embedder binary not found, indexing will not produce embeddings");
            EMBEDDER_CHECKED.store(true, Ordering::Relaxed);
            return;
        }
    };

    tracing::info!("spawning embedder: {}", binary.display());

    let port = std::env::var("OPENCODE_EMBED_HTTP_PORT")
        .ok()
        .and_then(|p| p.parse::<u16>().ok())
        .unwrap_or(DEFAULT_HTTP_PORT);

    match std::process::Command::new(&binary)
        .env("OPENCODE_EMBED_HTTP_PORT", port.to_string())
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
    {
        Ok(child) => {
            let pid = child.id();
            EMBEDDER_PID.store(pid, Ordering::Relaxed);
            tracing::info!("embedder spawned with PID {}", pid);

            // Write PID file
            if let Some(pid_path) = embedder_pid_path() {
                let _ = tokio::fs::write(&pid_path, pid.to_string()).await;
            }

            // Wait for it to become healthy (up to 90s for model loading)
            if wait_for_embedder(90).await {
                tracing::info!("embedder healthy on port {}", port);
                EMBEDDER_CHECKED.store(true, Ordering::Relaxed);
            } else {
                tracing::error!("embedder failed to become healthy after 90s, killing and resetting");
                unsafe { libc::kill(pid as i32, libc::SIGTERM); }
                tokio::time::sleep(std::time::Duration::from_millis(500)).await;
                unsafe { libc::kill(pid as i32, libc::SIGKILL); }
                if let Some(pid_path) = embedder_pid_path() {
                    let _ = tokio::fs::remove_file(&pid_path).await;
                }
                EMBEDDER_PID.store(0, Ordering::SeqCst);
                EMBEDDER_CHECKED.store(false, Ordering::SeqCst);
                return;
            }
        }
        Err(e) => {
            tracing::error!("failed to spawn embedder: {}", e);
            EMBEDDER_CHECKED.store(true, Ordering::Relaxed);
        }
    }
}

/// Wait for the embedder health check to pass.
async fn wait_for_embedder(timeout_secs: u64) -> bool {
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(timeout_secs);
    while tokio::time::Instant::now() < deadline {
        if http_health().await.unwrap_or(false) {
            return true;
        }
        tokio::time::sleep(std::time::Duration::from_secs(1)).await;
    }
    false
}

/// Shut down the embedder if we spawned it.
pub fn shutdown_embedder() {
    let pid = EMBEDDER_PID.load(Ordering::Relaxed);
    if pid == 0 {
        return;
    }
    tracing::info!("shutting down embedder PID {}", pid);
    unsafe {
        libc::kill(pid as i32, libc::SIGTERM);
    }
    // Clean up PID file
    if let Some(pid_path) = embedder_pid_path() {
        let _ = std::fs::remove_file(&pid_path);
    }
    EMBEDDER_PID.store(0, Ordering::Relaxed);
}

/// Reset embedder state so the next call to `ensure_embedder` will respawn it.
///
/// Called when an HTTP request detects the embedder has gone away (ECONNREFUSED).
fn reset_embedder() {
    tracing::warn!("embedder appears to have crashed — resetting state for respawn");
    EMBEDDER_CHECKED.store(false, Ordering::SeqCst);
    EMBEDDER_PID.store(0, Ordering::SeqCst);
}

/// Return `true` when `e` is a connection-level failure that likely means the
/// embedder process died (ECONNREFUSED, connection reset, or timeout).
fn is_retryable_error(e: &anyhow::Error) -> bool {
    for cause in e.chain() {
        if let Some(req) = cause.downcast_ref::<reqwest::Error>() {
            if req.is_connect() || req.is_timeout() {
                return true;
            }
            // Retry on server errors (5xx) — the embedder may still be loading models
            if req.is_status() {
                if let Some(status) = req.status() {
                    if status.is_server_error() {
                        return true;
                    }
                }
            }
        }
    }
    false
}

/// Wrap an async HTTP operation with embedder restart recovery.
///
/// If the first attempt fails with a connection error the embedder is reset,
/// `ensure_embedder` is called again, and the operation is retried up to
/// `MAX_RETRIES` times.  Any other error is returned immediately.
async fn with_embedder_recovery<T, F, Fut>(op: F) -> Result<T>
where
    F: Fn() -> Fut,
    Fut: Future<Output = Result<T>>,
{
    const MAX_RETRIES: u32 = 2;
    let mut failures = 0u32;
    loop {
        match op().await {
            Ok(v) => return Ok(v),
            Err(e) if failures < MAX_RETRIES && is_retryable_error(&e) => {
                failures += 1;
                tracing::warn!(
                    "embedder unreachable, resetting and retrying (attempt {}): {}",
                    failures, e
                );
                reset_embedder();
                tokio::time::sleep(Duration::from_millis(500)).await;
                ensure_embedder().await;
            }
            Err(e) => return Err(e),
        }
    }
}

/// Decode a standard-base64 string into raw bytes.
fn b64_decode(s: &str) -> Result<Vec<u8>> {
    use base64::Engine;
    base64::engine::general_purpose::STANDARD
        .decode(s)
        .context("base64 decode failed")
}

// ---- HTTP response shapes ----

/// Generic wrapper for HTTP responses (Python server wraps all responses in {"result": ...})
#[derive(Deserialize)]
struct HttpResultWrapper<T> {
    result: T,
}

#[derive(Deserialize)]
struct HttpPassagesF32Resp {
    vectors_f32: String,
    dimensions: usize,
    count: usize,
    endianness: String,
}

#[derive(Deserialize)]
struct HttpQueryF32Resp {
    vector_f32: String,
    dimensions: usize,
    endianness: String,
}

fn http_default_block() -> String {
    "block".to_string()
}
fn http_default_unknown() -> String {
    "unknown".to_string()
}

#[derive(Deserialize)]
struct HttpChunk {
    content: String,
    start_line: i32,
    end_line: i32,
    #[serde(default = "http_default_block")]
    chunk_type: String,
    #[serde(default = "http_default_unknown")]
    language: String,
}

#[derive(Deserialize)]
struct HttpChunkResp {
    chunks: Vec<HttpChunk>,
}

#[derive(Deserialize)]
struct HttpChunkAndEmbedResp {
    chunks: Vec<HttpChunk>,
    vectors_f32: String,
    dimensions: usize,
    count: usize,
    endianness: String,
}

#[derive(Deserialize)]
struct HttpRerankItem {
    index: usize,
    score: f32,
}

#[derive(Deserialize)]
struct HttpRerankResp {
    results: Vec<HttpRerankItem>,
}

// ---- HTTP API helpers ----

/// Health-check the HTTP embedder.  Returns `true` when the server is up.
pub async fn http_health() -> Result<bool> {
    let resp = http_client()
        .get(format!("{}/health", http_base_url()))
        .send()
        .await
        .context("HTTP health check failed")?;
    Ok(resp.status().is_success())
}

async fn http_embed_passages_inner(texts: &[String], model: &str, dimensions: u32) -> Result<Vec<Vec<f32>>> {
    let wrapper: HttpResultWrapper<HttpPassagesF32Resp> = http_client()
        .post(format!("{}/embed/passages_f32", http_base_url()))
        .json(&json!({"passages": texts, "model": model, "dimensions": dimensions}))
        .send()
        .await
        .context("HTTP embed/passages_f32 request failed")?
        .error_for_status()
        .context("HTTP embed/passages_f32 returned error status")?
        .json()
        .await
        .context("HTTP embed/passages_f32 response parse failed")?;

    let resp = wrapper.result;
    if resp.endianness != "le" {
        bail!("unsupported endianness: {}", resp.endianness);
    }
    let bytes = b64_decode(&resp.vectors_f32)?;
    validate_embed_passages_response(
        bytes.len(),
        resp.dimensions,
        resp.count,
        dimensions,
        "http_embed_passages_f32",
    )?;
    decode_f32le_mat(&bytes, resp.dimensions, resp.count)
}

async fn http_embed_passages(texts: &[String], model: &str, dimensions: u32) -> Result<Vec<Vec<f32>>> {
    with_embedder_recovery(|| http_embed_passages_inner(texts, model, dimensions)).await
}

async fn http_embed_query_inner(text: &str, model: &str, dimensions: u32) -> Result<Vec<f32>> {
    let wrapper: HttpResultWrapper<HttpQueryF32Resp> = http_client()
        .post(format!("{}/embed/query_f32", http_base_url()))
        .json(&json!({"query": text, "model": model, "dimensions": dimensions}))
        .send()
        .await
        .context("HTTP embed/query_f32 request failed")?
        .error_for_status()
        .context("HTTP embed/query_f32 returned error status")?
        .json()
        .await
        .context("HTTP embed/query_f32 response parse failed")?;

    let resp = wrapper.result;
    if resp.endianness != "le" {
        bail!("unsupported endianness: {}", resp.endianness);
    }
    let bytes = b64_decode(&resp.vector_f32)?;
    validate_embed_query_response(bytes.len(), resp.dimensions, dimensions, "http_embed_query_f32")?;
    decode_f32le_vec(&bytes, resp.dimensions)
}

async fn http_embed_query(text: &str, model: &str, dimensions: u32) -> Result<Vec<f32>> {
    with_embedder_recovery(|| http_embed_query_inner(text, model, dimensions)).await
}

async fn http_chunk_inner(content: &str, path: &str, tier: &str) -> Result<Vec<ChunkMeta>> {
    let wrapper: HttpResultWrapper<HttpChunkResp> = http_client()
        .post(format!("{}/embed/chunk", http_base_url()))
        .json(&json!({"content": content, "path": path, "tier": tier}))
        .send()
        .await
        .context("HTTP embed/chunk request failed")?
        .error_for_status()
        .context("HTTP embed/chunk returned error status")?
        .json()
        .await
        .context("HTTP embed/chunk response parse failed")?;

    Ok(wrapper.result.chunks.into_iter().map(|c| ChunkMeta {
        content: c.content,
        start_line: c.start_line,
        end_line: c.end_line,
        chunk_type: c.chunk_type,
        language: c.language,
    }).collect())
}

async fn http_chunk(content: &str, path: &str, tier: &str) -> Result<Vec<ChunkMeta>> {
    with_embedder_recovery(|| http_chunk_inner(content, path, tier)).await
}

async fn http_chunk_file_inner(file: &str, path: &str, tier: &str) -> Result<Vec<ChunkMeta>> {
    let wrapper: HttpResultWrapper<HttpChunkResp> = http_client()
        .post(format!("{}/embed/chunk_file", http_base_url()))
        .json(&json!({"path": file, "display_path": path, "tier": tier}))
        .send()
        .await
        .context("HTTP embed/chunk_file request failed")?
        .error_for_status()
        .context("HTTP embed/chunk_file returned error status")?
        .json()
        .await
        .context("HTTP embed/chunk_file response parse failed")?;

    Ok(wrapper.result.chunks.into_iter().map(|c| ChunkMeta {
        content: c.content,
        start_line: c.start_line,
        end_line: c.end_line,
        chunk_type: c.chunk_type,
        language: c.language,
    }).collect())
}

async fn http_chunk_file(file: &str, path: &str, tier: &str) -> Result<Vec<ChunkMeta>> {
    with_embedder_recovery(|| http_chunk_file_inner(file, path, tier)).await
}

async fn http_chunk_and_embed_inner(
    content: &str,
    path: &str,
    tier: &str,
    model: &str,
    dimensions: u32,
) -> Result<Vec<ChunkWithVector>> {
    let wrapper: HttpResultWrapper<HttpChunkAndEmbedResp> = http_client()
        .post(format!("{}/embed/chunk_and_embed", http_base_url()))
        .json(&json!({
            "content": content,
            "path": path,
            "tier": tier,
            "model": model,
            "dimensions": dimensions,
        }))
        .send()
        .await
        .context("HTTP embed/chunk_and_embed request failed")?
        .error_for_status()
        .context("HTTP embed/chunk_and_embed returned error status")?
        .json()
        .await
        .context("HTTP embed/chunk_and_embed response parse failed")?;

    let resp = wrapper.result;
    if resp.endianness != "le" {
        bail!("unsupported endianness: {}", resp.endianness);
    }
    let bytes = b64_decode(&resp.vectors_f32)?;
    validate_embed_passages_response(
        bytes.len(),
        resp.dimensions,
        resp.count,
        dimensions,
        "http_chunk_and_embed",
    )?;
    let vectors = decode_f32le_mat(&bytes, resp.dimensions, resp.count)?;
    Ok(resp.chunks.into_iter().zip(vectors).map(|(c, vector)| ChunkWithVector {
        content: c.content,
        start_line: c.start_line,
        end_line: c.end_line,
        chunk_type: c.chunk_type,
        language: c.language,
        vector,
    }).collect())
}

async fn http_chunk_and_embed(
    content: &str,
    path: &str,
    tier: &str,
    model: &str,
    dimensions: u32,
) -> Result<Vec<ChunkWithVector>> {
    with_embedder_recovery(|| http_chunk_and_embed_inner(content, path, tier, model, dimensions)).await
}

async fn http_chunk_and_embed_file_inner(
    file: &str,
    path: &str,
    tier: &str,
    model: &str,
    dimensions: u32,
) -> Result<Vec<ChunkWithVector>> {
    let wrapper: HttpResultWrapper<HttpChunkAndEmbedResp> = http_client()
        .post(format!("{}/embed/chunk_and_embed", http_base_url()))
        .json(&json!({
            "path": file,
            "display_path": path,
            "tier": tier,
            "model": model,
            "dimensions": dimensions,
        }))
        .send()
        .await
        .context("HTTP embed/chunk_and_embed (file) request failed")?
        .error_for_status()
        .context("HTTP embed/chunk_and_embed (file) returned error status")?
        .json()
        .await
        .context("HTTP embed/chunk_and_embed (file) response parse failed")?;

    let resp = wrapper.result;
    if resp.endianness != "le" {
        bail!("unsupported endianness: {}", resp.endianness);
    }
    let bytes = b64_decode(&resp.vectors_f32)?;
    validate_embed_passages_response(
        bytes.len(),
        resp.dimensions,
        resp.count,
        dimensions,
        "http_chunk_and_embed_file",
    )?;
    let vectors = decode_f32le_mat(&bytes, resp.dimensions, resp.count)?;
    Ok(resp.chunks.into_iter().zip(vectors).map(|(c, vector)| ChunkWithVector {
        content: c.content,
        start_line: c.start_line,
        end_line: c.end_line,
        chunk_type: c.chunk_type,
        language: c.language,
        vector,
    }).collect())
}

async fn http_chunk_and_embed_file(
    file: &str,
    path: &str,
    tier: &str,
    model: &str,
    dimensions: u32,
) -> Result<Vec<ChunkWithVector>> {
    with_embedder_recovery(|| http_chunk_and_embed_file_inner(file, path, tier, model, dimensions)).await
}

async fn http_rerank_inner(query: &str, docs: &[&str], model: &str, top_k: u32) -> Result<Vec<(usize, f32)>> {
    let wrapper: HttpResultWrapper<HttpRerankResp> = http_client()
        .post(format!("{}/embed/rerank", http_base_url()))
        .json(&json!({"query": query, "passages": docs, "model": model, "top_k": top_k}))
        .send()
        .await
        .context("HTTP embed/rerank request failed")?
        .error_for_status()
        .context("HTTP embed/rerank returned error status")?
        .json()
        .await
        .context("HTTP embed/rerank response parse failed")?;

    Ok(wrapper.result.results.into_iter().map(|r| (r.index, r.score)).collect())
}

async fn http_rerank(query: &str, docs: &[&str], model: &str, top_k: u32) -> Result<Vec<(usize, f32)>> {
    with_embedder_recovery(|| http_rerank_inner(query, docs, model, top_k)).await
}

// ============================================================================
// Public data types
// ============================================================================

/// Chunk metadata without embedding vector.
#[derive(Debug)]
pub struct ChunkMeta {
    pub content: String,
    pub start_line: i32,
    pub end_line: i32,
    pub chunk_type: String,
    pub language: String,
}

/// Chunk metadata with embedding vector (returned by chunk_and_embed).
#[derive(Debug)]
pub struct ChunkWithVector {
    pub content: String,
    pub start_line: i32,
    pub end_line: i32,
    pub chunk_type: String,
    pub language: String,
    pub vector: Vec<f32>,
}

// ============================================================================
// Public client API
// ============================================================================

/// Returns recommended concurrency for HTTP embedding workloads.
pub fn recommended_concurrency() -> usize {
    16
}

/// Always false — HTTP mode connects to a local server.
pub fn is_remote_mode() -> bool {
    false
}

/// No-op warmup (HTTP connections are stateless).
pub async fn warmup() -> Result<()> {
    Ok(())
}

/// An HTTP-backed model client.
///
/// All operations delegate to the HTTP embedder API on localhost.
pub struct PooledClient;

impl PooledClient {
    /// Chunk content without embedding.
    pub async fn chunk(&mut self, content: &str, path: &str, tier: &str) -> Result<Vec<ChunkMeta>> {
        http_chunk(content, path, tier).await
    }

    /// Chunk a file on disk without embedding.
    pub async fn chunk_file(&mut self, file: &str, path: &str, tier: &str) -> Result<Vec<ChunkMeta>> {
        http_chunk_file(file, path, tier).await
    }

    /// Chunk and embed in a single round-trip.
    pub async fn chunk_and_embed(
        &mut self,
        content: &str,
        path: &str,
        tier: &str,
        model: &str,
        dimensions: u32,
    ) -> Result<Vec<ChunkWithVector>> {
        http_chunk_and_embed(content, path, tier, model, dimensions).await
    }

    /// Chunk and embed a file on disk in a single round-trip.
    pub async fn chunk_and_embed_file(
        &mut self,
        file: &str,
        path: &str,
        tier: &str,
        model: &str,
        dimensions: u32,
    ) -> Result<Vec<ChunkWithVector>> {
        http_chunk_and_embed_file(file, path, tier, model, dimensions).await
    }

    /// Embed multiple passages.
    pub async fn embed_passages(&mut self, texts: &[String], model: &str, dimensions: u32) -> Result<Vec<Vec<f32>>> {
        http_embed_passages(texts, model, dimensions).await
    }

    /// Embed a search query.
    pub async fn embed_query(&mut self, text: &str, model: &str, dimensions: u32) -> Result<Vec<f32>> {
        http_embed_query(text, model, dimensions).await
    }

    /// Rerank documents against a query.
    pub async fn rerank(&mut self, query: &str, docs: &[&str], model: &str, top_k: u32) -> Result<Vec<(usize, f32)>> {
        http_rerank(query, docs, model, top_k).await
    }
}

/// Get an HTTP-backed model client.
pub async fn pooled() -> Result<PooledClient> {
    Ok(PooledClient)
}

// ============================================================================
// Top-level convenience functions
// ============================================================================

/// Chunk and embed content.
pub async fn chunk_and_embed(
    content: &str,
    path: &str,
    tier: &str,
    model: &str,
    dimensions: u32,
) -> Result<Vec<ChunkWithVector>> {
    http_chunk_and_embed(content, path, tier, model, dimensions).await
}

/// Chunk and embed a file on disk.
pub async fn chunk_and_embed_file(
    file: &str,
    path: &str,
    tier: &str,
    model: &str,
    dimensions: u32,
) -> Result<Vec<ChunkWithVector>> {
    http_chunk_and_embed_file(file, path, tier, model, dimensions).await
}

/// Embed multiple passages.
pub async fn embed_passages(
    texts: &[String],
    model: &str,
    dimensions: u32,
) -> Result<Vec<Vec<f32>>> {
    http_embed_passages(texts, model, dimensions).await
}

/// Embed a search query.
pub async fn embed_query(
    text: &str,
    model: &str,
    dimensions: u32,
) -> Result<Vec<f32>> {
    http_embed_query(text, model, dimensions).await
}

/// Chunk content without embedding.
pub async fn chunk(content: &str, path: &str, tier: &str) -> Result<Vec<ChunkMeta>> {
    http_chunk(content, path, tier).await
}

/// Chunk a file on disk without embedding.
pub async fn chunk_file(file: &str, path: &str, tier: &str) -> Result<Vec<ChunkMeta>> {
    http_chunk_file(file, path, tier).await
}

/// Rerank documents against a query.
pub async fn rerank(query: &str, docs: &[&str], model: &str, top_k: u32) -> Result<Vec<(usize, f32)>> {
    http_rerank(query, docs, model, top_k).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_decode_f32le_mat_multiple_vectors() {
        // 2 vectors of dimension 3
        let bytes: Vec<u8> = vec![
            0, 0, 128, 63, // 1.0
            0, 0, 0, 64,   // 2.0
            0, 0, 64, 64,  // 3.0
            0, 0, 128, 64, // 4.0
            0, 0, 160, 64, // 5.0
            0, 0, 192, 64, // 6.0
        ];
        let result = decode_f32le_mat(&bytes, 3, 2).unwrap();
        assert_eq!(result.len(), 2);
        assert_eq!(result[0], vec![1.0, 2.0, 3.0]);
        assert_eq!(result[1], vec![4.0, 5.0, 6.0]);
    }

    #[test]
    fn test_decode_f32le_mat_size_mismatch() {
        let bytes: Vec<u8> = vec![0; 10]; // Wrong size
        let result = decode_f32le_mat(&bytes, 3, 2);
        assert!(result.is_err());
    }

    #[test]
    fn test_decode_f32le_vec_correct() {
        let bytes: Vec<u8> = vec![
            0, 0, 128, 63, // 1.0
            0, 0, 0, 64,   // 2.0
        ];
        let result = decode_f32le_vec(&bytes, 2).unwrap();
        assert_eq!(result, vec![1.0, 2.0]);
    }

    // B6: Safe memory operations - decode_f32le_vec
    #[test]
    fn test_decode_f32le_vec_safe() {
        let bytes = vec![0u8; 4 * 10];
        let result = decode_f32le_vec(&bytes, 10);
        assert!(result.is_ok());
        assert_eq!(result.unwrap().len(), 10);
    }

    #[test]
    fn test_decode_f32le_vec_size_mismatch() {
        let bytes = vec![0u8; 4 * 5];
        let result = decode_f32le_vec(&bytes, 10);
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("invalid vector_f32 length"));
    }

    #[test]
    fn test_decode_f32le_vec_overflow_protection() {
        let bytes = vec![0u8; 100];
        let result = decode_f32le_vec(&bytes, usize::MAX);
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("invalid dimensions"));
    }

    #[test]
    fn test_decode_f32le_vec_partial_bytes() {
        let bytes = vec![0u8; 13]; // 13 bytes = 3.25 floats
        let result = decode_f32le_vec(&bytes, 3);
        assert!(result.is_err());
    }

    // B6: Safe memory operations - decode_f32le_mat
    #[test]
    fn test_decode_f32le_mat_safe() {
        let bytes = vec![0u8; 4 * 10 * 5];
        let result = decode_f32le_mat(&bytes, 10, 5);
        assert!(result.is_ok());
        let mat = result.unwrap();
        assert_eq!(mat.len(), 5);
        assert!(mat.iter().all(|v| v.len() == 10));
    }

    #[test]
    fn test_decode_f32le_mat_wrong_count() {
        let bytes = vec![0u8; 4 * 10 * 3];
        let result = decode_f32le_mat(&bytes, 10, 5);
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("invalid vectors_f32 length"));
    }

    #[test]
    fn test_decode_f32le_mat_overflow_protection() {
        let bytes = vec![0u8; 100];
        let result = decode_f32le_mat(&bytes, usize::MAX / 2, 10);
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("invalid dimensions"));
    }

    #[test]
    fn test_decode_f32le_mat_empty() {
        let bytes = vec![];
        let result = decode_f32le_mat(&bytes, 10, 0);
        assert!(result.is_ok());
        assert_eq!(result.unwrap().len(), 0);
    }

    #[test]
    fn test_decode_f32le_vec_values_correct() {
        let bytes: Vec<u8> = vec![
            0, 0, 128, 63,     // 1.0
            0, 0, 0, 64,       // 2.0
            0, 0, 64, 64,      // 3.0
            0, 0, 128, 64,     // 4.0
            154, 153, 153, 63, // 1.2 (approximately)
        ];
        let result = decode_f32le_vec(&bytes, 5).unwrap();
        assert_eq!(result.len(), 5);
        assert!((result[0] - 1.0).abs() < 0.001);
        assert!((result[1] - 2.0).abs() < 0.001);
        assert!((result[2] - 3.0).abs() < 0.001);
        assert!((result[3] - 4.0).abs() < 0.001);
        assert!((result[4] - 1.2).abs() < 0.001);
    }

    // ==================== MAX_EMBEDDING_DIM Validation Tests ====================

    #[test]
    fn test_decode_f32le_vec_rejects_zero_dimensions() {
        let bytes = vec![0u8; 100];
        let result = decode_f32le_vec(&bytes, 0);
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("invalid dimensions"));
        assert!(err.contains("deserialization corruption"));
    }

    #[test]
    fn test_decode_f32le_vec_rejects_excessive_dimensions() {
        let bytes = vec![0u8; 100];
        let result = decode_f32le_vec(&bytes, 38_871_760_896);
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("invalid dimensions"));
        assert!(err.contains("38871760896"));
    }

    #[test]
    fn test_decode_f32le_vec_accepts_max_valid_dimensions() {
        let bytes = vec![0u8; 4 * MAX_EMBEDDING_DIM];
        let result = decode_f32le_vec(&bytes, MAX_EMBEDDING_DIM);
        assert!(result.is_ok());
        assert_eq!(result.unwrap().len(), MAX_EMBEDDING_DIM);
    }

    #[test]
    fn test_decode_f32le_vec_rejects_just_over_max() {
        let bytes = vec![0u8; 4 * (MAX_EMBEDDING_DIM + 1)];
        let result = decode_f32le_vec(&bytes, MAX_EMBEDDING_DIM + 1);
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("invalid dimensions"));
    }

    #[test]
    fn test_decode_f32le_mat_rejects_zero_dimensions() {
        let bytes = vec![0u8; 100];
        let result = decode_f32le_mat(&bytes, 0, 5);
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("invalid dimensions"));
        assert!(err.contains("deserialization corruption"));
    }

    #[test]
    fn test_decode_f32le_mat_rejects_excessive_dimensions() {
        let bytes = vec![0u8; 100];
        let result = decode_f32le_mat(&bytes, 38_871_760_896, 2);
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("invalid dimensions"));
        assert!(err.contains("38871760896"));
    }

    #[test]
    fn test_decode_f32le_mat_accepts_max_valid_dimensions() {
        let count = 2;
        let bytes = vec![0u8; 4 * MAX_EMBEDDING_DIM * count];
        let result = decode_f32le_mat(&bytes, MAX_EMBEDDING_DIM, count);
        assert!(result.is_ok());
        let mat = result.unwrap();
        assert_eq!(mat.len(), count);
        assert!(mat.iter().all(|v| v.len() == MAX_EMBEDDING_DIM));
    }

    #[test]
    fn test_decode_f32le_mat_rejects_just_over_max() {
        let count = 2;
        let bytes = vec![0u8; 4 * (MAX_EMBEDDING_DIM + 1) * count];
        let result = decode_f32le_mat(&bytes, MAX_EMBEDDING_DIM + 1, count);
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("invalid dimensions"));
    }

    #[test]
    fn test_max_embedding_dim_constant_is_reasonable() {
        assert_eq!(MAX_EMBEDDING_DIM, 16384);
        assert!(MAX_EMBEDDING_DIM >= 4096, "Should accommodate large models");
        assert!(MAX_EMBEDDING_DIM <= 65536, "Should not be excessively large");
    }

    // ==================== Cross-Field Validation Tests ====================

    #[test]
    fn test_validate_embed_passages_response_valid() {
        let result = validate_embed_passages_response(20480, 512, 10, 512, "test");
        assert!(result.is_ok());
    }

    #[test]
    fn test_validate_embed_passages_response_detects_bytebuf_misread() {
        let result = validate_embed_passages_response(1000, 512, 10, 512, "test");
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("data integrity check failed"));
        assert!(err.contains("framing/deserialization bug"));
    }

    #[test]
    fn test_validate_embed_passages_response_detects_corrupted_dimensions() {
        let result = validate_embed_passages_response(20480, 38_871_760_896, 10, 512, "test");
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("invalid dimensions"));
        assert!(err.contains("38871760896"));
    }

    #[test]
    fn test_validate_embed_passages_response_detects_zero_dimensions() {
        let result = validate_embed_passages_response(20480, 0, 10, 512, "test");
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("invalid dimensions"));
    }

    #[test]
    fn test_validate_embed_passages_response_detects_count_mismatch() {
        let result = validate_embed_passages_response(20480, 512, 5, 512, "test");
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("data integrity check failed"));
    }

    #[test]
    fn test_validate_embed_query_response_valid() {
        let result = validate_embed_query_response(2048, 512, 512, "test");
        assert!(result.is_ok());
    }

    #[test]
    fn test_validate_embed_query_response_detects_bytebuf_misread() {
        let result = validate_embed_query_response(1000, 512, 512, "test");
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("data integrity check failed"));
    }

    #[test]
    fn test_validate_embed_query_response_detects_corrupted_dimensions() {
        let result = validate_embed_query_response(2048, 38_871_760_896, 512, "test");
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("invalid dimensions"));
    }

    #[test]
    fn test_validate_embed_query_response_detects_zero_dimensions() {
        let result = validate_embed_query_response(2048, 0, 512, "test");
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("invalid dimensions"));
    }

    #[test]
    fn test_cross_field_validation_catches_subtle_corruption() {
        let result = validate_embed_passages_response(20480, 1024, 10, 512, "test");
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("data integrity check failed"));
    }

    #[test]
    fn test_cross_field_validation_edge_case_single_vector() {
        let result = validate_embed_passages_response(4, 1, 1, 1, "test");
        assert!(result.is_ok());
    }

    #[test]
    fn test_cross_field_validation_edge_case_max_dimensions() {
        let vectors_len = MAX_EMBEDDING_DIM * 4 * 2;
        let result = validate_embed_passages_response(
            vectors_len,
            MAX_EMBEDDING_DIM,
            2,
            MAX_EMBEDDING_DIM as u32,
            "test",
        );
        assert!(result.is_ok());
    }

    // ==================== Embedder Recovery Tests ====================

    #[tokio::test]
    async fn test_is_retryable_error_detects_connect_error() {
        // Create a reqwest connection error by trying to connect to an invalid address
        let client = reqwest::Client::builder()
            .timeout(Duration::from_millis(100))
            .build()
            .unwrap();
        let err = client.get("http://127.0.0.1:1").send().await.unwrap_err();
        let anyhow_err: anyhow::Error = err.into();
        assert!(is_retryable_error(&anyhow_err), "should detect connection refused");
    }

    #[test]
    fn test_is_retryable_error_ignores_other_errors() {
        let err = anyhow::anyhow!("some random error");
        assert!(!is_retryable_error(&err), "should not detect non-connection error");
    }

    #[tokio::test]
    async fn test_is_retryable_error_traverses_chain() {
        // Wrap a connection error in context
        let client = reqwest::Client::builder()
            .timeout(Duration::from_millis(100))
            .build()
            .unwrap();
        let inner = client.get("http://127.0.0.1:1").send().await.unwrap_err();
        let wrapped: anyhow::Error = anyhow::Error::from(inner).context("outer context");
        assert!(is_retryable_error(&wrapped), "should find connection error in chain");
    }

    #[tokio::test]
    async fn test_is_retryable_error_detects_server_error() {
        use tokio::io::AsyncWriteExt;
        // Spawn a minimal HTTP server that returns 500
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            stream
                .write_all(b"HTTP/1.1 500 Internal Server Error
Content-Length: 0

")
                .await
                .unwrap();
        });
        let client = reqwest::Client::new();
        let resp = client.get(format!("http://{addr}")).send().await.unwrap();
        let err = resp.error_for_status().unwrap_err();
        let anyhow_err: anyhow::Error = err.into();
        assert!(is_retryable_error(&anyhow_err), "should detect HTTP 500 server error");
        server.await.unwrap();
    }

    fn test_reset_embedder_clears_state() {
        // Set up some state
        EMBEDDER_CHECKED.store(true, Ordering::SeqCst);
        EMBEDDER_PID.store(12345, Ordering::SeqCst);

        reset_embedder();

        assert!(!EMBEDDER_CHECKED.load(Ordering::SeqCst), "EMBEDDER_CHECKED should be false");
        assert_eq!(EMBEDDER_PID.load(Ordering::SeqCst), 0, "EMBEDDER_PID should be 0");
    }

    #[tokio::test]
    async fn test_with_embedder_recovery_returns_ok_on_success() {
        let result = with_embedder_recovery(|| async { Ok::<_, anyhow::Error>(42) }).await;
        assert_eq!(result.unwrap(), 42);
    }

    #[tokio::test]
    async fn test_with_embedder_recovery_propagates_non_connection_error() {
        let result = with_embedder_recovery(|| async {
            Err::<i32, _>(anyhow::anyhow!("not a connection error"))
        }).await;
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("not a connection error"));
    }

    #[tokio::test]
    async fn test_with_embedder_recovery_retries_on_connection_error() {
        use std::sync::atomic::AtomicU32;
        static CALL_COUNT: AtomicU32 = AtomicU32::new(0);

        // Reset for test isolation
        EMBEDDER_CHECKED.store(true, Ordering::SeqCst);
        CALL_COUNT.store(0, Ordering::SeqCst);

        let client = reqwest::Client::builder()
            .timeout(Duration::from_millis(50))
            .build()
            .unwrap();

        let result = with_embedder_recovery(|| {
            let client = client.clone();
            async move {
                let count = CALL_COUNT.fetch_add(1, Ordering::SeqCst);
                if count < 1 {
                    // First call fails with connection error
                    let inner = client.get("http://127.0.0.1:1").send().await.unwrap_err();
                    Err::<i32, _>(anyhow::Error::from(inner))
                } else {
                    // Second call succeeds
                    Ok(99)
                }
            }
        }).await;

        assert_eq!(result.unwrap(), 99);
        assert!(CALL_COUNT.load(Ordering::SeqCst) >= 2, "should have retried at least once");
    }

}
