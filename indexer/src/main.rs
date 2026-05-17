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
pub static JEMALLOC_CONF: &[u8] = b"background_thread:true,dirty_decay_ms:500,muzzy_decay_ms:250\0";

// Limit thread pools to prevent excessive thread creation.
// LanceDB creates its own multi-threaded tokio runtime, so we need to cap all thread pools
// BEFORE any runtime starts (env vars only work if set before runtime initialization).
fn limit_thread_pools() {
    let num_cpus = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4);
    // Conservative defaults: background service shouldn't compete with user workload.
    // All caps at 2 threads max. Override via env vars for power users.
    unsafe {
        if std::env::var("TOKIO_WORKER_THREADS").is_err() {
            let workers = (num_cpus / 4).clamp(1, 2);
            std::env::set_var("TOKIO_WORKER_THREADS", workers.to_string());
        }
        if std::env::var("RAYON_NUM_THREADS").is_err() {
            let rayon_threads = std::env::var("OPENCODE_SEARCH_RAYON_THREADS")
                .ok()
                .and_then(|v| v.trim().parse::<usize>().ok())
                .filter(|&n| n > 0)
                .unwrap_or_else(|| (num_cpus / 4).clamp(1, 2));
            std::env::set_var("RAYON_NUM_THREADS", rayon_threads.to_string());
        }
        // BLAS/LAPACK threads: always 1 for background service
        if std::env::var("OMP_NUM_THREADS").is_err() {
            std::env::set_var("OMP_NUM_THREADS", "1");
        }
        if std::env::var("MKL_NUM_THREADS").is_err() {
            std::env::set_var("MKL_NUM_THREADS", "1");
        }
        if std::env::var("OPENBLAS_NUM_THREADS").is_err() {
            std::env::set_var("OPENBLAS_NUM_THREADS", "1");
        }
    }
}

/// Lower process priority so indexer doesn't compete with user workload.
/// setpriority(PRIO_PROCESS, 0, 5) = absolute nice value 5 (not incremental).
/// ioprio BEST_EFFORT (4) = normal disk I/O priority below user processes.
#[cfg(unix)]
fn deprioritize_process() {
    if std::env::var("OPENCODE_INDEXER_NO_DEPRIORITIZE").is_ok() {
        return;
    }

    // Set absolute CPU nice to 5 (yields to user apps at 0, but gets reasonable scheduling)
    let ret = unsafe { libc::setpriority(libc::PRIO_PROCESS, 0, 5) };
    if ret == -1 {
        let err = std::io::Error::last_os_error();
        if err.raw_os_error() != Some(0) {
            eprintln!("warning: failed to set nice to 5: {}", err);
        }
    }

    // Set I/O scheduling to BEST_EFFORT class (class 1, data 4)
    // IOPRIO_WHO_PROCESS = 1, pid = 0 (current process)
    #[cfg(target_os = "linux")]
    {
        const IOPRIO_WHO_PROCESS: i32 = 1;
        const IOPRIO_CLASS_BE: i32 = 1;
        let ioprio = (IOPRIO_CLASS_BE << 13) | 4;
        let ret = unsafe { libc::syscall(libc::SYS_ioprio_set, IOPRIO_WHO_PROCESS, 0, ioprio) };
        if ret == -1 {
            eprintln!("warning: failed to set I/O priority to BEST_EFFORT: {}", std::io::Error::last_os_error());
        }
    }
}

#[cfg(not(unix))]
fn deprioritize_process() {}

// Use synchronous main to set env vars BEFORE tokio runtime starts.
// This is critical because LanceDB creates its own runtime and reads TOKIO_WORKER_THREADS.

/// Install a panic hook that sends a desktop notification with the crash
/// location (file, line, message) before the process aborts.
fn install_panic_hook() {
    let default_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        // Call the default hook first (prints to stderr)
        default_hook(info);

        // Extract crash location
        let location = info
            .location()
            .map(|l| format!("{}:{}:{}", l.file(), l.line(), l.column()))
            .unwrap_or_else(|| "unknown location".to_string());
        let msg = info
            .payload()
            .downcast_ref::<&str>()
            .map(|s| s.to_string())
            .or_else(|| info.payload().downcast_ref::<String>().cloned())
            .unwrap_or_else(|| "unknown panic".to_string());

        let body = format!("{location}\n{msg}");
        let _ = std::process::Command::new("notify-send")
            .args(["-u", "critical", "-a", "opencode-indexer", "opencode: Indexer Panic", &body])
            .spawn();
    }));
}

fn main() -> Result<()> {
    install_panic_hook();
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

    // Multi-threaded runtime with a small worker pool.
    // Required to serve concurrent RPC requests (search, status, etc.) without
    // queuing delays that cause socket timeouts under parallel clients.
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        // Exactly 1 blocking thread: physically limits CPU to 100% on 1 core
        // (= 4.2% of 24 cores, well under the 5% budget).
        // No governor, no speed cap, no adaptive delay needed — the thread
        // count itself IS the CPU bound. When there's work, it runs at full
        // speed on 1 core. When idle, it's 0% CPU.
        .max_blocking_threads(1)
        .enable_all()
        .build()?;

    // Run the CLI
    let args = cli::Args::parse();
    rt.block_on(cli::run(args))
}
