"""Multi-provider LLM client for code enrichment.

Uses stdlib urllib and subprocess — no new dependencies.

Environment variables:
  OPENCODE_LLM_PROVIDER=ollama|none|anthropic|openai|claude-code|codex
                              (default: codex)
  OPENCODE_LLM_MODEL          model name (provider-specific defaults below)
  OPENCODE_LLM_API_KEY        API key for anthropic/openai
                              Falls back to ANTHROPIC_API_KEY or OPENAI_API_KEY.
  OPENCODE_LLM_BASE_URL       override base URL (useful for proxies or Ollama host)
  OPENCODE_LLM_TIMEOUT        request timeout in seconds (default: 120)
  OPENCODE_LLM_NUM_CTX        Ollama context window size (default: 2048)
  OPENCODE_LLM_ENRICH_ON_INDEX=false  run enrichment automatically after index
  OPENCODE_LLM_NO_FALLBACK=1  disable automatic rate-limit fallback to claude-haiku-4.5

Provider defaults:
  ollama:      base_url=http://localhost:11434  model=phi4-mini:3.8b
  anthropic:   base_url=https://api.anthropic.com  model=claude-haiku-4-5-20251001
  openai:      base_url=https://api.openai.com  model=gpt-4o-mini
  claude-code: uses locally installed `claude` CLI; model=claude-haiku-4-5-20251001
  codex:       uses locally installed `codex` CLI; model=gpt-5.4-mini

Rate-limit fallback:
  When codex returns a rate-limit error (429 / quota exceeded), the client
  automatically retries with claude-code (claude-haiku-4-5-20251001) if the
  `claude` CLI is installed.  Disable with OPENCODE_LLM_NO_FALLBACK=1.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

# ---------------------------------------------------------------------------
# Ollama module-level defaults (inline to avoid circular imports with config.py)
# ---------------------------------------------------------------------------
_OLLAMA_DEFAULT_MODEL = os.environ.get("OPENCODE_LLM_MODEL", "qwen3-enrich:1.7b")
_OLLAMA_DEFAULT_NUM_CTX = int(os.environ.get("OPENCODE_LLM_NUM_CTX", "4096"))
_OLLAMA_DEFAULT_TIMEOUT = int(os.environ.get("OPENCODE_LLM_TIMEOUT", "120"))

# ---------------------------------------------------------------------------
# GPU thermal guard — blocks inference when the GPU is too hot.
# Necessary because the RTX 5080 Laptop SBIOS cannot report temperature limits
# to the NVIDIA driver (NV_ERR_INVALID_DATA), leaving the OS with no HW thermal
# protection fallback. This guard is our software-level circuit breaker.
# ---------------------------------------------------------------------------
# Safe defaults — RTX 5080 Laptop SBIOS has no hardware thermal protection, so this
# software guard is the only safeguard against thermal-induced CUDA SEGVs.
_GPU_TEMP_THROTTLE: int = int(os.environ.get("OPENCODE_GPU_TEMP_MAX", "80"))
_GPU_TEMP_RESUME: int = int(os.environ.get("OPENCODE_GPU_TEMP_RESUME", "72"))


def _read_gpu_temp() -> int | None:
    """Return current GPU temp in °C via nvidia-smi, or None if unavailable."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return int(result.stdout.strip())
    except Exception:
        pass
    return None


def _wait_for_gpu_cool(max_wait_s: float = 300.0) -> None:
    """Block until GPU temp is below the throttle threshold.

    Called before every Ollama inference call. If the GPU temp cannot be read
    (nvidia-smi unavailable), this is a no-op — caller proceeds immediately.
    max_wait_s caps total wait so no inference path blocks indefinitely.
    """
    import logging
    import time
    _log = logging.getLogger(__name__)
    temp = _read_gpu_temp()
    if temp is None or temp < _GPU_TEMP_THROTTLE:
        return  # Fast path — no throttling needed
    _log.warning(
        "GPU at %d°C (≥ %d°C limit) — pausing inference until ≤ %d°C (max wait %.0fs)",
        temp, _GPU_TEMP_THROTTLE, _GPU_TEMP_RESUME, max_wait_s,
    )
    waited = 0.0
    while waited < max_wait_s:
        time.sleep(15)
        waited += 15
        temp = _read_gpu_temp()
        if temp is None or temp <= _GPU_TEMP_RESUME:
            _log.info("GPU cooled to %s°C — resuming inference", temp)
            return
        _log.warning("GPU still at %d°C — waiting 15s more", temp)
    _log.warning(
        "GPU thermal wait exceeded max_wait_s=%.0fs at %s°C — proceeding anyway",
        max_wait_s, temp,
    )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class RateLimitError(RuntimeError):
    """Raised when the LLM provider returns a rate-limit / quota-exceeded error."""


def _is_rate_limit(msg: str) -> bool:
    low = msg.lower()
    return any(k in low for k in ("rate limit", "429", "quota exceeded", "too many requests",
                                   "ratelimit", "rate_limit"))


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

    def symbol_intent_batch(
        self, items: list[tuple[str, str, str | None]]
    ) -> list[str]:
        """Generate intent strings for N (name, signature, docstring) tuples.

        Returns a list of N strings in input order. Empty string means the model
        failed to produce an intent for that item. Default: sequential fallback.
        """
        results: list[str] = []
        for name, sig, doc in items:
            try:
                results.append(self.symbol_intent(name, sig, doc))
            except Exception:
                results.append("")
        return results

    def community_summary(
        self,
        node_summaries: list[str],
        code_samples: list[tuple[str, str]] | None = None,
    ) -> tuple[str, str, str]:
        """Generate title + summary + semantic_type for a community cluster.

        Returns (title, summary, semantic_type). semantic_type is one of:
        feature|business_process|business_rule|data_model|api_boundary|infrastructure|utility
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
                        "TYPE: <one of: feature|business_process|business_rule|data_model|api_boundary|infrastructure|utility>\n"
                        "No other text.\n\n"
                        "TYPE guide: feature=user-facing capability, business_process=workflow/flow, "
                        "business_rule=constraint/policy/validation, data_model=schema/entity/ORM, "
                        "api_boundary=HTTP/RPC interface, infrastructure=config/deploy/logging, "
                        "utility=helper/test/build\n\n"
                        f"Cluster symbols:\n{nodes_text}{code_text}"
                    ),
                }
            ],
            max_tokens=350,
        )
        title = summary = semantic_type = ""
        _valid_types = {"feature", "business_process", "business_rule", "data_model", "api_boundary", "infrastructure", "utility"}
        # Try JSON first (some models return {"TITLE":...,"SUMMARY":...,"TYPE":...})
        stripped = text.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                title = parsed.get("TITLE") or parsed.get("title") or ""
                summary = parsed.get("SUMMARY") or parsed.get("summary") or ""
                semantic_type = parsed.get("TYPE") or parsed.get("type") or ""
            except (json.JSONDecodeError, AttributeError):
                pass
        if not title:
            for line in text.splitlines():
                if line.startswith("TITLE:"):
                    title = line[6:].strip()
                elif line.startswith("SUMMARY:"):
                    summary = line[8:].strip()
                elif line.startswith("TYPE:"):
                    semantic_type = line[5:].strip().lower()
        semantic_type = semantic_type.lower() if semantic_type else ""
        if semantic_type not in _valid_types:
            semantic_type = "utility"
        return title or "Untitled cluster", summary or text, semantic_type

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

    def feature_trace(
        self,
        query: str,
        entry_points: str,
        call_chain: str,
        community_context: str,
    ) -> str:
        """Synthesize algorithm + design rationale for a feature.

        Returns a JSON string with keys: algorithm, design_rationale,
        involved_services, key_design_decisions.
        """
        return self.chat(
            [
                {
                    "role": "user",
                    "content": (
                        "You are a software architecture expert. Analyze the following feature "
                        "and explain it with algorithm steps and design rationale.\n\n"
                        f"FEATURE: \"{query}\"\n\n"
                        f"TOP ENTRY POINTS:\n{entry_points}\n\n"
                        f"CALL CHAIN:\n{call_chain}\n\n"
                        f"ARCHITECTURAL CONTEXT:\n{community_context}\n\n"
                        "Respond with ONLY valid JSON (no markdown fences):\n"
                        "{\n"
                        '  "algorithm": "Step-by-step description of how this feature works. '
                        '3-5 sentences covering the main steps, data flow, and result.",\n'
                        '  "design_rationale": "WHY the code is designed this way. '
                        'Why these services/patterns? What trade-offs? Like a PC builder choosing '
                        'components: why this CPU, why this architecture. 3-4 sentences.",\n'
                        '  "involved_services": ["ServiceA: role and why", "ServiceB: role and why"],\n'
                        '  "key_design_decisions": ["Decision 1 and why", "Decision 2 and why"]\n'
                        "}"
                    ),
                }
            ],
            max_tokens=800,
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

    def _assert_gpu_only(self) -> None:
        """Crash with a fatal error if this model has any CPU-offloaded layers.

        CPU inference is forbidden and prohibited — all LLM layers must run on GPU.
        Triggered before the first inference call to enforce the GPU-only policy.
        """
        try:
            req = urllib.request.Request(f"{self.base_url}/api/ps")
            with urllib.request.urlopen(req, timeout=5) as resp:
                ps = json.loads(resp.read().decode("utf-8"))
            for entry in ps.get("models", []):
                # Match by exact name or by model family prefix (strip tag)
                entry_name = entry.get("name", "")
                if entry_name != self.model and entry_name.split(":")[0] != self.model.split(":")[0]:
                    continue
                size_total = entry.get("size", 0)
                size_vram = entry.get("size_vram", 0)
                cpu_bytes = size_total - size_vram
                if cpu_bytes > 10_000_000:  # > 10 MB on CPU is a violation
                    cpu_gb = cpu_bytes / 1e9
                    raise RuntimeError(
                        f"FATAL: CPU inference violation — model '{self.model}' has "
                        f"{cpu_gb:.2f} GB offloaded to CPU. "
                        "All LLM inference MUST run on GPU. "
                        "Free up VRAM (kill stale processes) or use a smaller model."
                    )
        except RuntimeError:
            raise
        except Exception as exc:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "GPU-only check skipped for model '%s': /api/ps unreachable (%s). "
                "If a CPU fallback is active it will go undetected.",
                self.model, exc,
            )

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> str:
        _wait_for_gpu_cool()
        self._assert_gpu_only()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": False,  # disable CoT/thinking mode for qwen3+ (no-op on other models)
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

    def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> Iterator[str]:
        """Yield content tokens as they stream from Ollama. Blocking generator.

        Uses Ollama's NDJSON streaming API (stream=true). Each yielded string
        is a raw token fragment exactly as Ollama emits it.
        """

        self._assert_gpu_only()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "think": False,
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
                for raw_line in resp:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue
                    content = evt.get("message", {}).get("content", "")
                    if content:
                        yield content
                    if evt.get("done"):
                        break
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Ollama HTTP {exc.code}: {exc.read().decode()}") from exc
        except urllib.error.URLError as exc:
            raise ConnectionError(f"Ollama connection error: {exc.reason}") from exc


    def symbol_intent_batch(
        self, items: list[tuple[str, str, str | None]]
    ) -> list[str]:
        """Batched symbol intent — N (name, sig, doc) tuples in one Ollama call.

        Prompt asks for N numbered one-sentence intents.  Parses output by
        "N. " line prefix. Falls back to per-item sequential calls if parse
        fails for the whole batch.
        """
        if not items:
            return []
        n = len(items)
        lines: list[str] = []
        for i, (name, sig, doc) in enumerate(items, start=1):
            doc_part = f" | {doc[:80]}" if doc else ""
            lines.append(f"{i}. {name} | {sig[:120]}{doc_part}")
        numbered_text = "\n".join(lines)
        prompt = (
            "You are a code documentation assistant. "
            f"For each of the {n} functions below, write exactly one sentence "
            "describing what it does. Number your responses to match. "
            "Format: '1. <sentence>' on its own line, then '2. <sentence>', etc. "
            "No preamble. No extra text.\n\n"
            f"{numbered_text}"
        )
        try:
            raw = self.chat(
                [{"role": "user", "content": prompt}],
                max_tokens=max(32 * n, 64),
            )
        except Exception:
            return [""] * n

        # Parse "N. sentence" or "N) sentence" output using string ops — no regex
        results: list[str] = [""] * n
        matched = 0
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # Find the leading digit run followed by '.' or ')'
            i = 0
            while i < len(stripped) and stripped[i].isdigit():
                i += 1
            if i == 0 or i >= len(stripped):
                continue
            if stripped[i] not in (".", ")"):
                continue
            num_str = stripped[:i]
            rest = stripped[i + 1:].strip()
            if not rest:
                continue
            idx = int(num_str) - 1
            if 0 <= idx < n:
                results[idx] = rest
                matched += 1

        # If fewer than half parsed, fall back to sequential for better coverage
        if matched < n // 2:
            fallback = super().symbol_intent_batch(items)
            return fallback
        return results


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
            body = exc.read().decode()
            if exc.code == 429 or _is_rate_limit(body):
                raise RateLimitError(f"Anthropic rate-limited (HTTP {exc.code}): {body[:200]}") from exc
            raise RuntimeError(f"Anthropic HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise ConnectionError(f"Anthropic connection error: {exc.reason}") from exc

    def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> Iterator[str]:
        """Yield content tokens from Anthropic SSE streaming API."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
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
                for raw_line in resp:
                    line = raw_line.decode("utf-8").rstrip("\n\r")
                    if not line.startswith("data:"):
                        continue
                    payload_str = line[5:].strip()
                    if not payload_str or payload_str == "[DONE]":
                        continue
                    try:
                        evt = json.loads(payload_str)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("type") == "content_block_delta":
                        text = evt.get("delta", {}).get("text", "")
                        if text:
                            yield text
                    elif evt.get("type") == "message_stop":
                        break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            if exc.code == 429 or _is_rate_limit(body):
                raise RateLimitError(f"Anthropic rate-limited (HTTP {exc.code}): {body[:200]}") from exc
            raise RuntimeError(f"Anthropic HTTP {exc.code}: {body}") from exc
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
            body = exc.read().decode()
            if exc.code == 429 or _is_rate_limit(body):
                raise RateLimitError(f"OpenAI rate-limited (HTTP {exc.code}): {body[:200]}") from exc
            raise RuntimeError(f"OpenAI HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise ConnectionError(f"OpenAI connection error: {exc.reason}") from exc

    def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> Iterator[str]:
        """Yield content tokens from OpenAI SSE streaming API."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
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
                for raw_line in resp:
                    line = raw_line.decode("utf-8").rstrip("\n\r")
                    if not line.startswith("data:"):
                        continue
                    payload_str = line[5:].strip()
                    if not payload_str or payload_str == "[DONE]":
                        continue
                    try:
                        evt = json.loads(payload_str)
                    except json.JSONDecodeError:
                        continue
                    choices = evt.get("choices", [])
                    if choices:
                        text = choices[0].get("delta", {}).get("content", "")
                        if text:
                            yield text
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            if exc.code == 429 or _is_rate_limit(body):
                raise RateLimitError(f"OpenAI rate-limited (HTTP {exc.code}): {body[:200]}") from exc
            raise RuntimeError(f"OpenAI HTTP {exc.code}: {body}") from exc
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
        # Use stdin (--print flag) instead of -p argument — avoids ARG_MAX limits
        # on long prompts (code samples can reach 10-15KB).
        # Note: the claude CLI has no --max-tokens flag; max_tokens/temperature
        # are accepted for API compatibility but not forwarded to the CLI.
        cmd = ["claude", "--print", "--model", self.model]
        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
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
        # codex exec <task> runs in full-auto non-interactive mode.
        # --skip-git-repo-check avoids "Not inside a trusted directory" when the
        # daemon's working directory is not a git repo (e.g., /home/user/).
        cmd = ["codex", "exec", prompt, "--skip-git-repo-check"]
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
            err = result.stderr.strip() or result.stdout.strip()
            if _is_rate_limit(err):
                raise RateLimitError(f"codex rate-limited: {err[:200]}")
            raise RuntimeError(f"codex CLI exited {result.returncode}: {err}")
        output = result.stdout.strip()
        if not output:
            raise RuntimeError("codex CLI returned empty output")
        # stdout may contain rate-limit JSON event even on exit 0 in some versions
        if _is_rate_limit(output):
            raise RateLimitError(f"codex rate-limited (stdout): {output[:200]}")
        return output


# ---------------------------------------------------------------------------
# Rate-limit fallback wrapper
# ---------------------------------------------------------------------------

class FallbackLLMClient(LLMClient):
    """Wraps a primary client and retries with a fallback on RateLimitError.

    When the primary raises RateLimitError (429 / quota exceeded), the call is
    transparently retried against the fallback client.  All other errors from
    the primary propagate as-is without touching the fallback.
    """

    def __init__(self, primary: LLMClient, fallback: LLMClient) -> None:
        self.primary = primary
        self.fallback = fallback
        self.model = primary.model

    def is_available(self) -> bool:
        return self.primary.is_available()

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> str:
        try:
            return self.primary.chat(messages, temperature=temperature, max_tokens=max_tokens)
        except RateLimitError:
            import logging
            logging.getLogger(__name__).warning(
                "Primary LLM (%s) rate-limited — falling back to %s",
                type(self.primary).__name__, type(self.fallback).__name__,
            )
            return self.fallback.chat(messages, temperature=temperature, max_tokens=max_tokens)

    def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> Iterator[str]:
        """Stream from primary if it supports streaming; otherwise yield full response as one chunk.

        On RateLimitError OR any transient failure during the stream, switches to the
        fallback's stream_chat (or chat if stream not supported). If the fallback also
        fails, the exception propagates to the caller which emits an SSE error event.
        """
        import logging
        _log = logging.getLogger(__name__)
        if hasattr(self.primary, "stream_chat"):
            try:
                yield from self.primary.stream_chat(messages, temperature=temperature, max_tokens=max_tokens)
                return
            except Exception as _primary_err:
                _log.warning(
                    "Primary LLM (%s) failed during streaming (%s: %s) — falling back to %s",
                    type(self.primary).__name__, type(_primary_err).__name__, _primary_err,
                    type(self.fallback).__name__,
                )
        else:
            # Primary has no stream_chat — call it as a blocking call, yield result as one chunk
            try:
                yield self.primary.chat(messages, temperature=temperature, max_tokens=max_tokens)
                return
            except Exception as _primary_err:
                _log.warning(
                    "Primary LLM (%s) failed (%s: %s) — falling back to %s",
                    type(self.primary).__name__, type(_primary_err).__name__, _primary_err,
                    type(self.fallback).__name__,
                )
        # Fallback path — prefer streaming if available, otherwise chunk
        if hasattr(self.fallback, "stream_chat"):
            yield from self.fallback.stream_chat(messages, temperature=temperature, max_tokens=max_tokens)
        else:
            result = self.fallback.chat(messages, temperature=temperature, max_tokens=max_tokens)
            yield result


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_llm_client() -> LLMClient | None:
    """Create the appropriate LLM client from environment variables.

    Returns None when OPENCODE_LLM_PROVIDER=none.

    Default provider is claude-code (haiku-4.5) since Jun 2026.
    Override with OPENCODE_LLM_PROVIDER env var.

    Supported providers:
      claude-code — locally installed `claude` CLI (default, no API key needed)
      codex       — locally installed OpenAI `codex` CLI
      ollama      — local Ollama instance
      anthropic   — Anthropic Claude API (requires ANTHROPIC_API_KEY)
      openai      — OpenAI Chat API (requires OPENAI_API_KEY)
    """
    from opencode_search.config import DEFAULT_LLM_MODEL, DEFAULT_LLM_PROVIDER
    provider = os.environ.get("OPENCODE_LLM_PROVIDER", DEFAULT_LLM_PROVIDER).strip().lower()
    if provider == "none" or not provider:
        return None
    # Hard enforcement: codex and claude-code are forbidden for KB build operations.
    # They may only be used for dashboard chat queries via create_query_llm_client().
    if provider in ("codex", "claude-code"):
        raise RuntimeError(
            f"OPENCODE_LLM_PROVIDER={provider!r} is FORBIDDEN for KB build operations. "
            "Indexing, enrichment, wiki generation, and pattern analysis must use "
            "OPENCODE_LLM_PROVIDER=ollama (local GPU). "
            "To use codex/claude-code for dashboard chat, set OPENCODE_QUERY_LLM_PROVIDER instead."
        )
    # Read shared params once — each branch uses these rather than re-reading env.
    model = os.environ.get("OPENCODE_LLM_MODEL", DEFAULT_LLM_MODEL)
    timeout = int(os.environ.get("OPENCODE_LLM_TIMEOUT", "120"))
    if provider == "ollama":
        return OllamaClient(
            base_url=os.environ.get("OPENCODE_LLM_BASE_URL", "http://localhost:11434"),
            model=model,
            timeout=timeout,
        )
    if provider == "anthropic":
        return AnthropicClient.from_env()
    if provider == "openai":
        return OpenAIClient.from_env()
    raise ValueError(
        f"Unknown OPENCODE_LLM_PROVIDER={provider!r}. "
        "Valid values: none, ollama, anthropic, openai. "
        "(codex and claude-code are forbidden for KB build — use OPENCODE_QUERY_LLM_PROVIDER for chat)"
    )


def create_kb_query_llm_client() -> LLMClient | None:
    """Create the GPU-local LLM client for interactive KB queries (ask/search/graph handlers).

    Pinned to ollama qwen3-query:8b — never codex/cloud, never the enrich model.
    This is the third tier: ENRICH (build) → qwen3-enrich:1.7b, KB-QUERY (MCP ask) →
    qwen3-query:8b, CHAT (dashboard) → codex→haiku.

    If qwen3-query:8b is unavailable, falls back to create_llm_client() with a warning —
    degraded-but-GPU; never silently routes to cloud or CPU.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    model = os.environ.get("OPENCODE_KB_QUERY_LLM_MODEL", "qwen3-query:8b")
    timeout = int(os.environ.get("OPENCODE_KB_QUERY_LLM_TIMEOUT", "180"))
    num_ctx = 8192

    # 1. Try dedicated read instance (:11435)
    primary: LLMClient = OllamaClient(
        base_url=os.environ.get("OPENCODE_KB_QUERY_LLM_BASE_URL", "http://localhost:11435"),
        model=model,
        timeout=timeout,
        num_ctx=num_ctx,
    )
    if primary.is_available():
        return primary

    # 2. Fall back to the enrich instance (:11434) with the same query model.
    #    Keeps the correct model (qwen3-query:8b) even without the dedicated server.
    enrich_base = os.environ.get("OPENCODE_LLM_BASE_URL", "http://localhost:11434")
    _log.warning(
        "KB query model %r unavailable on :11435 — trying :11434 "
        "(interactive ask may be slow while a build is running)",
        model,
    )
    shared: LLMClient = OllamaClient(
        base_url=enrich_base,
        model=model,
        timeout=timeout,
        num_ctx=num_ctx,
    )
    if shared.is_available():
        return shared

    # 3. Last resort: enrich client with enrich model (degraded quality, still GPU)
    _log.warning(
        "KB query model %r unavailable on :11434 — falling back to enrich model (last resort)",
        model,
    )
    return create_llm_client()


def create_query_llm_client() -> LLMClient | None:
    """Create the LLM client for dashboard queries (higher quality than enrich tier).

    Reads OPENCODE_QUERY_LLM_* env vars. If the configured query model is not
    available (Ollama returns connection error or model not found), silently falls
    back to create_llm_client() so dashboards still work without qwen3-query:8b.

    Default: ollama + qwen3-query:8b (8192 ctx, ~50-80 t/s, ~5.5 GB VRAM).
    """
    from opencode_search.config import (
        DEFAULT_QUERY_LLM_MODEL,
        DEFAULT_QUERY_LLM_PROVIDER,
        DEFAULT_QUERY_LLM_TIMEOUT,
    )
    provider = os.environ.get("OPENCODE_QUERY_LLM_PROVIDER", DEFAULT_QUERY_LLM_PROVIDER).strip().lower()
    if provider == "none" or not provider:
        return create_llm_client()

    model = os.environ.get("OPENCODE_QUERY_LLM_MODEL", DEFAULT_QUERY_LLM_MODEL)
    timeout = int(os.environ.get("OPENCODE_QUERY_LLM_TIMEOUT", str(DEFAULT_QUERY_LLM_TIMEOUT)))
    num_ctx = int(os.environ.get("OPENCODE_QUERY_LLM_NUM_CTX", "8192"))

    if provider == "codex":
        primary: LLMClient = CodexClient(model=model, timeout=timeout)
        if not os.environ.get("OPENCODE_LLM_NO_FALLBACK") and shutil.which("claude"):
            fallback: LLMClient = ClaudeCodeClient(model=_CLAUDE_CODE_DEFAULT_MODEL, timeout=timeout)
            client: LLMClient = FallbackLLMClient(primary=primary, fallback=fallback)
        else:
            client = primary
        if not primary.is_available():
            return create_llm_client()
        return client
    elif provider == "ollama":
        client = OllamaClient(
            base_url=os.environ.get("OPENCODE_LLM_BASE_URL", "http://localhost:11434"),
            model=model,
            timeout=timeout,
            num_ctx=num_ctx,
        )
    elif provider == "anthropic":
        client = AnthropicClient.from_env()
    elif provider == "openai":
        client = OpenAIClient.from_env()
    elif provider == "claude-code":
        client = ClaudeCodeClient(model=model, timeout=timeout)
    else:
        return create_llm_client()

    if not client.is_available():
        return create_llm_client()
    return client


def create_map_llm_client() -> LLMClient | None:
    """Create the GPU-local LLM client for the MAP phase of global map-reduce synthesis.

    Pinned to the lightweight enrich model (qwen3-enrich:1.7b) on Ollama. MAP is a
    "summarize these community summaries with respect to the query" task — exactly
    what the enrich model already does during KB builds — so it runs ~4x faster and
    cooler than the 8B query model, while the quality-critical REDUCE stays on
    qwen3-query:8b. GPU-only, never cloud, never CPU.

    Returns None if the model is unreachable, so callers fall back to their main
    (8B) client and behave exactly as before the split.
    """
    model = os.environ.get("OPENCODE_MAP_LLM_MODEL", "qwen3-enrich:1.7b")
    timeout = int(os.environ.get("OPENCODE_MAP_LLM_TIMEOUT", "120"))
    base_url = os.environ.get(
        "OPENCODE_MAP_LLM_BASE_URL",
        os.environ.get("OPENCODE_LLM_BASE_URL", "http://localhost:11434"),
    )
    num_ctx = int(os.environ.get("OPENCODE_MAP_LLM_NUM_CTX", "8192"))
    client: LLMClient = OllamaClient(base_url=base_url, model=model, timeout=timeout, num_ctx=num_ctx)
    return client if client.is_available() else None
