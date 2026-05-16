//! OpenCode Indexer library
//!
//! This library exposes internal modules for testing purposes.
//! The main entry point is the `opencode-indexer` binary.

pub mod cleaner;
pub mod cli;
pub mod compaction;
pub mod config;
pub mod daemon;
pub mod handlers;
pub mod discover;
pub mod search;
pub mod memory_watcher;
pub mod tui;
pub mod wait;
pub mod hardware;
pub mod http_server;
pub mod links;
pub mod model_client;
pub mod process_group;
pub mod simd;
pub mod storage;
pub mod watcher;
pub mod watcher_startup;

// Re-export SIMD functions for convenience
pub use simd::{batch_cosine_similarity, cosine_similarity, rerank_by_cosine};
