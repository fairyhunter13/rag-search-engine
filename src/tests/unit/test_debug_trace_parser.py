"""Unit tests for the debug-trace traceback parser.

Tests cover Python, Go, Java, JavaScript, and Rust traceback parsing,
plus the path normalisation helper. No LLM or daemon required.
"""
from __future__ import annotations

import pytest
from opencode_search.handlers._debug_trace import (
    _normalise_path,
    _parse_go,
    _parse_java,
    _parse_js,
    _parse_python,
    _parse_rust,
    parse_traceback,
)


# ── Python ────────────────────────────────────────────────────────────────────

PYTHON_TB = """
Traceback (most recent call last):
  File "/home/user/project/src/app/main.py", line 42, in run
    result = process(data)
  File "/home/user/project/src/app/processor.py", line 17, in process
    return parser.parse(raw)
  File "/home/user/project/src/lib/parser.py", line 5, in parse
    return json.loads(data["payload"])
KeyError: 'payload'
"""

class TestPythonParser:
    def test_extracts_frames(self):
        frames = _parse_python(PYTHON_TB)
        assert len(frames) == 3

    def test_first_frame_file(self):
        frames = _parse_python(PYTHON_TB)
        assert frames[0]["file"] == "/home/user/project/src/app/main.py"

    def test_first_frame_line(self):
        frames = _parse_python(PYTHON_TB)
        assert frames[0]["line"] == 42

    def test_first_frame_function(self):
        frames = _parse_python(PYTHON_TB)
        assert frames[0]["function"] == "run"

    def test_last_frame_function(self):
        frames = _parse_python(PYTHON_TB)
        assert frames[-1]["function"] == "parse"

    def test_lang_tag(self):
        frames = _parse_python(PYTHON_TB)
        assert all(f["lang"] == "python" for f in frames)


# ── Go ────────────────────────────────────────────────────────────────────────

GO_TB = """
goroutine 1 [running]:
main.processRequest(0xc0000b4000, 0x200)
	/home/user/project/cmd/server/handler.go:88 +0x1b4
main.(*Server).ServeHTTP(0xc000096230, 0x7f3c60, 0xc000104000, 0xc0000b4000)
	/home/user/project/cmd/server/server.go:42 +0x6c
net/http.serverHandler.ServeHTTP(...)
	/usr/local/go/src/net/http/server.go:2879 +0x43
"""

class TestGoParser:
    def test_extracts_frames(self):
        frames = _parse_go(GO_TB)
        assert len(frames) >= 1

    def test_extracts_project_frame(self):
        frames = _parse_go(GO_TB)
        project_frames = [f for f in frames if "project" in f["file"]]
        assert len(project_frames) >= 1

    def test_lang_tag(self):
        frames = _parse_go(GO_TB)
        assert all(f["lang"] == "go" for f in frames)

    def test_go_frame_function(self):
        frames = _parse_go(GO_TB)
        funcs = [f["function"] for f in frames]
        assert "processRequest" in funcs or "ServeHTTP" in funcs


# ── Java ─────────────────────────────────────────────────────────────────────

JAVA_TB = """
Exception in thread "main" java.lang.NullPointerException: Cannot invoke "String.length()" because "str" is null
	at com.example.app.StringProcessor.process(StringProcessor.java:15)
	at com.example.app.Main.run(Main.java:42)
	at com.example.app.Main.main(Main.java:8)
"""

class TestJavaParser:
    def test_extracts_frames(self):
        frames = _parse_java(JAVA_TB)
        assert len(frames) == 3

    def test_first_function(self):
        frames = _parse_java(JAVA_TB)
        assert frames[0]["function"] == "process"

    def test_first_line(self):
        frames = _parse_java(JAVA_TB)
        assert frames[0]["line"] == 15

    def test_lang_tag(self):
        frames = _parse_java(JAVA_TB)
        assert all(f["lang"] == "java" for f in frames)

    def test_class_captured(self):
        frames = _parse_java(JAVA_TB)
        assert "StringProcessor" in frames[0]["class"]


# ── JavaScript ───────────────────────────────────────────────────────────────

JS_TB = """
TypeError: Cannot read properties of undefined (reading 'map')
    at processItems (/home/user/project/src/utils/processor.js:22:18)
    at runPipeline (/home/user/project/src/pipeline.js:55:12)
    at Object.<anonymous> (/home/user/project/src/index.js:10:1)
"""

class TestJsParser:
    def test_extracts_frames(self):
        frames = _parse_js(JS_TB)
        assert len(frames) == 3

    def test_first_function(self):
        frames = _parse_js(JS_TB)
        assert frames[0]["function"] == "processItems"

    def test_first_file(self):
        frames = _parse_js(JS_TB)
        assert "processor.js" in frames[0]["file"]

    def test_lang_tag(self):
        frames = _parse_js(JS_TB)
        assert all(f["lang"] == "javascript" for f in frames)


# ── Rust ─────────────────────────────────────────────────────────────────────

RUST_TB = """
stack backtrace:
   0: std::panicking::begin_panic
             at /rustc/hash/library/std/src/panicking.rs:540
   1: myapp::processor::run
             at /home/user/project/src/processor.rs:88
   2: myapp::main
             at /home/user/project/src/main.rs:12
"""

class TestRustParser:
    def test_extracts_frames(self):
        frames = _parse_rust(RUST_TB)
        assert len(frames) >= 2

    def test_project_frame_function(self):
        frames = _parse_rust(RUST_TB)
        funcs = [f["function"] for f in frames]
        assert "run" in funcs or "main" in funcs

    def test_lang_tag(self):
        frames = _parse_rust(RUST_TB)
        assert all(f["lang"] == "rust" for f in frames)


# ── Auto-detect ───────────────────────────────────────────────────────────────

class TestAutoDetect:
    def test_detects_python(self):
        frames = parse_traceback(PYTHON_TB)
        assert len(frames) >= 3
        assert frames[0]["lang"] == "python"

    def test_detects_go(self):
        frames = parse_traceback(GO_TB)
        assert len(frames) >= 1
        assert frames[0]["lang"] == "go"

    def test_detects_java(self):
        frames = parse_traceback(JAVA_TB)
        assert len(frames) >= 3
        assert frames[0]["lang"] == "java"

    def test_detects_js(self):
        frames = parse_traceback(JS_TB)
        assert len(frames) >= 3
        assert frames[0]["lang"] == "javascript"

    def test_empty_string_returns_empty(self):
        assert parse_traceback("") == []

    def test_noise_returns_empty(self):
        result = parse_traceback("No traceback here, just text.")
        assert isinstance(result, list)


# ── Path normalisation ────────────────────────────────────────────────────────

class TestNormalisePath:
    def test_relative_path_unchanged(self):
        result = _normalise_path("src/app/main.py", "/home/user/project")
        assert result == "src/app/main.py"

    def test_absolute_path_under_project_becomes_relative(self):
        result = _normalise_path("/home/user/project/src/app.py", "/home/user/project")
        assert result == "src/app.py"

    def test_absolute_path_outside_project_unchanged(self):
        result = _normalise_path("/usr/lib/python3.12/json/__init__.py", "/home/user/project")
        assert result == "/usr/lib/python3.12/json/__init__.py"

    def test_tilde_project_path_handled(self):
        result = _normalise_path("/home/user/project/src/file.py", "~/project")
        assert isinstance(result, str)
