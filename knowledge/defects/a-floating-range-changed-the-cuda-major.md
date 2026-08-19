---
type: Defect
resource: pyproject.toml, src/coderag/gpu.py
title: A floating range on onnxruntime-gpu changed the CUDA major version
description: "A resolver bump from onnxruntime-gpu 1.26 to 1.29 changed the linked CUDA major from 12 to 13 against a cu12 wheel set; every GPU test failed on a missing libcublasLt.so.13 and the session fell back to CPU."
tags: [gpu, cuda, dependencies, pinning]
status: stable
generated: { by: claude/opus-5, at: 2026-08-19T10:15:00Z }
---

# What happened

`tests/test_embed_gpu.py` failed 15/15 with `libcublasLt.so.13: cannot open shared object file`.
The manifest said `onnxruntime-gpu>=1.26`; the installed runtime was **1.29.0**, and somewhere in
that range ORT's default build changed its CUDA major from 12 to 13. The environment still carried
the `nvidia-*-cu12` wheels. `ort.preload_dlls()` was working correctly — the libraries it put on
the loader path were simply the wrong major version.

The session fell back to `CPUExecutionProvider`. `verify_session` refused it, which is the only
reason this is a defect report and not a fleet index that ran on CPU with nothing reporting it.

# The general shape

**A floating version range on a native-extension package can silently change the version of a
system library it links against.** No API changed, no import failed, and the resolver did exactly
what it was told. The failure surfaced two layers away, as a provider string.

This is the precise class of failure the GPU-only invariant exists to catch, and it is the argument
for the fourth assertion specifically: the first three all passed. The EP ladder was non-empty, a
GPU was present, and the device string was clean. Only re-reading `session.get_providers()[0]`
*after* the session loaded saw anything wrong.

# The fix, in order

1. Pin `onnxruntime-gpu==1.29.*`, with a comment saying why. The pin is the fix; everything below
   is just moving to where the pin points.
2. Install the CUDA 13 wheel set. The packages **dropped the `-cu12` suffix at 13**, so it is
   `nvidia-cublas` 13.6.1.10, `nvidia-cuda-runtime` 13.3.29, `nvidia-cudnn-cu13` 9.24.0.43, plus
   curand and cufft. `nvidia-cudnn` unsuffixed is a decoy package that exists only to warn you.
3. Drop the cu12 set, so both cannot be on the loader path at once.
4. Re-run the GPU tests and require **15/15**, not a provider string. The provider string is what
   was wrong; asserting on it is asserting on the symptom.

Going forward rather than pinning ORT back, because the driver on this box (595.84) supports CUDA
13. No source change was needed.
