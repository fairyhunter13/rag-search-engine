//! File discovery with gitignore support.
//!
//! Uses the `ignore` crate for fast, gitignore-aware traversal.
//! Ports the Python discover.py logic: extension blacklist, directory blacklist,
//! language detection.

use std::cell::RefCell;
use std::collections::HashMap;
use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::{OnceLock, RwLock};
use std::time::{Duration, Instant};

use anyhow::Result;

use crate::config;

// Cache configuration
const MAX_CACHE_SIZE: usize = 200;
const CACHE_TTL: Duration = Duration::from_secs(300); // 5 minutes

/// Maximum file size limits by category (compile-time defaults).
/// Source code files get generous limits since large generated files are common.
/// Non-code text files get stricter limits.
/// All limits configurable via OPENCODE_SEARCH_MAX_FILE_SIZE_KB env var (applies to all categories).
const DEFAULT_SOURCE_FILE_SIZE_KB: u64 = 1 * 1024; // 1 MB
const DEFAULT_TEXT_FILE_SIZE_KB: u64 = 512;         // 512 KB
const DEFAULT_UNKNOWN_FILE_SIZE_KB: u64 = 256;      // 256 KB

/// Read configurable file size limit from env (in KB). Returns None if not set.
/// OPENCODE_SEARCH_MAX_FILE_SIZE_KB overrides all per-category limits uniformly.
fn env_max_file_size_bytes() -> Option<u64> {
    std::env::var("OPENCODE_SEARCH_MAX_FILE_SIZE_KB")
        .ok()
        .and_then(|v| v.trim().parse::<u64>().ok())
        .filter(|&n| n > 0)
        .map(|kb| kb * 1024)
}

fn max_source_file_size() -> u64 {
    env_max_file_size_bytes().unwrap_or(DEFAULT_SOURCE_FILE_SIZE_KB * 1024)
}

fn max_text_file_size() -> u64 {
    env_max_file_size_bytes().unwrap_or(DEFAULT_TEXT_FILE_SIZE_KB * 1024)
}

fn max_unknown_file_size() -> u64 {
    env_max_file_size_bytes().unwrap_or(DEFAULT_UNKNOWN_FILE_SIZE_KB * 1024)
}

/// Generic cache entry with TTL support
#[derive(Clone)]
struct CacheEntry<T> {
    value: T,
    created: Instant,
}

impl<T: Clone> CacheEntry<T> {
    fn new(value: T) -> Self {
        Self {
            value,
            created: Instant::now(),
        }
    }

    fn is_expired(&self) -> bool {
        self.created.elapsed() > CACHE_TTL
    }
}

#[derive(Debug, Clone)]
pub struct LinkMount {
    pub repo: PathBuf,
    pub mount: String,
    pub name: String,
}

/// A symlinked directory that is NOT an external git repo.
/// These should be indexed with the parent project and watched for changes.
#[derive(Debug, Clone)]
pub struct SymlinkDir {
    /// The resolved target directory (canonical path).
    pub target: PathBuf,
    /// The symlink path relative to the project root.
    pub mount: String,
}

/// Extensions to ignore (never indexable).
pub static IGNORED_EXTENSIONS: &[&str] = &[
    // Compiled/binary
    "class", "jar", "war", "ear", "pyc", "pyo", "pyd", "o", "obj", "a", "lib", "so", "dylib", "dll",
    "exe", "wasm", "bin", "dat", // Build artifacts
    "dex", "apk", "ipa", "aab", // Archives
    "zip", "tar", "gz", "bz2", "xz", "7z", "rar", // Images
    "png", "jpg", "jpeg", "gif", "bmp", "ico", "webp", "svg", "tiff", "tif", // Media
    "mp3", "mp4", "avi", "mkv", "mov", "wav", "flac", "ogg", "webm", // Fonts
    "ttf", "otf", "woff", "woff2", "eot", // Documents
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", // Database
    "db", "sqlite", "sqlite3", // Lock/map
    "lock", "map",
];

/// Directories to skip entirely.
pub static IGNORED_DIRECTORIES: &[&str] = &[
    ".cache",
    ".cache-loader",
    ".coverage",
    ".env",
    ".git",
    ".gradle",
    ".hg",
    ".idea",
    ".lancedb",
    ".m2",
    ".next",
    ".node",
    ".npm",
    ".nuxt",
    ".nx",
    ".pytest_cache",
    ".serverless",
    ".svn",
    ".terraform",
    ".tox",
    ".venv",
    ".vs",
    ".vscode",
    ".yarn",
    "__generated__",
    "__pycache__",
    "DerivedData",
    "Pods",
    "artifacts",
    "bazel-bin",
    "bazel-out",
    "bazel-testlogs",
    "build",
    "buildtools",
    "cmake-build-debug",
    "cmake-build-release",
    "coverage",
    "dist",
    "env",
    "gen",
    "generated",
    "node_modules",
    "out",
    "output",
    "target",
    "third_party",
    "vendor",
    "venv",
];

/// Check if an extension is in the ignored set.
pub fn is_ignored_extension(ext: &str) -> bool {
    IGNORED_EXTENSIONS.contains(&ext)
}

/// Check if a directory name is in the ignored set.
pub fn is_ignored_dir(name: &str) -> bool {
    if name.starts_with(".lancedb") {
        return true;
    }
    IGNORED_DIRECTORIES.contains(&name)
}

/// Detect language from file extension.
pub fn detect_language(path: &Path) -> &'static str {
    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_lowercase();

    match ext.as_str() {
        // Rust
        "rs" => "rust",
        // Go
        "go" => "go",
        // TypeScript/JavaScript
        "ts" => "typescript",
        "tsx" => "tsx",
        "js" | "mjs" | "cjs" => "javascript",
        "jsx" => "jsx",
        // Python
        "py" | "pyi" | "pyw" => "python",
        // Java/JVM
        "java" => "java",
        "kt" | "kts" => "kotlin",
        "scala" => "scala",
        "clj" | "cljs" => "clojure",
        // C/C++
        "c" | "h" => "c",
        "cpp" | "cc" | "hpp" | "cxx" | "hxx" => "cpp",
        // .NET
        "cs" => "csharp",
        "fs" | "fsx" => "fsharp",
        // Ruby
        "rb" | "rake" | "gemspec" => "ruby",
        // PHP
        "php" => "php",
        // Swift
        "swift" => "swift",
        "m" => "objective-c",
        "mm" => "objective-cpp",
        // Other
        "lua" => "lua",
        "r" => "r",
        "pl" | "pm" => "perl",
        "ex" | "exs" => "elixir",
        "erl" | "hrl" => "erlang",
        "hs" => "haskell",
        "elm" => "elm",
        "lisp" => "lisp",
        "scm" => "scheme",
        "rkt" => "racket",
        "ml" | "mli" => "ocaml",
        "nim" => "nim",
        "zig" => "zig",
        "v" => "v",
        "d" => "d",
        "dart" => "dart",
        "jl" => "julia",
        // Markup
        "md" | "mdx" | "markdown" | "mdown" | "mkd" => "markdown",
        "rst" => "rst",
        "txt" => "text",
        // Data
        "yaml" | "yml" => "yaml",
        "json" | "jsonc" | "json5" | "jsonl" => "json",
        "toml" => "toml",
        "xml" | "xsl" | "xslt" | "plist" => "xml",
        // HTML/CSS
        "html" | "htm" | "xhtml" => "html",
        "css" => "css",
        "scss" | "sass" => "scss",
        "less" => "less",
        // Web frameworks
        "vue" => "vue",
        "svelte" => "svelte",
        "astro" => "astro",
        // LaTeX
        "tex" | "latex" | "ltx" => "latex",
        // Shell
        "sh" | "bash" => "bash",
        "zsh" => "zsh",
        "fish" => "fish",
        "ps1" | "psm1" | "psd1" => "powershell",
        "bat" | "cmd" => "batch",
        // Database
        "sql" => "sql",
        // Protocol/Schema
        "proto" => "protobuf",
        "graphql" | "gql" => "graphql",
        // Build
        "dockerfile" => "dockerfile",
        "makefile" => "makefile",
        "cmake" => "cmake",
        "gradle" => "gradle",
        "sbt" => "scala",
        "cabal" => "haskell",
        "nimble" => "nim",
        _ => "unknown",
    }
}

/// Returns the maximum file size limit for the given path, based on its detected language.
/// Override all limits uniformly with OPENCODE_SEARCH_MAX_FILE_SIZE_KB env var.
pub fn file_size_limit(path: &Path) -> u64 {
    match detect_language(path) {
        // Programming languages
        "rust" | "go" | "typescript" | "tsx" | "javascript" | "jsx" | "python"
        | "java" | "kotlin" | "scala" | "clojure" | "c" | "cpp" | "csharp"
        | "fsharp" | "ruby" | "php" | "swift" | "objective-c" | "objective-cpp"
        | "lua" | "r" | "perl" | "elixir" | "erlang" | "haskell" | "elm"
        | "lisp" | "scheme" | "racket" | "ocaml" | "nim" | "zig" | "v" | "d"
        | "dart" | "julia" | "vue" | "svelte" | "astro" | "latex"
        | "protobuf" | "graphql" | "cmake" | "gradle" | "makefile" => max_source_file_size(),
        // Text / config / markup
        "markdown" | "rst" | "text" | "yaml" | "json" | "toml" | "xml"
        | "html" | "css" | "scss" | "less" | "bash" | "zsh" | "fish"
        | "powershell" | "batch" | "sql" | "dockerfile" => max_text_file_size(),
        // Unknown
        _ => max_unknown_file_size(),
    }
}

/// Returns true if the file at `path` is within the extension-aware size limit.
/// Returns true when metadata cannot be obtained (let downstream handle errors).
pub fn is_within_size_limit(path: &Path) -> bool {
    let Ok(meta) = std::fs::metadata(path) else {
        return true;
    };
    let size = meta.len();
    let limit = file_size_limit(path);
    if size > limit {
        tracing::warn!(
            "skipping oversized file: {} ({} bytes > {} byte limit)",
            path.display(),
            size,
            limit
        );
        return false;
    }
    true
}

/// Result of file discovery.
#[derive(Clone)]
pub struct DiscoveryResult {
    pub files: Vec<PathBuf>,
    /// External git repos that were skipped (to be indexed separately).
    pub skipped_repos: Vec<PathBuf>,
    /// Non-git symlinked directories (to be watched with parent project).
    pub symlink_dirs: Vec<SymlinkDir>,
}

/// Cached discovery result, invalidated by filesystem notifications.
#[derive(Clone)]
struct CachedDiscoveryResult {
    result: DiscoveryResult,
}

fn git_root(dir: &Path) -> Option<PathBuf> {
    let output = std::process::Command::new("git")
        .args(["rev-parse", "--show-toplevel"])
        .current_dir(dir)
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    let line = stdout.lines().next()?.trim();
    if line.is_empty() {
        return None;
    }
    Some(PathBuf::from(line))
}

thread_local! {
    static GIT_ROOT_CACHE: RefCell<HashMap<PathBuf, CacheEntry<Option<PathBuf>>>> = RefCell::new(HashMap::new());
}

fn cached_git_root(path: &Path) -> Option<PathBuf> {
    GIT_ROOT_CACHE.with(|cache| {
        let mut cache = cache.borrow_mut();

        // Check if entry exists and is not expired
        if let Some(entry) = cache.get(path) {
            if !entry.is_expired() {
                return entry.value.clone();
            }
            // Entry expired, remove it
            cache.remove(path);
        }

        // Evict old entries if cache is full
        if cache.len() >= MAX_CACHE_SIZE {
            let keys_to_remove: Vec<PathBuf> = cache
                .iter()
                .take(MAX_CACHE_SIZE / 2)
                .map(|(k, _)| k.clone())
                .collect();
            for key in keys_to_remove {
                cache.remove(&key);
            }
        }

        let result = git_root(path);
        cache.insert(path.to_path_buf(), CacheEntry::new(result.clone()));
        result
    })
}

fn is_external_git_repo(target: &Path, project_root: &Path) -> bool {
    let Some(project_git_root) = cached_git_root(project_root) else {
        return false;
    };
    let Some(target_git_root) = cached_git_root(target) else {
        return false;
    };
    project_git_root != target_git_root
}

/// Discover symlinked directories that point to external git repos.
///
/// Returns repo git root + the symlink mount path (relative to `root`).
pub fn discover_link_mounts(root: &Path, cfg: &config::IndexConfig) -> Result<Vec<LinkMount>> {
    let root = root.canonicalize()?;
    let mut out = Vec::new();
    let mut seen_targets: HashSet<PathBuf> = HashSet::new();

    let output = std::process::Command::new("git")
        .args(["ls-files", "--cached", "--others", "--exclude-standard"])
        .current_dir(&root)
        .output();

    let Ok(outp) = output else {
        return Ok(out);
    };
    if !outp.status.success() {
        return Ok(out);
    }

    let stdout = String::from_utf8_lossy(&outp.stdout);
    for line in stdout.lines() {
        let mount = line.trim();
        if mount.is_empty() {
            continue;
        }

        let path = root.join(mount);
        let Ok(symlink_meta) = std::fs::symlink_metadata(&path) else {
            continue;
        };
        if !symlink_meta.file_type().is_symlink() {
            continue;
        }
        // Check if symlink target is a directory (follow the link)
        let Ok(target_meta) = std::fs::metadata(&path) else {
            continue;
        };
        if !target_meta.file_type().is_dir() {
            continue;
        }

        if !should_index(&path, &root, cfg) {
            continue;
        }

        let Ok(target) = path.canonicalize() else {
            continue;
        };
        if seen_targets.contains(&target) {
            continue;
        }
        if !is_external_git_repo(&target, &root) {
            continue;
        }
        seen_targets.insert(target.clone());

        let Some(repo) = cached_git_root(&target) else {
            continue;
        };
        let name = PathBuf::from(mount)
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("linked")
            .to_string();

        out.push(LinkMount {
            repo,
            mount: mount.replace('\\', "/"),
            name,
        });
    }

    Ok(out)
}

pub fn should_index(path: &Path, root: &Path, cfg: &config::IndexConfig) -> bool {
    // include overrides EVERYTHING
    if !cfg.include.is_empty() && config::matches_any_pattern(path, &cfg.include, root) {
        return true;
    }

    if cfg.use_default_ignores {
        if is_in_ignored_directory(path, root) {
            return false;
        }
        if is_ignored_extension_path(path) {
            return false;
        }
    }

    if !cfg.exclude.is_empty() && config::matches_any_pattern(path, &cfg.exclude, root) {
        return false;
    }

    // File size limit check (extension-aware)
    if !is_within_size_limit(path) {
        return false;
    }

    true
}

fn is_in_ignored_directory(path: &Path, root: &Path) -> bool {
    let rel = path.strip_prefix(root).unwrap_or(path);
    for component in rel.components() {
        let std::path::Component::Normal(name) = component else {
            continue;
        };
        let Some(s) = name.to_str() else {
            continue;
        };
        if is_ignored_dir(s) {
            return true;
        }
    }
    false
}

fn is_ignored_extension_path(path: &Path) -> bool {
    let Some(ext) = path.extension().and_then(|e| e.to_str()) else {
        return false;
    };
    is_ignored_extension(&ext.to_lowercase())
}

fn discovery_cache() -> &'static RwLock<HashMap<PathBuf, CacheEntry<CachedDiscoveryResult>>> {
    static CACHE: OnceLock<RwLock<HashMap<PathBuf, CacheEntry<CachedDiscoveryResult>>>> = OnceLock::new();
    CACHE.get_or_init(|| RwLock::new(HashMap::new()))
}

/// Invalidate the discovery cache for a specific project root.
/// Called when filesystem notifications indicate files were added or removed,
/// so subsequent discovery calls return fresh results instead of stale data.
pub fn invalidate_discovery_cache(root: &Path) {
    let key = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());
    if let Ok(mut write) = discovery_cache().try_write() {
        if write.remove(&key).is_some() {
            tracing::debug!("invalidated discovery cache for {}", root.display());
        }
    }
}

/// Config-aware discovery (git ls-files + include override).
pub fn discover_files_with_config(
    root: &Path,
    cfg: &config::IndexConfig,
) -> Result<DiscoveryResult> {
    let cache = discovery_cache();

    let key = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());

    // Check cache first (read lock)
    if let Ok(read) = cache.read() {
        if let Some(entry) = read.get(&key) {
            if !entry.is_expired() {
                return Ok(entry.value.result.clone());
            }
        }
    }

    // Compute fresh result
    let root = root.canonicalize()?;
    let mut files = Vec::new();
    let mut skipped_repos = Vec::new();
    let mut symlink_dirs = Vec::new();
    let mut seen_symlink_targets: HashSet<PathBuf> = HashSet::new();
    let mut seen: HashSet<PathBuf> = HashSet::new();

    let mut git_files: HashSet<PathBuf> = HashSet::new();
    let mut use_manual_walk = false;
    let output = std::process::Command::new("git")
        .args(["ls-files", "--cached", "--others", "--exclude-standard"])
        .current_dir(&root)
        .output();

    if let Ok(out) = output {
        if out.status.success() {
            let stdout = String::from_utf8_lossy(&out.stdout);
            for line in stdout.lines() {
                let line = line.trim();
                if line.is_empty() {
                    continue;
                }
                let path = root.join(line);
                let Ok(symlink_meta) = std::fs::symlink_metadata(&path) else {
                    continue;
                };
                let is_symlink = symlink_meta.file_type().is_symlink();
                let is_file = symlink_meta.file_type().is_file();

                if is_file {
                    git_files.insert(path);
                    continue;
                }
                // For symlinks, check if target is a directory (follow the link)
                if is_symlink {
                    let Ok(target_meta) = std::fs::metadata(&path) else {
                        continue;
                    };
                    if !target_meta.file_type().is_dir() {
                        continue;
                    }
                    if !should_index(&path, &root, cfg) {
                        continue;
                    }
                    if let Ok(target) = path.canonicalize() {
                        // Skip if we've already seen this target (cycle/duplicate prevention)
                        if seen_symlink_targets.contains(&target) {
                            continue;
                        }
                        // Skip if target is inside root (internal symlink)
                        if target.starts_with(&root) {
                            continue;
                        }
                        seen_symlink_targets.insert(target.clone());

                        if is_external_git_repo(&target, &root) {
                            // External git repos are indexed separately
                            skipped_repos.push(target);
                        } else {
                            // Non-git symlinks are indexed with parent and need to be watched
                            let mount = path
                                .strip_prefix(&root)
                                .map(|p| p.to_string_lossy().to_string())
                                .unwrap_or_default();
                            symlink_dirs.push(SymlinkDir {
                                target: target.clone(),
                                mount,
                            });

                            // Walk the symlinked directory to discover files inside
                            // Use the symlink path (not target) so paths are relative to project
                            for entry in walkdir::WalkDir::new(&path).follow_links(true) {
                                let Ok(entry) = entry else {
                                    continue;
                                };
                                if !entry.file_type().is_file() {
                                    continue;
                                }
                                let file_path = entry.path();
                                if should_index(file_path, &root, cfg) {
                                    git_files.insert(file_path.to_path_buf());
                                }
                            }
                        }
                    }
                }

                // Handle gitlink directories (nested git repos shown as dirs by git ls-files)
                if !is_symlink && symlink_meta.file_type().is_dir() {
                    let git_file = path.join(".git");
                    if git_file.is_file() {
                        if let Ok(abs) = path.canonicalize() {
                            if abs != root {
                                skipped_repos.push(abs);
                            }
                        }
                    }
                }
            }
        } else {
            use_manual_walk = true;
        }
    } else {
        use_manual_walk = true;
    }

    if use_manual_walk {
        for entry in walkdir::WalkDir::new(&root).follow_links(true) {
            let Ok(entry) = entry else {
                continue;
            };
            let path = entry.path();
            if !entry.file_type().is_file() {
                continue;
            }
            if path
                .components()
                .any(|c| matches!(c, std::path::Component::Normal(n) if n == ".git"))
            {
                continue;
            }
            git_files.insert(path.to_path_buf());
        }
    }

    // Step 2: scan include patterns (bypasses .gitignore)
    let mut include_files: HashSet<PathBuf> = HashSet::new();
    if !cfg.include.is_empty() {
        for pattern in &cfg.include {
            let globpat = root.join(pattern).to_string_lossy().replace('\\', "/");
            let matches = glob::glob(&globpat);
            if let Ok(iter) = matches {
                for m in iter.flatten() {
                    let Ok(meta) = std::fs::symlink_metadata(&m) else {
                        continue;
                    };
                    if meta.file_type().is_file() {
                        include_files.insert(m);
                        continue;
                    }
                    if meta.file_type().is_dir() {
                        for entry in walkdir::WalkDir::new(&m).follow_links(true) {
                            let Ok(entry) = entry else {
                                continue;
                            };
                            if entry.file_type().is_file() {
                                include_files.insert(entry.path().to_path_buf());
                            }
                        }
                    }
                }
            }

            // Also try direct path
            let direct = root.join(pattern);
            if let Ok(meta) = std::fs::symlink_metadata(&direct) {
                if meta.file_type().is_file() {
                    include_files.insert(direct.clone());
                } else if meta.file_type().is_dir() {
                    for entry in walkdir::WalkDir::new(&direct).follow_links(true) {
                        let Ok(entry) = entry else {
                            continue;
                        };
                        if entry.file_type().is_file() {
                            include_files.insert(entry.path().to_path_buf());
                        }
                    }
                }
            }
        }
    }

    for path in git_files.union(&include_files) {
        let Ok(real) = path.canonicalize() else {
            continue;
        };
        if seen.contains(&real) {
            continue;
        }
        seen.insert(real);

        if should_index(path, &root, cfg) {
            files.push(path.to_path_buf());
        }
    }

    let result = DiscoveryResult {
        files,
        skipped_repos,
        symlink_dirs,
    };

    // Store in cache with eviction if needed
    if let Ok(mut write) = cache.write() {
        // Evict old entries if cache is full
        if write.len() >= MAX_CACHE_SIZE {
            let keys_to_remove: Vec<PathBuf> = write
                .iter()
                .take(MAX_CACHE_SIZE / 2)
                .map(|(k, _)| k.clone())
                .collect();
            for k in keys_to_remove {
                write.remove(&k);
            }
        }

        write.insert(
            key,
            CacheEntry::new(CachedDiscoveryResult {
                result: result.clone(),
            }),
        );
    }

    Ok(result)
}

/// Discover indexable files in additional directories (e.g. memories).
pub fn discover_additional_files(
    dirs: &[PathBuf],
    exclude: Option<&[String]>,
    seen: &mut HashSet<PathBuf>,
) -> Vec<PathBuf> {
    let mut out = Vec::new();
    for dir in dirs {
        let Ok(meta) = std::fs::symlink_metadata(dir) else {
            continue;
        };
        if !meta.file_type().is_dir() {
            continue;
        }

        let Ok(root) = dir.canonicalize() else {
            continue;
        };

        for entry in walkdir::WalkDir::new(&root).follow_links(true) {
            let Ok(entry) = entry else {
                continue;
            };
            if !entry.file_type().is_file() {
                continue;
            }
            let path = entry.path();

            // Respect default ignored directories for additional dirs too.
            // This prevents accidentally indexing build outputs when include dirs are used.
            if is_in_ignored_directory(path, &root) {
                continue;
            }

            // Skip hidden directories (not hidden files)
            if let Ok(rel) = path.strip_prefix(&root) {
                let mut hidden = false;
                for component in rel.components() {
                    let std::path::Component::Normal(name) = component else {
                        continue;
                    };
                    let Some(s) = name.to_str() else {
                        continue;
                    };
                    // exclude the file name itself
                    if name == rel.file_name().unwrap_or_default() {
                        break;
                    }
                    if s.starts_with('.') {
                        hidden = true;
                        break;
                    }
                }
                if hidden {
                    continue;
                }
            }

            let Ok(real) = path.canonicalize() else {
                continue;
            };
            if seen.contains(&real) {
                continue;
            }
            seen.insert(real);

            if is_ignored_extension_path(path) {
                continue;
            }

            if let Some(patterns) = exclude {
                if config::matches_any_pattern(path, patterns, &root) {
                    continue;
                }
            }

            if !is_within_size_limit(path) {
                continue;
            }

            out.push(path.to_path_buf());
        }
    }
    out
}

/// Compute relative path for a file, handling included directories and symlink mappings.
pub fn relative_path(file: &Path, root: &Path, include_dirs: &[PathBuf]) -> String {
    relative_path_with_symlinks(file, root, include_dirs, &[])
}

/// Compute relative path for a file, handling included directories and symlink mappings.
pub fn relative_path_with_symlinks(
    file: &Path,
    root: &Path,
    include_dirs: &[PathBuf],
    symlink_dirs: &[SymlinkDir],
) -> String {
    if let Ok(rel) = file.strip_prefix(root) {
        return rel.to_string_lossy().to_string();
    }

    // Check if file is in a symlinked directory and map to symlink path
    for sd in symlink_dirs {
        if let Ok(rel) = file.strip_prefix(&sd.target) {
            // Map back to the symlink mount path
            return format!("{}/{}", sd.mount, rel.to_string_lossy());
        }
    }

    for dir in include_dirs {
        if let Ok(rel) = file.strip_prefix(dir) {
            if let Some(name) = dir.file_name().and_then(|n| n.to_str()) {
                return format!("@{name}/{}", rel.to_string_lossy());
            }
        }
    }

    file.to_string_lossy().to_string()
}


/// Global cache for submodule discovery results keyed by project root.
fn submodule_cache() -> &'static RwLock<HashMap<PathBuf, CacheEntry<Vec<(PathBuf, String)>>>> {
    static CACHE: OnceLock<RwLock<HashMap<PathBuf, CacheEntry<Vec<(PathBuf, String)>>>>> = OnceLock::new();
    CACHE.get_or_init(|| RwLock::new(HashMap::new()))
}

/// Discover git submodules in the given project root.
///
/// Runs `git submodule status --recursive` and parses the output.
/// Returns `(absolute_path, name)` pairs. Results are cached globally with TTL.
pub fn discover_submodules(root: &Path) -> Vec<(PathBuf, String)> {
    let key = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());

    // Check cache (read lock)
    if let Ok(read) = submodule_cache().read() {
        if let Some(entry) = read.get(&key) {
            if !entry.is_expired() {
                return entry.value.clone();
            }
        }
    }

    let result = run_discover_submodules(root);

    if let Ok(mut write) = submodule_cache().write() {
        // Evict old entries if cache is full
        if write.len() >= MAX_CACHE_SIZE {
            let keys_to_remove: Vec<PathBuf> = write
                .iter()
                .take(MAX_CACHE_SIZE / 2)
                .map(|(k, _)| k.clone())
                .collect();
            for k in keys_to_remove {
                write.remove(&k);
            }
        }

        write.insert(key, CacheEntry::new(result.clone()));
    }

    result
}

fn run_discover_submodules(root: &Path) -> Vec<(PathBuf, String)> {
    let output = std::process::Command::new("git")
        .args(["submodule", "status", "--recursive"])
        .current_dir(root)
        .output();

    let Ok(out) = output else {
        return Vec::new();
    };
    if !out.status.success() {
        return Vec::new();
    }

    let stdout = String::from_utf8_lossy(&out.stdout);
    let mut results = Vec::new();

    for line in stdout.lines() {
        let line = line.trim_start_matches(|c: char| c == ' ' || c == '+' || c == '-' || c == 'U');
        // format: <hash> <path> (<branch>)  OR  <hash> <path>
        let parts: Vec<&str> = line.splitn(3, ' ').collect();
        if parts.len() < 2 {
            continue;
        }
        let sub_rel = parts[1].trim();
        if sub_rel.is_empty() {
            continue;
        }
        let abs = match root.join(sub_rel).canonicalize() {
            Ok(p) => p,
            Err(_) => continue,
        };
        let name = PathBuf::from(sub_rel)
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or(sub_rel)
            .to_string();
        results.push((abs, name));
    }

    results
}

/// Discover nested git repositories that aren't registered as submodules.
/// These have `.git` files (gitlinks) instead of `.git` directories.
pub fn discover_nested_git_repos(root: &Path) -> Vec<(PathBuf, String)> {
    let root = match root.canonicalize() {
        Ok(r) => r,
        Err(_) => return vec![],
    };
    let mut repos = Vec::new();

    for entry in walkdir::WalkDir::new(&root)
        .follow_links(false)
        .into_iter()
        .filter_entry(|e| {
            let name = e.file_name().to_str().unwrap_or("");
            if e.file_type().is_dir() && (name == ".git" || name == "node_modules" || name == "target" || name == "__pycache__" || name == ".lancedb") {
                return false;
            }
            true
        })
    {
        let Ok(entry) = entry else { continue };
        if entry.file_name().to_str() != Some(".git") {
            continue;
        }
        if !entry.file_type().is_file() {
            continue;
        }
        if entry.depth() <= 1 {
            continue;
        }
        let Some(parent) = entry.path().parent() else { continue };
        let name = parent
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("unknown")
            .to_string();
        if let Ok(abs) = parent.canonicalize() {
            if abs != root {
                repos.push((abs, name));
            }
        }
    }
    repos
}
