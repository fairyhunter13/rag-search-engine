//! HTTP server for the indexer daemon.
//!
//! Exposes two endpoints:
//!   POST /rpc   — {"method": "...", "params": {...}} → {"result": ...}
//!   GET  /ping  — health check, returns "pong"
//!
//! The bound port is written to ~/.opencode/indexer.port so clients can
//! discover it without knowing the port in advance.
//!
//! Authentication: POST /rpc requires the `X-Indexer-Token` header to match
//! the shared secret written to ~/.opencode/embedder.token by the Python embedder.

use axum::{
    extract::State,
    http::{HeaderMap, StatusCode},
    routing::{get, post},
    Json, Router,
};
use serde_json::Value;

use crate::daemon::Dispatcher;

/// Shared app state: dispatcher + optional auth token.
#[derive(Clone)]
struct AppState {
    dispatch: Dispatcher,
    auth_token: Option<String>,
}

/// Read the shared secret from ~/.opencode/embedder.token.
/// Returns None when the file is missing (auth disabled for backwards compat).
fn read_auth_token() -> Option<String> {
    let path = dirs::home_dir()?.join(".opencode").join("embedder.token");
    std::fs::read_to_string(path).ok().map(|s| s.trim().to_string())
}

/// Start the HTTP server on `127.0.0.1:{port}` (pass 0 for a random port).
///
/// Writes the actual bound port to `~/.opencode/indexer.port` then blocks
/// serving requests until the process exits.
pub async fn serve(dispatch: Dispatcher, port: u16) -> anyhow::Result<()> {
    let addr = std::net::SocketAddr::from(([127, 0, 0, 1], port));
    let listener = tokio::net::TcpListener::bind(addr).await?;
    let actual = listener.local_addr()?.port();

    write_port(actual).await;

    println!(
        "{}",
        serde_json::json!({"type": "http_ready", "port": actual})
    );

    let auth_token = read_auth_token();
    if auth_token.is_some() {
        tracing::info!("RPC auth enabled (token loaded from ~/.opencode/embedder.token)");
    } else {
        tracing::warn!("RPC auth disabled: ~/.opencode/embedder.token not found");
    }

    let state = AppState { dispatch, auth_token };

    let app = Router::new()
        .route("/rpc", post(handle_rpc))
        .route("/ping", get(ping))
        .with_state(state);

    axum::serve(listener, app).await?;
    Ok(())
}

async fn ping() -> &'static str {
    "pong"
}

async fn handle_rpc(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    // Check shared-secret token when one is configured.
    if let Some(expected) = &state.auth_token {
        let provided = headers
            .get("x-indexer-token")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("");
        if provided != expected {
            return Err((
                StatusCode::UNAUTHORIZED,
                Json(serde_json::json!({"error": "Unauthorized"})),
            ));
        }
    }

    let method = body["method"].as_str().unwrap_or("").to_string();
    let params = body.get("params").cloned().unwrap_or(Value::Null);
    let result = (state.dispatch)(method, params).await;
    Ok(Json(serde_json::json!({"result": result})))
}

async fn write_port(port: u16) {
    let Some(home) = dirs::home_dir() else { return };
    let dir = home.join(".opencode");
    let _ = tokio::fs::create_dir_all(&dir).await;
    let _ = tokio::fs::write(dir.join("indexer.port"), port.to_string()).await;
}
