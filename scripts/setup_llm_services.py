"""REMOVED: local generative LLM decommissioned.

The engine no longer uses a local generative LLM. GPU is for embeddings + reranking only
(FastEmbed/ONNX/CUDA). KB build uses cloud DeepSeek; dashboard chat uses claude-haiku-4-5
with DeepSeek fallback. Run `git rm scripts/setup_llm_services.py` to fully remove this file.
"""
raise SystemExit("setup_llm_services.py has been removed — local generative LLM is decommissioned.")
