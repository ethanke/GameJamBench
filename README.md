# GameJamBench

A benchmark for AI-agent systems vibe-coding Unreal Engine.

A *system under test* is the full triple `(model + MCP servers + recipes)` —
not a model in isolation. Tasks are natural-language game-jam prompts; outputs
are playable artifacts in a clean UE project; grading is automated by a stack
of validators (compile → editor opens → automation tests → PIE recording with
log assertions → Insights trace event counts → screenshot perceptual diff →
optional capped LLM-judge).

> Built and maintained on Unreal Engine 5.7.4. Claude family only (Opus 4.7,
> Sonnet 4.6, Haiku 4.5). MIT licensed.

## Why

LLM-coded UE work has a stable failure pattern: agents nail the C++, struggle
with Blueprint graph manipulation, fall apart on Animation Blueprints,
hallucinate dependencies on opaque `.uasset` binaries, and burn hours
integrating Fab marketplace assets. Existing code-generation benchmarks
(SWE-Bench, HumanEval, etc.) don't capture any of this — they reward textual
patches against unit tests, not playable game artifacts.

GameJamBench measures the only thing that matters at the end: did the agent
ship a working playable that holds up under a deterministic test suite, and
how efficiently?

## Anatomy

```
tasks/                    YAML game-jam prompts, schema-pinned by revision
schema/task.schema.yaml   Annotated task spec
rubric.yaml               10 capability axes × 5 difficulty tiers + scoring
systems/                  System-under-test configs (model + MCPs + recipes)
templates/                Starter UE projects per task family
harness/
  runner.py               bootstrap → isolate → run → validate → score → archive
  validators.py           Validator registry (compile, spec_test, pie_recording, ...)
  scorer.py               Pure functions: pass × efficiency, axis fingerprint
  agent_runner.py         Interactive (subscription) and headless (API) modes
  model_adapter.py        Opus 4.7 / Sonnet 4.6 / Haiku 4.5 adapter
runs/                     One directory per (task, system, timestamp) execution
```

## Quickstart (Windows, UE 5.7.4 installed)

```pwsh
# 1. Install runner deps
python -m pip install -e .

# 2. Tell the harness where UE is (defaults to the standard Epic install)
$env:GJB_UE_ROOT = "C:\Program Files\Epic Games\UE_5.7"

# 3. Run a curated task interactively (subscription mode)
gjb --task tasks/T001_power_strike.yaml --system systems/claude_code_holo.yaml

# The runner clones the starter template into workdirs/<run_id>/, drops the
# prompt and the system config there, and prints next-step instructions:
#   1) cd workdirs/<run_id> && claude
#   2) work the task with Claude Code as you normally would
#   3) gjb claim <run_id>     -> records telemetry + runs the validator suite
```

In production (API mode) the same task runs unattended via the Anthropic SDK
under the system's configured model, with full streamed event capture.

## Capability axes

| Axis              | What it measures                                           |
|-------------------|------------------------------------------------------------|
| cpp               | UCLASS/UFUNCTION fluency, header/source split, builds clean |
| structured_data   | DataAssets, DataTables, INI edits — config without BP graph |
| blueprint_graph   | Node creation, pin wiring, sub-graph navigation            |
| animation         | AnimBP, State Tree Animation, Linked Anim Layers, Pose Search |
| replication       | Server/client logic, RPCs, replicated props, Iris-aware     |
| gas               | Abilities, Effects, AttributeSets, GameplayCues, ASC wiring |
| editor_scripting  | Python in editor, headless commandlets, batch ops           |
| integration       | Adapter pattern for Fab content, Game Features Plugin       |
| validation        | Spec tests, Functional Tests, Gauntlet, PIE recording       |
| debugging         | Crash dumps, Insights traces, fix compile/link/runtime      |

Per-system reports include axis-level pass rates so failure modes are
attributable, not just a single scalar.

## Tiers

| Tier | Name      | Target time | Example                                        |
|------|-----------|-------------|------------------------------------------------|
| 1    | Atomic    | 5 min       | Add a `UPROPERTY(EditAnywhere) float`         |
| 2    | Component | 20 min      | Add a `UActorComponent` that broadcasts overlap |
| 3    | Feature   | 60 min      | GAS ability with replicated cooldown + UI      |
| 4    | Mini-game | 180 min     | 4-player arena with knockback + respawn        |
| 5    | Game jam  | 480 min     | "Capture the Flag with one unique mechanic"     |

## Scoring

```
score = pass_score × (1 - efficiency_penalty) - human_intervention_penalty
```

- `pass_score` is a weighted sum across non-skipped validators. A FAIL on any
  hard-gate validator (compile, editor_open) zeroes the score outright.
- `efficiency_penalty` is the sum of three capped components: wall time,
  tokens (in+out), agent messages — each penalising overruns vs the task's
  declared budget.
- `human_intervention_penalty` is 0.50 — any human keystroke during a run
  halves the score. The whole point is autonomy.
- `capability_fingerprint` is reported separately, never folded into the
  scalar — single numbers hide where systems break.

## Status

**Phase A through F complete (2026-05-08)** — pipeline runs end-to-end
with five real validators, isolates 10 GB UE templates at zero disk
cost, ships a buildable blank C++ template that exercises the full
compile / editor_open / spec_test path, and talks to a live editor via
Python Remote Execution at ~330 ms per call (30× faster than
cold-launching a commandlet).

Real:
- `gjb run` — bootstrap → hardlink-isolate (or copy) → stage agent → validate → score → archive
- `gjb claim <run_id>` — back-fill telemetry for an interactive run, re-validate
- `gjb report` — aggregate `runs/` into a markdown leaderboard
- `gjb prune [--keep N]` — robust workdir cleanup (handles Windows long paths + read-only hardlinks)
- `gjb generate --seed N --tier T` — slot-grammar task generator
- Validators: `compile` (real UBT against the workdir's .uproject), `editor_open` (pythonscript commandlet + log assertions), `spec_test` (Automation framework + JSON report parse, BOM-tolerant), `pie_recording` (agent-driven simulation in commandlet mode + duration window + log assertions), `live_python` (Python Remote Execution against a running editor — ~330 ms per call vs ~10 sec cold-launch)
- Templates: `blank_5_7_4_python` (284-byte content-only Python sandbox), `blank_cpp_5_7_4` (full C++ project — Game + Editor targets, sample Spec test, builds clean against UE 5.7.4 in ~80 sec)
- Isolation: NTFS hardlink walk with `\\?\` long-path support; per-workdir disk
  ~ delta (10.5 GB template → 0 new bytes / 12 sec); read-only fence on
  hardlinked files prevents in-place mutation from corrupting the template;
  per-run sha256 manifest captures the exact starting state for tamper detection.

Stubs (return SKIP — implemented later):
`functional_test`, `insights_trace`, `screenshot_diff`, `llm_judge`.

### Talking to a live editor

`live_python` requires the editor to be already running against the
workdir's project, with Python Remote Execution enabled. Two paths:

  * The harness's `ue_remote.py` adapter dynamically loads UE's bundled
    `remote_execution.py` (no fork, no copy) and discovers the running
    node via UDP multicast.
  * For interactive work, the holo-unreal MCP exposes `ue_launch`,
    `ue_enable_remote`, `ue_py`, `ue_log`, `ue_close` to drive the
    editor end-to-end from a Claude Code conversation.

Launch flow:
  1. `gjb run --skip-validate` to stage a workdir
  2. `Build.bat <project>Editor Win64 Development -Project=…` to compile
  3. (once) `ue_enable_remote` to enable Python Remote Execution
  4. `ue_launch` against the workdir's `.uproject`
  5. `gjb claim <run_id>` (or any task with a `live_python` validator) to
     grade against the running editor

## Contributing

Issues and PRs welcome once the v0 pipeline produces a non-stub run.
Until then this is a public-development sketch.
