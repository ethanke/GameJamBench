"""Validator registry — each validator returns:

    {kind: str, status: "PASS"|"FAIL"|"SKIP", detail: str, duration_ms: int,
     artifacts: dict[str, str] (optional)}

Real implementations are wired in as we go. Order:
    compile      [DONE]
    editor_open  [DONE]
    spec_test    [DONE]
    functional_test, pie_recording, insights_trace, screenshot_diff, llm_judge
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import time
from typing import Any, Callable

# ---- helpers ----------------------------------------------------------------

FATAL_PATTERNS = [
    re.compile(r"\bFatal error\b", re.IGNORECASE),
    re.compile(r"LogWindows: Error: appError"),
    re.compile(r"Assertion failed:"),
    re.compile(r"\[FATAL\]"),
]


def _ue_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("GJB_UE_ROOT", r"C:\Program Files\Epic Games\UE_5.7"))


def _editor_cmd() -> pathlib.Path:
    return _ue_root() / "Engine" / "Binaries" / "Win64" / "UnrealEditor-Cmd.exe"


def _project_uproject(project_dir: pathlib.Path) -> pathlib.Path | None:
    matches = list(project_dir.glob("*.uproject"))
    return matches[0] if matches else None


def _scan_log_for_fatals(log_text: str) -> list[str]:
    hits: list[str] = []
    for line in log_text.splitlines():
        for pat in FATAL_PATTERNS:
            if pat.search(line):
                hits.append(line.strip())
                break
    return hits


def _stub(kind: str) -> Callable:
    def _impl(vdef: dict[str, Any], project_dir: pathlib.Path, run) -> dict[str, Any]:
        return {
            "kind": kind, "status": "SKIP",
            "detail": "v0 stub",
            "duration_ms": 0,
        }
    return _impl


# ---- compile ----------------------------------------------------------------

def compile_validator(vdef: dict[str, Any], project_dir: pathlib.Path, run) -> dict[str, Any]:
    """Run UnrealBuildTool against the workdir's .uproject and report result."""
    t0 = time.time()
    target = vdef.get("target", "Editor")

    uproject = _project_uproject(project_dir)
    if uproject is None:
        return {"kind": "compile", "status": "FAIL",
                "detail": f"no .uproject found in {project_dir}",
                "duration_ms": int((time.time() - t0) * 1000)}

    project_name = uproject.stem
    target_full = f"{project_name}{target}"
    ue_root = _ue_root()
    build_bat = ue_root / "Engine" / "Build" / "BatchFiles" / "Build.bat"

    if not build_bat.exists():
        return {"kind": "compile", "status": "SKIP",
                "detail": f"UBT not found at {build_bat} (set GJB_UE_ROOT)",
                "duration_ms": int((time.time() - t0) * 1000)}

    if not (project_dir / "Source").exists():
        return {"kind": "compile", "status": "PASS",
                "detail": "content-only project (no Source/ folder)",
                "duration_ms": int((time.time() - t0) * 1000)}

    log_path = run.artifacts_dir / "logs" / f"compile_{target}.log"
    cmd = [str(build_bat), target_full, "Win64", "Development",
           f"-Project={uproject}", "-WaitMutex", "-NoHotReloadFromIDE"]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, shell=False, timeout=1800)
    except subprocess.TimeoutExpired as exc:
        log_path.write_text(
            f"TIMEOUT after {exc.timeout}s\nSTDOUT:\n{exc.stdout or ''}\nSTDERR:\n{exc.stderr or ''}",
            encoding="utf-8")
        return {"kind": "compile", "status": "FAIL",
                "detail": "UBT timed out (>30 min)",
                "duration_ms": int((time.time() - t0) * 1000),
                "artifacts": {"log": str(log_path)}}

    log_path.write_text(
        f"CMD: {' '.join(cmd)}\nEXIT: {proc.returncode}\n\nSTDOUT:\n{proc.stdout}\n\nSTDERR:\n{proc.stderr}",
        encoding="utf-8")

    return {"kind": "compile",
            "status": "PASS" if proc.returncode == 0 else "FAIL",
            "detail": f"target={target_full} exit={proc.returncode}",
            "duration_ms": int((time.time() - t0) * 1000),
            "artifacts": {"log": str(log_path)}}


# ---- editor_open ------------------------------------------------------------

def editor_open_validator(vdef: dict[str, Any], project_dir: pathlib.Path, run) -> dict[str, Any]:
    """Launch UnrealEditor-Cmd against the project, scan log for fatals + assertions.

    vdef supports:
        timeout_sec: int (default 600)
        assert_log_contains: list[str] — every entry must appear in the log
        assert_log_absent:   list[str] — no entry may appear in the log

    UE auto-runs Content/Python/init_unreal.py in the project on editor open
    when PythonScriptPlugin is enabled, so an agent-authored probe script
    naturally fires here.
    """
    t0 = time.time()

    uproject = _project_uproject(project_dir)
    if uproject is None:
        return {"kind": "editor_open", "status": "FAIL",
                "detail": "no .uproject", "duration_ms": int((time.time() - t0) * 1000)}

    editor_cmd = _editor_cmd()
    if not editor_cmd.exists():
        return {"kind": "editor_open", "status": "SKIP",
                "detail": f"editor not found at {editor_cmd}",
                "duration_ms": int((time.time() - t0) * 1000)}

    log_path = run.artifacts_dir / "logs" / "editor_open.log"

    # Use the `pythonscript` commandlet — loads engine + project + Python plugin.
    # `init_unreal.py` is editor-mode-only; under the commandlet we explicitly
    # exec it if it exists. Forward-slash paths inside the inline script avoid
    # escape issues; the path is wrapped in repr() so spaces are quoted.
    init_py = project_dir / "Content" / "Python" / "init_unreal.py"
    if init_py.exists():
        init_path_for_python = str(init_py).replace("\\", "/")
        inline = (
            f"import sys; "
            f"print('GJB_EDITOR_OPEN_OK'); "
            f"exec(open({init_path_for_python!r}, 'r', encoding='utf-8').read())"
        )
    else:
        inline = "import sys; print('GJB_EDITOR_OPEN_OK')"

    cmd = [
        str(editor_cmd), str(uproject),
        "-run=pythonscript",
        f"-script={inline}",
        "-unattended", "-nullrhi", "-nosplash", "-nopause", "-NoLogTimes",
    ]

    timeout = int(vdef.get("timeout_sec", 600))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, shell=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") + "\n--- STDERR ---\n" + (exc.stderr or "")
        log_path.write_text(partial, encoding="utf-8")
        return {"kind": "editor_open", "status": "FAIL",
                "detail": f"editor timed out after {timeout}s",
                "duration_ms": int((time.time() - t0) * 1000),
                "artifacts": {"log": str(log_path)}}

    log_text = (proc.stdout or "") + "\n--- STDERR ---\n" + (proc.stderr or "")
    log_path.write_text(
        f"CMD: {' '.join(cmd)}\nEXIT: {proc.returncode}\n\n{log_text}",
        encoding="utf-8")
    fatals = _scan_log_for_fatals(log_text)

    if proc.returncode != 0 or fatals:
        return {"kind": "editor_open", "status": "FAIL",
                "detail": f"exit={proc.returncode} fatals={len(fatals)}: {fatals[:3]}",
                "duration_ms": int((time.time() - t0) * 1000),
                "artifacts": {"log": str(log_path)}}

    # Optional log assertions.
    missing = [s for s in vdef.get("assert_log_contains", []) if s not in log_text]
    forbidden = [s for s in vdef.get("assert_log_absent", []) if s in log_text]
    if missing or forbidden:
        return {"kind": "editor_open", "status": "FAIL",
                "detail": f"log assertions failed: missing={missing} forbidden={forbidden}",
                "duration_ms": int((time.time() - t0) * 1000),
                "artifacts": {"log": str(log_path)}}

    detail = "editor opened cleanly (exit=0, no fatals)"
    if vdef.get("assert_log_contains"):
        detail += f"; required strings present: {vdef['assert_log_contains']}"
    return {"kind": "editor_open", "status": "PASS",
            "detail": detail,
            "duration_ms": int((time.time() - t0) * 1000),
            "artifacts": {"log": str(log_path)}}


# ---- spec_test --------------------------------------------------------------

def spec_test_validator(vdef: dict[str, Any], project_dir: pathlib.Path, run) -> dict[str, Any]:
    """Run UE Automation framework Spec/Functional tests via -ExecCmds Automation RunTests.

    vdef:
        filter: glob-style filter passed to `Automation RunTests` (required)
        require_min: minimum number of tests that must execute (default 1; 0 = allow empty)
        timeout_sec: hard wall (default 900)

    The validator parses the AutomationReport JSON the editor writes to:
        Saved/Automation/AutomationReports/<run>/index.json
    """
    t0 = time.time()
    filter_str = vdef.get("filter")
    if not filter_str:
        return {"kind": "spec_test", "status": "SKIP",
                "detail": "no filter specified",
                "duration_ms": int((time.time() - t0) * 1000)}

    uproject = _project_uproject(project_dir)
    if uproject is None:
        return {"kind": "spec_test", "status": "FAIL",
                "detail": "no .uproject", "duration_ms": int((time.time() - t0) * 1000)}

    editor_cmd = _editor_cmd()
    if not editor_cmd.exists():
        return {"kind": "spec_test", "status": "SKIP",
                "detail": f"editor not found at {editor_cmd}",
                "duration_ms": int((time.time() - t0) * 1000)}

    log_path = run.artifacts_dir / "logs" / "spec_test.log"
    report_dir = run.artifacts_dir / "validator" / "spec_test_report"
    report_dir.mkdir(parents=True, exist_ok=True)

    exec_cmds = (
        f"Automation RunTests {filter_str}; "
        "Quit"
    )
    cmd = [
        str(editor_cmd), str(uproject),
        "-unattended", "-nullrhi", "-nosplash", "-nopause",
        "-stdout", "-NoLogTimes",
        f"-abslog={log_path}",
        "-ReportOutputPath=" + str(report_dir),
        "-ExecCmds=" + exec_cmds,
        "-TestExit=Automation Test Queue Empty",
    ]

    timeout = int(vdef.get("timeout_sec", 900))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, shell=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"kind": "spec_test", "status": "FAIL",
                "detail": f"timed out after {timeout}s",
                "duration_ms": int((time.time() - t0) * 1000),
                "artifacts": {"log": str(log_path)}}

    # Parse the report. UE writes index.json with the run summary.
    index = report_dir / "index.json"
    if not index.exists():
        # Some configs write to <reports>/<timestamp>/index.json; pick newest.
        candidates = list(report_dir.rglob("index.json"))
        if candidates:
            index = max(candidates, key=lambda p: p.stat().st_mtime)

    if not index.exists():
        log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        return {"kind": "spec_test", "status": "FAIL",
                "detail": f"no automation report at {report_dir} (exit={proc.returncode})",
                "duration_ms": int((time.time() - t0) * 1000),
                "artifacts": {"log": str(log_path)},
                "log_tail": log_text[-4000:]}

    try:
        report = json.loads(index.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"kind": "spec_test", "status": "FAIL",
                "detail": f"automation report unparseable: {exc}",
                "duration_ms": int((time.time() - t0) * 1000),
                "artifacts": {"log": str(log_path), "report": str(index)}}

    tests = report.get("tests") or report.get("Tests") or []
    total = len(tests)
    passed = sum(1 for t in tests if (t.get("state") or t.get("State")) in ("Success", "success", "Passed"))
    failed = total - passed
    require_min = int(vdef.get("require_min", 1))

    if total < require_min:
        return {"kind": "spec_test", "status": "FAIL",
                "detail": f"only {total} tests ran, need >={require_min} (filter={filter_str})",
                "duration_ms": int((time.time() - t0) * 1000),
                "artifacts": {"log": str(log_path), "report": str(index)}}

    if failed > 0:
        sample_fails = [t.get("fullTestPath") or t.get("FullTestPath") or t.get("TestDisplayName")
                        for t in tests
                        if (t.get("state") or t.get("State")) not in ("Success", "success", "Passed")][:3]
        return {"kind": "spec_test", "status": "FAIL",
                "detail": f"{failed}/{total} failed; sample={sample_fails}",
                "duration_ms": int((time.time() - t0) * 1000),
                "artifacts": {"log": str(log_path), "report": str(index)}}

    return {"kind": "spec_test", "status": "PASS",
            "detail": f"{passed}/{total} passed (filter={filter_str})",
            "duration_ms": int((time.time() - t0) * 1000),
            "artifacts": {"log": str(log_path), "report": str(index)}}


# ---- registry ---------------------------------------------------------------

REGISTRY: dict[str, Callable] = {
    "compile":         compile_validator,
    "editor_open":     editor_open_validator,
    "spec_test":       spec_test_validator,
    "functional_test": _stub("functional_test"),
    "pie_recording":   _stub("pie_recording"),
    "insights_trace":  _stub("insights_trace"),
    "screenshot_diff": _stub("screenshot_diff"),
    "llm_judge":       _stub("llm_judge"),
}
