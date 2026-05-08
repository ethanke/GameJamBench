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

**Phase A + B + C complete (2026-05-08)** — pipeline runs end-to-end and
isolates 10 GB UE templates at zero disk cost via hardlinks.

Real:
- `gjb run` — bootstrap → hardlink-isolate (or copy) → stage agent → validate → score → archive
- `gjb claim <run_id>` — back-fill telemetry for an interactive run, re-validate
- `gjb report` — aggregate `runs/` into a markdown leaderboard
- `gjb prune [--keep N]` — robust workdir cleanup (handles Windows long paths + read-only hardlinks)
- `gjb generate --seed N --tier T` — slot-grammar task generator
- Validators: `compile` (UBT), `editor_open` (pythonscript commandlet + log assertions), `spec_test` (Automation framework + JSON report parse)
- Isolation: NTFS hardlink walk with `\\?\` long-path support; per-workdir disk
  ~ delta (10.5 GB template → 0 new bytes / 12 sec); read-only fence on
  hardlinked files prevents in-place mutation from corrupting the template;
  per-run sha256 manifest captures the exact starting state for tamper detection.

Stubs (return SKIP — implemented in Phase D after agent-loop validation):
`functional_test`, `pie_recording`, `insights_trace`, `screenshot_diff`, `llm_judge`.

## Contributing

Issues and PRs welcome once the v0 pipeline produces a non-stub run.
Until then this is a public-development sketch.
