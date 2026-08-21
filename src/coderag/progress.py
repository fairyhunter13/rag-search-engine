"""One counter, written where the hours go, readable without loading the GPU stack.

`index.State` reported which project the worker was on. That is job granularity:
on a 7,000-file repo it names one path and then says nothing for 28 minutes.
The number that answers "how long" lives inside `_write_files`, the only loop
that runs for hours.

It goes to a file because the readers are out of process. Each bake-off arm is
its own subprocess with its own `STATE_DIR`, and `import coderag.index` pulls in
onnxruntime -- so a reader routed through the indexer would pay a GPU-stack
import to print a percentage.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config


@dataclass
class Progress:
    project: str = ""
    phase: str = "idle"
    done: int = 0
    total: int = 0
    started_at: float | None = None  # None, not 0.0: a zero timestamp is a time
    updated_at: float = 0.0
    pid: int = field(default_factory=os.getpid)


_state = Progress()
_last_write = 0.0


def begin(project: Path | str, total: int, phase: str = "indexing") -> None:
    global _state
    now = time.time()
    _state = Progress(
        project=str(project), phase=phase, total=int(total), started_at=now, updated_at=now
    )
    _write(force=True)


def advance(n: int = 1) -> None:
    _state.done += n
    _state.updated_at = time.time()
    _write()


def phase(name: str) -> None:
    _state.phase = name
    _state.updated_at = time.time()
    _write(force=True)


def finish() -> None:
    _state.phase = "idle"
    _state.updated_at = time.time()
    _write(force=True)


def snapshot(state: Progress | None = None) -> dict:
    s = state or _state
    if s.started_at is None:
        return {}
    elapsed = max(s.updated_at - s.started_at, 0.0)
    out = {
        "phase": s.phase,
        "files_done": s.done,
        "files_total": s.total,
        "elapsed_s": round(elapsed, 1),
        "pid": s.pid,
        "updated_at": s.updated_at,
    }
    if s.total:
        out["percent"] = round(100.0 * s.done / s.total, 1)
    if s.done:
        out["eta_s"] = round(max(s.total - s.done, 0) * elapsed / s.done, 1)
        if elapsed > 0:
            out["files_per_s"] = round(s.done / elapsed, 3)
    return out


def read(path: Path | None = None) -> dict:
    """What is on disk. `phase` is only trustworthy next to `updated_at`.

    A process killed mid-index leaves its last line saying "indexing" forever;
    liveness is the reader's call, from `updated_at` and `pid`.
    """
    try:
        return json.loads((path or config.PROGRESS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(force: bool = False) -> None:
    global _last_write
    now = time.time()
    # Terminal states bypass the throttle: the last write is the one a reader
    # needs most, and throttling it away is how the eval harness lost five arms.
    if not force and now - _last_write < config.PROGRESS_WRITE_S:
        return
    _last_write = now
    path = config.PROGRESS_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"project": _state.project} | snapshot()), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # telemetry: a full disk must not fail the index it reports on
