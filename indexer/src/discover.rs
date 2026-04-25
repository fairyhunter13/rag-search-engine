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
const MAX_CACHE_SIZE: usize = 1000;
const CACHE_TTL: Duration = Duration::from_secs(300); // 5 minutes

/// Maximum file size limits by category.
/// Source code files get generous limits since large generated files are common.
/// Non-code text files get stricter limits.
const MAX_SOURCE_FILE_SIZE: u64 = 10 * 1024 * 1024; // 10 MB - covers large generated Go/Rust/Java files
const MAX_TEXT_FILE_SIZE: u64 = 2 * 1024 * 1024;     // 2 MB - markdown, yaml, config, etc.
const MAX_UNKNOWN_FILE_SIZE: u64 = 1 * 1024 * 1024;  // 1 MB - unknown extensions

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
    "target",
    "build",
    "dist",
    "out",
    "output",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".env",
    ".idea",
    ".vscode",
    ".vs",
    ".git",
    ".svn",
    ".hg",
    ".cache",
    ".gradle",
    ".m2",
    ".npm",
    ".yarn",
    "coverage",
    ".coverage",
    ".pytest_cache",
    ".tox",
    "vendor",
    ".lancedb",
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
pub fn file_size_limit(path: &Path) -> u64 {
    match detect_language(path) {
        // Programming languages
        "rust" | "go" | "typescript" | "tsx" | "javascript" | "jsx" | "python"
        | "java" | "kotlin" | "scala" | "clojure" | "c" | "cpp" | "csharp"
        | "fsharp" | "ruby" | "php" | "swift" | "objective-c" | "objective-cpp"
        | "lua" | "r" | "perl" | "elixir" | "erlang" | "haskell" | "elm"
        | "lisp" | "scheme" | "racket" | "ocaml" | "nim" | "zig" | "v" | "d"
        | "dart" | "julia" | "vue" | "svelte" | "astro" | "latex"
        | "protobuf" | "graphql" | "cmake" | "gradle" | "makefile" => MAX_SOURCE_FILE_SIZE,
        // Text / config / markup
        "markdown" | "rst" | "text" | "yaml" | "json" | "toml" | "xml"
        | "html" | "css" | "scss" | "less" | "bash" | "zsh" | "fish"
        | "powershell" | "batch" | "sql" | "dockerfile" => MAX_TEXT_FILE_SIZE,
        // Unknown
        _ => MAX_UNKNOWN_FILE_SIZE,
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

    // Check cache first and validate TTL
    if let Ok(mut write) = cache.write() {
        if let Some(entry) = write.get(&key) {
            if !entry.is_expired() {
                return Ok(entry.value.result.clone());
            }
            // Entry expired, remove it
            write.remove(&key);
        }
    }

    // Read lock for final check before expensive operation
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

    // Check cache and validate TTL
    if let Ok(mut write) = submodule_cache().write() {
        if let Some(entry) = write.get(&key) {
            if !entry.is_expired() {
                return entry.value.clone();
            }
            // Entry expired, remove it
            write.remove(&key);
        }
    }

    // Read lock for final check
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

#[cfg(test)]
mod config_discovery_tests {
    use super::*;

    #[test]
    fn file_size_limit_source_code() {
        assert_eq!(file_size_limit(Path::new("main.go")), MAX_SOURCE_FILE_SIZE);
        assert_eq!(file_size_limit(Path::new("lib.rs")), MAX_SOURCE_FILE_SIZE);
        assert_eq!(file_size_limit(Path::new("app.tsx")), MAX_SOURCE_FILE_SIZE);
        assert_eq!(file_size_limit(Path::new("Main.java")), MAX_SOURCE_FILE_SIZE);
    }

    #[test]
    fn file_size_limit_text_files() {
        assert_eq!(file_size_limit(Path::new("README.md")), MAX_TEXT_FILE_SIZE);
        assert_eq!(file_size_limit(Path::new("config.yaml")), MAX_TEXT_FILE_SIZE);
        assert_eq!(file_size_limit(Path::new("schema.json")), MAX_TEXT_FILE_SIZE);
    }

    #[test]
    fn file_size_limit_unknown() {
        assert_eq!(file_size_limit(Path::new("data.xyz")), MAX_UNKNOWN_FILE_SIZE);
        assert_eq!(file_size_limit(Path::new("noext")), MAX_UNKNOWN_FILE_SIZE);
    }

    #[test]
    fn is_within_size_limit_accepts_small_file() {
        let dir = tempfile::TempDir::new().unwrap();
        let path = dir.path().join("small.rs");
        std::fs::write(&path, "fn main() {}").unwrap();
        assert!(is_within_size_limit(&path));
    }

    #[test]
    fn is_within_size_limit_rejects_oversized_file() {
        let dir = tempfile::TempDir::new().unwrap();
        let path = dir.path().join("huge.xyz");
        let data = vec![b'x'; (MAX_UNKNOWN_FILE_SIZE + 1) as usize];
        std::fs::write(&path, &data).unwrap();
        assert!(!is_within_size_limit(&path));
    }

    #[test]
    fn should_index_respects_file_size_limit() {
        let dir = tempfile::TempDir::new().unwrap();
        let root = dir.path();
        let cfg = config::IndexConfig::default();

        // Small source file: should be indexed
        let small = root.join("main.go");
        std::fs::write(&small, "package main").unwrap();
        assert!(should_index(&small, root, &cfg));

        // Oversized unknown file: should be rejected
        let big = root.join("huge.xyz");
        let data = vec![b'x'; (MAX_UNKNOWN_FILE_SIZE + 1) as usize];
        std::fs::write(&big, &data).unwrap();
        assert!(!should_index(&big, root, &cfg));
    }

    #[test]
    fn include_overrides_default_ignores() {
        let dir = tempfile::TempDir::new().unwrap();
        let root = dir.path();
        std::fs::create_dir_all(root.join("node_modules")).unwrap();
        std::fs::write(root.join("node_modules").join("keep.txt"), "hi").unwrap();

        let cfg = config::IndexConfig {
            include: vec!["node_modules/keep.txt".into()],
            ..config::IndexConfig::default()
        };

        let res = discover_files_with_config(root, &cfg).unwrap();
        assert!(res.files.iter().any(|p| p.ends_with("keep.txt")));
    }

    #[test]
    fn additional_discovery_respects_ignored_dirs() {
        let dir = tempfile::TempDir::new().unwrap();
        let root = dir.path();

        std::fs::create_dir_all(root.join("target")).unwrap();
        std::fs::write(root.join("target").join("keep.txt"), "hi").unwrap();

        let mut seen = std::collections::HashSet::new();
        let extra = discover_additional_files(&[root.to_path_buf()], None, &mut seen);
        assert!(!extra.iter().any(|p| p.ends_with("keep.txt")));
    }

    #[test]
    fn relative_path_with_symlinks_maps_correctly() {
        let symlink_dirs = vec![
            SymlinkDir {
                target: PathBuf::from("/external/libs"),
                mount: "libs".to_string(),
            },
            SymlinkDir {
                target: PathBuf::from("/shared/components"),
                mount: "src/shared".to_string(),
            },
        ];

        // File in symlink target should map to symlink path
        let result = relative_path_with_symlinks(
            &PathBuf::from("/external/libs/utils.rs"),
            &PathBuf::from("/project"),
            &[],
            &symlink_dirs,
        );
        assert_eq!(result, "libs/utils.rs");

        // Nested file in symlink target
        let result = relative_path_with_symlinks(
            &PathBuf::from("/shared/components/button/index.ts"),
            &PathBuf::from("/project"),
            &[],
            &symlink_dirs,
        );
        assert_eq!(result, "src/shared/button/index.ts");

        // File not in symlink should use regular relative path
        let result = relative_path_with_symlinks(
            &PathBuf::from("/project/src/main.rs"),
            &PathBuf::from("/project"),
            &[],
            &symlink_dirs,
        );
        assert_eq!(result, "src/main.rs");
    }

    #[test]
    fn relative_path_with_symlinks_prefers_root_over_symlink() {
        // If a file is within root, it should use root-relative path
        // even if it happens to match a symlink target
        let symlink_dirs = vec![SymlinkDir {
            target: PathBuf::from("/project/src"),
            mount: "linked-src".to_string(),
        }];

        let result = relative_path_with_symlinks(
            &PathBuf::from("/project/src/main.rs"),
            &PathBuf::from("/project"),
            &[],
            &symlink_dirs,
        );
        // Should be src/main.rs, not linked-src/main.rs
        assert_eq!(result, "src/main.rs");
    }

    #[test]
    fn discover_non_git_symlinked_directories() {
        // Create a temp directory structure:
        // /tmp/project/          <- git repo
        // /tmp/project/main.rs
        // /tmp/project/shared -> /tmp/external_lib  <- symlink to non-git dir
        // /tmp/external_lib/
        // /tmp/external_lib/lib.rs
        let dir = tempfile::TempDir::new().unwrap();
        let base = dir.path();

        // Create external lib (non-git)
        let external_lib = base.join("external_lib");
        std::fs::create_dir_all(&external_lib).unwrap();
        std::fs::write(external_lib.join("lib.rs"), "pub fn helper() {}").unwrap();

        // Create project (git repo)
        let project = base.join("project");
        std::fs::create_dir_all(&project).unwrap();
        std::fs::write(project.join("main.rs"), "fn main() {}").unwrap();

        // Initialize git repo
        std::process::Command::new("git")
            .args(["init"])
            .current_dir(&project)
            .output()
            .expect("git init failed");

        // Create symlink: project/shared -> ../external_lib
        #[cfg(unix)]
        std::os::unix::fs::symlink(&external_lib, project.join("shared")).unwrap();
        #[cfg(windows)]
        std::os::windows::fs::symlink_dir(&external_lib, project.join("shared")).unwrap();

        // Add symlink to git
        std::process::Command::new("git")
            .args(["add", "-A"])
            .current_dir(&project)
            .output()
            .expect("git add failed");

        // Run discovery
        let cfg = config::IndexConfig::default();
        let result = discover_files_with_config(&project, &cfg).unwrap();

        // Verify: symlink_dirs should contain the non-git symlinked directory
        assert_eq!(
            result.symlink_dirs.len(),
            1,
            "Should discover one symlink dir"
        );
        assert_eq!(result.symlink_dirs[0].mount, "shared");
        assert!(
            result.symlink_dirs[0].target.ends_with("external_lib"),
            "Target should point to external_lib"
        );

        // Verify: skipped_repos should be empty (no external git repos)
        assert!(
            result.skipped_repos.is_empty(),
            "No git repos should be skipped"
        );

        // Verify: files in symlinked directory are discovered
        let file_names: Vec<String> = result
            .files
            .iter()
            .filter_map(|p| p.file_name())
            .filter_map(|n| n.to_str())
            .map(|s| s.to_string())
            .collect();
        assert!(
            file_names.contains(&"main.rs".to_string()),
            "main.rs should be found"
        );
        assert!(
            file_names.contains(&"lib.rs".to_string()),
            "lib.rs from symlink should be found"
        );
    }

    #[test]
    fn discover_external_git_repo_symlink_separately() {
        // Create a temp directory structure:
        // /tmp/project/          <- git repo
        // /tmp/project/main.rs
        // /tmp/project/external_pkg -> /tmp/pkg_repo  <- symlink to ANOTHER git repo
        // /tmp/pkg_repo/         <- separate git repo
        // /tmp/pkg_repo/pkg.rs
        //
        // Note: We use "external_pkg" instead of "vendor" because "vendor" is in IGNORED_DIRECTORIES
        let dir = tempfile::TempDir::new().unwrap();
        let base = dir.path();

        // Create pkg repo (another git repo)
        let pkg_repo = base.join("pkg_repo");
        std::fs::create_dir_all(&pkg_repo).unwrap();
        std::fs::write(pkg_repo.join("pkg.rs"), "pub fn pkg_fn() {}").unwrap();
        std::process::Command::new("git")
            .args(["init"])
            .current_dir(&pkg_repo)
            .output()
            .expect("git init failed");
        std::process::Command::new("git")
            .args(["add", "-A"])
            .current_dir(&pkg_repo)
            .output()
            .expect("git add failed");

        // Create project (git repo)
        let project = base.join("project");
        std::fs::create_dir_all(&project).unwrap();
        std::fs::write(project.join("main.rs"), "fn main() {}").unwrap();
        std::process::Command::new("git")
            .args(["init"])
            .current_dir(&project)
            .output()
            .expect("git init failed");

        // Create symlink: project/external_pkg -> ../pkg_repo
        #[cfg(unix)]
        std::os::unix::fs::symlink(&pkg_repo, project.join("external_pkg")).unwrap();
        #[cfg(windows)]
        std::os::windows::fs::symlink_dir(&pkg_repo, project.join("external_pkg")).unwrap();

        // Add symlink to git
        std::process::Command::new("git")
            .args(["add", "-A"])
            .current_dir(&project)
            .output()
            .expect("git add failed");

        // Run discovery
        let cfg = config::IndexConfig::default();
        let result = discover_files_with_config(&project, &cfg).unwrap();

        // Verify: skipped_repos should contain the external git repo
        assert_eq!(
            result.skipped_repos.len(),
            1,
            "Should skip one external git repo"
        );
        assert!(
            result.skipped_repos[0].ends_with("pkg_repo"),
            "Skipped repo should be pkg_repo"
        );

        // Verify: symlink_dirs should be empty (git repos go to skipped_repos)
        assert!(
            result.symlink_dirs.is_empty(),
            "Git repo symlinks should not be in symlink_dirs"
        );

        // Verify: pkg.rs should NOT be in files (external git repo files are skipped)
        let file_names: Vec<String> = result
            .files
            .iter()
            .filter_map(|p| p.file_name())
            .filter_map(|n| n.to_str())
            .map(|s| s.to_string())
            .collect();
        assert!(
            file_names.contains(&"main.rs".to_string()),
            "main.rs should be found"
        );
        assert!(
            !file_names.contains(&"pkg.rs".to_string()),
            "pkg.rs should NOT be in parent files"
        );
    }

    #[test]
    fn mixed_symlinks_git_and_non_git() {
        // Create a complex structure with BOTH git and non-git symlinks:
        // /project/              <- main git repo
        // /project/main.rs
        // /project/libs -> /libs_dir     <- non-git symlink (index with parent)
        // /project/external -> /ext_repo <- external git repo (index separately)
        // /libs_dir/
        // /libs_dir/helper.rs
        // /ext_repo/            <- separate git repo
        // /ext_repo/ext.rs
        let dir = tempfile::TempDir::new().unwrap();
        let base = dir.path();

        // Create non-git libs directory
        let libs_dir = base.join("libs_dir");
        std::fs::create_dir_all(&libs_dir).unwrap();
        std::fs::write(libs_dir.join("helper.rs"), "pub fn help() {}").unwrap();

        // Create external git repo
        let ext_repo = base.join("ext_repo");
        std::fs::create_dir_all(&ext_repo).unwrap();
        std::fs::write(ext_repo.join("ext.rs"), "pub fn ext_fn() {}").unwrap();
        std::process::Command::new("git")
            .args(["init"])
            .current_dir(&ext_repo)
            .output()
            .expect("git init failed");
        std::process::Command::new("git")
            .args(["add", "-A"])
            .current_dir(&ext_repo)
            .output()
            .expect("git add failed");

        // Create main project
        let project = base.join("project");
        std::fs::create_dir_all(&project).unwrap();
        std::fs::write(project.join("main.rs"), "fn main() {}").unwrap();
        std::process::Command::new("git")
            .args(["init"])
            .current_dir(&project)
            .output()
            .expect("git init failed");

        // Create both symlinks
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(&libs_dir, project.join("libs")).unwrap();
            std::os::unix::fs::symlink(&ext_repo, project.join("external")).unwrap();
        }
        #[cfg(windows)]
        {
            std::os::windows::fs::symlink_dir(&libs_dir, project.join("libs")).unwrap();
            std::os::windows::fs::symlink_dir(&ext_repo, project.join("external")).unwrap();
        }

        // Add symlinks to git
        std::process::Command::new("git")
            .args(["add", "-A"])
            .current_dir(&project)
            .output()
            .expect("git add failed");

        // Run discovery
        let cfg = config::IndexConfig::default();
        let result = discover_files_with_config(&project, &cfg).unwrap();

        // Verify: external git repo is in skipped_repos
        assert_eq!(
            result.skipped_repos.len(),
            1,
            "Should have one skipped git repo"
        );
        assert!(
            result.skipped_repos[0].ends_with("ext_repo"),
            "Skipped repo should be ext_repo"
        );

        // Verify: non-git symlink is in symlink_dirs
        assert_eq!(
            result.symlink_dirs.len(),
            1,
            "Should have one non-git symlink dir"
        );
        assert_eq!(result.symlink_dirs[0].mount, "libs");
        assert!(
            result.symlink_dirs[0].target.ends_with("libs_dir"),
            "Symlink target should be libs_dir"
        );

        // Verify: files from non-git symlink ARE in files list
        let file_names: Vec<String> = result
            .files
            .iter()
            .filter_map(|p| p.file_name())
            .filter_map(|n| n.to_str())
            .map(|s| s.to_string())
            .collect();
        assert!(
            file_names.contains(&"main.rs".to_string()),
            "main.rs should be found"
        );
        assert!(
            file_names.contains(&"helper.rs".to_string()),
            "helper.rs from non-git symlink should be found"
        );
        assert!(
            !file_names.contains(&"ext.rs".to_string()),
            "ext.rs from git repo symlink should NOT be in parent files"
        );
    }

    #[test]
    fn symlink_path_mapping_integration() {
        // Verify that files from symlinked directories have correct relative paths
        let dir = tempfile::TempDir::new().unwrap();
        let base = dir.path();

        // Create external lib
        let external_lib = base.join("external_lib");
        std::fs::create_dir_all(external_lib.join("subdir")).unwrap();
        std::fs::write(external_lib.join("lib.rs"), "pub fn lib() {}").unwrap();
        std::fs::write(
            external_lib.join("subdir").join("nested.rs"),
            "pub fn nested() {}",
        )
        .unwrap();

        // Create project
        let project = base.join("project");
        std::fs::create_dir_all(&project).unwrap();
        std::fs::write(project.join("main.rs"), "fn main() {}").unwrap();
        std::process::Command::new("git")
            .args(["init"])
            .current_dir(&project)
            .output()
            .expect("git init failed");

        // Create symlink
        #[cfg(unix)]
        std::os::unix::fs::symlink(&external_lib, project.join("shared")).unwrap();
        #[cfg(windows)]
        std::os::windows::fs::symlink_dir(&external_lib, project.join("shared")).unwrap();

        std::process::Command::new("git")
            .args(["add", "-A"])
            .current_dir(&project)
            .output()
            .expect("git add failed");

        // Run discovery
        let cfg = config::IndexConfig::default();
        let result = discover_files_with_config(&project, &cfg).unwrap();

        // Verify symlink_dirs is populated
        assert_eq!(result.symlink_dirs.len(), 1);
        let sd = &result.symlink_dirs[0];
        assert_eq!(sd.mount, "shared");

        // Test relative_path_with_symlinks mapping
        let project_canonical = project.canonicalize().unwrap();

        // File at symlink target root
        let lib_path = external_lib.join("lib.rs").canonicalize().unwrap();
        let rel =
            relative_path_with_symlinks(&lib_path, &project_canonical, &[], &result.symlink_dirs);
        assert_eq!(rel, "shared/lib.rs", "lib.rs should map to shared/lib.rs");

        // Nested file
        let nested_path = external_lib
            .join("subdir")
            .join("nested.rs")
            .canonicalize()
            .unwrap();
        let rel = relative_path_with_symlinks(
            &nested_path,
            &project_canonical,
            &[],
            &result.symlink_dirs,
        );
        assert_eq!(
            rel, "shared/subdir/nested.rs",
            "nested.rs should map correctly"
        );

        // File in project root (not in symlink)
        let main_path = project.join("main.rs").canonicalize().unwrap();
        let rel =
            relative_path_with_symlinks(&main_path, &project_canonical, &[], &result.symlink_dirs);
        assert_eq!(rel, "main.rs", "main.rs should be relative to project root");
    }

    #[test]
    fn test_discover_files_cache_returns_cached_result() {
        // Test that calling discover_files_with_config twice returns the same cached result
        let dir = tempfile::TempDir::new().unwrap();
        let root = dir.path();

        // Create a simple git repo with one file
        std::fs::write(root.join("test.rs"), "fn test() {}").unwrap();
        std::process::Command::new("git")
            .args(["init"])
            .current_dir(root)
            .output()
            .expect("git init failed");
        std::process::Command::new("git")
            .args(["add", "-A"])
            .current_dir(root)
            .output()
            .expect("git add failed");

        let cfg = config::IndexConfig::default();

        // First call - populate cache
        let result1 = discover_files_with_config(root, &cfg).unwrap();
        assert_eq!(result1.files.len(), 1, "Should find one file");
        assert!(result1.files[0].ends_with("test.rs"));

        // Second call - should return cached result
        let result2 = discover_files_with_config(root, &cfg).unwrap();
        assert_eq!(result2.files.len(), 1, "Should find one file from cache");
        assert!(result2.files[0].ends_with("test.rs"));

        // Verify both results are identical
        assert_eq!(
            result1.files.len(),
            result2.files.len(),
            "Cache should return same file count"
        );
    }

    #[test]
    fn test_discover_files_cache_ignores_filesystem_changes_within_ttl() {
        // Test that cache returns stale results when filesystem changes within TTL
        // Note: Full TTL expiry test would require waiting 30+ seconds
        let dir = tempfile::TempDir::new().unwrap();
        let root = dir.path();

        // Create initial file
        std::fs::write(root.join("original.rs"), "fn original() {}").unwrap();
        std::process::Command::new("git")
            .args(["init"])
            .current_dir(root)
            .output()
            .expect("git init failed");
        std::process::Command::new("git")
            .args(["add", "-A"])
            .current_dir(root)
            .output()
            .expect("git add failed");

        let cfg = config::IndexConfig::default();

        // First call - populate cache
        let result1 = discover_files_with_config(root, &cfg).unwrap();
        assert_eq!(
            result1.files.len(),
            1,
            "Should find one file before modification"
        );

        // Modify filesystem - add a new file
        std::fs::write(root.join("new.rs"), "fn new() {}").unwrap();
        std::process::Command::new("git")
            .args(["add", "new.rs"])
            .current_dir(root)
            .output()
            .expect("git add failed");

        // Second call immediately - should return cached (stale) result
        let result2 = discover_files_with_config(root, &cfg).unwrap();

        // Cache should return old result, not including new.rs
        // This demonstrates caching behavior (stale data within TTL)
        assert_eq!(
            result2.files.len(),
            1,
            "Cache should return old result (1 file), not fresh result with new.rs"
        );

        let file_names: Vec<String> = result2
            .files
            .iter()
            .filter_map(|p| p.file_name())
            .filter_map(|n| n.to_str())
            .map(|s| s.to_string())
            .collect();
        assert!(
            file_names.contains(&"original.rs".to_string()),
            "Should contain original.rs from cache"
        );
        assert!(
            !file_names.contains(&"new.rs".to_string()),
            "Should NOT contain new.rs (cached result is stale)"
        );
    }

}

#[cfg(test)]
mod submodule_tests {
    use super::*;

    #[test]
    fn discover_submodules_empty_when_none() {
        // A directory with no submodules should return empty vec
        let dir = tempfile::TempDir::new().unwrap();
        std::process::Command::new("git")
            .args(["init"])
            .current_dir(dir.path())
            .output()
            .expect("git init failed");
        let result = run_discover_submodules(dir.path());
        assert!(result.is_empty(), "no submodules → empty result");
    }

    #[test]
    fn discover_submodules_non_git_dir_returns_empty() {
        // A plain directory (not a git repo) should return empty, not panic
        let dir = tempfile::TempDir::new().unwrap();
        let result = run_discover_submodules(dir.path());
        assert!(result.is_empty(), "non-git dir → empty result");
    }

    #[test]
    fn discover_submodules_finds_submodule() {
        // Create parent repo with a submodule
        let base = tempfile::TempDir::new().unwrap();
        let parent = base.path().join("parent");
        let sub = base.path().join("sub_repo");

        // Create sub repo
        std::fs::create_dir_all(&sub).unwrap();
        std::fs::write(sub.join("lib.rs"), "// lib").unwrap();
        std::process::Command::new("git").args(["init"]).current_dir(&sub).output().unwrap();
        std::process::Command::new("git").args(["config", "user.email", "t@t.com"]).current_dir(&sub).output().unwrap();
        std::process::Command::new("git").args(["config", "user.name", "T"]).current_dir(&sub).output().unwrap();
        std::process::Command::new("git").args(["add", "-A"]).current_dir(&sub).output().unwrap();
        std::process::Command::new("git").args(["commit", "-m", "init"]).current_dir(&sub).output().unwrap();

        // Create parent repo
        std::fs::create_dir_all(&parent).unwrap();
        std::process::Command::new("git").args(["init"]).current_dir(&parent).output().unwrap();
        std::process::Command::new("git").args(["config", "user.email", "t@t.com"]).current_dir(&parent).output().unwrap();
        std::process::Command::new("git").args(["config", "user.name", "T"]).current_dir(&parent).output().unwrap();
        std::process::Command::new("git").args(["write-tree"]).current_dir(&parent).output().unwrap();

        // Add submodule
        let out = std::process::Command::new("git")
            .args(["submodule", "add", sub.to_str().unwrap(), "vendor/sub_repo"])
            .current_dir(&parent)
            .output()
            .unwrap();

        if !out.status.success() {
            // Skip test if git submodule add fails (e.g. CI restrictions)
            eprintln!("git submodule add failed, skipping: {}", String::from_utf8_lossy(&out.stderr));
            return;
        }

        let result = run_discover_submodules(&parent);
        assert_eq!(result.len(), 1, "should find one submodule");
        assert_eq!(result[0].1, "sub_repo", "name should be last path component");
        assert!(result[0].0.ends_with("sub_repo"), "path should end with sub_repo");
    }
}

#[cfg(test)]
mod nested_git_tests {
    use super::*;
    use std::fs;
    use tempfile::TempDir;

    #[test]
    fn test_discover_nested_git_repos() {
        let tmp = TempDir::new().unwrap();
        let root = tmp.path();

        let nested = root.join("vendor").join("ai");
        fs::create_dir_all(&nested).unwrap();
        fs::write(nested.join(".git"), "gitdir: ../../.git/modules/vendor/ai").unwrap();
        fs::write(nested.join("README.md"), "# AI").unwrap();

        let nested2 = root.join("packages").join("plugin");
        fs::create_dir_all(&nested2).unwrap();
        fs::write(nested2.join(".git"), "gitdir: ../../.git/modules/packages/plugin").unwrap();

        let normal = root.join("src");
        fs::create_dir_all(&normal).unwrap();
        fs::write(normal.join("main.rs"), "fn main() {}").unwrap();

        fs::create_dir_all(root.join(".git")).unwrap();

        let repos = discover_nested_git_repos(root);
        assert_eq!(repos.len(), 2);

        let names: Vec<&str> = repos.iter().map(|(_, n)| n.as_str()).collect();
        assert!(names.contains(&"ai"));
        assert!(names.contains(&"plugin"));
    }

    #[test]
    fn test_discover_nested_git_repos_empty() {
        let tmp = TempDir::new().unwrap();
        let repos = discover_nested_git_repos(tmp.path());
        assert!(repos.is_empty());
    }
}
