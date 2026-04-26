# opencode-search-engine

## CRITICAL: GPU Enforcement Rule

**ALL operations that CAN run on GPU MUST be offloaded to GPU.** This is non-negotiable.

- FORBIDDEN to use CPU for GPU-capable workloads
- FORBIDDEN to hog CPU and memory — causes device crash, kernel panic, and lagging
- For Rust services: any GPU-accelerable computation (e.g., vector operations, similarity search) MUST use GPU when available
- Minimize CPU and memory footprint for all Rust and Python services
- CPU is acceptable ONLY for inherently CPU-bound tasks (file I/O, text parsing, tree-sitter)

Violating this rule risks device crash and kernel panic.
