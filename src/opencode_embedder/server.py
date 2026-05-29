"""Legacy-compatible HTTP embedder service.

Exposes endpoints used by this repo's E2E tests:
  - GET  /health
  - POST /embed/passages  {"texts":[...], "model"?, "dimensions"?}
  - POST /embed/query     {"text": "...", "model"?, "dimensions"?}
  - POST /embed/chunk     {"content":"...", "content_type":"text|code|markdown"}
  - POST /embed/rerank    {"query":"...", "docs":[...], "model"?, "top_k"?}

This server is intentionally small and built on the stdlib so tests can run
without extra web framework dependencies.
"""

from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from opencode_search.config import DEFAULT_DIMS, DEFAULT_EMBED_MODEL, DEFAULT_RERANK_MODEL


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: dict[str, Any]) -> None:
    data = json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    try:
        length = int(handler.headers.get("Content-Length", "0") or "0")
    except ValueError:
        length = 0
    raw = handler.rfile.read(max(0, length)) if length else b""
    try:
        obj = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        obj = {}
    return obj if isinstance(obj, dict) else {}


class _Handler(BaseHTTPRequestHandler):
    server_version = "opencode-embedder/compat"

    def log_message(self, fmt: str, *args: object) -> None:  # pragma: no cover
        if os.environ.get("OPENCODE_EMBEDDER_QUIET", "").strip().lower() in {"1", "true", "yes", "on"}:
            return
        super().log_message(fmt, *args)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            from opencode_search.embeddings import assert_gpu_available
            gpu: dict[str, Any]
            try:
                assert_gpu_available()
                gpu = {"is_gpu": True, "provider": "cuda"}
            except Exception as exc:
                gpu = {"is_gpu": False, "provider": "cpu", "error": str(exc)}
            _json_response(
                self,
                200,
                {
                    "result": {
                        "ok": True,
                        "ts": time.time(),
                        "gpu": gpu,
                    }
                },
            )
            return
        _json_response(self, 404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        body = _read_json(self)

        if path == "/embed/passages":
            texts = body.get("texts")
            if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
                _json_response(self, 400, {"error": "texts must be a list[str]"})
                return
            dims = body.get("dimensions")
            dimensions = int(dims) if isinstance(dims, int) else DEFAULT_DIMS
            from opencode_search.embeddings import embed_passages
            vectors = embed_passages(texts, model=DEFAULT_EMBED_MODEL, dimensions=dimensions)
            _json_response(self, 200, {"result": {"vectors": vectors}})
            return

        if path == "/embed/query":
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                _json_response(self, 400, {"error": "text must be a non-empty string"})
                return
            dims = body.get("dimensions")
            dimensions = int(dims) if isinstance(dims, int) else DEFAULT_DIMS
            from opencode_search.embeddings import embed_query
            vector = embed_query(text, model=DEFAULT_EMBED_MODEL, dimensions=dimensions)
            _json_response(self, 200, {"result": {"vector": vector}})
            return

        if path == "/embed/chunk":
            content = body.get("content")
            content_type = body.get("content_type", "text")
            if not isinstance(content, str):
                _json_response(self, 400, {"error": "content must be a string"})
                return
            if not isinstance(content_type, str):
                content_type = "text"
            ext = {
                "markdown": ".md",
                "code": ".py",
                "text": ".txt",
            }.get(content_type.lower(), ".txt")
            from opencode_search.chunker import chunk_file
            chunks = chunk_file(content, Path("input" + ext))
            _json_response(self, 200, {"result": {"chunks": [c.content for c in chunks]}})
            return

        if path == "/embed/rerank":
            query = body.get("query")
            docs = body.get("docs")
            if not isinstance(query, str) or not isinstance(docs, list) or not all(isinstance(d, str) for d in docs):
                # Reject legacy wrong key 'documents' by returning 400 or empty scores.
                _json_response(self, 400, {"error": "expected {'query': str, 'docs': list[str]}"} )
                return
            top_k = body.get("top_k")
            k = int(top_k) if isinstance(top_k, int) else len(docs)
            from opencode_search.embeddings import rerank
            ranked = rerank(query, docs, model=DEFAULT_RERANK_MODEL, top_k=k)
            # Return only scores in original order (tests just need numeric list).
            scores = [0.0] * len(docs)
            for idx, score in ranked:
                if 0 <= idx < len(scores):
                    scores[idx] = float(score)
            _json_response(self, 200, {"result": {"scores": scores}})
            return

        _json_response(self, 404, {"error": "not_found"})


def main() -> None:
    host = os.environ.get("OPENCODE_EMBEDDER_HOST", "127.0.0.1")
    port = int(os.environ.get("OPENCODE_EMBEDDER_PORT", "9998"))
    httpd = ThreadingHTTPServer((host, port), _Handler)
    print(f"opencode_embedder compat listening on http://{host}:{port}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

