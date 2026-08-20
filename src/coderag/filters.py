"""What must not be indexed, and what it is when it is.

`is_secret_path` is the one that matters. Everything else here is a quality or
cost filter -- an indexed image wastes a vector. An indexed `.env` is a
credential sitting in a store that answers natural-language queries, retrieved
by an agent, in a result payload that gets pasted into a transcript. It is the
only filter here whose failure is not recoverable by re-indexing.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

from . import config

_SECRET_NAMES = frozenset(
    {
        ".env",
        ".envrc",
        ".netrc",
        ".pgpass",
        ".htpasswd",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "service-account.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "secrets.yaml",
        "secrets.yml",
        "secrets.json",
    }
)

_SECRET_GLOBS = (
    # Three patterns, not one, because `fnmatch` anchors the whole name: `.env.*`
    # matches `.env.local` and misses both `prod.env` and `svc.env.enc`. A fleet
    # sweep found 456 tracked files shaped like credentials that the single
    # pattern let through -- none of them holding a live value, which is the
    # reason this reads as a latent hole rather than an incident.
    ".env.*",
    "*.env",
    "*.env.*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "*.jks",
    "*.keystore",
    "*_rsa",
    "*.ppk",
    "*secret*.json",
    "*credential*.json",
)

# A .env.example is a template of key names with no values, and it is often the
# only documentation of what a service needs. Indexing it is useful.
_SECRET_EXEMPT = ("*.example", "*.sample", "*.template", "*.dist")

_IMAGE_EXT = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".avif", ".heic"}
)

_BINARY_EXT = frozenset(
    {
        ".pdf", ".zip", ".gz", ".bz2", ".xz", ".7z", ".rar", ".tar", ".jar", ".war",
        ".so", ".dylib", ".dll", ".exe", ".bin", ".o", ".a", ".class", ".pyc", ".pyo",
        ".wasm", ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".wav", ".flac", ".ogg",
        ".woff", ".woff2", ".ttf", ".otf", ".eot", ".db", ".sqlite", ".sqlite3",
        ".parquet", ".onnx", ".safetensors", ".pt", ".pth", ".ckpt", ".h5", ".npy",
    }
)  # fmt: skip

LANGS = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".vue": "vue", ".svelte": "svelte", ".php": "php", ".go": "go", ".rs": "rust",
    ".java": "java", ".kt": "kotlin", ".rb": "ruby", ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp", ".swift": "swift",
    ".scala": "scala", ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".sql": "sql", ".html": "html", ".htm": "html", ".css": "css", ".scss": "css",
    ".less": "css", ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".xml": "xml", ".md": "markdown", ".mdx": "markdown", ".rst": "docs", ".txt": "docs",
    ".tf": "terraform", ".lua": "lua", ".ex": "elixir", ".exs": "elixir", ".erl": "erlang",
    ".dart": "dart", ".r": "r", ".m": "objc", ".pl": "perl", ".proto": "proto",
}  # fmt: skip

# `scope="docs"` in the old engine, kept as a language group so a caller can
# still ask for prose without knowing every extension that carries it.
DOC_LANGS = frozenset({"markdown", "docs", "html", "xml"})

# Files whose structure is a key path rather than a declaration. Separate from
# DOC_LANGS because the header built for them is different, not because the
# chunker treats them differently -- it does not.
DATA_LANGS = frozenset({"json", "yaml", "toml"})


def is_secret_path(path: Path | str) -> bool:
    name = Path(path).name
    lower = name.lower()
    if any(fnmatch(lower, pat) for pat in _SECRET_EXEMPT):
        return False
    return lower in _SECRET_NAMES or any(fnmatch(lower, pat) for pat in _SECRET_GLOBS)


def is_image_path(path: Path | str) -> bool:
    return Path(path).suffix.lower() in _IMAGE_EXT


def is_binary_ext(path: Path | str) -> bool:
    return Path(path).suffix.lower() in _BINARY_EXT


def lang_of(path: Path | str) -> str:
    return LANGS.get(Path(path).suffix.lower(), "")


def looks_binary(head: bytes) -> bool:
    """git's own heuristic: a NUL byte in the first 8 KB.

    Cheap, and it catches the extensionless binaries an allowlist never will --
    a compiled helper in bin/, a vendored blob with no suffix.
    """
    return b"\x00" in head[: config.BINARY_SNIFF_BYTES]


def is_forbidden_root(path: Path | str) -> bool:
    """Refuse to index a root that is not a project.

    A walk rooted at / or ~ enumerates the whole machine; the cache dirs hold
    copies of source already indexed under its real path.
    """
    resolved = Path(path).expanduser().resolve()
    if resolved in config.FORBIDDEN_ROOTS:
        return True
    return any(tree in resolved.parents for tree in config.FORBIDDEN_TREES)


def matches_any(rel: str, patterns) -> bool:
    """fnmatch against a relative path, with directory prefixes handled.

    fnmatch's `*` spans `/`, so `wiki/*` matches at any depth -- that is what
    the live exclude list assumes, and it is why these are not glob(1).
    """
    return any(fnmatch(rel, pat) or fnmatch(f"{rel}/", pat) for pat in patterns)
