#!/usr/bin/env python3
"""Auto-configure local LLM services for opencode-search.

Sets up TWO Ollama instances (model partition — VRAM-neutral):
  :11434 (enrich)  — qwen3-enrich:1.7b, MAX_LOADED_MODELS=1, KB building only
  :11435 (read)    — qwen3-query:8b,    MAX_LOADED_MODELS=1, MCP ask + KB chat

Partitioning prevents head-of-line blocking: reads on :11435 are never queued
behind background enrichment on :11434. Total resident VRAM (~7.6 GB) stays
neutral vs the previous single-server MAX_LOADED_MODELS=2 setup.

Also installs an ollama-models.service systemd oneshot that ensures both models
are present on every boot (survives reboots, re-creates custom modelfiles).

Usage:
  python scripts/setup_llm_services.py           # interactive
  python scripts/setup_llm_services.py --dry-run # preview only, no changes
  python scripts/setup_llm_services.py --force   # re-pull base models even if present
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent.resolve()
REPO_ROOT = SCRIPTS_DIR.parent
MODELFILES_DIR = SCRIPTS_DIR / "modelfiles"

ENRICH_MODELFILE = MODELFILES_DIR / "qwen3-enrich.modelfile"
QUERY_MODELFILE = MODELFILES_DIR / "qwen3-query.modelfile"

ENRICH_BASE = "qwen3:1.7b"
QUERY_BASE = "qwen3:8b"
ENRICH_MODEL = "qwen3-enrich:1.7b"
QUERY_MODEL = "qwen3-query:8b"

SYSTEMD_SERVICE_PATH = Path("/etc/systemd/system/ollama-models.service")
OLLAMA_READ_SERVICE_PATH = Path("/etc/systemd/system/ollama-read.service")

QUERY_MODELFILE_CONTENT = """\
FROM qwen3:8b
PARAMETER num_ctx 8192
PARAMETER num_predict 2048
PARAMETER temperature 0.1
PARAMETER top_p 0.9
SYSTEM "You are a senior software architect. Answer questions about codebases factually and completely based on the provided context. When asked to list features or functionalities, be exhaustive, structured, and include code file references. Never fabricate code, files, or functionality not present in the context."
"""

SYSTEMD_SERVICE_TEMPLATE = """\
[Unit]
Description=Auto-pull and create Ollama models for opencode-search
After=ollama.service ollama-read.service
Requires=ollama.service ollama-read.service

[Service]
Type=oneshot
User={user}
ExecStart=/bin/bash -c '\
    until curl -sf http://localhost:11434/ > /dev/null 2>&1; do sleep 1; done; \
    /usr/local/bin/ollama pull {enrich_base}; \
    /usr/local/bin/ollama create {enrich_model} -f {enrich_modelfile}; \
    until curl -sf http://localhost:11435/ > /dev/null 2>&1; do sleep 1; done; \
    OLLAMA_HOST=127.0.0.1:11435 /usr/local/bin/ollama pull {query_base}; \
    OLLAMA_HOST=127.0.0.1:11435 /usr/local/bin/ollama create {query_model} -f {query_modelfile}'
RemainAfterExit=yes
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""

# Dedicated read Ollama instance on :11435 — holds only qwen3-query:8b.
# MAX_LOADED_MODELS=1: keeps qwen3-query resident; evicts nothing (only model).
# KEEP_ALIVE=-1: never evict the query model between requests.
# GPU-only: CUDA_VISIBLE_DEVICES inherits from the environment.
OLLAMA_READ_SERVICE_TEMPLATE = """\
[Unit]
Description=Ollama read instance for opencode-search (qwen3-query:8b, port 11435)
After=network-online.target ollama.service
Wants=network-online.target
PartOf=ollama.service

[Service]
Type=simple
User={user}
Environment="OLLAMA_HOST=127.0.0.1:11435"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_MODELS={ollama_models_dir}"
ExecStart=/usr/local/bin/ollama serve
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def _run(cmd: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    kwargs: dict = {"check": check}
    if capture:
        kwargs["capture_output"] = True
        kwargs["text"] = True
    return subprocess.run(cmd, **kwargs)  # noqa: S603


def _ollama_available() -> bool:
    return shutil.which("ollama") is not None


def _ollama_running() -> bool:
    try:
        import urllib.request
        urllib.request.urlopen("http://localhost:11434/", timeout=2)  # noqa: S310
        return True
    except Exception:
        return False


def _model_present(name: str) -> bool:
    try:
        r = _run(["ollama", "list"], capture=True, check=False)
        return name.split(":")[0] in r.stdout
    except Exception:
        return False


def _print_table(rows: list[tuple]) -> None:
    if not rows:
        return
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up LLM services for opencode-search")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without executing")
    parser.add_argument("--force", action="store_true", help="Re-pull base models even if present")
    args = parser.parse_args()
    dry = args.dry_run

    print("\nopencode-search LLM Service Setup")
    print("=" * 42)
    if dry:
        print("  [DRY RUN — no changes will be made]\n")

    # ── Check ollama ────────────────────────────────────────────────────────
    if not _ollama_available():
        print("ERROR: ollama not found in PATH. Install from https://ollama.com first.")
        return 1

    if not _ollama_running():
        print("ERROR: Ollama is not running. Start it with: sudo systemctl start ollama")
        return 1

    print("  ✓ Ollama is running\n")

    # ── Ensure modelfiles dir ───────────────────────────────────────────────
    if not dry:
        MODELFILES_DIR.mkdir(parents=True, exist_ok=True)

    # ── Write qwen3-query modelfile ─────────────────────────────────────────
    print(f"  Modelfile: {QUERY_MODELFILE}")
    if dry:
        print(f"  [DRY RUN] Would write:\n{textwrap.indent(QUERY_MODELFILE_CONTENT, '    ')}")
    else:
        QUERY_MODELFILE.write_text(QUERY_MODELFILE_CONTENT)
        print("  ✓ Written")

    # ── Pull base models ────────────────────────────────────────────────────
    models = [
        (ENRICH_BASE, ENRICH_MODEL, ENRICH_MODELFILE, "2.9 GB", "~270 t/s", "KB enrichment"),
        (QUERY_BASE, QUERY_MODEL, QUERY_MODELFILE, "~5.5 GB", "~50-80 t/s", "MCP ask + Dashboard chat"),
    ]

    print()
    for base, name, modelfile, vram, speed, role in models:
        present = _model_present(base) and not args.force
        status = "already present" if present else "will pull"
        print(f"  {name:<25} {vram:<10} {speed:<15} {role}  [{status}]")

    print()
    for base, name, modelfile, vram, speed, role in models:
        present = _model_present(base) and not args.force
        if not present:
            print(f"  Pulling {base}…")
            if not dry:
                _run(["ollama", "pull", base])
                print(f"  ✓ {base} pulled")
            else:
                print(f"  [DRY RUN] Would run: ollama pull {base}")

        if modelfile.exists() or not dry:
            print(f"  Creating {name} from {modelfile.name}…")
            if not dry:
                _run(["ollama", "create", name, "-f", str(modelfile)])
                print(f"  ✓ {name} created")
            else:
                print(f"  [DRY RUN] Would run: ollama create {name} -f {modelfile}")

    # ── Ensure OLLAMA_MAX_LOADED_MODELS=1 on :11434 (enrich-only) ─────────────
    # Now that qwen3-query:8b lives exclusively on :11435, :11434 only needs to
    # hold qwen3-enrich:1.7b. MAX_LOADED_MODELS=1 prevents inadvertent loading of
    # the query model on the enrich server and frees ~0.4 GB headroom.
    memory_dropin = Path("/etc/systemd/system/ollama.service.d/memory-limits.conf")
    print(f"\n  Ollama :11434 drop-in: {memory_dropin}")
    if dry:
        print("  [DRY RUN] Would set OLLAMA_MAX_LOADED_MODELS=1 in memory-limits.conf (enrich-only)")
    elif memory_dropin.exists():
        text = memory_dropin.read_text()
        if "OLLAMA_MAX_LOADED_MODELS=2" in text:
            try:
                updated = text.replace(
                    'Environment="OLLAMA_MAX_LOADED_MODELS=2"',
                    'Environment="OLLAMA_MAX_LOADED_MODELS=1"',
                )
                import subprocess as _sp
                _sp.run(["sudo", "tee", str(memory_dropin)], input=updated.encode(), check=True, capture_output=True)
                _sp.run(["sudo", "systemctl", "daemon-reload"], check=True)
                _sp.run(["sudo", "systemctl", "restart", "ollama"], check=True)
                print("  ✓ Updated to OLLAMA_MAX_LOADED_MODELS=1 (enrich-only) and restarted ollama")
            except Exception as exc:
                print(f"  WARN: Could not update drop-in automatically: {exc}")
                print("  Run manually: sudo sed -i 's/MAX_LOADED_MODELS=2/MAX_LOADED_MODELS=1/' "
                      f"{memory_dropin} && sudo systemctl daemon-reload && sudo systemctl restart ollama")
        elif "OLLAMA_MAX_LOADED_MODELS=1" in text:
            print("  ✓ Already set to OLLAMA_MAX_LOADED_MODELS=1 (enrich-only)")
        else:
            print("  INFO: OLLAMA_MAX_LOADED_MODELS not found in drop-in — verify manually")
    else:
        print("  INFO: Drop-in not found — ollama will load models on demand")

    # ── Install :11435 read Ollama service (ollama-read.service) ──────────────
    ollama_models_dir = os.environ.get("OLLAMA_MODELS", os.path.expanduser("~/.ollama/models"))
    current_user = os.environ.get("USER", os.environ.get("LOGNAME", "user"))
    read_service_content = OLLAMA_READ_SERVICE_TEMPLATE.format(
        user=current_user,
        ollama_models_dir=ollama_models_dir,
    )
    print(f"\n  Read Ollama service (:11435): {OLLAMA_READ_SERVICE_PATH}")
    if dry:
        print(f"  [DRY RUN] Would write:\n{textwrap.indent(read_service_content, '    ')}")
    else:
        try:
            OLLAMA_READ_SERVICE_PATH.write_text(read_service_content)
            _run(["sudo", "systemctl", "daemon-reload"])
            _run(["sudo", "systemctl", "enable", "--now", "ollama-read.service"])
            print("  ✓ ollama-read.service installed, enabled and started")
        except PermissionError:
            print("  WARN: No sudo access — write service file manually:")
            print(f"\n--- {OLLAMA_READ_SERVICE_PATH} ---")
            print(read_service_content)
            print("---")
            print("  Then run: sudo systemctl daemon-reload && "
                  "sudo systemctl enable --now ollama-read.service")

    # ── Systemd models oneshot ───────────────────────────────────────────────
    current_user = os.environ.get("USER", os.environ.get("LOGNAME", "user"))
    service_content = SYSTEMD_SERVICE_TEMPLATE.format(
        user=current_user,
        enrich_base=ENRICH_BASE,
        enrich_model=ENRICH_MODEL,
        enrich_modelfile=ENRICH_MODELFILE,
        query_base=QUERY_BASE,
        query_model=QUERY_MODEL,
        query_modelfile=QUERY_MODELFILE,
    )

    print(f"\n  Systemd service: {SYSTEMD_SERVICE_PATH}")
    if dry:
        print(f"  [DRY RUN] Would write:\n{textwrap.indent(service_content, '    ')}")
        print("  [DRY RUN] Would run: sudo systemctl daemon-reload")
        print("  [DRY RUN] Would run: sudo systemctl enable ollama-models.service")
    else:
        try:
            SYSTEMD_SERVICE_PATH.write_text(service_content)
            _run(["sudo", "systemctl", "daemon-reload"])
            _run(["sudo", "systemctl", "enable", "ollama-models.service"])
            print("  ✓ Service installed and enabled")
        except PermissionError:
            print("  WARN: No sudo access — write service file manually:")
            print(f"\n--- {SYSTEMD_SERVICE_PATH} ---")
            print(service_content)
            print("---")
            print("  Then run: sudo systemctl daemon-reload && sudo systemctl enable ollama-models.service")

    # ── Summary ─────────────────────────────────────────────────────────────
    print("\n  Summary")
    print("  " + "-" * 38)
    rows = [
        ("Instance", "Model", "VRAM", "Role", "Config Var"),
        (":11434 (enrich)", ENRICH_MODEL, "2.9 GB", "KB build", "OPENCODE_LLM_BASE_URL"),
        (":11435 (read)",   QUERY_MODEL, "~5.5 GB", "MCP ask + KB chat", "OPENCODE_KB_QUERY_LLM_BASE_URL"),
        ("", "Total", "~7.6 GB", "Partition (neutral VRAM)", "MAX_LOADED_MODELS=1 each"),
    ]
    _print_table(rows)
    print()
    print("  To verify: ollama list")
    print("  To test:   curl -s http://localhost:11434/api/tags | python -m json.tool")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
