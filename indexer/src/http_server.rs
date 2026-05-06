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
use axum::{
    extract::State,
    http::StatusCode,
    routing::{get, post},
    Json, Router,
};
use serde_json::Value;

use crate::daemon::Dispatcher;

/// Shared app state: dispatcher.
#[derive(Clone)]
struct AppState {
    dispatch: Dispatcher,
}

fn build_router(dispatch: Dispatcher) -> Router {
    let state = AppState { dispatch };
    Router::new()
        .route("/rpc", post(handle_rpc))
        .route("/ping", get(ping))
        .with_state(state)
}

/// Returns the Unix socket file path used on macOS.
pub fn socket_file_path() -> std::path::PathBuf {
    dirs::home_dir().unwrap_or_default().join(".opencode").join("indexer.sock")
}

/// Start the HTTP server on a TCP socket (for testing / --port mode).
///
/// Binds to 127.0.0.1:{port} (port 0 = OS assigns a free port).
/// Emits `{"type":"http_ready","port":N}` on stdout when ready.
pub async fn serve_tcp(port: u16, dispatch: Dispatcher) -> anyhow::Result<()> {
    let app = build_router(dispatch);
    let listener = tokio::net::TcpListener::bind(format!("127.0.0.1:{port}")).await?;
    let bound_port = listener.local_addr()?.port();
    println!("{}", serde_json::json!({"type": "http_ready", "port": bound_port}));
    axum::serve(listener, app).await?;
    Ok(())
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
    Json(body): Json<Value>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let method = body["method"].as_str().unwrap_or("").to_string();
    let params = body.get("params").cloned().unwrap_or(Value::Null);
    let result = (state.dispatch)(method, params).await;
    Ok(Json(serde_json::json!({"result": result})))
}
