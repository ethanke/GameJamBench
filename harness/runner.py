"""GameJamBench runner — CLI entry point with subcommands.

    gjb run      <task> <system>     run a task against a system, validate, score
    gjb claim    <run_id>            back-fill telemetry for an interactive run, re-validate
    gjb report   [--runs DIR]        aggregate runs/ into a markdown leaderboard
    gjb generate [--seed N]          procedurally generate a task spec from the slot grammar

Pipeline stages (run subcommand):
    bootstrap -> isolate -> stage_agent -> validate -> score -> archive
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import uuid
from typing import Any

import yaml


# ---------- helpers ----------------------------------------------------------

def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat().replace("+00:00", "Z")


# ---------- data model -------------------------------------------------------

@dataclasses.dataclass
class TaskSpec:
    raw: dict[str, Any]

    @property
    def id(self) -> str: return self.raw["id"]

    @property
    def revision(self) -> int: return int(self.raw["revision"])

    @property
    def time_budget_sec(self) -> int: return int(self.raw["time_budget_min"]) * 60

    @property
    def msg_budget(self) -> int: return int(self.raw["msg_budget"])

    @property
    def starter_template(self) -> str: return self.raw["starter_template"]

    @property
    def validators(self) -> list[dict[str, Any]]: return self.raw["validators"]

    @property
    def prompt(self) -> str: return self.raw["prompt"]


@dataclasses.dataclass
class SystemConfig:
    raw: dict[str, Any]

    @property
    def id(self) -> str: return self.raw["id"]

    @property
    def model(self) -> str: return self.raw["model"]

    @property
    def mcps(self) -> list[str]: return self.raw.get("mcps", [])

    def fingerprint(self) -> str:
        canon = json.dumps(self.raw, sort_keys=True).encode()
        return hashlib.sha256(canon).hexdigest()[:16]


@dataclasses.dataclass
class Run:
    run_id: str
    task: TaskSpec
    system: SystemConfig
    artifacts_dir: pathlib.Path
    started_at: str
    ended_at: str | None = None
    wall_seconds: float | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    msgs: int = 0
    tool_calls: int = 0
    validator_results: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    score: float | None = None
    capability_fingerprint: dict[str, float] = dataclasses.field(default_factory=dict)
    notes: list[str] = dataclasses.field(default_factory=list)


# ---------- pipeline ---------------------------------------------------------

def bootstrap(task: TaskSpec, system: SystemConfig, runs_root: pathlib.Path) -> Run:
    run_id = f"{task.id}-{system.id}-{_utcnow().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    artifacts = runs_root / run_id
    artifacts.mkdir(parents=True, exist_ok=False)
    (artifacts / "logs").mkdir()
    (artifacts / "validator").mkdir()
    return Run(run_id=run_id, task=task, system=system, artifacts_dir=artifacts,
               started_at=_utcnow_iso())


def isolate(task: TaskSpec, run: Run, templates_root: pathlib.Path,
            workdir_root: pathlib.Path, copy_mode: str = "hardlink",
            write_manifest: bool = True) -> pathlib.Path:
    """Clone the starter template into a clean workdir for this run.

    copy_mode:
      "hardlink" (default) — NTFS/Linux hardlinks; per-workdir disk ~ delta.
      "copy"               — full robocopy/copytree; tasks that mutate existing
                             files in-place must use this to avoid corrupting
                             the template.
    """
    from harness import isolation as I

    src = templates_root / task.starter_template
    if not src.exists():
        raise FileNotFoundError(f"starter template not found: {src}")
    dst = workdir_root / run.run_id

    if copy_mode == "hardlink":
        stats = I.hardlink_clone(src, dst)
        log = run.artifacts_dir / "logs" / "isolate_hardlink.json"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps({
            "files_linked": stats.files_linked,
            "files_copied": stats.files_copied,
            "files_skipped": stats.files_skipped,
            "dirs_created": stats.dirs_created,
            "bytes_logical": stats.bytes_logical,
            "bytes_physical": stats.bytes_physical,
            "seconds": round(stats.seconds, 3),
            "fallback_to_copy": stats.fallback_to_copy,
            "error": stats.error,
        }, indent=2), encoding="utf-8")
        if stats.error:
            raise RuntimeError(f"hardlink_clone failed: {stats.error}")

        if write_manifest:
            I.write_manifest(dst, run.artifacts_dir / "validator" / "template_manifest.json")
        return dst

    # copy mode (legacy, slow but isolation-bulletproof)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt" and shutil.which("robocopy"):
        log = run.artifacts_dir / "logs" / "isolate_robocopy.log"
        cmd = [
            "robocopy", str(src), str(dst), "/E",
            "/XD", "Binaries", "Intermediate", "Saved", "DerivedDataCache",
                  ".git", ".vs", ".claude",
            "/XF", "*.pdb", "*.exe",
            "/NP", "/NFL", "/NDL", "/R:2", "/W:2", "/MT:8",
            f"/LOG:{log}",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode >= 8:
            raise RuntimeError(f"robocopy failed (exit={proc.returncode}); see {log}")
    else:
        shutil.copytree(src, dst, dirs_exist_ok=False, ignore=shutil.ignore_patterns(
            "Binaries", "Intermediate", "Saved", "DerivedDataCache", ".git",
            ".vs", ".claude"))
    return dst


def stage_agent(task: TaskSpec, system: SystemConfig, project_dir: pathlib.Path,
                run: Run) -> None:
    """Stage the agent invocation. Interactive mode prints next-step instructions
    and exits; headless mode (TODO) actually invokes the API."""
    (run.artifacts_dir / "prompt.md").write_text(task.prompt, encoding="utf-8")
    (run.artifacts_dir / "system.json").write_text(
        json.dumps(system.raw, indent=2), encoding="utf-8")

    next_steps = (
        f"GameJamBench — interactive run staged: {run.run_id}\n\n"
        f"Project:    {project_dir}\n"
        f"Prompt:     {run.artifacts_dir / 'prompt.md'}\n"
        f"System:     {run.artifacts_dir / 'system.json'}\n\n"
        f"  1. cd \"{project_dir}\"\n"
        f"  2. claude       # open Claude Code in the workdir\n"
        f"  3. Paste prompt and let the agent work.\n"
        f"  4. gjb claim {run.run_id}\n"
    )
    (run.artifacts_dir / "NEXT_STEPS.txt").write_text(next_steps, encoding="utf-8")
    run.notes.append("interactive_pending")


def validate(task: TaskSpec, project_dir: pathlib.Path, run: Run) -> None:
    from harness import validators as V

    for vdef in task.validators:
        kind = vdef["kind"]
        impl = V.REGISTRY.get(kind)
        if impl is None:
            run.validator_results.append({
                "kind": kind, "status": "SKIP",
                "detail": f"no validator impl for {kind}", "duration_ms": 0,
            })
            continue
        result = impl(vdef, project_dir, run)
        run.validator_results.append(result)
        if result["status"] == "FAIL" and vdef.get(
                "hard_gate", kind in {"compile", "editor_open"}):
            run.notes.append(f"hard_gate_fail:{kind}")
            break


def score(task: TaskSpec, run: Run, rubric: dict[str, Any]) -> None:
    from harness import scorer as S
    run.score = S.compute_score(run, rubric)
    run.capability_fingerprint = S.compute_fingerprint(task, run, rubric)


def archive(run: Run) -> pathlib.Path:
    record = {
        "run_id": run.run_id,
        "task_id": run.task.id,
        "task_revision": run.task.revision,
        "system_id": run.system.id,
        "system_fingerprint": run.system.fingerprint(),
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "wall_seconds": run.wall_seconds,
        "tokens_in": run.tokens_in,
        "tokens_out": run.tokens_out,
        "msgs": run.msgs,
        "tool_calls": run.tool_calls,
        "validator_results": run.validator_results,
        "score": run.score,
        "capability_fingerprint": run.capability_fingerprint,
        "notes": run.notes,
    }
    out = run.artifacts_dir / "run.json"
    out.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return out


# ---------- subcommand: run --------------------------------------------------

def cmd_run(args: argparse.Namespace) -> int:
    task = TaskSpec(yaml.safe_load(args.task.read_text(encoding="utf-8")))
    system = SystemConfig(yaml.safe_load(args.system.read_text(encoding="utf-8")))
    rubric = yaml.safe_load(args.rubric.read_text(encoding="utf-8"))

    runs_root = args.runs.resolve()
    templates_root = args.templates.resolve()
    workdir_root = args.workdir.resolve()
    runs_root.mkdir(parents=True, exist_ok=True)
    workdir_root.mkdir(parents=True, exist_ok=True)

    run = bootstrap(task, system, runs_root)
    project = isolate(task, run, templates_root, workdir_root,
                      copy_mode=args.copy_mode)

    started = _utcnow()
    try:
        if not args.skip_agent:
            stage_agent(task, system, project, run)
        if not args.skip_validate:
            validate(task, project, run)
    finally:
        run.ended_at = _utcnow_iso()
        run.wall_seconds = (_utcnow() - started).total_seconds()

    score(task, run, rubric)
    record_path = archive(run)
    print(json.dumps({"run": run.run_id, "score": run.score, "record": str(record_path)}))
    return 0 if (run.score or 0) > 0 else 1


# ---------- subcommand: claim ------------------------------------------------

def cmd_claim(args: argparse.Namespace) -> int:
    """Back-fill telemetry for an interactive run and re-run validators.

    Telemetry is read from --telemetry JSON (or stdin):
        {model, msgs, tokens_in, tokens_out, tool_calls, transcript_path?}
    """
    runs_root = args.runs.resolve()
    run_dir = runs_root / args.run_id
    if not run_dir.exists():
        print(f"run not found: {run_dir}", file=sys.stderr)
        return 2

    record_path = run_dir / "run.json"
    if not record_path.exists():
        print(f"run.json missing in {run_dir}", file=sys.stderr)
        return 2

    record = json.loads(record_path.read_text(encoding="utf-8"))

    # Load telemetry.
    telemetry: dict[str, Any] = {}
    if args.telemetry:
        telemetry = json.loads(args.telemetry.read_text(encoding="utf-8"))
    elif not sys.stdin.isatty():
        telemetry = json.loads(sys.stdin.read())

    record["msgs"] = int(telemetry.get("msgs", record.get("msgs", 0)))
    record["tokens_in"] = int(telemetry.get("tokens_in", record.get("tokens_in", 0)))
    record["tokens_out"] = int(telemetry.get("tokens_out", record.get("tokens_out", 0)))
    record["tool_calls"] = int(telemetry.get("tool_calls", record.get("tool_calls", 0)))

    # Re-validate against the workdir.
    workdir_root = args.workdir.resolve()
    project_dir = workdir_root / args.run_id
    if not project_dir.exists():
        print(f"workdir not found: {project_dir}", file=sys.stderr)
        return 2

    task = TaskSpec({
        **{"id": record["task_id"], "revision": record["task_revision"]},
        **_load_task_by_id(args.tasks_dir, record["task_id"]),
    })
    system_path = _find_system_config(args.systems_dir, record["system_id"])
    system = SystemConfig(yaml.safe_load(system_path.read_text(encoding="utf-8")))
    rubric = yaml.safe_load(args.rubric.read_text(encoding="utf-8"))

    run = Run(
        run_id=record["run_id"], task=task, system=system,
        artifacts_dir=run_dir, started_at=record["started_at"],
        ended_at=record.get("ended_at"), wall_seconds=record.get("wall_seconds"),
        tokens_in=record["tokens_in"], tokens_out=record["tokens_out"],
        msgs=record["msgs"], tool_calls=record["tool_calls"],
        notes=list(record.get("notes", [])),
    )
    if "interactive_claimed" not in run.notes:
        run.notes.append("interactive_claimed")

    started = _utcnow()
    try:
        validate(task, project_dir, run)
    finally:
        if run.wall_seconds is None:
            run.wall_seconds = (_utcnow() - started).total_seconds()

    score(task, run, rubric)
    archive(run)
    print(json.dumps({"run": run.run_id, "score": run.score}))
    return 0 if (run.score or 0) > 0 else 1


def _load_task_by_id(tasks_dir: pathlib.Path, task_id: str) -> dict[str, Any]:
    for p in tasks_dir.glob("*.yaml"):
        spec = yaml.safe_load(p.read_text(encoding="utf-8"))
        if spec.get("id") == task_id:
            return spec
    raise FileNotFoundError(f"no task spec found for id={task_id} under {tasks_dir}")


def _find_system_config(systems_dir: pathlib.Path, system_id: str) -> pathlib.Path:
    for p in systems_dir.glob("*.yaml"):
        spec = yaml.safe_load(p.read_text(encoding="utf-8"))
        if spec.get("id") == system_id:
            return p
    raise FileNotFoundError(f"no system config for id={system_id} under {systems_dir}")


# ---------- subcommand: report -----------------------------------------------

def cmd_report(args: argparse.Namespace) -> int:
    """Aggregate runs/ into a markdown leaderboard."""
    runs_root = args.runs.resolve()
    runs = []
    for run_json in runs_root.glob("*/run.json"):
        try:
            runs.append(json.loads(run_json.read_text(encoding="utf-8")))
        except Exception as exc:
            print(f"skipping unreadable {run_json}: {exc}", file=sys.stderr)

    runs.sort(key=lambda r: r.get("started_at", ""))

    by_system: dict[str, dict[str, Any]] = {}
    by_task: dict[str, dict[str, Any]] = {}
    for r in runs:
        sid = r["system_id"]
        tid = r["task_id"]
        by_system.setdefault(sid, {"runs": 0, "score_sum": 0.0, "wall_sum": 0.0,
                                    "tokens_sum": 0, "msgs_sum": 0})
        by_task.setdefault(tid, {"runs": 0, "score_sum": 0.0})
        by_system[sid]["runs"] += 1
        by_system[sid]["score_sum"] += float(r.get("score") or 0.0)
        by_system[sid]["wall_sum"] += float(r.get("wall_seconds") or 0.0)
        by_system[sid]["tokens_sum"] += int(r.get("tokens_in", 0)) + int(r.get("tokens_out", 0))
        by_system[sid]["msgs_sum"] += int(r.get("msgs", 0))
        by_task[tid]["runs"] += 1
        by_task[tid]["score_sum"] += float(r.get("score") or 0.0)

    lines: list[str] = []
    lines.append("# GameJamBench — Run Report")
    lines.append("")
    lines.append(f"Total runs: **{len(runs)}**")
    lines.append("")
    lines.append("## Systems leaderboard")
    lines.append("")
    lines.append("| System | Runs | Avg score | Avg wall (s) | Avg tokens | Avg msgs |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for sid, s in sorted(by_system.items(), key=lambda x: -x[1]["score_sum"] / max(1, x[1]["runs"])):
        n = s["runs"]
        lines.append(f"| `{sid}` | {n} | {s['score_sum']/n:.3f} | {s['wall_sum']/n:.1f} | "
                     f"{s['tokens_sum']//n} | {s['msgs_sum']//n} |")
    lines.append("")
    lines.append("## Tasks pass-rate")
    lines.append("")
    lines.append("| Task | Runs | Avg score |")
    lines.append("|---|---:|---:|")
    for tid, t in sorted(by_task.items()):
        n = t["runs"]
        lines.append(f"| `{tid}` | {n} | {t['score_sum']/n:.3f} |")
    lines.append("")
    lines.append("## Recent runs")
    lines.append("")
    lines.append("| Started | Task | System | Score | Wall (s) | Validators |")
    lines.append("|---|---|---|---:|---:|---|")
    for r in runs[-20:][::-1]:
        v = ", ".join(f"{x['kind']}={x['status']}" for x in r.get("validator_results", []))
        lines.append(f"| {r.get('started_at', '')} | `{r['task_id']}` | `{r['system_id']}` | "
                     f"{r.get('score', 0):.3f} | {r.get('wall_seconds') or 0:.1f} | {v} |")
    lines.append("")

    out = args.output if args.output else None
    text = "\n".join(lines)
    if out:
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
    else:
        sys.stdout.write(text + "\n")
    return 0


# ---------- subcommand: prune ------------------------------------------------

def _rm_workdir_robust(path: pathlib.Path) -> bool:
    """Remove a workdir reliably on Windows long paths + read-only files.

    Uses Windows rd /s /q which handles long paths and read-only files; falls
    back to shutil.rmtree on non-Windows.
    """
    if not path.exists():
        return True
    if os.name == "nt":
        proc = subprocess.run(
            ["cmd", "/c", "rd", "/s", "/q", str(path)],
            capture_output=True, text=True,
        )
        return not path.exists()
    try:
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    f.chmod(0o644)
                except OSError:
                    pass
        shutil.rmtree(path)
        return True
    except OSError:
        return False


def cmd_prune(args: argparse.Namespace) -> int:
    """Delete old workdirs and (optionally) runs/, keeping the latest N per task.

    Always preserves runs/<run_id>/run.json (cheap to keep historic scores) by
    default; use --drop-runs to also remove archived run records.
    """
    workdirs_root = args.workdirs.resolve()
    runs_root = args.runs.resolve()

    by_task: dict[str, list[pathlib.Path]] = {}
    if workdirs_root.exists():
        for d in workdirs_root.iterdir():
            if not d.is_dir():
                continue
            tid = d.name.split("-")[0]
            by_task.setdefault(tid, []).append(d)

    keep_n = max(0, int(args.keep))
    freed_bytes = 0
    deleted: list[str] = []
    for tid, dirs in by_task.items():
        dirs.sort(key=lambda p: p.name)  # name embeds timestamp
        for old in dirs[:-keep_n] if keep_n else dirs:
            try:
                size = sum(f.stat().st_size for f in old.rglob("*") if f.is_file())
            except OSError:
                size = 0
            ok = _rm_workdir_robust(old)
            if ok:
                freed_bytes += size
                deleted.append(old.name)
            else:
                print(f"failed to remove {old}", file=sys.stderr)

            if args.drop_runs:
                run_archive = runs_root / old.name
                if run_archive.exists():
                    _rm_workdir_robust(run_archive)

    print(json.dumps({
        "deleted": deleted,
        "freed_gb": round(freed_bytes / (1024**3), 2),
        "kept_per_task": keep_n,
    }, indent=2))
    return 0


# ---------- subcommand: generate ---------------------------------------------

def cmd_generate(args: argparse.Namespace) -> int:
    from harness import generator
    spec = generator.generate(seed=args.seed, tier=args.tier)
    out = args.output if args.output else pathlib.Path(f"tasks/{spec['id']}.yaml")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    print(f"wrote {out}  (id={spec['id']}, tier={spec['tier']}, axes={spec['capability_axes']})")
    return 0


# ---------- entrypoint -------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gjb")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="run a task against a system")
    pr.add_argument("--task", required=True, type=pathlib.Path)
    pr.add_argument("--system", required=True, type=pathlib.Path)
    pr.add_argument("--rubric", default=pathlib.Path("rubric.yaml"), type=pathlib.Path)
    pr.add_argument("--templates", default=pathlib.Path("templates"), type=pathlib.Path)
    pr.add_argument("--workdir", default=pathlib.Path("workdirs"), type=pathlib.Path)
    pr.add_argument("--runs", default=pathlib.Path("runs"), type=pathlib.Path)
    pr.add_argument("--skip-agent", action="store_true",
                    help="don't stage agent (validate-only run)")
    pr.add_argument("--skip-validate", action="store_true")
    pr.add_argument("--copy-mode", choices=["hardlink", "copy"], default="hardlink",
                    help="hardlink (cheap, default) or copy (slow, isolation-strict)")
    pr.set_defaults(func=cmd_run)

    pc = sub.add_parser("claim", help="record telemetry + re-validate an interactive run")
    pc.add_argument("run_id")
    pc.add_argument("--telemetry", type=pathlib.Path,
                    help="JSON file with {model,msgs,tokens_in,tokens_out,tool_calls}")
    pc.add_argument("--rubric", default=pathlib.Path("rubric.yaml"), type=pathlib.Path)
    pc.add_argument("--workdir", default=pathlib.Path("workdirs"), type=pathlib.Path)
    pc.add_argument("--runs", default=pathlib.Path("runs"), type=pathlib.Path)
    pc.add_argument("--tasks-dir", default=pathlib.Path("tasks"), type=pathlib.Path)
    pc.add_argument("--systems-dir", default=pathlib.Path("systems"), type=pathlib.Path)
    pc.set_defaults(func=cmd_claim)

    prep = sub.add_parser("report", help="aggregate runs/ into a markdown leaderboard")
    prep.add_argument("--runs", default=pathlib.Path("runs"), type=pathlib.Path)
    prep.add_argument("--output", "-o", type=pathlib.Path)
    prep.set_defaults(func=cmd_report)

    pp = sub.add_parser("prune", help="delete old workdirs, keep latest N per task")
    pp.add_argument("--keep", type=int, default=2,
                    help="keep latest N workdirs per task (default 2; 0 = remove all)")
    pp.add_argument("--workdirs", default=pathlib.Path("workdirs"), type=pathlib.Path)
    pp.add_argument("--runs", default=pathlib.Path("runs"), type=pathlib.Path)
    pp.add_argument("--drop-runs", action="store_true",
                    help="also remove the runs/<id>/ archive (default keeps run.json history)")
    pp.set_defaults(func=cmd_prune)

    pg = sub.add_parser("generate", help="procedurally generate a task spec from the slot grammar")
    pg.add_argument("--seed", type=int)
    pg.add_argument("--tier", type=int, default=2, choices=[1, 2, 3, 4, 5])
    pg.add_argument("--output", "-o", type=pathlib.Path)
    pg.set_defaults(func=cmd_generate)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
