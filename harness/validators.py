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
        # UE writes a UTF-8 BOM on Windows; utf-8-sig strips it transparently.
        report = json.loads(index.read_text(encoding="utf-8-sig"))
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

# ---- pie_recording ----------------------------------------------------------

def pie_recording_validator(vdef: dict[str, Any], project_dir: pathlib.Path, run) -> dict[str, Any]:
    """Run an agent-authored simulation script under UE for a bounded duration,
    capture stdout, apply log assertions.

    v0 implementation: pythonscript commandlet executes the agent's script.
    The agent uses unreal.* APIs to load levels, instantiate actors, run
    simulation logic, and emit log_warning / log_error markers that the
    validator asserts on. The engine is loaded with project + plugins but
    does not enter true PIE/game tick — that requires a C++-bearing
    template + game module (Phase E).

    vdef:
        agent_script:   relative path under project_dir (default Content/Python/pie_simulate.py)
        duration_sec:   intent for how long the script should run (informational; the
                        script controls its own timing)
        timeout_sec:    hard wall on the subprocess (default duration_sec + 120)
        assert_log_contains: list[str]
        assert_log_absent:   list[str]
    """
    t0 = time.time()
    duration_sec = int(vdef.get("duration_sec", 15))
    timeout_sec = int(vdef.get("timeout_sec", duration_sec + 120))
    script_rel = vdef.get("agent_script", "Content/Python/pie_simulate.py")

    uproject = _project_uproject(project_dir)
    if uproject is None:
        return {"kind": "pie_recording", "status": "FAIL",
                "detail": "no .uproject", "duration_ms": int((time.time() - t0) * 1000)}

    editor_cmd = _editor_cmd()
    if not editor_cmd.exists():
        return {"kind": "pie_recording", "status": "SKIP",
                "detail": f"editor not found at {editor_cmd}",
                "duration_ms": int((time.time() - t0) * 1000)}

    agent_script = (project_dir / script_rel).resolve()
    if not agent_script.exists():
        return {"kind": "pie_recording", "status": "FAIL",
                "detail": f"agent script not found: {script_rel}",
                "duration_ms": int((time.time() - t0) * 1000)}

    log_path = run.artifacts_dir / "logs" / "pie_recording.log"
    script_for_python = str(agent_script).replace("\\", "/")

    # The pythonscript commandlet's -script= argument is parsed as a single
    # line of Python — control-flow blocks (try/except, for, def) don't fit
    # there. Instead we write a wrapper to the run's artifacts dir and exec it.
    wrapper_path = run.artifacts_dir / "validator" / "pie_wrapper.py"
    wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    wrapper_path.write_text(
        "import time, traceback\n"
        "import unreal\n"
        "unreal.log_warning('GJB_PIE_BEGIN')\n"
        "_t0 = time.time()\n"
        "_ok = False\n"
        "try:\n"
        f"    exec(open({script_for_python!r}, 'r', encoding='utf-8').read())\n"
        "    _ok = True\n"
        "except SystemExit:\n"
        "    _ok = True\n"
        "except Exception:\n"
        "    unreal.log_error('GJB_PIE_EXCEPTION: ' + traceback.format_exc())\n"
        "finally:\n"
        "    unreal.log_warning(f'GJB_PIE_END elapsed={time.time()-_t0:.2f}s ok={_ok}')\n",
        encoding="utf-8")
    wrapper_for_python = str(wrapper_path).replace("\\", "/")

    inline = f"exec(open({wrapper_for_python!r}, 'r', encoding='utf-8').read())"

    cmd = [
        str(editor_cmd), str(uproject),
        "-run=pythonscript",
        f"-script={inline}",
        "-unattended", "-nullrhi", "-nosplash", "-nopause", "-NoLogTimes",
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, shell=False, timeout=timeout_sec)
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") + "\n--- STDERR ---\n" + (exc.stderr or "")
        log_path.write_text(partial, encoding="utf-8")
        return {"kind": "pie_recording", "status": "FAIL",
                "detail": f"hard timeout after {timeout_sec}s",
                "duration_ms": int((time.time() - t0) * 1000),
                "artifacts": {"log": str(log_path)}}

    log_text = (proc.stdout or "") + "\n--- STDERR ---\n" + (proc.stderr or "")
    log_path.write_text(
        f"CMD: {' '.join(cmd[:6])} ...\nEXIT: {proc.returncode}\n\n{log_text}",
        encoding="utf-8")

    fatals = _scan_log_for_fatals(log_text)
    if proc.returncode != 0 or fatals:
        return {"kind": "pie_recording", "status": "FAIL",
                "detail": f"exit={proc.returncode} fatals={len(fatals)}: {fatals[:3]}",
                "duration_ms": int((time.time() - t0) * 1000),
                "artifacts": {"log": str(log_path)}}

    if "GJB_PIE_EXCEPTION" in log_text:
        return {"kind": "pie_recording", "status": "FAIL",
                "detail": "agent script raised an exception (see log GJB_PIE_EXCEPTION)",
                "duration_ms": int((time.time() - t0) * 1000),
                "artifacts": {"log": str(log_path)}}

    missing = [s for s in vdef.get("assert_log_contains", []) if s not in log_text]
    forbidden = [s for s in vdef.get("assert_log_absent", []) if s in log_text]
    if missing or forbidden:
        return {"kind": "pie_recording", "status": "FAIL",
                "detail": f"log assertions failed: missing={missing} forbidden={forbidden}",
                "duration_ms": int((time.time() - t0) * 1000),
                "artifacts": {"log": str(log_path)}}

    return {"kind": "pie_recording", "status": "PASS",
            "detail": f"agent script ran cleanly; required strings present: {vdef.get('assert_log_contains', [])}",
            "duration_ms": int((time.time() - t0) * 1000),
            "artifacts": {"log": str(log_path)}}


# ---- registry ---------------------------------------------------------------

# ---- live_python (MCP-style live editor RPC) -------------------------------

def live_python_validator(vdef: dict[str, Any], project_dir: pathlib.Path, run) -> dict[str, Any]:
    """Run Python in a live UE editor (already launched against this workdir's
    project) and assert on the structured output.

    Cost vs editor_open: ~330 ms per call vs ~10 sec cold-launch — 30x faster
    when the editor is reused across multiple validators in the same run.

    The caller is responsible for launching the editor first (via the
    holo-unreal MCP `ue_launch` or any other path) with Python Remote
    Execution enabled. If discovery fails, we return SKIP with guidance.

    vdef:
        code:                string of Python to exec in the editor
        agent_script:        OR a path (relative to project_dir) whose
                             contents should be exec'd
        assert_output_contains: list[str] — must each appear in the
                             concatenated `output[*].output` strings
        assert_output_absent:   list[str]
        require_success:     default True; FAIL if the command's
                             success field is False
        timeout_sec:         per-call timeout (default 60)
    """
    t0 = time.time()
    timeout = float(vdef.get("timeout_sec", 60.0))

    code = vdef.get("code")
    if code is None:
        rel = vdef.get("agent_script")
        if not rel:
            return {"kind": "live_python", "status": "FAIL",
                    "detail": "vdef must specify either `code` or `agent_script`",
                    "duration_ms": int((time.time() - t0) * 1000)}
        script = (project_dir / rel).resolve()
        if not script.exists():
            return {"kind": "live_python", "status": "FAIL",
                    "detail": f"agent script not found: {rel}",
                    "duration_ms": int((time.time() - t0) * 1000)}
        code = script.read_text(encoding="utf-8")

    try:
        from harness.ue_remote import LiveEditorSession
    except Exception as exc:  # pragma: no cover
        return {"kind": "live_python", "status": "SKIP",
                "detail": f"ue_remote import failed: {exc}",
                "duration_ms": int((time.time() - t0) * 1000)}

    log_path = run.artifacts_dir / "logs" / "live_python.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with LiveEditorSession(discovery_timeout_sec=timeout) as ue:
            t_call = time.time()
            res = ue.run(code)
            call_ms = int((time.time() - t_call) * 1000)
    except TimeoutError as exc:
        log_path.write_text(f"discovery timeout: {exc}\n", encoding="utf-8")
        return {"kind": "live_python", "status": "SKIP",
                "detail": ("no live editor discovered — launch UE with Python "
                           "Remote Execution enabled (e.g. holo-unreal "
                           "ue_launch + ue_enable_remote)"),
                "duration_ms": int((time.time() - t0) * 1000),
                "artifacts": {"log": str(log_path)}}

    output_lines = [item.get("output", "") for item in res.get("output", [])]
    output_text = "\n".join(output_lines)
    log_path.write_text(
        f"call_ms: {call_ms}\nsuccess: {res.get('success')}\n\n"
        f"=== code ===\n{code}\n\n=== output ===\n{output_text}\n"
        f"\n=== result ===\n{res.get('result')}\n",
        encoding="utf-8")

    if vdef.get("require_success", True) and not res.get("success", False):
        return {"kind": "live_python", "status": "FAIL",
                "detail": f"editor reported success=False (call_ms={call_ms})",
                "duration_ms": int((time.time() - t0) * 1000),
                "artifacts": {"log": str(log_path)}}

    missing = [s for s in vdef.get("assert_output_contains", [])
               if s not in output_text]
    forbidden = [s for s in vdef.get("assert_output_absent", [])
                 if s in output_text]
    if missing or forbidden:
        return {"kind": "live_python", "status": "FAIL",
                "detail": f"output assertions failed: missing={missing} forbidden={forbidden}",
                "duration_ms": int((time.time() - t0) * 1000),
                "artifacts": {"log": str(log_path)}}

    return {"kind": "live_python", "status": "PASS",
            "detail": (f"live editor RPC completed in {call_ms} ms; "
                       f"required strings present: {vdef.get('assert_output_contains', [])}"),
            "duration_ms": int((time.time() - t0) * 1000),
            "artifacts": {"log": str(log_path)}}


# ---- registry ---------------------------------------------------------------

REGISTRY: dict[str, Callable] = {
    "compile":         compile_validator,
    "editor_open":     editor_open_validator,
    "spec_test":       spec_test_validator,
    "functional_test": _stub("functional_test"),
    "pie_recording":   pie_recording_validator,
    "live_python":     live_python_validator,
    "insights_trace":  _stub("insights_trace"),
    "screenshot_diff": _stub("screenshot_diff"),
    "llm_judge":       _stub("llm_judge"),
}
