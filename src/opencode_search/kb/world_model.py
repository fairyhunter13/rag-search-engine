"""Concept→Spec→Implementation→Test traceability model (HR30).

Parses §1a principles, §13b HRs, §14 test map and FEATURES.md; runs four chain
checks; returns a V&V report.  GPU-free parse path; optional GPU-embed for C4.
"""
from __future__ import annotations

import ast
import json
import re
import time
from pathlib import Path

_DOCTRINE_EXEMPT: frozenset[str] = frozenset({"HR19"})
_HR_REF_RE = re.compile(r'\bHR(\d+)(?:–HR(\d+))?')
_BACKTICK_RE = re.compile(r'`([^`]+)`')
_FILE_ANCHOR_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_/-]+\.[a-zA-Z]{2,6}$')
_TEST_ROW_RE = re.compile(r'^\| HR(\d+)\b[^|]*\| ([^|]+) \| ([^|]+) \|', re.MULTILINE)


def _expand_hr_refs(text: str) -> list[str]:
    refs: list[str] = []
    for m in _HR_REF_RE.finditer(text):
        lo, hi = int(m.group(1)), int(m.group(2)) if m.group(2) else int(m.group(1))
        refs.extend(f"HR{i}" for i in range(lo, hi + 1))
    return list(dict.fromkeys(refs))


def parse_principles(doc_text: str) -> list[dict]:
    sec_m = re.search(r'## 1a\..*?(?=\n## [0-9]|\Z)', doc_text, re.DOTALL)
    if not sec_m:
        return []
    sec = sec_m.group(0)
    first_num = re.search(r'\n1\. \*\*', sec)
    out = [{"pid": "P0", "hr_refs": _expand_hr_refs(sec[:first_num.start()] if first_num else sec)}]
    for block in re.split(r'\n(?=\d+\. \*\*)', sec):
        m = re.match(r'^(\d+)\. \*\*', block)
        if m:
            out.append({"pid": m.group(1), "hr_refs": _expand_hr_refs(block)})
    return out


def parse_hrs(doc_text: str) -> tuple[dict, dict]:
    hrs, cells = {}, {}
    for m in re.finditer(r'^\| \*\*HR(\d+)\*\* \| ([^\n]+)', doc_text, re.MULTILINE):
        hid, cell = f"HR{m.group(1)}", m.group(2).rstrip(" |")
        anchors = [m2.group(1).split()[0] for m2 in _BACKTICK_RE.finditer(cell)
                   if "/" in m2.group(1) and _FILE_ANCHOR_RE.match(m2.group(1).split()[0])]
        hrs[hid] = {"hid": hid, "anchors": anchors, "tests": [], "test_file": ""}
        cells[hid] = re.sub(r'\*\*|`[^`]+`|\([^)]*\)', ' ', cell).strip()[:300]
    return hrs, cells


def parse_test_map(doc_text: str) -> dict:
    result: dict = {}
    for m in _TEST_ROW_RE.finditer(doc_text):
        hid, tc, fc = f"HR{m.group(1)}", m.group(2), m.group(3).strip()
        names = _BACKTICK_RE.findall(tc) or ["<shorthand>"]
        if hid in result:
            result[hid][0].extend(names)
        else:
            result[hid] = [names, fc]
    return result


def parse_features(features_text: str) -> list[dict]:
    features, area = [], ""
    for line in features_text.splitlines():
        hm = re.match(r'^#{1,3} (.+)', line)
        if hm:
            area = hm.group(1).strip()
            continue
        cm = re.match(r'^\s*[-*] \[(x| )\] (.+)', line)
        if cm:
            features.append({"area": area, "text": cm.group(2).strip(),
                              "checked": cm.group(1) == "x",
                              "hr_refs": _expand_hr_refs(cm.group(2))})
    return features


def _collectable_tests(src_root: Path) -> set[str]:
    names: set[str] = set()
    for p in (src_root / "tests" / "live").glob("*.py"):
        try:
            tree = ast.parse(p.read_bytes())
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
    return names


def _c1(principles: list[dict], hrs: dict) -> dict:
    gaps: list[dict] = []
    cited: set[str] = set()
    for p in principles:
        cited.update(p["hr_refs"])
        for hid in p["hr_refs"]:
            if hid not in hrs:
                gaps.append({"kind": "principle_dangling_hr", "ref": hid,
                             "detail": f"Principle {p['pid']} cites {hid} absent from §13b"})
    orphans = [hid for hid in hrs if hid not in cited and hid not in _DOCTRINE_EXEMPT]
    for hid in orphans:
        gaps.append({"kind": "hr_doctrine_orphan", "ref": hid,
                     "detail": f"{hid} not cited by any §1a principle"})
    n = len(hrs)
    return {"passed": not gaps, "gaps": gaps,
            "coverage_pct": round(100 * (n - len(orphans)) / n, 1) if n else 100.0}


def _c2(hrs: dict, collectable: set[str]) -> dict:
    gaps: list[dict] = []
    for hid, hr in hrs.items():
        if not hr["tests"]:
            gaps.append({"kind": "hr_untested", "ref": hid,
                         "detail": f"{hid} has no row in §14 test coverage map"})
            continue
        for t in hr["tests"]:
            if t == "<shorthand>":
                continue
            for part in t.split("."):
                if part and part not in collectable:
                    msg = f"{hid} §14 names '{t}' but '{part}' not in src/tests/live/"
                    gaps.append({"kind": "phantom_test_ref", "ref": t, "detail": msg})
                    break
    n = len(hrs)
    untested = sum(1 for g in gaps if g["kind"] == "hr_untested")
    return {"passed": not gaps, "gaps": gaps,
            "coverage_pct": round(100 * (n - untested) / n, 1) if n else 100.0}


def _c3(hrs: dict, src_root: Path) -> dict:
    ose_root = src_root / "opencode_search"
    gaps: list[dict] = []
    total = dead = 0
    for hid, hr in hrs.items():
        for anchor in hr["anchors"]:
            total += 1
            if not ((ose_root / anchor).exists() or (src_root / anchor).exists()):
                dead += 1
                gaps.append({"kind": "dead_code_anchor", "ref": anchor,
                             "detail": f"{hid} anchor '{anchor}' not found under src/"})
    return {"passed": not gaps, "gaps": gaps,
            "coverage_pct": round(100 * (total - dead) / total, 1) if total else 100.0}


def _embed_map(feature_text: str, hr_cells: dict) -> str | None:
    try:
        from opencode_search.kb.resolve_rerank import rerank_candidates
        hids = list(hr_cells.keys())
        best, _ = rerank_candidates(feature_text[:300],
                                    [f"{h}: {hr_cells[h]}" for h in hids], margin=0.10)
        if best:
            hid = best.split(":")[0].strip()
            return hid if hid in hr_cells else None
        return None
    except Exception:
        return None


def _c4(features: list[dict], hrs: dict, hr_cells: dict) -> dict:
    gaps: list[dict] = []
    mapped = total = 0
    for f in features:
        if not f["checked"]:
            gaps.append({"kind": "feature_undelivered", "ref": f["text"][:60],
                         "detail": f"[ ] feature in '{f['area']}' not yet shipped"})
            continue
        total += 1
        if any(h in hrs for h in f["hr_refs"]) or _embed_map(f["text"], hr_cells):
            mapped += 1
        else:
            gaps.append({"kind": "feature_unmapped", "ref": f["text"][:60],
                         "detail": f"[x] feature in '{f['area']}' has no HR ref; embed abstained"})
    return {"passed": True, "gaps": gaps, "mapped": mapped, "total_checked": total,
            "feature_pct": round(100 * mapped / total, 1) if total else 100.0}


def _stamp_path() -> Path:
    from opencode_search.core.config import REGISTRY_PATH
    return REGISTRY_PATH.parent / "world_model_stamp.json"


def read_stamp() -> dict:
    p = _stamp_path()
    if not p.exists():
        return {"status": "unvalidated"}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"status": "unreadable"}


def write_stamp(suite_result: str) -> None:
    import subprocess
    commit = ""
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        commit = r.stdout.strip()
    except Exception:
        pass
    _stamp_path().write_text(json.dumps(
        {"validated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "suite_result": suite_result, "commit": commit}
    ))


def world_model_report(project_root: str | Path = ".") -> dict:
    """Parse the four doc layers, run C1–C4, return a V&V report."""
    root = Path(project_root).resolve()
    docs = root / "docs" / "architecture"
    doc1 = (docs / "federation-and-search-engine.md").read_text()
    doc2 = (docs / "federation-ops-and-invariants.md").read_text()
    feat = (root / "FEATURES.md").read_text()

    principles = parse_principles(doc1)
    hrs_raw, hr_cells = parse_hrs(doc2)
    test_map = parse_test_map(doc2)
    hrs = {hid: {**hr, "tests": test_map.get(hid, [[], ""])[0],
                  "test_file": test_map.get(hid, [[], ""])[1]}
           for hid, hr in hrs_raw.items()}

    src_root = root / "src"
    collectable = _collectable_tests(src_root)
    features = parse_features(feat)
    c1 = _c1(principles, hrs)
    c2 = _c2(hrs, collectable)
    c3 = _c3(hrs, src_root)
    c4 = _c4(features, hrs, hr_cells)
    hard_gaps = c1["gaps"] + c2["gaps"] + c3["gaps"]
    structural: dict = {}
    try:
        from opencode_search.index.validate import validate_index
        sv = validate_index(str(root))
        structural = {"verdict": sv.get("verdict")}
        if sv.get("verdict") != "VALID":
            hard_gaps.append({"kind": "structural_invalid", "ref": "validate",
                              "detail": f"structural verdict={sv.get('verdict')}"})
    except Exception as exc:
        structural = {"error": str(exc)}

    return {
        "verdict": "GAPS" if hard_gaps else "VALID",
        "coverage": {"concept_spec": c1["coverage_pct"], "spec_test": c2["coverage_pct"],
                     "spec_impl": c3["coverage_pct"], "feature_pct": c4["feature_pct"]},
        "gaps": {"c1_concept_spec": c1["gaps"], "c2_spec_test": c2["gaps"],
                 "c3_spec_impl": c3["gaps"], "c4_feature_spec": c4["gaps"]},
        "counts": {"principles": len(principles), "hrs": len(hrs),
                   "features_checked": c4["total_checked"], "features_mapped": c4["mapped"]},
        "structural_validate": structural,
        "validation": read_stamp(),
    }
