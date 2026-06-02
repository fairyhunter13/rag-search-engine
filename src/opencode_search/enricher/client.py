"""Multi-provider LLM client for code enrichment.

Uses stdlib urllib and subprocess — no new dependencies.

Environment variables:
  OPENCODE_LLM_PROVIDER=ollama|none|anthropic|openai|claude-code|codex
                              (default: ollama)
  OPENCODE_LLM_MODEL          model name (provider-specific defaults below)
  OPENCODE_LLM_API_KEY        API key for anthropic/openai
                              Falls back to ANTHROPIC_API_KEY or OPENAI_API_KEY.
  OPENCODE_LLM_BASE_URL       override base URL (useful for proxies or Ollama host)
  OPENCODE_LLM_TIMEOUT        request timeout in seconds (default: 120)
  OPENCODE_LLM_NUM_CTX        Ollama context window size (default: 2048)
  OPENCODE_LLM_ENRICH_ON_INDEX=false  run enrichment automatically after index

Provider defaults:
  ollama:      base_url=http://localhost:11434  model=phi4-mini:3.8b
  anthropic:   base_url=https://api.anthropic.com  model=claude-haiku-4-5-20251001
  openai:      base_url=https://api.openai.com  model=gpt-4o-mini
  claude-code: uses locally installed `claude` CLI; model=claude-haiku-4-5-20251001
  codex:       uses locally installed `codex` CLI; model=gpt-5.4-mini
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from typing import Any

# ---------------------------------------------------------------------------
# Ollama module-level defaults (inline to avoid circular imports with config.py)
# ---------------------------------------------------------------------------
_OLLAMA_DEFAULT_MODEL = os.environ.get("OPENCODE_LLM_MODEL", "phi4-mini:3.8b")
_OLLAMA_DEFAULT_NUM_CTX = int(os.environ.get("OPENCODE_LLM_NUM_CTX", "2048"))
_OLLAMA_DEFAULT_TIMEOUT = int(os.environ.get("OPENCODE_LLM_TIMEOUT", "120"))


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class LLMClient:
    """Abstract base: every provider subclass only needs to implement chat()."""

    model: str
    timeout: int

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> str:
        raise NotImplementedError

    def is_available(self) -> bool:
        """Probe the provider to confirm it is reachable. Default: optimistic."""
        return True

    # ------------------------------------------------------------------
    # Prompt helpers (shared across all providers)
    # ------------------------------------------------------------------

    def symbol_intent(self, name: str, signature: str, docstring: str | None) -> str:
        """One-sentence description of what a function/class does."""
        doc_part = f"\nDocstring: {docstring}" if docstring else ""
        return self.chat(
            [
                {
                    "role": "user",
                    "content": (
                        "You are a code documentation assistant. "
                        "Respond with exactly one sentence describing what the given function does. "
                        "Be concise. No preamble.\n\n"
                        f"Function: {name}\nSignature: {signature}{doc_part}"
                    ),
                }
            ],
            max_tokens=128,
        )

    def community_summary(
        self,
        node_summaries: list[str],
        code_samples: list[tuple[str, str]] | None = None,
    ) -> tuple[str, str]:
        """Generate title + 2-3 sentence summary for a community cluster.

        LLM-first approach: when code_samples are provided the LLM sees actual
        source code in addition to symbol names, producing richer and more
        accurate descriptions.
        """
        nodes_text = "\n".join(f"- {s}" for s in node_summaries[:30])
        code_text = ""
        if code_samples:
            snippets = "\n\n".join(
                f"--- {path} ---\n{snippet[:800]}"
                for path, snippet in code_samples[:3]
            )
            code_text = f"\n\nCode samples from this cluster:\n{snippets}"
        text = self.chat(
            [
                {
                    "role": "user",
                    "content": (
                        "You are a software architect. Given this code cluster, respond with:\n"
                        "TITLE: <short descriptive title>\n"
                        "SUMMARY: <2-3 sentence summary of what this cluster does>\n"
                        "No other text.\n\n"
                        f"Cluster symbols:\n{nodes_text}{code_text}"
                    ),
                }
            ],
            max_tokens=300,
        )
        title = summary = ""
        for line in text.splitlines():
            if line.startswith("TITLE:"):
                title = line[6:].strip()
            elif line.startswith("SUMMARY:"):
                summary = line[8:].strip()
        return title or "Untitled cluster", summary or text

    def module_wiki_page(
        self,
        module_path: str,
        symbols: list[str],
        imports: list[str],
    ) -> str:
        """Generate markdown wiki page for a module."""
        symbols_text = "\n".join(f"- {s}" for s in symbols[:20])
        imports_text = "\n".join(f"- {i}" for i in imports[:10])
        return self.chat(
            [
                {
                    "role": "user",
                    "content": (
                        "You are a technical writer. Generate a concise markdown wiki page "
                        "for the given module. Include: purpose, key symbols, and dependencies. "
                        "Use markdown headers. Be factual, not verbose.\n\n"
                        f"Module: {module_path}\n\n"
                        f"Symbols:\n{symbols_text}\n\n"
                        f"Dependencies:\n{imports_text}"
                    ),
                }
            ],
            max_tokens=512,
        )

    def project_overview(self, file_samples: list[tuple[str, str]]) -> str:
        """Step 1 of LLM-first pattern detection: high-level project overview.

        Sends sampled source files to the LLM and asks for a structured JSON
        overview: architecture, tech stack, observed patterns, primary language.
        Returns a JSON string (parsed by caller).
        """
        files_text = "\n\n".join(
            f"--- {rel} ---\n{content[:1500]}" for rel, content in file_samples[:8]
        )
        return self.chat(
            [
                {
                    "role": "user",
                    "content": (
                        "You are a senior software architect. Analyse these source file samples "
                        "and respond with a JSON object (no markdown fences) describing the project:\n"
                        '{\n'
                        '  "primary_language": "go|python|java|...",\n'
                        '  "tech_stack": ["list of frameworks/libraries observed"],\n'
                        '  "architecture_style": "microservices|monolith|clean_architecture|...",\n'
                        '  "key_patterns": ["pattern1", "pattern2"],\n'
                        '  "project_purpose": "one sentence describing what this does",\n'
                        '  "confidence": "high|medium|low"\n'
                        "}\n\nSource files:\n\n" + files_text
                    ),
                }
            ],
            max_tokens=512,
        )

    def project_synthesis(
        self,
        overview_json: str,
        exact_facts: dict,
    ) -> str:
        """Step 3 of LLM-first pattern detection: synthesise deep semantic knowledge.

        Takes the LLM overview (Step 1) + exact parsed facts (Step 2: tree-sitter
        graph stats, manifest versions, language counts) and produces a rich
        structured analysis. Returns a JSON string.
        """
        import json as _json
        facts_text = _json.dumps(exact_facts, indent=2)[:3000]
        return self.chat(
            [
                {
                    "role": "user",
                    "content": (
                        "You are a senior software architect. Combine this LLM overview and "
                        "these exact parsed facts to produce a comprehensive project analysis.\n\n"
                        "LLM Overview:\n" + overview_json + "\n\n"
                        "Exact Facts (tree-sitter graph, manifests, file counts):\n" + facts_text + "\n\n"
                        "Respond with a JSON object (no markdown fences):\n"
                        '{\n'
                        '  "architecture_description": "paragraph describing architecture",\n'
                        '  "primary_language": "go|python|java|...",\n'
                        '  "coding_patterns": ["pattern1", "pattern2"],\n'
                        '  "naming_conventions": "description of naming style",\n'
                        '  "error_handling_style": "description",\n'
                        '  "test_approach": "description of testing strategy",\n'
                        '  "key_abstractions": ["top abstractions visible in code"],\n'
                        '  "code_quality_signals": ["positive signals", "potential concerns"],\n'
                        '  "version_highlights": {"pkg": "version"},\n'
                        '  "confidence": "high|medium|low"\n'
                        "}"
                    ),
                }
            ],
            max_tokens=1024,
        )

    def map_query(self, query: str, community_summaries: list[str]) -> str:
        """MAP phase of global synthesis: extract query-relevant info from a batch of communities.

        Called once per batch of ~8 community summaries. Returns a partial answer
        string that the REDUCE phase will synthesize into a final response.
        """
        batch_text = "\n\n".join(
            f"[Community {i+1}] {s}" for i, s in enumerate(community_summaries[:10])
        )
        return self.chat(
            [
                {
                    "role": "user",
                    "content": (
                        "You are a software architect analyzing a codebase. Given the following "
                        "code community descriptions, extract any information relevant to answering "
                        "the query. If none are relevant, say 'No relevant information.'\n\n"
                        f"Query: {query}\n\n"
                        f"Community descriptions:\n{batch_text}\n\n"
                        "Respond concisely with only the relevant findings:"
                    ),
                }
            ],
            max_tokens=400,
        )

    def reduce_answers(self, query: str, partial_answers: list[str]) -> str:
        """REDUCE phase of global synthesis: synthesize partial answers into a final response.

        Takes all the MAP outputs and combines them into a coherent, complete answer.
        """
        answers_text = "\n\n---\n\n".join(
            f"Finding {i+1}:\n{a}" for i, a in enumerate(partial_answers[:20])
            if a.strip() and "no relevant information" not in a.lower()
        )
        if not answers_text:
            return "No relevant information found across the codebase communities."
        return self.chat(
            [
                {
                    "role": "user",
                    "content": (
                        "You are a senior software architect. Synthesize these partial findings "
                        "from different parts of a codebase into a single comprehensive answer. "
                        "Be specific, reference component names, and avoid repetition.\n\n"
                        f"Original query: {query}\n\n"
                        f"Partial findings:\n{answers_text}\n\n"
                        "Provide a complete, well-structured answer:"
                    ),
                }
            ],
            max_tokens=800,
        )

    def impact_narrative(
        self,
        symbol: str,
        callers: list[str],
        affected_domains: list[str],
        impact_count: int,
    ) -> str:
        """Generate a natural-language impact analysis for a code change."""
        callers_text = "\n".join(f"- {c}" for c in callers[:20])
        domains_text = ", ".join(affected_domains[:10]) or "unknown"
        return self.chat(
            [
                {
                    "role": "user",
                    "content": (
                        "You are a senior software engineer. Analyze the blast radius of changing "
                        "the given symbol and provide a concise impact summary.\n\n"
                        f"Symbol: {symbol}\n"
                        f"Direct/transitive callers ({impact_count} total):\n{callers_text}\n"
                        f"Affected architecture domains: {domains_text}\n\n"
                        "Respond with:\n"
                        "RISK: <low|medium|high>\n"
                        "SUMMARY: <2-3 sentences describing who is affected and why>\n"
                        "ACTION: <one sentence recommending what to test or review>"
                    ),
                }
            ],
            max_tokens=300,
        )

    def trace_narrative(
        self,
        from_symbol: str,
        to_symbol: str,
        path: list[dict],
    ) -> str:
        """Generate a natural-language narrative for a call chain trace."""
        steps = "\n".join(
            f"  {i+1}. {n.get('qualified_name', n.get('name', '?'))} "
            f"({n.get('kind', '?')}) in {(n.get('file','?') or '?').rsplit('/', 1)[-1]}"
            for i, n in enumerate(path[:20])
        )
        return self.chat(
            [
                {
                    "role": "user",
                    "content": (
                        "You are a software architect. Explain this call chain in plain English, "
                        "describing what each step does and the overall flow.\n\n"
                        f"From: {from_symbol}\n"
                        f"To: {to_symbol}\n"
                        f"Call chain ({len(path)} hops):\n{steps}\n\n"
                        "Write a concise narrative (3-5 sentences) describing the flow:"
                    ),
                }
            ],
            max_tokens=400,
        )

    def service_mesh_description(self, service_edges: list[dict]) -> str:
        """Describe a detected service mesh topology in natural language."""
        edges_text = "\n".join(
            f"- {e.get('from','?')} → {e.get('to','?')} via {e.get('protocol','?')}"
            for e in service_edges[:30]
        )
        return self.chat(
            [
                {
                    "role": "user",
                    "content": (
                        "You are a software architect. Describe this microservice topology in 2-3 sentences, "
                        "noting the main communication patterns and any critical services.\n\n"
                        f"Detected service calls:\n{edges_text}\n\n"
                        "Provide a concise architectural description:"
                    ),
                }
            ],
            max_tokens=300,
        )

    def raw_doc_to_wiki(self, content: str, source_name: str) -> str:
        """Convert a raw document into a wiki page."""
        truncated = content[:4000]
        return self.chat(
            [
                {
                    "role": "user",
                    "content": (
                        "You are a technical writer. Convert the given document into a clean "
                        "markdown wiki page. Extract the key information and structure it clearly. "
                        "Use markdown headers. Keep it concise.\n\n"
                        f"Source: {source_name}\n\nContent:\n{truncated}"
                    ),
                }
            ],
            max_tokens=1024,
        )


# ---------------------------------------------------------------------------
# Ollama provider
# ---------------------------------------------------------------------------


class OllamaClient(LLMClient):
    """Blocking HTTP client for Ollama's /api/chat endpoint."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "phi4-mini:3.8b",
        timeout: int = 120,
        num_ctx: int = 2048,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.num_ctx = num_ctx

    @classmethod
    def from_env(cls) -> OllamaClient | None:
        """Returns None unless OPENCODE_LLM_PROVIDER=ollama."""
        provider = os.environ.get("OPENCODE_LLM_PROVIDER", "ollama").strip().lower()
        if provider != "ollama":
            return None
        return cls(
            base_url=os.environ.get("OPENCODE_LLM_BASE_URL", "http://localhost:11434"),
            model=os.environ.get("OPENCODE_LLM_MODEL", _OLLAMA_DEFAULT_MODEL),
            timeout=int(os.environ.get("OPENCODE_LLM_TIMEOUT", str(_OLLAMA_DEFAULT_TIMEOUT))),
            num_ctx=int(os.environ.get("OPENCODE_LLM_NUM_CTX", str(_OLLAMA_DEFAULT_NUM_CTX))),
        )

    def is_available(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.base_url}/api/tags")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "num_ctx": self.num_ctx,
            },
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body["message"]["content"]
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Ollama HTTP {exc.code}: {exc.read().decode()}") from exc
        except urllib.error.URLError as exc:
            raise ConnectionError(f"Ollama connection error: {exc.reason}") from exc


# ---------------------------------------------------------------------------
# Anthropic provider
# ---------------------------------------------------------------------------

_ANTHROPIC_API_VERSION = "2023-06-01"
_ANTHROPIC_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_ANTHROPIC_BASE_URL = "https://api.anthropic.com"


class AnthropicClient(LLMClient):
    """HTTP client for the Anthropic Messages API (stdlib urllib, no SDK)."""

    def __init__(
        self,
        api_key: str,
        model: str = _ANTHROPIC_DEFAULT_MODEL,
        base_url: str = _ANTHROPIC_BASE_URL,
        timeout: int = 120,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> AnthropicClient | None:
        """Returns None unless OPENCODE_LLM_PROVIDER=anthropic."""
        provider = os.environ.get("OPENCODE_LLM_PROVIDER", "none").strip().lower()
        if provider != "anthropic":
            return None
        api_key = (
            os.environ.get("OPENCODE_LLM_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or ""
        )
        if not api_key:
            raise ValueError(
                "OPENCODE_LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY "
                "or OPENCODE_LLM_API_KEY to be set."
            )
        return cls(
            api_key=api_key,
            model=os.environ.get("OPENCODE_LLM_MODEL", _ANTHROPIC_DEFAULT_MODEL),
            base_url=os.environ.get("OPENCODE_LLM_BASE_URL", _ANTHROPIC_BASE_URL),
            timeout=int(os.environ.get("OPENCODE_LLM_TIMEOUT", "120")),
        )

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/v1/messages",
            data=data,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": _ANTHROPIC_API_VERSION,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                # content is a list of blocks; grab first text block
                for block in body.get("content", []):
                    if block.get("type") == "text":
                        return block["text"]
                raise RuntimeError(f"Anthropic returned no text block: {body}")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"Anthropic HTTP {exc.code}: {exc.read().decode()}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ConnectionError(f"Anthropic connection error: {exc.reason}") from exc


# ---------------------------------------------------------------------------
# OpenAI provider (also compatible with any OpenAI-compatible endpoint)
# ---------------------------------------------------------------------------

_OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
_OPENAI_BASE_URL = "https://api.openai.com"


class OpenAIClient(LLMClient):
    """HTTP client for OpenAI Chat Completions API (and compatible endpoints)."""

    def __init__(
        self,
        api_key: str,
        model: str = _OPENAI_DEFAULT_MODEL,
        base_url: str = _OPENAI_BASE_URL,
        timeout: int = 120,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> OpenAIClient | None:
        """Returns None unless OPENCODE_LLM_PROVIDER=openai."""
        provider = os.environ.get("OPENCODE_LLM_PROVIDER", "none").strip().lower()
        if provider != "openai":
            return None
        api_key = (
            os.environ.get("OPENCODE_LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        )
        if not api_key:
            raise ValueError(
                "OPENCODE_LLM_PROVIDER=openai requires OPENAI_API_KEY "
                "or OPENCODE_LLM_API_KEY to be set."
            )
        return cls(
            api_key=api_key,
            model=os.environ.get("OPENCODE_LLM_MODEL", _OPENAI_DEFAULT_MODEL),
            base_url=os.environ.get("OPENCODE_LLM_BASE_URL", _OPENAI_BASE_URL),
            timeout=int(os.environ.get("OPENCODE_LLM_TIMEOUT", "120")),
        )

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                choices = body.get("choices", [])
                if not choices:
                    raise RuntimeError(f"OpenAI returned no choices: {body}")
                return choices[0]["message"]["content"]
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"OpenAI HTTP {exc.code}: {exc.read().decode()}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ConnectionError(f"OpenAI connection error: {exc.reason}") from exc


# ---------------------------------------------------------------------------
# Claude Code CLI provider  (shells out to `claude -p`)
# ---------------------------------------------------------------------------

_CLAUDE_CODE_DEFAULT_MODEL = "claude-haiku-4-5-20251001"


def _format_messages_as_prompt(messages: list[dict[str, str]]) -> str:
    """Flatten a chat message list into a single prompt string for CLI tools."""
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            parts.append(f"[System instructions]\n{content}")
        elif role == "assistant":
            parts.append(f"[Previous assistant response]\n{content}")
        else:
            parts.append(content)
    return "\n\n".join(parts)


class ClaudeCodeClient(LLMClient):
    """LLM client that delegates to the locally installed `claude` CLI.

    Uses `claude -p <prompt> --model <model>` (print/non-interactive mode).
    No API key needed — authentication is managed by Claude Code itself.

    Install: https://claude.ai/code
    """

    def __init__(
        self,
        model: str = _CLAUDE_CODE_DEFAULT_MODEL,
        timeout: int = 120,
    ) -> None:
        self.model = model
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> ClaudeCodeClient | None:
        """Returns None unless OPENCODE_LLM_PROVIDER=claude-code."""
        provider = os.environ.get("OPENCODE_LLM_PROVIDER", "none").strip().lower()
        if provider != "claude-code":
            return None
        return cls(
            model=os.environ.get("OPENCODE_LLM_MODEL", _CLAUDE_CODE_DEFAULT_MODEL),
            timeout=int(os.environ.get("OPENCODE_LLM_TIMEOUT", "120")),
        )

    def is_available(self) -> bool:
        return shutil.which("claude") is not None

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> str:
        if not shutil.which("claude"):
            raise RuntimeError(
                "claude CLI not found. Install Claude Code: https://claude.ai/code"
            )
        prompt = _format_messages_as_prompt(messages)
        cmd = ["claude", "-p", prompt, "--model", self.model]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"claude CLI timed out after {self.timeout}s") from exc
        if result.returncode != 0:
            raise RuntimeError(
                f"claude CLI exited {result.returncode}: {result.stderr.strip()}"
            )
        output = result.stdout.strip()
        if not output:
            raise RuntimeError("claude CLI returned empty output")
        return output


# ---------------------------------------------------------------------------
# Codex CLI provider  (shells out to `codex`)
# ---------------------------------------------------------------------------

_CODEX_DEFAULT_MODEL = "gpt-5.4-mini"


class CodexClient(LLMClient):
    """LLM client that delegates to the locally installed OpenAI `codex` CLI.

    Uses `codex exec <prompt>` in non-interactive mode.
    Authentication is managed by the Codex CLI (OPENAI_API_KEY or ChatGPT account
    via `codex login`). Model selection is controlled by codex's own config —
    ChatGPT-backed accounts do not support --model overrides, so we do not
    pass --model by default. Set pass_model_flag=True (or OPENCODE_CODEX_PASS_MODEL=1)
    only when using codex with an OpenAI API key that supports model selection.

    Install: https://github.com/openai/codex
    """

    def __init__(
        self,
        model: str = _CODEX_DEFAULT_MODEL,
        timeout: int = 120,
        pass_model_flag: bool = False,
    ) -> None:
        self.model = model
        self.timeout = timeout
        # Only pass --model to codex when explicitly enabled — ChatGPT-backed
        # codex accounts reject model overrides with an API error.
        self.pass_model_flag = pass_model_flag or bool(
            os.environ.get("OPENCODE_CODEX_PASS_MODEL", "")
        )

    @classmethod
    def from_env(cls) -> CodexClient | None:
        """Returns None unless OPENCODE_LLM_PROVIDER=codex."""
        provider = os.environ.get("OPENCODE_LLM_PROVIDER", "none").strip().lower()
        if provider != "codex":
            return None
        return cls(
            model=os.environ.get("OPENCODE_LLM_MODEL", _CODEX_DEFAULT_MODEL),
            timeout=int(os.environ.get("OPENCODE_LLM_TIMEOUT", "120")),
            pass_model_flag=bool(os.environ.get("OPENCODE_CODEX_PASS_MODEL", "")),
        )

    def is_available(self) -> bool:
        return shutil.which("codex") is not None

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> str:
        if not shutil.which("codex"):
            raise RuntimeError(
                "codex CLI not found. Install from: https://github.com/openai/codex"
            )
        prompt = _format_messages_as_prompt(messages)
        # codex exec <task> runs in full-auto non-interactive mode
        cmd = ["codex", "exec", prompt]
        if self.pass_model_flag:
            cmd += ["--model", self.model]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"codex CLI timed out after {self.timeout}s") from exc
        if result.returncode != 0:
            raise RuntimeError(
                f"codex CLI exited {result.returncode}: {result.stderr.strip()}"
            )
        output = result.stdout.strip()
        if not output:
            raise RuntimeError("codex CLI returned empty output")
        return output


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_llm_client() -> LLMClient | None:
    """Create the appropriate LLM client from environment variables.

    Returns None when OPENCODE_LLM_PROVIDER=none (default).

    Supported providers:
      ollama      — local Ollama instance
      anthropic   — Anthropic Claude API (requires ANTHROPIC_API_KEY)
      openai      — OpenAI Chat API (requires OPENAI_API_KEY)
      claude-code — locally installed `claude` CLI (no separate API key needed)
      codex       — locally installed OpenAI `codex` CLI
    """
    provider = os.environ.get("OPENCODE_LLM_PROVIDER", "ollama").strip().lower()
    if provider == "none" or not provider:
        return None
    if provider == "ollama":
        return OllamaClient.from_env()
    if provider == "anthropic":
        return AnthropicClient.from_env()
    if provider == "openai":
        return OpenAIClient.from_env()
    if provider == "claude-code":
        return ClaudeCodeClient.from_env()
    if provider == "codex":
        return CodexClient.from_env()
    raise ValueError(
        f"Unknown OPENCODE_LLM_PROVIDER={provider!r}. "
        "Valid values: none, ollama, anthropic, openai, claude-code, codex"
    )
