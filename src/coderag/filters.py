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

from . import config, ignores

_SECRET_NAMES = frozenset(
    {
        ".env",
        ".flaskenv",
        ".envrc",
        ".netrc",
        "_netrc",
        ".pgpass",
        ".htpasswd",
        ".npmrc",
        ".pypirc",
        ".s3cfg",
        ".dockercfg",
        ".git-credentials",
        ".terraformrc",
        "credentials",
        "credentials.json",
        "service-account.json",
        "google-services.json",
        "sftp-config.json",
        "master.key",
        "secring.gpg",
        "terraform.tfvars",
        # A shell or REPL history is where a pasted token ends up: nobody writes
        # a credential into one deliberately, which is why nobody redacts one.
        ".bash_history",
        ".zsh_history",
        ".mysql_history",
        ".psql_history",
        ".irb_history",
        ".node_repl_history",
        ".byebug_history",
        ".lein-repl-history",
        ".rhistory",
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
    # The dot in `*.env` has to be literal, so the dash spellings were a second
    # hole: `laravel-env` was indexed here with 287 value-bearing assignments.
    "*-env",
    "*-env.*",
    "env-*",
    "*.env-*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "*.jks",
    "*.keystore",
    "*.ovpn",
    "*.kdb",
    "*.kdbx",
    "*.psafe3",
    "*.keychain",
    "*.keyring",
    "*.asc",
    "*.gpg",
    "*_rsa",
    "*_dsa",
    "*_ecdsa",
    "*_ed25519",
    "*.ppk",
    "*.secret.exs",
    "*secret*.json",
    "*credential*.json",
)

# A .env.example is a template of key names with no values, and it is often the
# only documentation of what a service needs. Indexing it is useful. The dash
# spellings are here for the same reason they are in the globs above.
_SECRET_EXEMPT = (
    "*.example", "*.sample", "*.template", "*.dist",
    "*-example", "*-sample", "*-template", "*-dist",
)  # fmt: skip

_IMAGE_EXT = frozenset(
    {
        # `.svg` is markup, and was indexable on that reading. It is 8,039 files
        # here, nearly all generated icon sets, and the single largest source of
        # chunks holding no line break at all. Markup no human wrote.
        ".svg", ".svgz",
        ".png", ".apng", ".jpg", ".jpeg", ".jpe", ".jfif", ".gif", ".bmp", ".dib",
        ".ico", ".cur", ".webp", ".tiff", ".avif", ".heic", ".heif", ".jp2", ".jxl",
        ".tga", ".exr", ".hdr", ".pbm", ".pgm", ".ppm", ".pnm", ".xbm", ".xpm", ".raw",
    }
)  # fmt: skip

# Refused on collision with source: `.mod` (go.mod), `.ts` (MPEG-TS vs
# TypeScript), `.m`, `.d`, `.v`, `.res`, `.pb`, `.out`, `.spec`, `.cache`.
_BINARY_EXT = frozenset(
    {
        ".pdf", ".zip", ".gz", ".bz2", ".xz", ".7z", ".rar", ".tar", ".jar", ".war",
        ".tgz", ".txz", ".zst", ".iso", ".dmg", ".deb", ".rpm", ".msi", ".cab",
        ".gem", ".whl", ".nupkg",
        ".so", ".dylib", ".dll", ".exe", ".bin", ".o", ".a", ".class", ".pyc", ".pyo",
        ".ko", ".la", ".lo", ".dex", ".pdb", ".ilk", ".exp", ".rlib", ".pyd", ".pyz",
        ".beam", ".ez", ".hi", ".hie", ".wasm",
        ".ptx", ".cubin", ".fatbin",
        ".apk", ".aab", ".aar", ".ipa", ".unitypackage",
        ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".wav", ".flac", ".ogg",
        ".webm", ".m4a", ".m4v", ".aac", ".opus", ".aiff", ".wmv", ".flv",
        ".mpg", ".mpeg", ".3gp", ".mid", ".midi",
        ".woff", ".woff2", ".ttf", ".ttc", ".otf", ".eot", ".icns", ".lnk",
        ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp",
        ".rtf", ".epub", ".mobi", ".djvu", ".ps", ".eps",
        ".db", ".sqlite", ".sqlite3", ".accdb", ".mdb",
        ".parquet", ".orc", ".avro", ".feather", ".arrow",
        ".onnx", ".safetensors", ".pt", ".pth", ".ckpt", ".h5", ".npy", ".npz",
        ".gguf", ".ggml", ".hdf5", ".pkl", ".joblib", ".tflite", ".mlmodel",
        ".img", ".vhd", ".vmdk", ".qcow2", ".vdi",
        ".psd", ".xcf", ".ai", ".sketch", ".fig", ".blend", ".fbx", ".stl",
        ".pcap", ".der",
    }
)  # fmt: skip

# Ambiguous suffixes get one resolution each: `.v` verilog over coq, `.t` perl
# over turing, `.cls` apex over tex, `.res` rescript over the Windows resource,
# `.sc` scala over supercollider. Each is the one this fleet actually holds.
LANGS = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".mts": "typescript", ".cts": "typescript",
    ".vue": "vue", ".svelte": "svelte", ".php": "php", ".go": "go", ".rs": "rust",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".groovy": "groovy",
    ".gradle": "groovy", ".rb": "ruby", ".rake": "ruby", ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".cs": "csharp", ".fs": "fsharp", ".fsx": "fsharp", ".fsi": "fsharp", ".vb": "vbnet",
    ".swift": "swift", ".scala": "scala", ".sc": "scala",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".ksh": "shell", ".fish": "fish",
    ".ps1": "powershell", ".psm1": "powershell", ".psd1": "powershell",
    ".bat": "batchfile", ".cmd": "batchfile",
    ".sql": "sql", ".html": "html", ".htm": "html", ".xhtml": "html",
    ".css": "css", ".scss": "css", ".sass": "css", ".less": "css", ".styl": "css",
    ".json": "json", ".json5": "json", ".jsonc": "json", ".jsonl": "json",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini", ".cfg": "ini",
    # `.conf` is not a linguist extension and is kept anyway: it is 191 files here
    # and every one is a config file. `.local` was the same bet and lost -- the
    # 1,740 it labeled are nginx server blocks, which are not an INI dialect.
    ".conf": "ini", ".properties": "ini", ".editorconfig": "ini",
    ".cnf": "ini", ".service": "ini", ".socket": "ini", ".target": "ini",
    ".timer": "ini", ".mount": "ini", ".network": "ini", ".container": "ini",
    ".neon": "neon",
    ".xml": "xml", ".xsd": "xml", ".xsl": "xml", ".xslt": "xml", ".plist": "xml",
    ".md": "markdown", ".mdx": "markdown", ".markdown": "markdown",
    ".rst": "docs", ".txt": "docs", ".adoc": "docs", ".org": "docs", ".tex": "tex",
    ".tf": "terraform", ".tfvars": "terraform", ".hcl": "hcl", ".nix": "nix",
    ".bicep": "bicep", ".jsonnet": "jsonnet", ".libsonnet": "jsonnet",
    ".lua": "lua", ".ex": "elixir", ".exs": "elixir", ".erl": "erlang", ".hrl": "erlang",
    ".dart": "dart", ".r": "r", ".m": "objc", ".mm": "objc",
    ".pl": "perl", ".pm": "perl", ".t": "perl", ".proto": "proto",
    ".hs": "haskell", ".lhs": "haskell", ".ml": "ocaml", ".mli": "ocaml",
    ".elm": "elm", ".clj": "clojure", ".cljs": "clojure", ".cljc": "clojure",
    ".edn": "clojure",
    ".jl": "julia", ".zig": "zig", ".nim": "nim", ".nims": "nim", ".cr": "crystal",
    ".hx": "haxe", ".cls": "apex", ".trigger": "apex", ".res": "rescript",
    ".graphql": "graphql", ".gql": "graphql", ".sol": "solidity",
    ".glsl": "glsl", ".vert": "glsl", ".frag": "glsl", ".comp": "glsl",
    ".hlsl": "hlsl", ".wgsl": "wgsl", ".cu": "cuda", ".cuh": "cuda",
    ".v": "verilog", ".sv": "systemverilog", ".svh": "systemverilog",
    ".vhd": "vhdl", ".vhdl": "vhdl",
    ".f90": "fortran", ".f95": "fortran", ".f03": "fortran", ".for": "fortran",
    ".adb": "ada", ".ads": "ada", ".pas": "pascal", ".pp": "pascal",
    ".feature": "gherkin", ".puml": "plantuml", ".dot": "dot", ".cmake": "cmake",
    ".erb": "erb", ".haml": "haml", ".slim": "slim", ".pug": "pug",
    ".hbs": "handlebars", ".mustache": "handlebars", ".ejs": "ejs",
    ".j2": "jinja", ".jinja": "jinja", ".jinja2": "jinja",
    ".twig": "twig", ".liquid": "liquid", ".blade": "blade",
    ".astro": "astro", ".patch": "diff", ".diff": "diff",
}  # fmt: skip

# Consulted after the suffix, because a file with no extension resolves to `""`
# by construction and 1,760 of them here are Dockerfiles, Makefiles and CI
# pipelines -- the files that say how a project builds.
FILENAMES = {
    "dockerfile": "dockerfile", "containerfile": "dockerfile",
    "makefile": "make", "gnumakefile": "make", "cmakelists.txt": "cmake",
    "build": "bazel", "build.bazel": "bazel", "workspace": "bazel",
    "workspace.bazel": "bazel", "module.bazel": "bazel",
    "gemfile": "ruby", "rakefile": "ruby", "podfile": "ruby", "fastfile": "ruby",
    "capfile": "ruby", "guardfile": "ruby", "vagrantfile": "ruby", "brewfile": "ruby",
    "cpanfile": "perl", "jenkinsfile": "groovy", "tiltfile": "starlark",
    "pkgbuild": "shell", "artisan": "php", "justfile": "just", "procfile": "yaml",
}  # fmt: skip

# `scope="docs"` in the old engine, kept as a language group so a caller can
# still ask for prose without knowing every extension that carries it.
DOC_LANGS = frozenset({"markdown", "docs", "html", "xml"})


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
    p = Path(path)
    return LANGS.get(p.suffix.lower()) or FILENAMES.get(p.name.lower(), "")


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


def in_ignored_dir(rel: str) -> bool:
    """Any directory on the way to this file being one nobody hand-writes.

    Segment membership, not a glob: `fnmatch` anchors the whole string, so the
    `node_modules/*` this replaces matched only a top-level one and let 278
    files through from nested copies. Gitignore's `node_modules/` -- no leading
    slash -- matches at any depth, and this is that.
    """
    return any(part in ignores.IGNORE_DIRS for part in Path(rel).parts[:-1])


def matches_any(rel: str, patterns) -> bool:
    """fnmatch against a relative path, with directory prefixes handled.

    fnmatch's `*` spans `/`, so `wiki/*` matches at any depth -- that is what
    the live exclude list assumes, and it is why these are not glob(1).
    """
    return any(fnmatch(rel, pat) or fnmatch(f"{rel}/", pat) for pat in patterns)
