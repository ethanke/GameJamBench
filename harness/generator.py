"""Procedural task generator — slot-grammar over verbs/systems/modifiers.

Seeded for reproducibility. Generated tasks emit the same YAML shape as
curated ones, with `generated_by: grammar:<rule_id>` recorded in the spec.

The grammar is intentionally narrow in v0 — it produces tasks the harness
can actually validate today (compile + editor_open + spec_test). As more
validators come online, the grammar widens.
"""

from __future__ import annotations

import dataclasses
import hashlib
import random
from typing import Any


@dataclasses.dataclass
class Slot:
    verb: str
    system: str
    modifier: str
    starter_template: str
    capability_axes: list[str]
    tier: int


VERBS = ["Add", "Replace", "Extend", "Optimise"]

SYSTEMS = [
    {"name": "AttributeSet field",
     "axes": ["cpp", "structured_data"], "tier": 1, "template": "blank_cpp_5_7_4"},
    {"name": "UActorComponent",
     "axes": ["cpp", "validation"], "tier": 2, "template": "blank_cpp_5_7_4"},
    {"name": "Anim Layer Interface override",
     "axes": ["animation", "blueprint_graph"], "tier": 3, "template": "gameanim_5_7_4"},
    {"name": "Mover MovementMode",
     "axes": ["cpp", "animation"], "tier": 4, "template": "gameanim_5_7_4"},
    {"name": "Pose Search descriptor",
     "axes": ["animation", "structured_data"], "tier": 3, "template": "gameanim_5_7_4"},
    {"name": "GameplayCue notify",
     "axes": ["gas", "blueprint_graph"], "tier": 2, "template": "gas_plugin_5_7_4"},
    {"name": "GAS ability with replicated cooldown",
     "axes": ["cpp", "gas", "replication"], "tier": 3, "template": "gas_plugin_5_7_4"},
]

MODIFIERS = [
    "with one Spec test that proves the change",
    "exposed to Blueprint via UFUNCTION(BlueprintCallable)",
    "with replication authority on the server",
    "configurable via a UDataAsset",
    "wired into the existing input mapping context",
    "bounded by a 60Hz update rate",
]


def _slot_for_seed(seed: int, tier: int | None) -> Slot:
    rng = random.Random(seed)
    candidates = [s for s in SYSTEMS if tier is None or s["tier"] == tier]
    if not candidates:
        candidates = SYSTEMS
    sysdef = rng.choice(candidates)
    return Slot(
        verb=rng.choice(VERBS),
        system=sysdef["name"],
        modifier=rng.choice(MODIFIERS),
        starter_template=sysdef["template"],
        capability_axes=list(sysdef["axes"]) + ["validation"],
        tier=sysdef["tier"],
    )


def generate(seed: int | None = None, tier: int | None = None) -> dict[str, Any]:
    if seed is None:
        seed = random.randint(1, 2**31 - 1)
    slot = _slot_for_seed(seed, tier)
    short = hashlib.sha256(f"{seed}-{slot.system}".encode()).hexdigest()[:8]
    task_id = f"G{slot.tier}_{short}"
    title = f"{slot.verb} {slot.system}, {slot.modifier}"

    prompt = (
        f"{slot.verb} a {slot.system} to the project, {slot.modifier}.\n\n"
        f"Constraints:\n"
        f"  - The validator suite must pass without modification.\n"
        f"  - Prefer C++ for behaviour; reserve Blueprint for asset wiring.\n"
        f"  - Add at least one Spec test under filter `GameJamBench.{task_id}.*`.\n"
    )

    spec: dict[str, Any] = {
        "id": task_id,
        "revision": 1,
        "title": title,
        "generated_by": "grammar:v0",
        "generator_seed": seed,
        "tier": slot.tier,
        "capability_axes": slot.capability_axes,
        "composes": [],
        "prompt": prompt,
        "starter_template": slot.starter_template,
        "starter_branch": "main",
        "time_budget_min": {1: 10, 2: 30, 3: 90, 4: 240, 5: 600}[slot.tier],
        "token_budget":   {1: 100_000, 2: 300_000, 3: 800_000,
                            4: 1_500_000, 5: 3_000_000}[slot.tier],
        "msg_budget":     {1: 30, 2: 80, 3: 200, 4: 400, 5: 800}[slot.tier],
        "allowed_tools": ["Read", "Edit", "Write", "Glob", "Grep", "Bash",
                           "mcp__holo-unreal__*"],
        "blocked_tools": [],
        "validators": [
            {"kind": "compile", "target": "Editor"},
            {"kind": "editor_open", "timeout_sec": 600},
            {"kind": "spec_test", "filter": f"GameJamBench.{task_id}.*",
             "require_min": 1},
        ],
        "golden": {"test_files": [], "reference_screenshots": []},
    }
    return spec
