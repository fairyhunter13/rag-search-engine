"""Live answer quality tests — LLM judge scores real answers 1-5.

Each test sends a real question to /api/chat_stream and scores the answer
using the local query LLM. Score must be ≥ 3/5 to pass.

Requires: daemon at :8765, indexed project with communities, Ollama running.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live

from .conftest import judge_answer, parse_sse  # noqa: E402

_MIN_SCORE = 3


def _ask_chat(http, project: str, query: str) -> str:
    r = http.post(
        "/api/chat_stream",
        json={"project": project, "query": query},
        headers={"Accept": "text/event-stream"},
    )
    assert r.status_code == 200, f"chat_stream failed: {r.status_code}"
    events = parse_sse(r)
    return "".join(e.get("text", "") for e in events if e.get("type") == "token")


def test_quality_architecture_answer(http, project):
    """Architecture answer must score ≥ 3/5 for describing system structure."""
    answer = _ask_chat(http, project, "What is the overall architecture of this codebase?")
    assert len(answer) > 50, f"Architecture answer too short: {answer!r}"
    score = judge_answer(answer, "Does this describe system architecture with concrete components or layers?")
    assert score >= _MIN_SCORE, f"Architecture answer quality {score}/5 too low:\n{answer[:400]}"


def test_quality_search_explanation(http, project):
    """Search explanation must score ≥ 3/5 for describing how search works."""
    answer = _ask_chat(http, project, "How does search work end to end?")
    assert len(answer) > 50, f"Search answer too short: {answer!r}"
    score = judge_answer(answer, "Does this explain how search works with implementation details?")
    assert score >= _MIN_SCORE, f"Search answer quality {score}/5 too low:\n{answer[:400]}"


def test_quality_entry_points_answer(http, project):
    """Entry points answer must score ≥ 3/5 for naming real code entry points."""
    answer = _ask_chat(http, project, "What are the main entry points of this system?")
    assert len(answer) > 50, f"Entry points answer too short: {answer!r}"
    score = judge_answer(answer, "Does this identify concrete code entry points (functions, handlers, main)?")
    assert score >= _MIN_SCORE, f"Entry points answer quality {score}/5 too low:\n{answer[:400]}"


def test_quality_global_overview(http, project):
    """Global overview must score ≥ 3/5 for breadth of system coverage."""
    answer = _ask_chat(http, project, "Give me a comprehensive global overview of this entire system")
    assert len(answer) > 100, f"Global overview too short: {answer!r}"
    score = judge_answer(answer, "Does this provide a broad, multi-domain system overview?")
    assert score >= _MIN_SCORE, f"Global overview quality {score}/5 too low:\n{answer[:400]}"


def test_quality_frameworks_answer(http, project):
    """Frameworks answer must score ≥ 3/5 for naming real libraries/frameworks used."""
    answer = _ask_chat(http, project, "What frameworks and libraries does this project use?")
    assert len(answer) > 30, f"Frameworks answer too short: {answer!r}"
    score = judge_answer(answer, "Does this name specific frameworks or libraries with reasonable accuracy?")
    assert score >= _MIN_SCORE, f"Frameworks answer quality {score}/5 too low:\n{answer[:400]}"
