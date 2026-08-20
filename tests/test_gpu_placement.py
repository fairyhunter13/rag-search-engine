"""Where the nodes actually ran, on the real models.

`verify_session` reads the session's provider *list*, and ORT registers the CPU
EP as an implicit fallback, so a healthy session reports `[CUDA, CPU]` whatever
happened underneath. This module is the only thing that opens the profile and
looks, which is how the nine CPU-assigned nodes in both exports were found at
all. It is the gate on a model swap: a new export that pushes tensor math to
the host fails here and nowhere else.
"""

from __future__ import annotations

import json
import pathlib

import onnxruntime as ort
import pytest
from huggingface_hub import hf_hub_download
from tests.live import require_clear_gpu

from coderag import config, embed, gpu

pytestmark = pytest.mark.gpu


def _profile(repo: str, filename: str, pairs, max_tokens: int, out: pathlib.Path):
    """One real batch through a production-identical session, profiling on.

    The options come from `embed.session_options()` rather than being rebuilt
    here: a probe that constructs its own is measuring a session production
    never runs.
    """
    require_clear_gpu()
    gpu.preload()
    path = hf_hub_download(repo_id=repo, filename=filename)
    opts = embed.session_options()
    opts.enable_profiling = True
    opts.profile_file_prefix = str(out / "prof")
    session = ort.InferenceSession(path, opts, providers=gpu.providers())
    gpu.verify_session(session, repo)
    tokenizer = embed._tokenizer(repo, max_tokens)
    session.run(None, embed._feed(session, tokenizer.encode_batch(pairs)))
    events = json.loads(pathlib.Path(session.end_profiling()).read_text())
    nodes = [
        (e["args"]["op_name"], e["args"]["provider"], e["dur"])
        for e in events
        if e.get("cat") == "Node" and (e.get("args") or {}).get("provider")
    ]
    assert nodes, "the profile carried no node events; the measurement is void"
    return nodes


def test_the_embedder_runs_its_tensor_math_on_the_gpu(tmp_path):
    nodes = _profile(
        config.EMBED_MODEL,
        config.EMBED_ONNX_FILE,
        ["def parse(path):\n    return json.load(open(path))", "SELECT id FROM users"],
        config.EMBED_MAX_TOKENS,
        tmp_path,
    )
    gpu.check_placement(nodes, f"embedder {config.EMBED_MODEL}")


def test_the_reranker_runs_its_tensor_math_on_the_gpu(tmp_path):
    """Separately, because it is the larger model and the one that has landed
    on CPU before while the embedder was fine."""
    nodes = _profile(
        config.RERANK_MODEL,
        config.RERANK_ONNX_FILE,
        [("read a config file", "def parse(path): return json.load(open(path))")],
        config.RERANK_MAX_TOKENS,
        tmp_path,
    )
    gpu.check_placement(nodes, f"reranker {config.RERANK_MODEL}")


def test_the_check_would_have_caught_the_heaviest_node_moving(tmp_path):
    """Falsification, against the real graph rather than a fixture.

    Only the single heaviest node is moved, and the rest of the census is left
    exactly as measured -- so this fails if `check_placement` returns
    unconditionally, and it also fails if the bound is loose enough to absorb
    one real op.
    """
    nodes = _profile(
        config.EMBED_MODEL,
        config.EMBED_ONNX_FILE,
        ["def parse(path): return 1"],
        config.EMBED_MAX_TOKENS,
        tmp_path,
    )
    heaviest = max(range(len(nodes)), key=lambda i: nodes[i][2])
    op, _, dur = nodes[heaviest]
    moved = list(nodes)
    moved[heaviest] = (op, "CPUExecutionProvider", dur)
    with pytest.raises(RuntimeError, match="CPU inference is forbidden"):
        gpu.check_placement(moved, "embedder")
