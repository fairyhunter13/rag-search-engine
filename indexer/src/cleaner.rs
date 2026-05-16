//! Automatic cleanup of orphaned and stale project embedding directories.
//!
//! Scans `~/.local/share/opencode/projects/` and removes:
//! - Orphaned projects (source directory no longer exists)
//! - Stale projects (not modified in N days, configurable)
//! - Auxiliary dirs (backups, compaction-history, corrupted-backup-*)

use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use serde::{Deserialize, Serialize};

// Default interval: 24 hours
const DEFAULT_INTERVAL_SECS: u64 = 86400;
// Default stale threshold: 90 days
const DEFAULT_STALE_DAYS: u64 = 90;

/// Configuration for the cleaner.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    /// How often to run cleanup (seconds)
    pub interval: u64,
    /// Days after which unused project dirs are considered stale
    pub stale_days: u64,
    /// Whether to clean orphaned project dirs
    pub orphans: bool,
    /// Whether to clean stale project dirs
    pub stale: bool,
    /// Whether to clean auxiliary dirs (backups, compaction-history, etc.)
    pub aux: bool,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            interval: DEFAULT_INTERVAL_SECS,
            stale_days: DEFAULT_STALE_DAYS,
            orphans: true,
            stale: true,
            aux: true,
        }
    }
}

/// Read config from env vars, falling back to defaults.
pub fn config() -> Config {
    Config {
        interval: std::env::var("OPENCODE_CLEANUP_INTERVAL_SECS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(DEFAULT_INTERVAL_SECS),
        stale_days: std::env::var("OPENCODE_CLEANUP_STALE_DAYS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(DEFAULT_STALE_DAYS),
        orphans: std::env::var("OPENCODE_CLEANUP_ORPHANS")
            .map(|v| v != "0" && v != "false")
            .unwrap_or(true),
        stale: std::env::var("OPENCODE_CLEANUP_STALE")
            .map(|v| v != "0" && v != "false")
            .unwrap_or(true),
        aux: std::env::var("OPENCODE_CLEANUP_AUX")
            .map(|v| v != "0" && v != "false")
            .unwrap_or(true),
    }
}

/// Info about a project directory discovered during scan.
#[derive(Debug, Clone, Serialize)]
pub struct ProjectInfo {
    /// The project directory path (e.g. ~/.local/share/opencode/projects/<id>)
    pub path: PathBuf,
    /// The project ID (directory name)
    pub id: String,
    /// The project root from .symlinks.json (if available)
    pub root: Option<String>,
    /// Size in bytes
    pub bytes: u64,
    /// Last modification time
    pub modified: Option<SystemTime>,
}

/// Classification of why a project should be cleaned up.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub enum Reason {
    /// Project root directory no longer exists
    Orphaned,
    /// Project hasn't been modified in stale_days
    Stale,
}

/// A project identified for cleanup.
#[derive(Debug, Clone, Serialize)]
pub struct Target {
    pub info: ProjectInfo,
    pub reason: Reason,
}

/// Result of a cleanup operation.
#[derive(Debug, Clone, Serialize, Default)]
pub struct Report {
    /// Number of orphaned project dirs removed
    pub orphans: u64,
    /// Number of stale project dirs removed
    pub stale: u64,
    /// Number of auxiliary dirs removed
    pub aux_dirs: u64,
    /// Total bytes freed
    pub freed: u64,
    /// Errors encountered (non-fatal)
    pub errors: Vec<String>,
    /// Details of what was cleaned (for dry_run)
    pub targets: Vec<Target>,
}

/// Compute directory size recursively (expensive — only used by 24h fallback path).
fn dir_size(path: &Path) -> u64 {
    walkdir::WalkDir::new(path)
        .into_iter()
        .filter_map(|e| e.ok())
        .filter_map(|e| e.metadata().ok())
        .filter(|m| m.is_file())
        .map(|m| m.len())
        .sum()
}

/// Get the top-level mtime of a directory via a single stat() call.
/// Fast O(1) replacement for the recursive latest_modified used in normal cleanup.
fn dir_mtime(path: &Path) -> Option<SystemTime> {
    std::fs::metadata(path).ok()?.modified().ok()
}

/// Get the latest modification time of any file in a directory (full walkdir).
/// Only used when stale detection needs deep mtime accuracy (24h fallback path).
fn latest_modified(path: &Path) -> Option<SystemTime> {
    walkdir::WalkDir::new(path)
        .into_iter()
        .filter_map(|e| e.ok())
        .filter_map(|e| e.metadata().ok().and_then(|m| m.modified().ok()))
        .max()
}

/// Read project_root from .symlinks.json in a project directory.
fn read_root(dir: &Path) -> Option<String> {
    let file = dir.join(".symlinks.json");
    let data = std::fs::read_to_string(&file).ok()?;
    let json: serde_json::Value = serde_json::from_str(&data).ok()?;
    json.get("project_root")?.as_str().map(String::from)
}

/// Cursor file path for batched scan resumption.
fn cursor_path(base: &Path) -> std::path::PathBuf {
    // Write cursor OUTSIDE projects/ to avoid triggering inotify on projects/
    base.join("cleaner_cursor")
}

/// Read the current cursor offset from disk (0 if absent/invalid).
fn read_cursor(base: &Path) -> usize {
    std::fs::read_to_string(cursor_path(base))
        .ok()
        .and_then(|s| s.trim().parse().ok())
        .unwrap_or(0)
}

/// Write the cursor offset to disk.
fn write_cursor(base: &Path, offset: usize) {
    let _ = std::fs::write(cursor_path(base), offset.to_string());
}

/// Batch size for each scan invocation (default 500, env OPENCODE_INDEXER_CLEANUP_BATCH).
fn batch_size() -> usize {
    std::env::var("OPENCODE_INDEXER_CLEANUP_BATCH")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(500)
}

/// Full scan using recursive walkdir for deep mtime/size accuracy.
/// Only called by the 24h fallback path.
pub fn scan_deep(base: &Path) -> Vec<ProjectInfo> {
    scan_full(base, true)
}

fn scan_full(base: &Path, deep: bool) -> Vec<ProjectInfo> {
    let dir = base.join("projects");
    let Ok(entries) = std::fs::read_dir(&dir) else {
        return vec![];
    };
    entries
        .filter_map(|e| e.ok())
        .filter(|e| e.path().is_dir())
        .map(|e| {
            let path = e.path();
            let id = e.file_name().to_string_lossy().to_string();
            let root = read_root(&path);
            // Use cheap O(1) stat for event-driven path; deep walkdir only for 24h pass
            let (bytes, modified) = if deep {
                (dir_size(&path), latest_modified(&path))
            } else {
                (0u64, dir_mtime(&path))
            };
            ProjectInfo {
                path,
                id,
                root,
                bytes,
                modified,
            }
        })
        .collect()
}

/// Batched scan: process at most `batch_size` entries per call, resuming via cursor.
/// Returns (infos, complete) where complete=true when a full pass has finished.
pub fn scan_batch(base: &Path) -> (Vec<ProjectInfo>, bool) {
    let dir = base.join("projects");
    let Ok(entries) = std::fs::read_dir(&dir) else {
        return (vec![], true);
    };
    // Collect and sort IDs for stable cursor ordering
    let mut ids: Vec<String> = entries
        .filter_map(|e| e.ok())
        .filter(|e| e.path().is_dir())
        .map(|e| e.file_name().to_string_lossy().to_string())
        .collect();
    ids.sort();

    let total = ids.len();
    let cursor = read_cursor(base).min(total);
    let batch = batch_size();
    let end = (cursor + batch).min(total);
    let complete = end >= total;
    let next = if complete { 0 } else { end };
    write_cursor(base, next);

    let infos: Vec<ProjectInfo> = ids[cursor..end]
        .iter()
        .map(|id| {
            let path = dir.join(id);
            let root = read_root(&path);
            let modified = dir_mtime(&path);
            ProjectInfo {
                path,
                id: id.clone(),
                root,
                bytes: 0,
                modified,
            }
        })
        .collect();

    (infos, complete)
}

/// Identify projects that should be cleaned up.
pub fn identify(projects: &[ProjectInfo], cfg: &Config) -> Vec<Target> {
    let threshold = SystemTime::now() - Duration::from_secs(cfg.stale_days * 86400);
    let mut targets = vec![];

    for info in projects {
        // Check orphaned: project_root doesn't exist
        if cfg.orphans {
            if let Some(ref root) = info.root {
                if !Path::new(root).exists() {
                    targets.push(Target {
                        info: info.clone(),
                        reason: Reason::Orphaned,
                    });
                    continue;
                }
            }
        }

        // Check stale: not modified recently
        if cfg.stale {
            if let Some(modified) = info.modified {
                if modified < threshold {
                    targets.push(Target {
                        info: info.clone(),
                        reason: Reason::Stale,
                    });
                }
            }
        }
    }

    targets
}

/// Find auxiliary directories that can be cleaned.
fn aux_dirs(base: &Path) -> Vec<PathBuf> {
    let mut dirs = vec![];
    // backups/
    let backups = base.join("backups");
    if backups.is_dir() {
        dirs.push(backups);
    }
    // compaction-history/
    let compaction = base.join("compaction-history");
    if compaction.is_dir() {
        dirs.push(compaction);
    }
    // corrupted-backup-* dirs
    if let Ok(entries) = std::fs::read_dir(base) {
        for entry in entries.flatten() {
            let name = entry.file_name();
            let name = name.to_string_lossy();
            if name.starts_with("corrupted-backup-") && entry.path().is_dir() {
                dirs.push(entry.path());
            }
        }
    }
    dirs
}

/// Execute cleanup using batched shallow scan (event-driven path).
/// Returns (report, complete) where complete=true after a full pass.
pub fn run_batch(base: &Path, cfg: &Config, dry: bool) -> (Report, bool) {
    let mut report = Report::default();
    let (projects, complete) = scan_batch(base);
    let targets = identify(&projects, cfg);

    for target in &targets {
        // bytes=0 in batch path (shallow scan) — compute only when deleting
        let size = if !dry { dir_size(&target.info.path) } else { 0 };
        if !dry {
            if let Err(e) = std::fs::remove_dir_all(&target.info.path) {
                report.errors.push(format!(
                    "failed to remove {}: {}",
                    target.info.path.display(),
                    e
                ));
                continue;
            }
        }
        report.freed += size;
        match target.reason {
            Reason::Orphaned => report.orphans += 1,
            Reason::Stale => report.stale += 1,
        }
    }
    report.targets = targets;
    (report, complete)
}

/// Execute full cleanup: remove orphaned/stale project dirs and auxiliary dirs.
/// Uses deep recursive scan — intended for the 24h fallback timer path.
/// If `dry` is true, only report what would be cleaned without deleting.
pub fn run(base: &Path, cfg: &Config, dry: bool) -> Report {
    let mut report = Report::default();
    let projects = scan_deep(base);
    let targets = identify(&projects, cfg);

    for target in &targets {
        let size = target.info.bytes;
        if !dry {
            if let Err(e) = std::fs::remove_dir_all(&target.info.path) {
                report.errors.push(format!(
                    "failed to remove {}: {}",
                    target.info.path.display(),
                    e
                ));
                continue;
            }
        }
        report.freed += size;
        match target.reason {
            Reason::Orphaned => report.orphans += 1,
            Reason::Stale => report.stale += 1,
        }
    }
    report.targets = targets;

    // Clean auxiliary dirs
    if cfg.aux {
        for dir in aux_dirs(base) {
            let size = dir_size(&dir);
            if !dry {
                if let Err(e) = std::fs::remove_dir_all(&dir) {
                    report
                        .errors
                        .push(format!("failed to remove {}: {}", dir.display(), e));
                    continue;
                }
            }
            report.aux_dirs += 1;
            report.freed += size;
        }
    }

    report
}

/// Duration for the periodic cleanup interval, reading from env.
pub fn interval() -> Duration {
    Duration::from_secs(config().interval)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, SystemTime};

    fn make_project(id: &str, root: Option<&str>, days_ago: u64) -> ProjectInfo {
        let modified = SystemTime::now() - Duration::from_secs(days_ago * 86400);
        ProjectInfo {
            path: std::path::PathBuf::from(format!("/tmp/{}", id)),
            id: id.to_string(),
            root: root.map(String::from),
            bytes: 0,
            modified: Some(modified),
        }
    }

    #[test]
    fn empty_input_returns_no_targets() {
        let result = identify(&[], &Config::default());
        assert!(result.is_empty());
    }

    #[test]
    fn orphan_when_root_missing() {
        let cfg = Config {
            orphans: true,
            stale: false,
            ..Config::default()
        };
        let projects = vec![make_project("orphan", Some("/nonexistent"), 0)];
        let result = identify(&projects, &cfg);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].reason, Reason::Orphaned);
    }

    #[test]
    fn stale_when_old() {
        let cfg = Config {
            orphans: false,
            stale: true,
            ..Config::default()
        };
        let projects = vec![make_project("stale", None, 200)];
        let result = identify(&projects, &cfg);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].reason, Reason::Stale);
    }

    #[test]
    fn recent_project_not_stale() {
        let cfg = Config {
            stale: true,
            orphans: false,
            ..Config::default()
        };
        let projects = vec![make_project("recent", None, 1)];
        let result = identify(&projects, &cfg);
        assert!(result.is_empty());
    }

    #[test]
    fn orphan_takes_priority_over_stale() {
        let cfg = Config {
            orphans: true,
            stale: true,
            ..Config::default()
        };
        let projects = vec![make_project("both", Some("/nonexistent"), 200)];
        let result = identify(&projects, &cfg);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].reason, Reason::Orphaned);
    }

    #[test]
    fn orphans_disabled_skips_orphan_check() {
        let cfg = Config {
            orphans: false,
            stale: true,
            ..Config::default()
        };
        let projects = vec![make_project("orphan_skipped", Some("/nonexistent"), 200)];
        let result = identify(&projects, &cfg);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].reason, Reason::Stale);
    }

    #[test]
    fn stale_disabled_skips_stale_check() {
        let cfg = Config {
            orphans: true,
            stale: false,
            ..Config::default()
        };
        let projects = vec![make_project("stale_skipped", None, 200)];
        let result = identify(&projects, &cfg);
        assert!(result.is_empty());
    }

    #[test]
    fn both_disabled_returns_empty() {
        let cfg = Config {
            orphans: false,
            stale: false,
            ..Config::default()
        };
        let projects = vec![make_project("disabled", Some("/nonexistent"), 200)];
        let result = identify(&projects, &cfg);
        assert!(result.is_empty());
    }

    #[test]
    fn custom_stale_days() {
        let cfg_under = Config {
            orphans: false,
            stale: true,
            stale_days: 30,
            ..Config::default()
        };
        // 31 days old → stale (exceeds 30 day threshold)
        let stale = vec![make_project("old", None, 31)];
        assert_eq!(identify(&stale, &cfg_under).len(), 1);
        // 29 days old → not stale
        let recent = vec![make_project("young", None, 29)];
        assert!(identify(&recent, &cfg_under).is_empty());
    }

    #[test]
    fn stale_days_zero_identifies_all() {
        let cfg = Config {
            orphans: false,
            stale: true,
            stale_days: 0,
            ..Config::default()
        };
        let projects = vec![make_project("any", None, 0)];
        let result = identify(&projects, &cfg);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].reason, Reason::Stale);
    }

    #[test]
    fn missing_modified_is_not_stale() {
        let cfg = Config {
            orphans: false,
            stale: true,
            ..Config::default()
        };
        let p = ProjectInfo {
            modified: None,
            ..make_project("nomod", None, 200)
        };
        let projects = vec![p];
        let result = identify(&projects, &cfg);
        assert!(result.is_empty());
    }

    #[test]
    fn empty_root_is_orphaned() {
        let cfg = Config {
            orphans: true,
            stale: false,
            ..Config::default()
        };
        let projects = vec![make_project("empty_root", Some(""), 0)];
        let result = identify(&projects, &cfg);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].reason, Reason::Orphaned);
    }

    #[test]
    fn multiple_projects_mixed() {
        let cfg = Config::default();
        let orphan = make_project("orph", Some("/nonexistent"), 0);
        let stale = ProjectInfo {
            root: None,
            ..make_project("stale_proj", None, 200)
        };
        let recent = ProjectInfo {
            root: None,
            ..make_project("recent_proj", None, 1)
        };
        let projects = vec![orphan, stale, recent];
        let result = identify(&projects, &cfg);
        assert_eq!(result.len(), 2);
        assert_eq!(result[0].reason, Reason::Orphaned);
        assert_eq!(result[0].info.id, "orph");
        assert_eq!(result[1].reason, Reason::Stale);
        assert_eq!(result[1].info.id, "stale_proj");
    }

    #[test]
    fn cursor_path_is_outside_projects_dir() {
        let base = Path::new("/tmp/opencode");
        let cursor = cursor_path(base);
        assert!(
            !cursor.starts_with("/tmp/opencode/projects"),
            "cursor should not be inside projects/ dir"
        );
        assert_eq!(cursor, Path::new("/tmp/opencode/cleaner_cursor"));
    }

    #[test]
    fn report_default_is_all_zeros() {
        let r = Report::default();
        assert_eq!(r.orphans, 0);
        assert_eq!(r.stale, 0);
        assert_eq!(r.aux_dirs, 0);
        assert_eq!(r.freed, 0);
        assert!(r.errors.is_empty());
        assert!(r.targets.is_empty());
    }
}
