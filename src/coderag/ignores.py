"""The two lists of things nobody hand-wrote.

Split out of `config` because they are data, not knobs -- nothing here reads the
environment, and left in place they were a third of that module.

Gitignore itself is delegated to git: `discover._git_files` runs `git ls-files
--exclude-standard`, honouring the repo's own chain including its negations. For
a git work tree these are a *second* filter, and what they are for is the
non-git walk plus the directories a repo deliberately commits. Being correct at
any depth is what makes them worth having; being long is not.

Curated, not copied. `linguist/vendor.yml` also prunes `testdata`, `fixtures`,
`third_party` and `.github`, but it defines what to leave out of language
*statistics*, not what is worth reading. `lib` is a Dart package's source root
and `public`/`resources`/`storage` are hand-written in Laravel and Rails. A
wrongly-excluded file raises nothing -- it is just missing from the answer.
"""

from __future__ import annotations

# Bare names matched against path segments, not globs: `fnmatch` anchors the
# whole string, so the `node_modules/*` these used to be excluded a top-level
# one and let `packages/a/node_modules/x.js` through -- 278 files across 157
# stores. Gitignore's own spelling is `node_modules/`, matching at any depth.
IGNORE_DIRS = frozenset(
    {
        ".git", ".svn", ".hg", ".bzr",
        "node_modules", "bower_components", "jspm_packages", "web_modules", ".pnp",
        ".next", ".nuxt", ".svelte-kit", ".astro", ".angular", ".output",
        ".turbo", ".nx", ".vite", ".parcel-cache", ".nyc_output", ".serverless",
        ".docusaurus", ".vuepress", ".vitepress", ".vercel", ".netlify",
        "__pycache__", ".venv", "venv", ".tox", ".nox", ".mypy_cache", ".ruff_cache",
        ".pytest_cache", ".pytype", ".hypothesis", ".ipynb_checkpoints", ".eggs",
        "htmlcov", ".scrapy", "cython_debug", "__pypackages__",
        "target", ".gradle", ".mvn", ".kotlin", "obj",
        "CMakeFiles", "_deps",
        ".bundle", ".yardoc",
        ".build", "DerivedData", ".dart_tool",
        "_build", ".elixir_ls",
        ".externalNativeBuild", ".cxx", "captures",
        "dist-newstyle", ".stack-work", "elm-stuff", ".zig-cache", "zig-out",
        ".direnv", ".terraform", ".vagrant",
        "_site", ".jekyll-cache", ".sass-cache",
        "dist", "build", "coverage", "vendor",
        ".idea", ".vs", "__MACOSX",
    }
)  # fmt: skip

# Whole filenames, matched at any depth, for the same reason IGNORE_DIRS is:
# spelled as globs here they were root-anchored, so a monorepo's
# `packages/a/package-lock.json` went in -- 27 files and 1,290 chunks of
# dependency graph across the fleet. Those ending `.lock` were covered anyway
# by the `*.lock` glob, which is why only some of them leaked.
IGNORE_NAMES = frozenset(
    {
        "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
        "bun.lock", "deno.lock", "composer.lock", "gemfile.lock", "poetry.lock",
        "pdm.lock", "uv.lock", "pipfile.lock", "go.sum", "flake.lock",
        "package.resolved", "packages.lock.json", ".terraform.lock.hcl",
        "gradlew", "gradlew.bat", "mvnw", "mvnw.cmd",
    }
)  # fmt: skip

# Globs, root-anchored, and that is deliberate: this list shares its matcher
# with a project's own `exclude:`, where `wiki/*` means the one at the root.
DEFAULT_IGNORES = (
    "*.min.js",
    "*.min.css",
    "*-min.js",
    "*-min.css",
    "*.map",
    "*.tsbuildinfo",
    # Generated from a schema that is itself indexed, so the output is a second
    # copy of something already retrievable.
    "*.pb.go",
    "*_grpc.pb.go",
    "*.pb.cc",
    "*.pb.h",
    "*_pb2.py",
    "*_pb2.pyi",
    "*_pb2_grpc.py",
    "*.g.dart",
    "*.freezed.dart",
    "*.Designer.cs",
    "*.g.cs",
    "*.lock",
    # Not a chunker problem, so not a chunker fix. A notebook is JSON holding
    # base64 output blobs and escaped \n that are not newlines, so the splitter's
    # top rung -- runs of blank lines -- does not exist in it and every cut lands
    # mid-object. A CSV is the same shape with its header stranded in chunk one.
    # Extracting a notebook's code cells is a real feature and is not this.
    "*.ipynb",
    "*.csv",
    "*.tsv",
)
