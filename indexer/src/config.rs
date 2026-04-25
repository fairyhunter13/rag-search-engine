//! Config parsing and glob matching for indexing.
//!
//! Ports `opencode_embedder/config.py` from commit 2f0d2cb.

use std::collections::HashMap;
use std::path::Path;
use std::sync::{OnceLock, RwLock};

use regex::Regex;
use serde::Deserialize;

const CONFIG_FILENAMES: [&str; 2] = [".opencode-index.yaml", ".opencode-index.yml"];

/// Cached compiled regex patterns. Avoids recompiling the same glob->regex on every file match.
fn regex_cache() -> &'static RwLock<HashMap<String, Option<Regex>>> {
    static CACHE: OnceLock<RwLock<HashMap<String, Option<Regex>>>> = OnceLock::new();
    CACHE.get_or_init(|| RwLock::new(HashMap::new()))
}

/// Get or compile a regex pattern, returning None if the pattern is invalid.
fn cached_regex(pattern: &str) -> Option<Regex> {
    // Fast path: check read lock
    if let Ok(cache) = regex_cache().read() {
        if let Some(cached) = cache.get(pattern) {
            return cached.clone();
        }
    }

    // Slow path: compile and cache
    let compiled = Regex::new(pattern).ok();
    if let Ok(mut cache) = regex_cache().write() {
        if cache.len() >= 10_000 {
            cache.clear();
        }
        cache.insert(pattern.to_string(), compiled.clone());
    }
    compiled
}

fn default_true() -> bool {
    true
}

fn strings<'de, D>(deserializer: D) -> Result<Vec<String>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum Value {
        One(String),
        Many(Vec<String>),
        None,
    }

    Ok(match Value::deserialize(deserializer)? {
        Value::One(s) => vec![s],
        Value::Many(v) => v,
        Value::None => Vec::new(),
    })
}

#[derive(Debug, Clone, Deserialize)]
pub struct IndexConfig {
    #[serde(default = "default_true")]
    pub use_default_ignores: bool,
    #[serde(default, deserialize_with = "strings")]
    pub exclude: Vec<String>,
    #[serde(default, deserialize_with = "strings")]
    pub include: Vec<String>,
}

impl Default for IndexConfig {
    fn default() -> Self {
        Self {
            use_default_ignores: true,
            exclude: Vec::new(),
            include: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct LinkedConfig {
    #[serde(default, deserialize_with = "strings")]
    pub exclude: Vec<String>,
    #[serde(default, deserialize_with = "strings")]
    pub include: Vec<String>,
    #[serde(default)]
    pub inherit: bool,
    #[serde(default)]
    pub skip: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct WatcherConfig {
    /// Maximum number of pending file changes before backpressure kicks in.
    /// Default: 10000. Increase for very large repos with rapid changes.
    #[serde(default = "default_max_pending_files")]
    pub max_pending_files: usize,
}

fn default_max_pending_files() -> usize {
    10000
}

impl Default for WatcherConfig {
    fn default() -> Self {
        Self {
            max_pending_files: default_max_pending_files(),
        }
    }
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct ProjectConfig {
    #[serde(default)]
    pub index: IndexConfig,
    #[serde(default)]
    pub linked: HashMap<String, LinkedConfig>,
    #[serde(default)]
    pub watcher: WatcherConfig,
}

pub fn load(root: &Path) -> ProjectConfig {
    for name in CONFIG_FILENAMES {
        let path = root.join(name);
        if !path.exists() {
            continue;
        }
        let Ok(text) = std::fs::read_to_string(&path) else {
            continue;
        };
        let Ok(cfg) = serde_yaml::from_str::<ProjectConfig>(&text) else {
            continue;
        };
        return cfg;
    }
    ProjectConfig::default()
}

pub fn effective(
    project: &ProjectConfig,
    linked_name: Option<&str>,
    linked: Option<&ProjectConfig>,
) -> IndexConfig {
    let Some(name) = linked_name else {
        return project.index.clone();
    };

    let Some(override_cfg) = project.linked.get(name) else {
        if let Some(cfg) = linked {
            return cfg.index.clone();
        }
        return IndexConfig::default();
    };

    if override_cfg.skip {
        return IndexConfig {
            exclude: vec!["**/*".into()],
            ..IndexConfig::default()
        };
    }

    if override_cfg.inherit {
        return project.index.clone();
    }

    let base = linked.map(|c| c.index.clone()).unwrap_or_default();
    IndexConfig {
        use_default_ignores: base.use_default_ignores,
        exclude: [base.exclude, override_cfg.exclude.clone()].concat(),
        include: [base.include, override_cfg.include.clone()].concat(),
    }
}

pub fn matches_any_pattern(path: &Path, patterns: &[String], root: &Path) -> bool {
    patterns.iter().any(|p| matches_pattern(path, p, root))
}

pub fn matches_pattern(path: &Path, pattern: &str, root: &Path) -> bool {
    let rel = path.strip_prefix(root).unwrap_or(path);
    let rel_str = rel.to_string_lossy().replace('\\', "/");
    let name = path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("")
        .replace('\\', "/");

    // Directory patterns ending in /
    if pattern.ends_with('/') {
        let dir = pattern.trim_end_matches('/').replace('\\', "/");
        if rel_str.starts_with(&(dir.clone() + "/")) {
            return true;
        }

        let parts: Vec<&str> = rel_str.split('/').collect();
        let pat_parts: Vec<&str> = dir.split('/').collect();
        if parts.len() >= pat_parts.len() {
            for i in 0..=(parts.len() - pat_parts.len()) {
                if parts[i..i + pat_parts.len()] == pat_parts {
                    return true;
                }
            }
        }
    }

    for pat in expand_braces(pattern) {
        if match_glob(&rel_str, &pat) {
            return true;
        }
        if match_glob(&name, &pat) {
            return true;
        }
    }

    false
}

fn expand_braces(pattern: &str) -> Vec<String> {
    let mut start = None;
    let mut end = None;
    for (i, c) in pattern.char_indices() {
        if c == '{' {
            start = Some(i);
            continue;
        }
        if c == '}' {
            end = Some(i);
            break;
        }
    }

    let Some(s) = start else {
        return vec![pattern.to_string()];
    };
    let Some(e) = end else {
        return vec![pattern.to_string()];
    };

    let prefix = &pattern[..s];
    let suffix = &pattern[e + 1..];
    let inner = &pattern[s + 1..e];
    let options = inner.split(',').map(str::trim).filter(|o| !o.is_empty());

    let mut out = Vec::new();
    for opt in options {
        out.extend(expand_braces(&format!("{prefix}{opt}{suffix}")));
    }
    out
}

fn match_glob(path: &str, pattern: &str) -> bool {
    let path = path.replace('\\', "/");
    let pattern = pattern.replace('\\', "/");

    let regex_str = if pattern.contains("**") {
        glob_to_regex(&pattern)
    } else {
        fnmatch_to_regex(&pattern)
    };

    // Pattern comes from user config; treat invalid regex as non-match.
    let Some(re) = cached_regex(&regex_str) else {
        return false;
    };
    re.is_match(&path)
}

fn glob_to_regex(pattern: &str) -> String {
    let mut out = String::new();
    let bytes = pattern.as_bytes();
    let mut i = 0usize;
    while i < bytes.len() {
        let c = bytes[i] as char;
        if c == '*' {
            if i + 1 < bytes.len() && bytes[i + 1] as char == '*' {
                if i + 2 < bytes.len() && bytes[i + 2] as char == '/' {
                    out.push_str("(?:.*/)?");
                    i += 3;
                    continue;
                }
                out.push_str(".*");
                i += 2;
                continue;
            }
            out.push_str("[^/]*");
            i += 1;
            continue;
        }

        if c == '?' {
            out.push_str("[^/]");
            i += 1;
            continue;
        }

        if c == '[' {
            let mut j = i + 1;
            while j < bytes.len() && bytes[j] as char != ']' {
                j += 1;
            }
            if j < bytes.len() {
                out.push_str(&pattern[i..=j]);
                i = j + 1;
                continue;
            }
        }

        if ".^$+{}|()".contains(c) {
            out.push('\\');
        }
        out.push(c);
        i += 1;
    }
    format!("^{out}$")
}

fn fnmatch_to_regex(pattern: &str) -> String {
    let mut out = String::new();
    let bytes = pattern.as_bytes();
    let mut i = 0usize;
    while i < bytes.len() {
        let c = bytes[i] as char;
        if c == '*' {
            out.push_str(".*");
            i += 1;
            continue;
        }
        if c == '?' {
            out.push('.');
            i += 1;
            continue;
        }
        if c == '[' {
            let mut j = i + 1;
            while j < bytes.len() && bytes[j] as char != ']' {
                j += 1;
            }
            if j < bytes.len() {
                let mut class = pattern[i + 1..j].to_string();
                if let Some(first) = class.chars().next() {
                    if first == '!' {
                        class.remove(0);
                        class.insert(0, '^');
                    }
                }
                out.push('[');
                out.push_str(&class);
                out.push(']');
                i = j + 1;
                continue;
            }
        }

        if ".^$+{}|()\\".contains(c) {
            out.push('\\');
        }
        out.push(c);
        i += 1;
    }
    format!("^{out}$")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn brace_expansion() {
        assert_eq!(
            expand_braces("*.{ts,js}"),
            vec!["*.ts".to_string(), "*.js".to_string()]
        );
    }

    #[test]
    fn dir_pattern_matches_nested() {
        let root = Path::new("/root");
        let path = Path::new("/root/foo/node_modules/bar.txt");
        assert!(matches_pattern(path, "node_modules/", root));
    }

    #[test]
    fn double_star_matches_across_dirs() {
        let root = Path::new("/root");
        let path = Path::new("/root/a/b/c.ts");
        assert!(matches_pattern(path, "**/*.ts", root));
    }

    #[test]
    fn include_glob_without_double_star_uses_fnmatch_semantics() {
        let root = Path::new("/root");
        let path = Path::new("/root/a/b/c.ts");
        // fnmatch '*' can match '/'
        assert!(matches_pattern(path, "a/*", root));
    }

    #[test]
    fn watcher_config_default_values() {
        let cfg = WatcherConfig::default();
        assert_eq!(cfg.max_pending_files, 10000);
    }

    #[test]
    fn watcher_config_parses_from_yaml() {
        let yaml = r#"
watcher:
  max_pending_files: 25000
"#;
        let cfg: ProjectConfig = serde_yaml::from_str(yaml).unwrap();
        assert_eq!(cfg.watcher.max_pending_files, 25000);
    }

    #[test]
    fn watcher_config_uses_default_when_omitted() {
        let yaml = r#"
index:
  use_default_ignores: true
"#;
        let cfg: ProjectConfig = serde_yaml::from_str(yaml).unwrap();
        assert_eq!(cfg.watcher.max_pending_files, 10000);
    }

    #[test]
    fn watcher_config_empty_yaml_uses_defaults() {
        let yaml = "";
        let cfg: ProjectConfig = serde_yaml::from_str(yaml).unwrap();
        assert_eq!(cfg.watcher.max_pending_files, 10000);
    }

    #[test]
    fn watcher_config_partial_watcher_section_uses_defaults() {
        // If watcher section exists but max_pending_files is not specified
        let yaml = r#"
watcher: {}
"#;
        let cfg: ProjectConfig = serde_yaml::from_str(yaml).unwrap();
        assert_eq!(cfg.watcher.max_pending_files, 10000);
    }

    #[test]
    fn regex_cache_returns_same_result() {
        let pattern = "^.*\\.ts$";
        let re1 = cached_regex(pattern);
        let re2 = cached_regex(pattern);
        assert!(re1.is_some());
        assert!(re2.is_some());
        assert!(re1.unwrap().is_match("file.ts"));
        assert!(re2.unwrap().is_match("file.ts"));
    }

    #[test]
    fn regex_cache_handles_invalid_pattern() {
        assert!(cached_regex("[").is_none());
    }

    #[test]
    fn match_glob_uses_cache_correctly() {
        assert!(match_glob("file.ts", "*.ts"));
        assert!(match_glob("other.ts", "*.ts"));
        assert!(!match_glob("file.rs", "*.ts"));
    }

    #[test]
    fn full_config_with_watcher_parses_correctly() {
        let yaml = r#"
index:
  use_default_ignores: true
  exclude:
    - "node_modules/"
    - "*.test.ts"
watcher:
  max_pending_files: 50000
linked:
  some-lib:
    inherit: true
"#;
        let cfg: ProjectConfig = serde_yaml::from_str(yaml).unwrap();
        assert!(cfg.index.use_default_ignores);
        assert_eq!(cfg.index.exclude.len(), 2);
        assert_eq!(cfg.watcher.max_pending_files, 50000);
        assert!(cfg.linked.contains_key("some-lib"));
    }
}
