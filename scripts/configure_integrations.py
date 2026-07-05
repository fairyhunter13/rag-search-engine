#!/usr/bin/env python3
"""Configure all system integrations for rag-search MCP tools.

Writes/verifies system prompt blocks and MCP entries across the config trees.
Adapts to whatever tool ecosystem is present (claude profiles, opencode, hermes).
(Codex support removed.)

Usage:
    .venv/bin/python scripts/configure_integrations.py           # configure + verify
    .venv/bin/python scripts/configure_integrations.py --check   # verify only (no writes)
    .venv/bin/python scripts/configure_integrations.py --apply-all  # repair all drift
    .venv/bin/python scripts/configure_integrations.py --apply-all --dry-run  # preview
    .venv/bin/python scripts/configure_integrations.py --json    # JSON output

Exit codes: 0 = all configured, 1 = some missing/drifted
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# Add scripts/ to path so we can import from integrations/
sys.path.insert(0, str(Path(__file__).parent))
from integrations.canonical import (
    CANONICAL_MCP_URL,
    SENTINEL_AGENTS_END,
    SENTINEL_AGENTS_START,
    SENTINEL_CLAUDE_END,
    SENTINEL_CLAUDE_START,
)

_REPO = Path(__file__).parent.parent


@dataclass
class ConfigResult:
    tool: str
    status: str  # "configured" | "already_ok" | "missing" | "skipped" | "error" | "warning"
    message: str
    path: str = ""
    diff: str = ""


# ---------------------------------------------------------------------------
# System prompt repair helpers
# ---------------------------------------------------------------------------

def _replace_sentinel_block(text: str, start: str, end: str, new_body: str) -> str:
    """Replace everything between start/end sentinels (inclusive) with new block.

    If sentinels not found, appends the full block at the end.
    """
    si = text.find(start)
    ei = text.find(end)
    if si == -1 or ei == -1 or ei < si:
        return text.rstrip() + "\n\n" + start + "\n" + new_body + "\n" + end + "\n"
    return text[:si] + start + "\n" + new_body + "\n" + end + text[ei + len(end):]


def _verify_sentinel_block(text: str, start: str, end: str, expected_body: str) -> bool:
    """Return True if the sentinel block body exactly matches expected_body."""
    si = text.find(start)
    ei = text.find(end)
    if si == -1 or ei == -1 or ei < si:
        return False
    actual_body = text[si + len(start):ei].strip("\n")
    return actual_body.strip() == expected_body.strip()


def _diff(old: str, new: str, label: str) -> str:
    lines = list(difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"{label} (old)",
        tofile=f"{label} (new)",
        n=2,
    ))
    return "".join(lines)


# ---------------------------------------------------------------------------
# CLAUDE.md targets (3 profiles)
# ---------------------------------------------------------------------------

def _verify_claude_md(md_path: Path) -> ConfigResult:
    label = f"claude({md_path.parent.name})/CLAUDE.md"
    if not md_path.exists():
        return ConfigResult(tool=label, status="missing",
                            message=f"CLAUDE.md not found at {md_path}", path=str(md_path))
    text = md_path.read_text()
    from integrations.canonical import CANONICAL_BODY
    ose_ok = _verify_sentinel_block(text, SENTINEL_CLAUDE_START, SENTINEL_CLAUDE_END, CANONICAL_BODY)
    if ose_ok:
        return ConfigResult(tool=label, status="already_ok",
                            message=f"System prompt in sync: {md_path}", path=str(md_path))
    return ConfigResult(tool=label, status="missing",
                        message=f"System prompt drifted or missing: {md_path}", path=str(md_path))


def _repair_claude_md(md_path: Path, dry_run: bool = False) -> ConfigResult:
    label = f"claude({md_path.parent.name})/CLAUDE.md"
    from integrations.canonical import CANONICAL_BODY
    old_text = md_path.read_text() if md_path.exists() else ""
    new_text = _replace_sentinel_block(old_text, SENTINEL_CLAUDE_START, SENTINEL_CLAUDE_END, CANONICAL_BODY)
    if old_text == new_text:
        return ConfigResult(tool=label, status="already_ok",
                            message=f"Already in sync: {md_path}", path=str(md_path))
    diff = _diff(old_text, new_text, str(md_path))
    if dry_run:
        return ConfigResult(tool=label, status="configured",
                            message=f"[DRY-RUN] Would update {md_path}", path=str(md_path), diff=diff)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(new_text)
    return ConfigResult(tool=label, status="configured",
                        message=f"Updated system prompt: {md_path}", path=str(md_path), diff=diff)


# ---------------------------------------------------------------------------
# AGENTS.md targets (opencode-default, opencode-personal)
# ---------------------------------------------------------------------------

def _verify_agents_md(md_path: Path, label: str) -> ConfigResult:
    if not md_path.exists():
        return ConfigResult(tool=label, status="missing",
                            message=f"AGENTS.md not found at {md_path}", path=str(md_path))
    text = md_path.read_text()
    from integrations.canonical import CANONICAL_BODY
    ose_ok = _verify_sentinel_block(text, SENTINEL_AGENTS_START, SENTINEL_AGENTS_END, CANONICAL_BODY)
    if ose_ok:
        return ConfigResult(tool=label, status="already_ok",
                            message=f"System prompt in sync: {md_path}", path=str(md_path))
    return ConfigResult(tool=label, status="missing",
                        message=f"System prompt drifted or missing: {md_path}", path=str(md_path))


def _repair_agents_md(md_path: Path, label: str, dry_run: bool = False) -> ConfigResult:
    from integrations.canonical import CANONICAL_BODY
    old_text = md_path.read_text() if md_path.exists() else ""
    new_text = _replace_sentinel_block(old_text, SENTINEL_AGENTS_START, SENTINEL_AGENTS_END, CANONICAL_BODY)
    if old_text == new_text:
        return ConfigResult(tool=label, status="already_ok",
                            message=f"Already in sync: {md_path}", path=str(md_path))
    diff = _diff(old_text, new_text, str(md_path))
    if dry_run:
        return ConfigResult(tool=label, status="configured",
                            message=f"[DRY-RUN] Would update {md_path}", path=str(md_path), diff=diff)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(new_text)
    return ConfigResult(tool=label, status="configured",
                        message=f"Updated system prompt: {md_path}", path=str(md_path), diff=diff)


# ---------------------------------------------------------------------------
# MCP entry repair: claude settings.json (3 profiles)
# ---------------------------------------------------------------------------

_EXPECTED_MCP_ENTRY = {"type": "http", "url": CANONICAL_MCP_URL}


def _verify_settings_json(settings_path: Path) -> ConfigResult:
    label = f"claude({settings_path.parent.name})/settings.json"
    if not settings_path.exists():
        return ConfigResult(tool=label, status="missing",
                            message=f"settings.json not found: {settings_path}", path=str(settings_path))
    try:
        data = json.loads(settings_path.read_text())
    except Exception as exc:
        return ConfigResult(tool=label, status="error",
                            message=f"Failed to parse {settings_path}: {exc}", path=str(settings_path))
    entry = data.get("mcpServers", {}).get("rag-search", {})
    if not entry:
        return ConfigResult(tool=label, status="missing",
                            message=f"mcpServers.rag-search missing in {settings_path}", path=str(settings_path))
    if entry.get("type") == "http" and entry.get("url") == CANONICAL_MCP_URL:
        return ConfigResult(tool=label, status="already_ok",
                            message=f"MCP entry in sync: {settings_path}", path=str(settings_path))
    return ConfigResult(tool=label, status="missing",
                        message=f"MCP entry drifted (not HTTP) in {settings_path}", path=str(settings_path))


def _repair_settings_json(settings_path: Path, dry_run: bool = False) -> ConfigResult:
    label = f"claude({settings_path.parent.name})/settings.json"
    try:
        old_text = settings_path.read_text() if settings_path.exists() else "{}"
        data = json.loads(old_text)
    except Exception as exc:
        return ConfigResult(tool=label, status="error",
                            message=f"Failed to parse {settings_path}: {exc}", path=str(settings_path))
    data.setdefault("mcpServers", {})["rag-search"] = _EXPECTED_MCP_ENTRY.copy()
    new_text = json.dumps(data, indent=2) + "\n"
    if old_text.strip() == new_text.strip():
        return ConfigResult(tool=label, status="already_ok",
                            message=f"Already in sync: {settings_path}", path=str(settings_path))
    diff = _diff(old_text, new_text, str(settings_path))
    if dry_run:
        return ConfigResult(tool=label, status="configured",
                            message=f"[DRY-RUN] Would update {settings_path}", path=str(settings_path), diff=diff)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(new_text)
    return ConfigResult(tool=label, status="configured",
                        message=f"Updated MCP entry: {settings_path}", path=str(settings_path), diff=diff)


# (Codex MCP-client integration removed — rag-search no longer configures codex.)


# ---------------------------------------------------------------------------
# MCP entry repair: hermes config.yaml
# ---------------------------------------------------------------------------

def _build_hermes_config_text() -> str:
    """Build the complete canonical hermes config.yaml (MCP entry + agent system prompt)."""
    from integrations.canonical import CANONICAL_BODY
    # YAML literal block scalars must not contain backticks/arrows/em-dashes
    safe_body = (CANONICAL_BODY
                 .replace("`", "'")
                 .replace("→", "->")
                 .replace("—", "--"))
    wrapped = f"{SENTINEL_AGENTS_START}\n{safe_body}\n{SENTINEL_AGENTS_END}"
    indented = "\n".join("    " + ln for ln in wrapped.splitlines())
    return (
        "mcp_servers:\n"
        "  rag-search:\n"
        "    enabled: true\n"
        f"    url: {CANONICAL_MCP_URL}\n"
        "agent:\n"
        "  system_prompt: |\n"
        + indented + "\n"
    )


def _verify_hermes_yaml(config_path: Path) -> ConfigResult:
    label = "hermes/config.yaml"
    if not config_path.exists():
        return ConfigResult(tool=label, status="skipped",
                            message="hermes not installed (no config.yaml)", path=str(config_path))
    text = config_path.read_text()
    if "rag-search" not in text:
        return ConfigResult(tool=label, status="missing",
                            message="rag-search MCP not in hermes config.yaml", path=str(config_path))
    if CANONICAL_MCP_URL not in text:
        return ConfigResult(tool=label, status="missing",
                            message="hermes config.yaml missing HTTP URL for rag-search", path=str(config_path))
    return ConfigResult(tool=label, status="already_ok",
                        message="hermes MCP entry in sync", path=str(config_path))


def _repair_hermes_yaml(config_path: Path, dry_run: bool = False) -> ConfigResult:
    """Rewrite the entire hermes config.yaml with the canonical MCP entry + agent system prompt."""
    label = "hermes/config.yaml"
    if not config_path.exists():
        return ConfigResult(tool=label, status="skipped",
                            message="hermes not installed", path=str(config_path))
    old_text = config_path.read_text(encoding="utf-8")
    new_text = _build_hermes_config_text()
    if old_text.strip() == new_text.strip():
        return ConfigResult(tool=label, status="already_ok",
                            message="hermes config.yaml already in sync", path=str(config_path))
    diff = _diff(old_text, new_text, str(config_path))
    if dry_run:
        return ConfigResult(tool=label, status="configured",
                            message=f"[DRY-RUN] Would update {config_path}", path=str(config_path), diff=diff)
    config_path.write_text(new_text, encoding="utf-8")
    return ConfigResult(tool=label, status="configured",
                        message=f"Updated hermes config.yaml: {config_path}", path=str(config_path), diff=diff)


# ---------------------------------------------------------------------------
# hermes agent.system_prompt in config.yaml (replaces SYSTEM_PROMPT.md)
# ---------------------------------------------------------------------------

def _verify_hermes_agent_prompt(config_path: Path) -> ConfigResult:
    """Verify that agent.system_prompt in hermes config.yaml contains the canonical body."""
    label = "hermes/agent_system_prompt"
    if not config_path.parent.exists():
        return ConfigResult(tool=label, status="skipped",
                            message="hermes not installed", path=str(config_path))
    if not config_path.exists():
        return ConfigResult(tool=label, status="missing",
                            message=f"hermes config.yaml not found: {config_path}", path=str(config_path))
    text = config_path.read_text()
    # In the YAML literal block scalar, sentinel lines are indented 4 spaces
    indented_start = "    " + SENTINEL_AGENTS_START
    indented_end = "    " + SENTINEL_AGENTS_END
    if ("agent:" not in text or indented_start not in text
            or indented_end not in text):
        return ConfigResult(tool=label, status="missing",
                            message="hermes agent.system_prompt missing or no sentinel block", path=str(config_path))
    return ConfigResult(tool=label, status="already_ok",
                        message="hermes agent system prompt in sync", path=str(config_path))


def _repair_hermes_agent_prompt(config_path: Path, dry_run: bool = False) -> ConfigResult:
    """Repair hermes agent.system_prompt — rewrites the full config.yaml canonically."""
    label = "hermes/agent_system_prompt"
    if not config_path.parent.exists():
        return ConfigResult(tool=label, status="skipped",
                            message="hermes not installed", path=str(config_path))
    # Delegate to full hermes config rewrite (idempotent — same target file)
    result = _repair_hermes_yaml(config_path, dry_run=dry_run)
    return ConfigResult(tool=label, status=result.status,
                        message=result.message.replace("hermes config.yaml", "hermes agent_system_prompt"),
                        path=result.path, diff=result.diff)


# ---------------------------------------------------------------------------
# MCP entry repair: opencode jsonc (2 profiles)
# ---------------------------------------------------------------------------

def _parse_jsonc(text: str) -> dict:
    """Parse JSONC by stripping line comments that are outside string literals."""
    # Only strip // that appears outside double-quoted strings.
    # Strategy: remove // ... until end-of-line, but not when inside a string.
    cleaned_lines = []
    for line in text.splitlines():
        # Find // that is NOT inside a string by counting un-escaped quotes
        in_string = False
        result = []
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == '"' and (i == 0 or line[i - 1] != "\\"):
                in_string = not in_string
                result.append(ch)
            elif ch == "/" and not in_string and i + 1 < len(line) and line[i + 1] == "/":
                break  # rest of line is a comment
            else:
                result.append(ch)
            i += 1
        cleaned_lines.append("".join(result))
    return json.loads("\n".join(cleaned_lines))


def _verify_opencode_jsonc(config_path: Path, label: str) -> ConfigResult:
    if not config_path.exists():
        return ConfigResult(tool=label, status="missing",
                            message=f"opencode config not found: {config_path}", path=str(config_path))
    try:
        text = config_path.read_text()
        data = _parse_jsonc(text)
    except Exception as exc:
        return ConfigResult(tool=label, status="error",
                            message=f"Failed to parse {config_path}: {exc}", path=str(config_path))
    entry = data.get("mcp", {}).get("rag-search", {})
    if not entry:
        return ConfigResult(tool=label, status="missing",
                            message=f"mcp.rag-search missing in {config_path}", path=str(config_path))
    if entry.get("type") == "remote" and entry.get("url") == CANONICAL_MCP_URL:
        return ConfigResult(tool=label, status="already_ok",
                            message=f"opencode MCP entry in sync: {config_path}", path=str(config_path))
    return ConfigResult(tool=label, status="missing",
                        message=f"mcp entry not HTTP remote in {config_path}", path=str(config_path))


def _repair_opencode_jsonc(config_path: Path, label: str, dry_run: bool = False) -> ConfigResult:
    old_text = config_path.read_text() if config_path.exists() else "{}"
    try:
        data = _parse_jsonc(old_text)
    except Exception as exc:
        return ConfigResult(tool=label, status="error",
                            message=f"Failed to parse {config_path}: {exc}", path=str(config_path))
    new_entry: dict = {"type": "remote", "url": CANONICAL_MCP_URL}
    data.setdefault("mcp", {})["rag-search"] = new_entry
    new_text = json.dumps(data, indent=2) + "\n"
    if old_text.strip() == new_text.strip():
        return ConfigResult(tool=label, status="already_ok",
                            message=f"Already in sync: {config_path}", path=str(config_path))
    diff = _diff(old_text, new_text, str(config_path))
    if dry_run:
        return ConfigResult(tool=label, status="configured",
                            message=f"[DRY-RUN] Would update {config_path}", path=str(config_path), diff=diff)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(new_text)
    return ConfigResult(tool=label, status="configured",
                        message=f"Updated opencode MCP entry: {config_path}", path=str(config_path), diff=diff)


# ---------------------------------------------------------------------------
# bash_aliases sentinel block
# ---------------------------------------------------------------------------

_BASH_ALIASES_SENTINEL_START = "# [rag-search-aliases:start]"
_BASH_ALIASES_SENTINEL_END   = "# [rag-search-aliases:end]"
_BASH_ALIASES_COMMENT = """\
# rag-search shell helpers (managed by configure_integrations.py):
#   ocs            — rag-search CLI entry point
#   ocs-index PATH — index + build KB (entity enrichment + wiki)
#   ocs-dash       — open the search-engine dashboard"""


def verify_bash_aliases() -> ConfigResult:
    aliases_path = Path.home() / ".bash_aliases"
    if not aliases_path.exists():
        return ConfigResult(tool="bash_aliases", status="missing",
                            message="~/.bash_aliases not found", path=str(aliases_path))
    text = aliases_path.read_text()
    has_sentinel = _BASH_ALIASES_SENTINEL_START in text
    has_ocs = "rag_search" in text
    if has_sentinel and has_ocs:
        return ConfigResult(tool="bash_aliases", status="already_ok",
                            message="rag-search aliases with sentinel in ~/.bash_aliases",
                            path=str(aliases_path))
    if has_ocs and not has_sentinel:
        return ConfigResult(tool="bash_aliases", status="missing",
                            message="ocs aliases exist but sentinel block missing (run --apply-all)",
                            path=str(aliases_path))
    return ConfigResult(tool="bash_aliases", status="missing",
                        message="rag-search aliases missing from ~/.bash_aliases",
                        path=str(aliases_path))


def repair_bash_aliases(dry_run: bool = False) -> ConfigResult:
    aliases_path = Path.home() / ".bash_aliases"
    if not aliases_path.exists():
        return ConfigResult(tool="bash_aliases", status="missing",
                            message="~/.bash_aliases not found — cannot repair",
                            path=str(aliases_path))
    old_text = aliases_path.read_text()

    if _BASH_ALIASES_SENTINEL_START in old_text and _BASH_ALIASES_SENTINEL_END in old_text:
        # Update comment inside existing sentinel block only
        new_text = _replace_sentinel_block(
            old_text, _BASH_ALIASES_SENTINEL_START, _BASH_ALIASES_SENTINEL_END, _BASH_ALIASES_COMMENT
        )
    elif "rag_search" in old_text:
        # Find the ocs section header and insert sentinel block before it
        target = "# ── rag-search engine aliases ──"
        if target in old_text:
            new_text = old_text.replace(
                target,
                f"{_BASH_ALIASES_SENTINEL_START}\n{_BASH_ALIASES_COMMENT}\n{_BASH_ALIASES_SENTINEL_END}\n{target}",
                1,
            )
        else:
            # Fallback: insert before first ocs alias
            import re
            new_text = re.sub(
                r"(alias ocs=)",
                f"{_BASH_ALIASES_SENTINEL_START}\n{_BASH_ALIASES_COMMENT}\n{_BASH_ALIASES_SENTINEL_END}\n\\1",
                old_text,
                count=1,
            )
    else:
        return ConfigResult(tool="bash_aliases", status="skipped",
                            message="No ocs aliases found — cannot insert sentinel",
                            path=str(aliases_path))

    if old_text == new_text:
        return ConfigResult(tool="bash_aliases", status="already_ok",
                            message="Already in sync: ~/.bash_aliases", path=str(aliases_path))
    diff = _diff(old_text, new_text, "~/.bash_aliases")
    if dry_run:
        return ConfigResult(tool="bash_aliases", status="configured",
                            message="[DRY-RUN] Would update ~/.bash_aliases",
                            path=str(aliases_path), diff=diff)
    aliases_path.write_text(new_text)
    return ConfigResult(tool="bash_aliases", status="configured",
                        message="Updated sentinel block in ~/.bash_aliases",
                        path=str(aliases_path), diff=diff)



# ---------------------------------------------------------------------------
# Run functions
# ---------------------------------------------------------------------------

_H = Path.home()

def _build_targets() -> tuple[list[tuple[str, Path, str]], list[tuple[str, Path, str]]]:
    """Build targets dynamically — adapts to the directories present on this machine.

    Extra profiles can be added via OSE_INTEGRATION_EXTRA_PROFILES (colon-separated
    CLAUDE.md paths). Run with OSE_INTEGRATION_EXTRA_PROFILES=~/.custom/CLAUDE.md to
    include additional profiles.
    """
    sys_t: list[tuple[str, Path, str]] = [
        ("claude", _H / ".claude" / "CLAUDE.md", "claude(main)/CLAUDE.md"),
    ]
    mcp_t: list[tuple[str, Path, str]] = [
        ("settings", _H / ".claude" / "settings.json", "claude(main)/settings.json"),
    ]
    for idx in range(1, 10):
        d = _H / f".claude-account{idx}"
        if d.exists():
            lbl = f"claude(account{idx})"
            sys_t.append(("claude", d / "CLAUDE.md", f"{lbl}/CLAUDE.md"))
            mcp_t.append(("settings", d / "settings.json", f"{lbl}/settings.json"))
    oc_default = _H / ".config" / "opencode"
    if oc_default.exists():
        sys_t.append(("agents", oc_default / "AGENTS.md", "opencode-default/AGENTS.md"))
        mcp_t.append(("opencode", oc_default / "opencode.jsonc", "opencode-default/opencode.jsonc"))
    cfg = _H / ".config"
    if cfg.exists():
        for d in sorted(cfg.iterdir()):
            if d.is_dir() and d.name.startswith("opencode-") and d.name != "opencode":
                sys_t.append(("agents", d / "opencode" / "AGENTS.md", f"{d.name}/AGENTS.md"))
                mcp_t.append(("opencode", d / "opencode" / "opencode.jsonc", f"{d.name}/opencode.jsonc"))
    hermes = _H / ".hermes" / "config.yaml"
    if hermes.parent.exists():
        sys_t.append(("hermes_agent", hermes, "hermes/agent_system_prompt"))
        mcp_t.append(("hermes", hermes, "hermes/config.yaml"))
    for raw in os.environ.get("OSE_INTEGRATION_EXTRA_PROFILES", "").split(":"):
        p = raw.strip()
        if p:
            path = Path(p).expanduser()
            if path.parent.exists():
                lbl = str(path.relative_to(_H)) if path.is_relative_to(_H) else p
                sys_t.append(("claude", path, lbl))
                mcp_t.append(("settings", path.parent / "settings.json", f"{lbl[:-10]}/settings.json"))
    return sys_t, mcp_t


_SYSTEM_PROMPT_TARGETS, _MCP_TARGETS = _build_targets()

def verify_all() -> list[ConfigResult]:
    results: list[ConfigResult] = []
    for kind, path, label in _SYSTEM_PROMPT_TARGETS:
        if kind == "claude":
            results.append(_verify_claude_md(path))
        elif kind == "agents":
            results.append(_verify_agents_md(path, label))
        elif kind == "hermes_agent":
            results.append(_verify_hermes_agent_prompt(path))
    for kind, path, label in _MCP_TARGETS:
        if kind == "settings":
            results.append(_verify_settings_json(path))
        elif kind == "hermes":
            results.append(_verify_hermes_yaml(path))
        elif kind == "opencode":
            results.append(_verify_opencode_jsonc(path, label))
    results.append(verify_bash_aliases())
    return results


def repair_all(dry_run: bool = False) -> list[ConfigResult]:
    results: list[ConfigResult] = []
    for kind, path, label in _SYSTEM_PROMPT_TARGETS:
        if kind == "claude":
            results.append(_repair_claude_md(path, dry_run=dry_run))
        elif kind == "agents":
            results.append(_repair_agents_md(path, label, dry_run=dry_run))
        elif kind == "hermes_agent":
            results.append(_repair_hermes_agent_prompt(path, dry_run=dry_run))
    for kind, path, label in _MCP_TARGETS:
        if kind == "settings":
            results.append(_repair_settings_json(path, dry_run=dry_run))
        elif kind == "hermes":
            results.append(_repair_hermes_yaml(path, dry_run=dry_run))
        elif kind == "opencode":
            results.append(_repair_opencode_jsonc(path, label, dry_run=dry_run))
    results.append(repair_bash_aliases(dry_run=dry_run))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure rag-search integrations")
    parser.add_argument("--check", action="store_true",
                        help="Verify only (no writes); exit 1 if any drift detected")
    parser.add_argument("--apply-all", action="store_true",
                        help="Repair all drifted config trees")
    parser.add_argument("--dry-run", action="store_true",
                        help="With --apply-all: show what would change without writing")
    parser.add_argument("--json", dest="json_out", action="store_true",
                        help="Output results as JSON")
    args = parser.parse_args()

    if args.apply_all:
        results = repair_all(dry_run=args.dry_run)
    else:
        results = verify_all()

    if args.json_out:
        import dataclasses
        print(json.dumps([dataclasses.asdict(r) for r in results], indent=2))
    else:
        print(f"\n{'='*70}")
        title = "Integration Sync" if args.apply_all else "Integration Verification"
        print(f"  {title}")
        print(f"{'='*70}")
        icons = {
            "configured": "✅", "already_ok": "✅",
            "missing": "🔴", "skipped": "⏭️",
            "error": "❌", "warning": "⚠️",
        }
        for r in results:
            icon = icons.get(r.status, "?")
            print(f"  {icon} [{r.status:<12}] {r.tool:<42} {r.message[:40]}")
            if args.dry_run and r.diff:
                print("     --- diff preview ---")
                for line in r.diff.splitlines()[:10]:
                    print(f"     {line}")
        print(f"{'='*70}")
        ok_count = sum(1 for r in results if r.status in ("configured", "already_ok", "skipped"))
        drifted = [r for r in results if r.status in ("missing", "error")]
        print(f"  {ok_count}/{len(results)} targets ok, {len(drifted)} need attention\n")
        if drifted:
            print("  Run --apply-all to repair all drifted targets.")

    drifted = [r for r in results if r.status in ("missing", "error")]
    return 1 if drifted else 0


if __name__ == "__main__":
    sys.exit(main())
