//! Client for the Python model server (HTTP-only transport).
//!
//! All communication with the embedder uses the HTTP API on localhost:9998
//! (or the port set by `OPENCODE_EMBED_HTTP_PORT`).
//!
//! The daemon will auto-start the embedder if it's not already running.

use std::future::Future;
use std::os::unix::process::CommandExt;
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

// Per-operation timeouts — sized to typical completion times with headroom.
const HEALTH_TIMEOUT: Duration = Duration::from_secs(5);
const QUERY_TIMEOUT: Duration = Duration::from_secs(15);
const CHUNK_TIMEOUT: Duration = Duration::from_secs(30);
const EMBED_TIMEOUT: Duration = Duration::from_secs(60);
const RERANK_TIMEOUT: Duration = Duration::from_secs(30);

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
        let arr: [u8; 4] = chunk.try_into().map_err(|_| anyhow::anyhow!("internal: f32 decode chunk size mismatch at index {}", i))?;
        out[i] = f32::from_le_bytes(arr);
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
            let arr: [u8; 4] = chunk.try_into().map_err(|_| anyhow::anyhow!("internal: f32 decode chunk size mismatch at vec={}, index={}", i, j))?;
            vec[j] = f32::from_le_bytes(arr);
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
        match reqwest::Client::builder()
            .connect_timeout(std::time::Duration::from_secs(5))
            .pool_max_idle_per_host(4)
            .pool_idle_timeout(std::time::Duration::from_secs(30))
            .tcp_keepalive(std::time::Duration::from_secs(15))
            .build()
        {
            Ok(client) => client,
            Err(e) => {
                eprintln!("FATAL: Failed to build HTTP client: {}", e);
                std::process::exit(1);
            }
        }
    })
}

use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};

/// Whether we've already confirmed the embedder is running this session.
static EMBEDDER_CHECKED: AtomicBool = AtomicBool::new(false);

/// PID of the embedder process we spawned (0 = not spawned by us).
static EMBEDDER_PID: AtomicU32 = AtomicU32::new(0);

/// Unix-millis timestamp of last successful embed request (0 = never used).
static EMBEDDER_LAST_USED: AtomicU64 = AtomicU64::new(0);

/// Consecutive spawn failure count. Reset on first successful spawn.
/// Used as a circuit breaker to prevent infinite restart loops when the
/// embedder binary is broken/missing (e.g., onedir vs onefile mismatch).
static EMBEDDER_SPAWN_FAILURES: AtomicU32 = AtomicU32::new(0);

/// Unix-millis timestamp of the first spawn failure in the current burst (0 = no burst).
static EMBEDDER_SPAWN_FAILURE_START: AtomicU64 = AtomicU64::new(0);

/// Max consecutive spawn failures before giving up.
const MAX_SPAWN_FAILURES: u32 = 5;

/// Time window for counting consecutive spawn failures.
const SPAWN_FAILURE_WINDOW_MS: u64 = 30_000; // 30 seconds

/// Guard: idle-shutdown background task spawned at most once.
static IDLE_MONITOR_STARTED: std::sync::OnceLock<()> = std::sync::OnceLock::new();

/// In-flight request limiter to prevent overwhelming the embedder.
static INFLIGHT_SEM: std::sync::OnceLock<tokio::sync::Semaphore> = std::sync::OnceLock::new();

fn inflight_semaphore() -> &'static tokio::sync::Semaphore {
    INFLIGHT_SEM.get_or_init(|| {
        let limit = std::env::var("OPENCODE_INDEXER_INFLIGHT_LIMIT")
            .ok()
            .and_then(|v| v.parse::<usize>().ok())
            .unwrap_or(2);
        tracing::info!("in-flight request limit: {}", limit);
        tokio::sync::Semaphore::new(limit)
    })
}

/// Child handle for the embedder process we spawned (None = not spawned by us).
static EMBEDDER_CHILD: std::sync::Mutex<Option<std::process::Child>> = std::sync::Mutex::new(None);

/// Port file path for the embedder PID.
fn embedder_pid_path() -> Option<std::path::PathBuf> {
    dirs::home_dir().map(|h| h.join(".opencode").join("embedder.pid"))
}

/// Resolve the embedder binary path.
///
/// PyInstaller onedir mode creates a directory structure:
///   opencode-embedder/
///     _internal/    (Python runtime + libs)
///     opencode-embedder  (executable stub)
///
/// The legacy onefile mode produced a single file. We handle both.
fn embedder_binary() -> Option<std::path::PathBuf> {
    let home = dirs::home_dir()?;
    let bin = home.join(".opencode/bin");

    // onedir: directory containing the executable with same name
    let onedir = bin.join("opencode-embedder");
    if onedir.is_dir() {
        let exe = onedir.join("opencode-embedder");
        if exe.is_file() {
            return Some(exe);
        }
    }

    // onefile (legacy): single executable file
    if onedir.is_file() {
        return Some(onedir);
    }

    // onedir variant directory name
    let onedir_alt = bin.join("opencode-embedder-dir");
    if onedir_alt.is_dir() {
        let exe = onedir_alt.join("opencode-embedder");
        if exe.is_file() {
            return Some(exe);
        }
    }

    None
}

/// Ensure the Python embedder is running. Called lazily on first use.
/// This is a singleton check — if already running (by us or externally), returns immediately.
pub async fn ensure_embedder() {
    // Fast path: already checked this session
    if EMBEDDER_CHECKED.load(Ordering::Relaxed) {
        return;
    }

    // Serialize the spawn path — prevents duplicate embedder processes when
    // multiple concurrent requests all see EMBEDDER_CHECKED=false simultaneously.
    static SPAWN_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());
    let _guard = SPAWN_LOCK.lock().await;

    // Re-check after acquiring lock (another caller may have already spawned)
    if EMBEDDER_CHECKED.load(Ordering::Relaxed) {
        return;
    }

    // Check if embedder is already healthy
    if http_health().await.unwrap_or(false) {
        EMBEDDER_CHECKED.store(true, Ordering::Relaxed);
        EMBEDDER_SPAWN_FAILURES.store(0, Ordering::SeqCst);
        EMBEDDER_SPAWN_FAILURE_START.store(0, Ordering::SeqCst);
        tracing::info!("embedder already running");
        spawn_idle_monitor();
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
                    // Kill the stale external process directly (not our child)
                    unsafe { libc::kill(pid as i32, libc::SIGTERM); }
                    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
                    unsafe { libc::kill(pid as i32, libc::SIGKILL); }
                    // Also reap our own child if any
                    reap_embedder_child();
                }
                // Remove stale PID file
                let _ = tokio::fs::remove_file(&pid_path).await;
            }
        }
    }

    // Circuit breaker: if we've had too many recent spawn failures, give up.
    // Prevents infinite restart loops when the embedder binary is broken/missing.
    let failures = EMBEDDER_SPAWN_FAILURES.load(Ordering::Relaxed);
    let failure_start = EMBEDDER_SPAWN_FAILURE_START.load(Ordering::Relaxed);
    if failures >= MAX_SPAWN_FAILURES && failure_start > 0 {
        let now_ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64;
        if now_ms.saturating_sub(failure_start) < SPAWN_FAILURE_WINDOW_MS {
            tracing::error!(
                "embedder spawn circuit breaker: {} failures in {}s, giving up permanently",
                failures,
                SPAWN_FAILURE_WINDOW_MS / 1000
            );
            EMBEDDER_CHECKED.store(true, Ordering::SeqCst);
            return;
        }
        // Window expired, reset failure counter for a fresh attempt
        EMBEDDER_SPAWN_FAILURES.store(0, Ordering::SeqCst);
        EMBEDDER_SPAWN_FAILURE_START.store(0, Ordering::SeqCst);
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

    // Pass our PID to the embedder so it can monitor us
    let our_pid = std::process::id();
    
    let mut cmd = std::process::Command::new(&binary);
    cmd.env("OPENCODE_EMBED_HTTP_PORT", port.to_string())
        .env("OPENCODE_EMBEDDER_PARENT_PID", our_pid.to_string())
        // Cap thread counts to keep CPU ≤10% during active indexing.
        .env("OMP_NUM_THREADS", "2")
        .env("MKL_NUM_THREADS", "2")
        .env("ORT_NUM_THREADS", "4")
        .env("OPENBLAS_NUM_THREADS", "2")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null());

    // Redirect stderr to a log file so crash evidence is preserved.
    // Each spawn rotates: embedder.log → embedder.log.1 (previous run).
    if let Some(home) = dirs::home_dir() {
        let log_dir = home.join(".opencode");
        let log_path = log_dir.join("embedder.log");
        let log_prev = log_dir.join("embedder.log.1");
        let _ = std::fs::rename(&log_path, &log_prev);
        if let Ok(file) = std::fs::File::create(&log_path) {
            cmd.stderr(file);
        }
    }

    // REC-5+6: Set resource limits and priority on embedder child process.
    // - RLIMIT_AS: GPU mode needs large virtual address space for CUDA memory mapping.
    // - setpriority(5): absolute nice=5 — yields to user-facing processes (nice 0)
    //   but still gets reasonable CPU scheduling to finish embedding quickly.
    // - ioprio best-effort class (4) prevents I/O starvation during large scans.
    unsafe {
        cmd.pre_exec(|| {
            let mem_limit: u64 = 32 * 1024 * 1024 * 1024; // 32 GB virtual memory limit (GPU needs VRAM mapping)
            let rl = libc::rlimit { rlim_cur: mem_limit, rlim_max: mem_limit };
            libc::setrlimit(libc::RLIMIT_AS, &rl);
            libc::setpriority(libc::PRIO_PROCESS, 0, 5);
            libc::syscall(libc::SYS_ioprio_set, 1i64, 0i64, (1i64 << 13) | 4);
            Ok(())
        });
    }

    match cmd.spawn()
    {
        Ok(child) => {
            let pid = child.id();
            EMBEDDER_PID.store(pid, Ordering::Relaxed);
            // Tell Linux OOM-killer to prefer killing the embedder child (score 600)
            // over the indexer (500) and user processes (~0-200). Fails silently.
            #[cfg(target_os = "linux")]
            {
                let path = format!("/proc/{}/oom_score_adj", pid);
                let _ = std::fs::write(path, "600");
            }
            match EMBEDDER_CHILD.lock() {
                Ok(mut guard) => *guard = Some(child),
                Err(e) => {
                    tracing::error!("EMBEDDER_CHILD mutex poisoned: {}", e);
                    std::process::exit(1);
                }
            }
            tracing::info!("embedder spawned with PID {}", pid);

            // Write PID file
            if let Some(pid_path) = embedder_pid_path() {
                let _ = tokio::fs::write(&pid_path, pid.to_string()).await;
            }

            // Wait for it to become healthy (up to 90s for model loading)
            if wait_for_embedder(90).await {
                tracing::info!("embedder healthy on port {}", port);
                EMBEDDER_CHECKED.store(true, Ordering::Relaxed);
                EMBEDDER_SPAWN_FAILURES.store(0, Ordering::SeqCst);
                EMBEDDER_SPAWN_FAILURE_START.store(0, Ordering::SeqCst);
                touch_last_used();
                spawn_idle_monitor();
            } else {
                tracing::error!("embedder failed to become healthy after 90s, killing and resetting");
                reap_embedder_child();
                if let Some(pid_path) = embedder_pid_path() {
                    let _ = tokio::fs::remove_file(&pid_path).await;
                }
                EMBEDDER_PID.store(0, Ordering::SeqCst);
                EMBEDDER_CHECKED.store(false, Ordering::SeqCst);
                return;
            }
        }
        Err(e) => {
            let failures = EMBEDDER_SPAWN_FAILURES.fetch_add(1, Ordering::SeqCst) + 1;
            let now_ms = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_millis() as u64;
            EMBEDDER_SPAWN_FAILURE_START.compare_exchange(
                0, now_ms, Ordering::SeqCst, Ordering::SeqCst
            ).ok();

            tracing::error!(
                "failed to spawn embedder (attempt {}/{}): {}",
                failures, MAX_SPAWN_FAILURES, e
            );

            if failures >= MAX_SPAWN_FAILURES {
                tracing::error!(
                    "embedder spawn permanently disabled after {} consecutive failures",
                    failures
                );
                EMBEDDER_CHECKED.store(true, Ordering::SeqCst);
            } else {
                // Allow one more retry cycle
                EMBEDDER_CHECKED.store(false, Ordering::SeqCst);
            }
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

/// The embedder is a child of the indexer and should never shut down independently.
/// It dies only when the indexer itself shuts down (via reap_embedder_child on exit).
/// This is required because the indexer depends on the embedder for search, and
/// an idle embedder being killed while the indexer is still servicing TUI connections
/// causes "Not watching" / "Not indexed" states and breaks codebase_search.
///
/// The old idle monitor killed the embedder after OPENCODE_INDEXER_EMBEDDER_IDLE_SECS
/// (default 300s) of inactivity regardless of whether the indexer had active clients.
/// Re-enabled via env: OPENCODE_INDEXER_EMBEDDER_IDLE_SECS=300 (set explicitly, not default).
fn spawn_idle_monitor() {
    // Only enable embedder idle shutdown if explicitly opted in via env var.
    // The default is disabled — the embedder lives as long as the indexer.
    let idle_secs = std::env::var("OPENCODE_INDEXER_EMBEDDER_IDLE_SECS")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .unwrap_or(0); // 0 = disabled

    if idle_secs == 0 {
        tracing::info!("embedder idle monitor disabled (embedder lives as long as indexer)");
        return;
    }

    IDLE_MONITOR_STARTED.get_or_init(|| {
        tracing::info!("embedder idle monitor enabled (timeout={}s)", idle_secs);
        tokio::spawn(async move {
            let threshold = Duration::from_secs(idle_secs);
            loop {
                tokio::time::sleep(Duration::from_secs(120)).await;
                let last = EMBEDDER_LAST_USED.load(Ordering::Relaxed);
                if last == 0 {
                    continue;
                }
                let now = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_millis() as u64;
                let elapsed = Duration::from_millis(now.saturating_sub(last));
                if elapsed >= threshold {
                    tracing::info!(
                        "embedder idle for {}s (threshold {}s), shutting down",
                        elapsed.as_secs(),
                        idle_secs
                    );
                    shutdown_embedder();
                    EMBEDDER_CHECKED.store(false, Ordering::SeqCst);
                    EMBEDDER_LAST_USED.store(0, Ordering::SeqCst);
                }
            }
        });
    });
}

/// Kill the embedder child process and wait for it to be reaped (no zombies).
fn reap_embedder_child() {
    let pid = EMBEDDER_PID.load(Ordering::Relaxed);
    if pid != 0 {
        unsafe { libc::kill(pid as i32, libc::SIGTERM); }
        // Give it a moment to exit gracefully
        std::thread::sleep(std::time::Duration::from_millis(500));
    }
    if let Ok(mut guard) = EMBEDDER_CHILD.lock() {
        if let Some(ref mut child) = *guard {
            match child.try_wait() {
                Ok(Some(_)) => {} // already exited
                Ok(None) => {
                    // Still running, force kill and wait
                    let _ = child.kill();
                    let _ = child.wait();
                }
                Err(_) => {
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
        }
        *guard = None;
    }
}

/// Shut down the embedder if we spawned it.
pub fn shutdown_embedder() {
    let pid = EMBEDDER_PID.load(Ordering::Relaxed);
    if pid == 0 {
        return;
    }
    tracing::info!("shutting down embedder PID {}", pid);
    reap_embedder_child();
    // Clean up PID file
    if let Some(pid_path) = embedder_pid_path() {
        let _ = std::fs::remove_file(&pid_path);
    }
    EMBEDDER_PID.store(0, Ordering::Relaxed);
}

/// Reset embedder state so the next call to `ensure_embedder` will respawn it.
///
/// Called when an HTTP request detects the embedder has gone away (ECONNREFUSED).
/// Does NOT reset if the spawn circuit breaker has tripped.
fn reset_embedder() {
    let failures = EMBEDDER_SPAWN_FAILURES.load(Ordering::Relaxed);
    if failures >= MAX_SPAWN_FAILURES {
        tracing::warn!("embedder spawn circuit breaker active, not resetting");
        return;
    }
    tracing::warn!("embedder appears to have crashed — resetting state for respawn");
    EMBEDDER_SPAWN_FAILURES.store(0, Ordering::SeqCst);
    EMBEDDER_SPAWN_FAILURE_START.store(0, Ordering::SeqCst);
    EMBEDDER_CHECKED.store(false, Ordering::SeqCst);
    EMBEDDER_PID.store(0, Ordering::SeqCst);
}

/// Return `true` only for connection-level failures that indicate the embedder
/// process died (ECONNREFUSED, connection reset), timeouts, and transient
/// HTTP 5xx errors (502/503/504).  HTTP 500 and 501 are NOT retryable.
fn is_retryable_error(e: &anyhow::Error) -> bool {
    for cause in e.chain() {
        if let Some(req) = cause.downcast_ref::<reqwest::Error>() {
            if req.is_connect() || req.is_timeout() {
                return true;
            }
            if let Some(status) = req.status() {
                if status.is_server_error()
                    && status != reqwest::StatusCode::INTERNAL_SERVER_ERROR
                    && status != reqwest::StatusCode::NOT_IMPLEMENTED
                {
                    return true;
                }
            }
        }
    }
    false
}

/// Return `true` when `e` indicates the embedder is overloaded (HTTP 503/429)
/// but still alive — these should trigger backoff, not a process restart.
fn is_overloaded_error(e: &anyhow::Error) -> bool {
    for cause in e.chain() {
        if let Some(req) = cause.downcast_ref::<reqwest::Error>() {
            if let Some(status) = req.status() {
                return status == reqwest::StatusCode::TOO_MANY_REQUESTS
                    || status == reqwest::StatusCode::SERVICE_UNAVAILABLE;
            }
        }
    }
    false
}

/// Record current time as last embed usage (unix millis).
fn touch_last_used() {
    use std::time::{SystemTime, UNIX_EPOCH};
    let ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64;
    EMBEDDER_LAST_USED.store(ms, Ordering::Relaxed);
}

async fn with_embedder_recovery<T, F, Fut>(op: F) -> Result<T>
where
    F: Fn() -> Fut,
    Fut: Future<Output = Result<T>>,
{
    const MAX_RETRIES: u32 = 2;
    const MAX_BACKOFF_RETRIES: u32 = 3;

    // REC-1: Acquire in-flight permit to cap concurrent requests
    let t_wait = tokio::time::Instant::now();
    let _permit = inflight_semaphore()
        .acquire()
        .await
        .map_err(|_| anyhow::anyhow!("in-flight semaphore closed"))?;

    // REC-10: Log if we waited a long time for a permit
    let wait_ms = t_wait.elapsed().as_millis();
    if wait_ms > 2000 {
        tracing::warn!("embed semaphore wait={:.1}s (server may be overloaded)", wait_ms as f64 / 1000.0);
    }

    // Gradual ramp-up tracking: after an embedder restart, ease into full
    // concurrency to avoid a thundering herd that overwhelms the fresh
    // embedder and causes cascading failures.
    let mut ramp_up_remaining: u32 = 10; // first N requests after restart get delayed
    let mut failures = 0u32;
    let mut backoff_failures = 0u32;
    loop {
        // Gradual ramp-up: after a restart, introduce a small delay for the
        // first few requests so the embedder can initialize its model caches
        // and GPU memory allocators before receiving the full load.
        if ramp_up_remaining > 0 && failures == 0 && backoff_failures == 0 {
            ramp_up_remaining -= 1;
            let ramp_wait = Duration::from_millis(50 * (11 - ramp_up_remaining) as u64);
            tokio::time::sleep(ramp_wait).await;
        }

        match op().await {
            Ok(v) => {
                touch_last_used();
                return Ok(v);
            }
            // REC-2: Backoff on 503/429 — embedder alive but busy
            Err(e) if backoff_failures < MAX_BACKOFF_RETRIES && is_overloaded_error(&e) => {
                backoff_failures += 1;
                let delay = Duration::from_millis(500 * 2u64.pow(backoff_failures - 1));
                tracing::warn!(
                    "embedder overloaded (503/429), backing off {}ms (attempt {})",
                    delay.as_millis(), backoff_failures
                );
                tokio::time::sleep(delay).await;
            }
            // Connection refused — embedder process likely died
            Err(e) if failures < MAX_RETRIES && is_retryable_error(&e) => {
                // Check if spawn circuit breaker is active before retrying
                let spawn_failures = EMBEDDER_SPAWN_FAILURES.load(Ordering::Relaxed);
                if spawn_failures >= MAX_SPAWN_FAILURES {
                    tracing::error!(
                        "embedder spawn circuit breaker active ({} failures), giving up",
                        spawn_failures
                    );
                    return Err(e);
                }

                failures += 1;
                tracing::warn!(
                    "embedder unreachable, resetting and retrying (attempt {}): {}",
                    failures, e
                );
                reset_embedder();
                tokio::time::sleep(Duration::from_millis(500)).await;
                ensure_embedder().await;
                // Reset ramp-up counter after restart — the fresh embedder
                // needs gentle warmup to avoid cascading crashes.
                ramp_up_remaining = 10;
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
        .timeout(HEALTH_TIMEOUT)
        .send()
        .await
        .context("HTTP health check failed")?;
    Ok(resp.status().is_success())
}

async fn http_embed_passages_inner(texts: &[String], model: &str, dimensions: u32) -> Result<Vec<Vec<f32>>> {
    let wrapper: HttpResultWrapper<HttpPassagesF32Resp> = http_client()
        .post(format!("{}/embed/passages_f32", http_base_url()))
        .json(&json!({"passages": texts, "model": model, "dimensions": dimensions}))
        .timeout(EMBED_TIMEOUT)
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
        .timeout(QUERY_TIMEOUT)
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
        .timeout(CHUNK_TIMEOUT)
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
        .timeout(CHUNK_TIMEOUT)
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

async fn http_rerank_inner(query: &str, docs: &[&str], model: &str, top_k: u32) -> Result<Vec<(usize, f32)>> {
    let wrapper: HttpResultWrapper<HttpRerankResp> = http_client()
        .post(format!("{}/embed/rerank", http_base_url()))
        .json(&json!({"query": query, "passages": docs, "model": model, "top_k": top_k}))
        .timeout(RERANK_TIMEOUT)
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

// ============================================================================
// Public client API
// ============================================================================

/// Returns recommended concurrency for HTTP embedding workloads.
///
/// Defaults to 2 — matches the embedder's default worker count (1-2) and
/// the reduced in-flight semaphore limit (2). Override with
/// `OPENCODE_INDEXER_EMBED_CONCURRENCY`.
pub fn recommended_concurrency() -> usize {
    std::env::var("OPENCODE_INDEXER_EMBED_CONCURRENCY")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .unwrap_or(2)
}

/// Returns true when running against a remote (non-local) embedding server.
/// Set OPENCODE_INDEXER_REMOTE=1 to enable remote mode.
/// TODO: wire remote endpoint URL and auth when remote infrastructure is added.
pub fn is_remote_mode() -> bool {
    std::env::var("OPENCODE_INDEXER_REMOTE").as_deref() == Ok("1")
}

/// An HTTP-backed embedder client.
///
/// All operations delegate to the HTTP embedder API on localhost.
pub struct EmbedderClient;

impl EmbedderClient {
    /// Chunk content without embedding.
    pub async fn chunk(&mut self, content: &str, path: &str, tier: &str) -> Result<Vec<ChunkMeta>> {
        http_chunk(content, path, tier).await
    }

    /// Chunk a file on disk without embedding.
    pub async fn chunk_file(&mut self, file: &str, path: &str, tier: &str) -> Result<Vec<ChunkMeta>> {
        http_chunk_file(file, path, tier).await
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

/// Get an HTTP-backed embedder client.
pub async fn client() -> Result<EmbedderClient> {
    Ok(EmbedderClient)
}


