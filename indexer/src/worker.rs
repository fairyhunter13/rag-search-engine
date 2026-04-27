//! Worker pool for processing file indexing work items.
//!
//! This module implements a channel-based worker pool. Workers block on
//! channel recv() when idle, achieving ~0% CPU usage.

use std::path::PathBuf;
use std::sync::{Arc, OnceLock};
use tokio::sync::mpsc;
use tokio::sync::Semaphore;
use anyhow::Result;

use crate::config::IndexConfig;
use crate::storage::{Storage, WriteQueue};
use crate::discover::SymlinkDir;

/// Work operation type
#[derive(Debug)]
pub enum WorkOperation {
    /// Index changed files
    IndexFiles { changed: Vec<PathBuf> },
    /// Delete files from index
    DeleteFiles { deleted: Vec<PathBuf> },
    /// Handle symlink changes (re-discover files)
    SymlinkChanged { paths: Vec<PathBuf> },
}

/// A unit of work for the worker pool
#[derive(Debug)]
pub struct WorkItem {
    pub project_key: String,
    pub root: Arc<PathBuf>,
    pub db_path: Arc<PathBuf>,
    pub include_dirs: Arc<Vec<PathBuf>>,
    pub symlink_dirs: Arc<Vec<SymlinkDir>>,
    pub storage: Arc<Storage>,
    pub write_queue: Arc<WriteQueue>,
    pub tier: String,
    pub dimensions: u32,
    pub index_cfg: Arc<IndexConfig>,
    pub operation: WorkOperation,
}

/// Global concurrency limit across all workers.
/// Prevents total file operations from exceeding this cap regardless of worker count.
/// Default: 16 concurrent file operations (configurable via OPENCODE_INDEXER_MAX_CONCURRENT_FILES)
pub(crate) fn global_file_semaphore() -> &'static Arc<Semaphore> {
    static SEM: OnceLock<Arc<Semaphore>> = OnceLock::new();
    SEM.get_or_init(|| {
        let max = std::env::var("OPENCODE_INDEXER_MAX_CONCURRENT_FILES")
            .ok()
            .and_then(|v| v.parse::<usize>().ok())
            .unwrap_or(8);
        Arc::new(Semaphore::new(max))
    })
}

/// Channel capacity for work items
pub const WORK_CHANNEL_CAPACITY: usize = 256;

/// Create a work channel
pub fn work_channel() -> (WorkSender, WorkReceiver) {
    let (tx, rx) = mpsc::channel(WORK_CHANNEL_CAPACITY);
    (WorkSender(tx), WorkReceiver(rx))
}

/// Sender half of the work channel
#[derive(Clone)]
pub struct WorkSender(mpsc::Sender<WorkItem>);

impl WorkSender {
    pub async fn send(&self, item: WorkItem) -> Result<(), mpsc::error::SendError<WorkItem>> {
        self.0.send(item).await
    }
    
    pub fn try_send(&self, item: WorkItem) -> Result<(), mpsc::error::TrySendError<WorkItem>> {
        self.0.try_send(item)
    }
}

/// Receiver half of the work channel (shared among workers)
pub struct WorkReceiver(mpsc::Receiver<WorkItem>);

/// Worker pool that processes work items
pub struct WorkerPool {
    workers: Vec<tokio::task::JoinHandle<()>>,
    shutdown: tokio::sync::broadcast::Sender<()>,
}

impl WorkerPool {
    /// Create a new worker pool with the specified number of workers
    pub fn new(
        num_workers: usize,
        rx: WorkReceiver,
        model_client_factory: impl Fn() -> crate::model_client::PooledClient + Send + Sync + 'static,
    ) -> Self {
        let (shutdown_tx, _) = tokio::sync::broadcast::channel(1);
        let rx = Arc::new(tokio::sync::Mutex::new(rx.0));
        let factory = Arc::new(model_client_factory);
        
        let mut workers = Vec::with_capacity(num_workers);
        
        for worker_id in 0..num_workers {
            let rx = Arc::clone(&rx);
            let mut shutdown_rx = shutdown_tx.subscribe();
            let factory = Arc::clone(&factory);
            
            let handle = tokio::spawn(async move {
                // Per-worker concurrency limit for file processing
                let file_semaphore = Arc::new(Semaphore::new(2));
                
                loop {
                    // Wait for work or shutdown
                    let item = tokio::select! {
                        biased;
                        
                        _ = shutdown_rx.recv() => {
                            tracing::debug!("worker {} received shutdown signal", worker_id);
                            break;
                        }
                        
                        item = async {
                            let mut guard = rx.lock().await;
                            guard.recv().await
                        } => item,
                    };
                    
                    let Some(item) = item else {
                        tracing::debug!("worker {} channel closed", worker_id);
                        break;
                    };
                    
                    tracing::debug!(
                        "worker {} processing {:?} for {}",
                        worker_id,
                        std::mem::discriminant(&item.operation),
                        item.project_key
                    );
                    
                    // Process the work item
                    if let Err(e) = process_work_item(item, &file_semaphore, &factory).await {
                        tracing::error!("worker {} error: {:?}", worker_id, e);
                    }
                }
                
                tracing::info!("worker {} exiting", worker_id);
            });
            
            workers.push(handle);
        }
        
        tracing::info!("started {} workers", num_workers);
        
        Self {
            workers,
            shutdown: shutdown_tx,
        }
    }
    
    /// Shutdown the worker pool gracefully
    pub async fn shutdown(self) {
        tracing::info!("shutting down worker pool");
        
        // Signal all workers to stop
        let _ = self.shutdown.send(());
        
        // Wait for all workers to finish
        for (i, handle) in self.workers.into_iter().enumerate() {
            if let Err(e) = handle.await {
                tracing::error!("worker {} panicked: {:?}", i, e);
            }
        }
        
        tracing::info!("worker pool shutdown complete");
    }
    
    /// Get the number of workers
    pub fn num_workers(&self) -> usize {
        self.workers.len()
    }
}

/// Process a single work item
async fn process_work_item(
    item: WorkItem,
    file_semaphore: &Arc<Semaphore>,
    _model_client_factory: &Arc<impl Fn() -> crate::model_client::PooledClient + Send + Sync>,
) -> Result<()> {
    match item.operation {
        WorkOperation::IndexFiles { changed } => {
            tracing::debug!("indexing {} files for {}", changed.len(), item.project_key);

            let global_sem = global_file_semaphore();

            // Process files in parallel with both global and per-worker semaphore limits
            let futures: Vec<_> = changed.into_iter().map(|path| {
                let sem = Arc::clone(file_semaphore);
                let global = Arc::clone(global_sem);
                let _storage = Arc::clone(&item.storage);
                let _write_queue = Arc::clone(&item.write_queue);
                let _root = Arc::clone(&item.root);
                let _include_dirs = Arc::clone(&item.include_dirs);

                async move {
                    // Acquire global limit first, then per-worker limit
                    let _global_permit = global.acquire().await.ok()?;
                    let _permit = sem.acquire().await.ok()?;

                    tracing::trace!("would index: {:?}", path);

                    Some(())
                }
            }).collect();

            futures::future::join_all(futures).await;
        }
        
        WorkOperation::DeleteFiles { deleted } => {
            tracing::debug!("deleting {} files for {}", deleted.len(), item.project_key);
            
            // Delete via write queue
            for path in deleted {
                let rel_path = path.strip_prefix(&*item.root)
                    .map(|p| p.to_string_lossy().to_string())
                    .unwrap_or_else(|_| path.to_string_lossy().to_string());
                    
                let _ = item.write_queue.delete(vec![rel_path]).await;
            }
        }
        
        WorkOperation::SymlinkChanged { paths } => {
            tracing::debug!("symlink changed: {:?} for {}", paths, item.project_key);
            // Invalidate caches and re-discover files
            // This would trigger a re-index of the affected symlink
        }
    }
    
    Ok(())
}

/// Calculate the default number of workers based on CPU count
pub fn default_num_workers() -> usize {
    let cpus = std::thread::available_parallelism()
        .map(|p| p.get())
        .unwrap_or(4);
    
    // Use half the CPUs, clamped to 2..8
    (cpus / 2).clamp(2, 8)
}
