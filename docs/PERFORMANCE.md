# Performance & Optimization — state, findings, and the v2 path

Last researched: **June 2026** (RTX 5080 Laptop / Blackwell, MSI Vector 16 HX AI A2XWIG).

This documents what is already optimal, what was ruled out (with evidence), and the
genuinely-faster-without-quality-loss levers that require a deliberate v2 migration.

## Current state (already well-optimized)

| Layer | Choice | Notes |
|---|---|---|
| Embeddings | `jinaai/jina-embeddings-v2-base-code` (768d, ONNX/CUDA) | code-specific, ~0.64 GB |
| Reranker | `jinaai/jina-reranker-v1-turbo-en` | distilled, ~0.15 GB |
| Vectors | **float16** + LanceDB **IVF_PQ** + refine | ~49% smaller than f32; total index **~898 MB** |
| KB enrich / MAP | `qwen3-enrich:1.7b` (Q4_K_M) | model-tier MAP split (commit f1f5681) |
| Query / REDUCE | `qwen3-query:8b` (Q4_K_M) | quality-critical final synthesis |
| Inference engine | **ollama 0.30.7** (llama.cpp, GPU-only) | upgraded from 0.24.0 (neutral on speed, newer/safer) |

Global synthesis latency: **276s → ~65s** after the MAP fan-out cap + model-tier MAP
(1.7b MAP, 8b REDUCE). See [[project-global-synthesis-perf]].

## Findings (June 2026 research, verified against this machine)

- **Storage is NOT a bottleneck** — 898 MB total. RaBitQ (LanceDB GA 2026) and
  Matryoshka+int8/binary quant give ~78% reductions but only matter at the 10B-vector
  tier. **Leave storage alone.**
- **Speculative decoding is NOT in ollama** (verified: ollama issue #5800 still open,
  #9216 closed-as-dup; no `OLLAMA_SPECULATIVE_DECODE`/`--model-draft` in any release,
  latest 0.30.7). Web summaries claiming "Ollama 5.x supports it" are wrong.
- **Fan / Cooler Boost is firmware-locked** — the `msi-ec` driver refuses firmware
  `15M3EMS1.109` ("Firmware version is not supported"). Forcing it / raw `ec_sys` EC
  pokes risk erratic thermal behavior. **Not safely automatable** on this laptop.
- **Thermal throttling is the real laptop limiter** — GPU power-capped at 80 W (of
  175 W max), still hits 84 °C → the daemon's 80 °C guard pauses inference in 15 s
  steps. The compute isn't the wall; the cooling is.

## v2 status (June 2026): BLOCKED on this GPU — wait for upstream

The two real wins (NVFP4, speculative decoding) require replacing ollama with
vLLM/TensorRT-LLM. **As of June 2026 this is not viable on this specific GPU** —
verified against the vLLM issue tracker:

- This GPU is **sm_120 (consumer Blackwell)**; vLLM's NVFP4 kernels target **sm_100
  (datacenter B200)**. sm_120 is NOT a superset of sm_100.
- Open vLLM bugs: #33416 (NVFP4 MoE kernels miss SM120 in capability check),
  #30707 (SM120 NVFP4 pre-flight memory check fails), **#38718 (NVFP4 produces
  garbage output on SM120)**, #29030 (undefined symbol on 5080), #31085 (open
  feature req for native SM120 NVFP4). Track #31085/#33416 — migrate when they close.
- Precompiled vLLM does not support sm_120 → must build from source (CUDA 12.8 +
  torch 2.6 + `FLASHINFER_CUDA_ARCH_LIST=12.0f`). Fragile, hours of work, high
  failure odds today.
- vLLM's advantage is **batched throughput**, not the **single-stream latency** the
  daemon needs — so even working, it may not beat ollama for one-query-at-a-time.
- The NVFP4 model exists (RedHatAI/Qwen3-8B-NVFP4); the *runtime* is the blocker.

**Hardware "unlock" is also firmware-locked:** `nvidia-smi -pl` returns "not supported
for GPU" (power management locked by MSI vBIOS); the GPU auto-boosts via Dynamic Boost
to its thermal ceiling already. No user knob — only physical cooling lets it boost higher.

**Conclusion:** the current ollama (0.30.7, Q4_K_M, model-tier MAP) setup is the
pragmatic optimum for this hardware right now. Revisit vLLM+NVFP4 when consumer-Blackwell
(sm_120) support matures upstream. The two wins, once unblocked:

1. **NVFP4 on Blackwell** (biggest prize). The RTX 5080 has native FP4 tensor cores;
   ollama/llama.cpp Q4_K_M uses *software* dequant and never touches them. Serving the
   models as **NVFP4 via vLLM or TensorRT-LLM** (e.g. `nvidia/*-NVFP4`) uses the tensor
   cores natively: ~27% faster than FP16, half the memory of INT8, and *better* quality
   than INT4/Q4. Effort: high (new server + NVFP4 model conversion + rewrite the
   `OllamaClient` path). Reward: faster **and** higher quality.
2. **Speculative decoding** for REDUCE. `qwen3:0.6b` draft → `qwen3-query:8b` target =
   ~1.9× faster generation, byte-identical output. Available via **llama.cpp
   `llama-server --model-draft`** or vLLM — not ollama. Effort: medium-high (run
   llama-server alongside/instead of ollama for the query tier).

**Recommended sequencing:** stand up vLLM (or TensorRT-LLM) for the query tier with an
NVFP4 8B model behind a feature flag → benchmark vs the ollama path → if it wins on
speed *and* quality, migrate REDUCE; keep ollama for enrich/MAP (1.7b is already cheap).
Add speculative decoding only if you stay on llama.cpp instead of vLLM.

## Cooling — the on-top multiplier (physical, the user's to apply)

Software can't fix the throttle; cooling can. In rough ROI order:
- **Cooling pad** — zero risk, zero software, instant headroom.
- **MSI Center "Cooler Boost" / the Fn fan key** (in-OS), since `msi-ec` can't on Linux
  for this firmware. (Revisit `msi-ec` when MSI adds `15M3EMS1.109` support upstream.)
- **Power-limit A/B** (`sudo nvidia-smi -pl 70` vs 80): on a cooling-capped laptop a
  *lower* cap can run *faster sustained* by avoiding throttle pauses — worth measuring.

## What NOT to do
- Don't optimize storage (already minimal).
- Don't force `msi-ec` on unsupported firmware (hardware risk).
- Don't raise the daemon's 80 °C thermal guard far (it's crash-prevention; the hardware
  throttle ~84-87 °C is the only backstop).
- Don't chase lower model quants — already at the sensible Q4 floor.
