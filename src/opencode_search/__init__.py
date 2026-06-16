"""opencode-search — GPU-accelerated code intelligence MCP server."""
# Cap CPU thread usage at package import time — all inference runs on CUDA EP.
# setdefault keeps env overridable; passive = sleep-wait instead of spin-wait.
import os

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OMP_WAIT_POLICY", "passive")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
__version__ = "0.3.0"
