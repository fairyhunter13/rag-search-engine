use anyhow::Result;
use clap::Parser;
use tracing_subscriber::{fmt, EnvFilter};

use opencode_indexer::cli;

#[cfg(not(target_env = "msvc"))]
#[global_allocator]
static GLOBAL: tikv_jemallocator::Jemalloc = tikv_jemallocator::Jemalloc;

/// Jemalloc configuration applied at allocator init (before main).
/// Reduces RSS by returning dirty/muzzy pages to OS faster.
/// Note: jemalloc reads this symbol at library init, unlike MALLOC_CONF env var
/// which is too late when set via std::env::set_var.
#[cfg(not(target_env = "msvc"))]
#[unsafe(export_name = "_rjem_malloc_conf")]
pub static JEMALLOC_CONF: &[u8] = b"background_thread:true,dirty_decay_ms:1000,muzzy_decay_ms:500\0";

// Limit thread pools to prevent excessive thread creation.
// LanceDB creates its own multi-threaded tokio runtime, so we need to cap all thread pools
// BEFORE any runtime starts (env vars only work if set before runtime initialization).
fn limit_thread_pools() {
    let num_cpus = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4);
    // SAFETY: Setting env vars is safe when done before spawning threads.
    // We call this at program start before any async runtime is created.
    unsafe {
        if std::env::var("TOKIO_WORKER_THREADS").is_err() {
            let workers = (num_cpus / 4).max(2);
            std::env::set_var("TOKIO_WORKER_THREADS", workers.to_string());
        }
        if std::env::var("RAYON_NUM_THREADS").is_err() {
            // Dynamic rayon threads: max(1, available_cpus / 4). Override via OPENCODE_SEARCH_RAYON_THREADS.
            let rayon_threads = std::env::var("OPENCODE_SEARCH_RAYON_THREADS")
                .ok()
                .and_then(|v| v.trim().parse::<usize>().ok())
                .filter(|&n| n > 0)
                .unwrap_or_else(|| (num_cpus / 4).max(1));
            std::env::set_var("RAYON_NUM_THREADS", rayon_threads.to_string());
        }
        if std::env::var("OMP_NUM_THREADS").is_err() {
            std::env::set_var("OMP_NUM_THREADS", (num_cpus / 4).max(1).to_string());
        }
        if std::env::var("MKL_NUM_THREADS").is_err() {
            std::env::set_var("MKL_NUM_THREADS", (num_cpus / 4).max(1).to_string());
        }
        if std::env::var("OPENBLAS_NUM_THREADS").is_err() {
            std::env::set_var("OPENBLAS_NUM_THREADS", (num_cpus / 4).max(1).to_string());
        }
    }
}

/// Lower process priority so indexer doesn't compete with user workload.
/// nice(10) = lower CPU priority, ioprio IDLE = only use I/O when nothing else needs it.
#[cfg(unix)]
fn deprioritize_process() {
    if std::env::var("OPENCODE_INDEXER_NO_DEPRIORITIZE").is_ok() {
        return;
    }

    // Lower CPU scheduling priority (nice value 10)
    let ret = unsafe { libc::nice(10) };
    if ret == -1 {
        let err = std::io::Error::last_os_error();
        if err.raw_os_error() != Some(0) {
            eprintln!("warning: failed to set nice(10): {}", err);
        }
    }

    // Set I/O scheduling to IDLE class (class 3, data 0)
    // IOPRIO_WHO_PROCESS = 1, pid = 0 (current process)
    #[cfg(target_os = "linux")]
    {
        const IOPRIO_WHO_PROCESS: i32 = 1;
        const IOPRIO_CLASS_IDLE: i32 = 3;
        let ioprio = (IOPRIO_CLASS_IDLE << 13) | 0;
        let ret = unsafe { libc::syscall(libc::SYS_ioprio_set, IOPRIO_WHO_PROCESS, 0, ioprio) };
        if ret == -1 {
            eprintln!("warning: failed to set I/O priority to IDLE: {}", std::io::Error::last_os_error());
        }
    }
}

#[cfg(not(unix))]
fn deprioritize_process() {}

// Use synchronous main to set env vars BEFORE tokio runtime starts.
// This is critical because LanceDB creates its own runtime and reads TOKIO_WORKER_THREADS.
fn main() -> Result<()> {
    // Set thread limits FIRST, before ANY runtime is created
    limit_thread_pools();
    deprioritize_process();

    // Initialize logging
    fmt()
        .with_env_filter(
            EnvFilter::from_default_env().add_directive("opencode_indexer=info".parse()?),
        )
        .with_target(false)
        .with_writer(std::io::stderr)
        .init();

    // Create single-threaded runtime manually
    // Benefits: event-loop style, ~0% CPU when idle, simpler debugging
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;

    // Run the CLI
    let args = cli::Args::parse();
    rt.block_on(cli::run(args))
}
