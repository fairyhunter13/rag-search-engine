//! HTTP server for the indexer daemon.
//!
//! Exposes two endpoints:
//!   POST /rpc   — {"method": "...", "params": {...}} → {"result": ...}
//!   GET  /ping  — health check, returns "pong"
//!
//! Communication uses Unix domain sockets:
//!   Linux: abstract socket "@opencode-indexer" (kernel auto-cleans on death)
//!   macOS: file socket at ~/.opencode/indexer.sock
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

fn build_router(dispatch: Dispatcher) -> Router {
    let auth_token = read_auth_token();
    if auth_token.is_some() {
        tracing::info!("RPC auth enabled (token loaded from ~/.opencode/embedder.token)");
    } else {
        tracing::warn!("RPC auth disabled: ~/.opencode/embedder.token not found");
    }
    let state = AppState { dispatch, auth_token };
    Router::new()
        .route("/rpc", post(handle_rpc))
        .route("/ping", get(ping))
        .with_state(state)
}

/// Returns the Unix socket file path used on macOS.
pub fn socket_file_path() -> std::path::PathBuf {
    dirs::home_dir().unwrap_or_default().join(".opencode").join("indexer.sock")
}

/// Start the HTTP server on an abstract Unix domain socket (Linux).
///
/// The abstract socket "@opencode-indexer" is automatically cleaned up by the
/// kernel when the process exits — no stale file cleanup needed.
#[cfg(target_os = "linux")]
pub async fn serve(dispatch: Dispatcher) -> anyhow::Result<()> {
    use std::os::linux::net::SocketAddrExt;

    let app = build_router(dispatch);
    let socket_name = b"opencode-indexer";
    let addr = std::os::unix::net::SocketAddr::from_abstract_name(socket_name)?;
    let std_listener = std::os::unix::net::UnixListener::bind_addr(&addr)?;
    std_listener.set_nonblocking(true)?;
    let listener = tokio::net::UnixListener::from_std(std_listener)?;

    println!("{}", serde_json::json!({"type": "unix_ready", "socket": "@opencode-indexer"}));
    axum::serve(listener, app).await?;
    Ok(())
}

/// Start the HTTP server on a file-based Unix domain socket (macOS).
///
/// Removes any stale socket file from a previous crash before binding.
#[cfg(target_os = "macos")]
pub async fn serve(dispatch: Dispatcher) -> anyhow::Result<()> {
    let app = build_router(dispatch);
    let socket_path = socket_file_path();
    // Remove stale socket file (from previous crash)
    let _ = std::fs::remove_file(&socket_path);
    let listener = tokio::net::UnixListener::bind(&socket_path)?;

    println!("{}", serde_json::json!({"type": "unix_ready", "socket": socket_path.display().to_string()}));
    axum::serve(listener, app).await?;
    Ok(())
}

/// Start the HTTP server (fallback for non-Linux/macOS Unix platforms).
#[cfg(not(any(target_os = "linux", target_os = "macos")))]
pub async fn serve(dispatch: Dispatcher) -> anyhow::Result<()> {
    let app = build_router(dispatch);
    let socket_path = socket_file_path();
    let _ = std::fs::remove_file(&socket_path);
    let listener = tokio::net::UnixListener::bind(&socket_path)?;

    println!("{}", serde_json::json!({"type": "unix_ready", "socket": socket_path.display().to_string()}));
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
