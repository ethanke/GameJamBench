"""Score computation — pure functions on a Run + rubric.

Final score = pass_score * (1 - efficiency_penalty) - human_intervention_penalty
Capability fingerprint is reported separately, NOT folded into the scalar.
"""

from __future__ import annotations

from typing import Any


def compute_score(run, rubric: dict[str, Any]) -> float:
    cfg = rubric["scoring"]
    weights = cfg["validator_weights"]

    # Hard gates: any FAIL on a hard-gate validator zeroes the score.
    for vr in run.validator_results:
        meta = weights.get(vr["kind"], {})
        if meta.get("hard_gate") and vr["status"] == "FAIL":
            return 0.0

    # Weighted pass score across non-skipped, non-hard-gate validators.
    total_w = 0.0
    earned = 0.0
    for vr in run.validator_results:
        meta = weights.get(vr["kind"], {})
        w = float(meta.get("weight", 0.0))
        cap = float(meta.get("max", w))
        if vr["status"] == "SKIP":
            continue
        total_w += w
        if vr["status"] == "PASS":
            earned += min(w, cap)

    pass_score = (earned / total_w) if total_w > 0 else 0.0

    # Efficiency penalty: each component capped, summed.
    eff_cfg = cfg["efficiency_penalty"]["components"]
    pen = 0.0
    pen += _budget_penalty(run.wall_seconds or 0,
                           run.task.time_budget_sec, eff_cfg["wall"]["cap"])
    pen += _budget_penalty(run.tokens_in + run.tokens_out,
                           run.task.raw["token_budget"], eff_cfg["tokens"]["cap"])
    pen += _budget_penalty(run.msgs, run.task.msg_budget, eff_cfg["msgs"]["cap"])

    score = max(0.0, pass_score * (1.0 - pen))

    if any(n == "human_intervention" for n in run.notes):
        score -= cfg["human_intervention_penalty"]

    return max(0.0, min(1.0, score))


def compute_fingerprint(task, run, rubric: dict[str, Any]) -> dict[str, float]:
    """Per-axis pass-rate contribution.

    v0: spread the run's pass-score evenly across this task's declared axes.
    v1: per-validator axis attribution (compile -> cpp, spec -> validation, etc.).
    """
    axes = task.raw.get("capability_axes", [])
    if not axes:
        return {}
    score = run.score or 0.0
    return {axis: score for axis in axes}


def _budget_penalty(actual: float, budget: float, cap: float) -> float:
    if budget <= 0:
        return 0.0
    over = max(0.0, (actual - budget) / budget)
    return min(cap, cap * over)  # 0..cap as we go from 1x to 2x of budget
