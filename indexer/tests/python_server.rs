//! Helper to spawn and manage the real Python model server for tests.
//!
//! If an embedder is already running (HTTP), we reuse it instead of spawning
//! a new one. This is safe because the embedder is stateless.

use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context, Result};

const DEFAULT_PORT: u16 = 9998;

/// Handle to a Python model server (either existing or spawned).
pub struct PythonServer {
    pub home: tempfile::TempDir,
    /// HTTP port the embedder listens on
    pub port: u16,
    /// Child process if we spawned a new server (None if reusing existing)
    child: Option<std::process::Child>,
}

impl PythonServer {
    /// Get or start a Python model server.
    ///
    /// Tries in order:
    /// 1. Existing HTTP server at OPENCODE_EMBED_HTTP_PORT (default 9998)
    /// 2. Spawn a new HTTP server
    pub async fn start() -> Result<Self> {
        let home = tempfile::TempDir::new().context("create temp home")?;

        let port = std::env::var("OPENCODE_EMBED_HTTP_PORT")
            .ok()
            .and_then(|p| p.parse::<u16>().ok())
            .unwrap_or(DEFAULT_PORT);

        // 1. Try existing HTTP server
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(2))
            .build()
            .context("build http client")?;
        if client
            .get(format!("http://127.0.0.1:{port}/health"))
            .send()
            .await
            .is_ok()
        {
            tracing::info!("reusing existing HTTP embedder at port {port}");
            return Ok(Self {
                home,
                port,
                child: None,
            });
        }

        // 2. Spawn a new HTTP server
        tracing::info!("no existing embedder found, spawning new one on port {port}");
        let real_home = PathBuf::from(std::env::var("HOME").unwrap_or_default());

        // Resolve embedder binary: env override, then known candidates
        let bin = resolve_embedder_bin(&real_home)?;

        // Use the real home's HuggingFace cache to avoid re-downloading models
        // This significantly speeds up tests since models are already cached
        let hf_home = real_home.join(".cache/huggingface");

        let child = std::process::Command::new(&bin)
            .arg("--serve")
            .env("HOME", home.path())
            .env("HF_HOME", &hf_home)
            .env("HF_HUB_CACHE", hf_home.join("hub"))
            .env("HF_HUB_DISABLE_XET", "1")
            .env("OPENCODE_EMBED_HTTP_PORT", port.to_string())
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .with_context(|| format!("spawn Python server: {}", bin.display()))?;

        // Wait for HTTP health check (up to 60 seconds for model loading)
        let deadline = tokio::time::Instant::now() + Duration::from_secs(60);
        while tokio::time::Instant::now() < deadline {
            if client
                .get(format!("http://127.0.0.1:{port}/health"))
                .send()
                .await
                .is_ok()
            {
                return Ok(Self {
                    home,
                    port,
                    child: Some(child),
                });
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }

        anyhow::bail!("Python server did not start within 60s")
    }

    pub fn home_path(&self) -> &Path {
        self.home.path()
    }

    /// Get environment variables to configure the Rust daemon to connect to this embedder.
    pub fn env_vars(&self) -> Vec<(&'static str, String)> {
        let vars = vec![
            ("OPENCODE_EMBED_HTTP_PORT", self.port.to_string()),
        ];
        vars
    }
}

impl Drop for PythonServer {
    fn drop(&mut self) {
        if let Some(ref mut child) = self.child {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

/// Resolve the embedder binary path.
///
/// Checks in order:
/// 1. `OPENCODE_EMBEDDER_PATH` env var (explicit override)
/// 2. Known candidate paths under `real_home`:
///    - `~/.opencode/bin/opencode-embedder-dir/opencode-embedder` (original default)
///    - `~/.opencode/bin/opencode-embedder/opencode-embedder`
/// 3. Known candidate paths relative to the project root (via `CARGO_MANIFEST_DIR`):
///    - `embedder/build/opencode-embedder/opencode-embedder`
///    - `embedder/dist/opencode-embedder/opencode-embedder`
/// 4. If none found, returns error listing all tried paths.
fn resolve_embedder_bin(real_home: &Path) -> Result<PathBuf> {
    // 1. Env override
    if let Ok(val) = std::env::var("OPENCODE_EMBEDDER_PATH") {
        let bin = PathBuf::from(&val);
        anyhow::ensure!(
            bin.is_file(),
            "Python embedder binary not found at OPENCODE_EMBEDDER_PATH={}",
            val
        );
        return Ok(bin);
    }

    // 2. Build candidate list (use is_file() so directories are skipped)
    let mut candidates: Vec<PathBuf> = vec![
        // Original default path
        real_home.join(".opencode/bin/opencode-embedder-dir/opencode-embedder"),
        // Common home-relative installs
        real_home.join(".opencode/bin/opencode-embedder/opencode-embedder"),
        // Project-relative build outputs (via CARGO_MANIFEST_DIR)
    ];

    // 3. Add project-relative candidates (compile-time known)
    // CARGO_MANIFEST_DIR is .../opencode/cmd/opencode-search-engine/indexer
    // project root is .../opencode
    if let Some(cargo_dir) = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).parent() {
        if let Some(root) = cargo_dir.parent().and_then(|p| p.parent()) {
            candidates.push(root.join("embedder/build/opencode-embedder/opencode-embedder"));
            candidates.push(root.join("embedder/dist/opencode-embedder/opencode-embedder"));
        }
    }

    // Pick first existing candidate
    if let Some(bin) = candidates.iter().find(|p| p.is_file()) {
        return Ok(bin.clone());
    }

    anyhow::bail!(
        "Python embedder binary not found. Tried:\n  {}",
        candidates
            .iter()
            .map(|p| p.display().to_string())
            .collect::<Vec<_>>()
            .join("\n  ")
    )
}
