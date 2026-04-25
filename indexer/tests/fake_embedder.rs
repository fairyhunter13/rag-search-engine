//! Minimal fake Python embedder server for integration tests.
//!
//! Implements only the endpoints the Rust indexer actually calls, returning
//! zero-vector embeddings and trivial single-chunk responses. No real ML
//! inference is performed — the goal is correctness of the Rust daemon logic,
//! not embedding quality.
//!
//! Uses axum (already in Cargo.toml) bound to 127.0.0.1:0 so the OS picks a
//! free port. The assigned port is readable via `FakeEmbedder::port()`.

use std::sync::Arc;

use axum::{
    Json, Router,
    http::StatusCode,
    routing::{get, post},
};
use serde_json::{Value, json};
use tokio::net::TcpListener;
use tokio::sync::Notify;

// ---------------------------------------------------------------------------
// Shared server state
// ---------------------------------------------------------------------------

#[derive(Clone)]
struct Srv {
    shutdown: Arc<Notify>,
}

// ---------------------------------------------------------------------------
// Vector helpers
// ---------------------------------------------------------------------------

const DIMS: usize = 384;

fn zero_b64(floats: usize) -> String {
    use base64::Engine as _;
    base64::engine::general_purpose::STANDARD.encode(vec![0u8; floats * 4])
}

// ---------------------------------------------------------------------------
// Endpoint handlers
// ---------------------------------------------------------------------------

async fn health() -> Json<Value> {
    Json(json!({"status": "ok"}))
}

async fn embed_passages(Json(body): Json<Value>) -> Json<Value> {
    let count = body["texts"]
        .as_array()
        .map(|a| a.len())
        .unwrap_or(1)
        .max(1);
    Json(json!({
        "result": {
            "vectors_f32": zero_b64(count * DIMS),
            "dimensions": DIMS,
            "count": count,
            "endianness": "le"
        }
    }))
}

async fn embed_query(Json(_body): Json<Value>) -> Json<Value> {
    Json(json!({
        "result": {
            "vector_f32": zero_b64(DIMS),
            "dimensions": DIMS,
            "endianness": "le"
        }
    }))
}

fn fake_chunk(content: &str) -> Value {
    json!({
        "content": content,
        "start_line": 0,
        "end_line": 1,
        "chunk_type": "block",
        "language": "unknown"
    })
}

async fn embed_chunk(Json(body): Json<Value>) -> Json<Value> {
    let content = body["content"].as_str().unwrap_or("");
    Json(json!({"result": {"chunks": [fake_chunk(content)]}}))
}

async fn embed_chunk_file(Json(body): Json<Value>) -> Json<Value> {
    let content = body["file_path"].as_str().unwrap_or("");
    Json(json!({"result": {"chunks": [fake_chunk(content)]}}))
}

async fn chunk_and_embed(Json(body): Json<Value>) -> Json<Value> {
    let content = body["content"]
        .as_str()
        .or_else(|| body["file_path"].as_str())
        .unwrap_or("");
    Json(json!({
        "result": {
            "chunks": [fake_chunk(content)],
            "vectors_f32": zero_b64(DIMS),
            "dimensions": DIMS,
            "count": 1,
            "endianness": "le"
        }
    }))
}

async fn rerank(Json(_body): Json<Value>) -> Json<Value> {
    Json(json!({"result": {"results": [{"index": 0, "score": 0.5}]}}))
}

async fn shutdown_handler(
    axum::extract::State(srv): axum::extract::State<Srv>,
) -> StatusCode {
    srv.shutdown.notify_one();
    StatusCode::OK
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/// Handle to the running fake embedder.
pub struct FakeEmbedder {
    pub port: u16,
    shutdown: Arc<Notify>,
    _task: tokio::task::JoinHandle<()>,
}

#[allow(dead_code)]
impl FakeEmbedder {
    /// Bind to a random port on 127.0.0.1 and start serving.
    pub async fn start() -> anyhow::Result<Self> {
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let port = listener.local_addr()?.port();

        let shutdown = Arc::new(Notify::new());
        let srv = Srv { shutdown: shutdown.clone() };

        let app = Router::new()
            .route("/health", get(health))
            .route("/embed/passages_f32", post(embed_passages))
            .route("/embed/query_f32", post(embed_query))
            .route("/embed/chunk", post(embed_chunk))
            .route("/embed/chunk_file", post(embed_chunk_file))
            .route("/embed/chunk_and_embed", post(chunk_and_embed))
            .route("/embed/chunk_and_embed_f32", post(chunk_and_embed))
            .route("/embed/rerank", post(rerank))
            .route("/shutdown", post(shutdown_handler))
            .with_state(srv);

        let shutdown_clone = shutdown.clone();
        let task = tokio::spawn(async move {
            axum::serve(listener, app)
                .with_graceful_shutdown(async move { shutdown_clone.notified().await })
                .await
                .ok();
        });

        // Wait for the server to be reachable (max 5s)
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(5))
            .build()?;
        let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(5);
        loop {
            if client
                .get(format!("http://127.0.0.1:{port}/health"))
                .send()
                .await
                .is_ok()
            {
                break;
            }
            if tokio::time::Instant::now() > deadline {
                anyhow::bail!("fake embedder did not start within 5s");
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }

        Ok(Self { port, shutdown, _task: task })
    }

    pub fn url(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }

    pub fn port(&self) -> u16 {
        self.port
    }

    /// Environment variables to point the Rust daemon at this server.
    pub fn env_vars(&self) -> Vec<(&'static str, String)> {
        vec![("OPENCODE_EMBED_HTTP_PORT", self.port.to_string())]
    }
}

impl Drop for FakeEmbedder {
    fn drop(&mut self) {
        self.shutdown.notify_one();
    }
}
