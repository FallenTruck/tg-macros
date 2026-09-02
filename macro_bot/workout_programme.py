"""Shared, immutable workout-programme definitions for JavaanFitness.

This module contains only programme metadata.  It deliberately has no user,
session, set, load, or progression state.  The records are JSON-like so they
can be stored in the existing single-table DynamoDB repository and rendered by
the authenticated Mini App without introducing a second persistence system.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping

PROGRAMME_ID = "javaanfitness"
INITIAL_VERSION_ID = "2026-09-01-v1"
PROGRAMME_PK = f"PROGRAM#{PROGRAMME_ID}"

WEEKDAYS = {"TUESDAY", "FRIDAY", "SUNDAY"}
DAY_ORDER = ("PULL", "SUPPORT_CORE", "PUSH")


def _exercise(
    exercise_id: str,
    name: str,
    aliases: Iterable[str],
    equipment: str,
    muscles: Iterable[str],
    execution_type: str,
    unilateral_mode: str = "bilateral",
    loading_convention: str = "none",
) -> dict[str, Any]:
    return {
        "entity_type": "exercise",
        "exercise_id": exercise_id,
        "canonical_name": name,
        "aliases": list(aliases),
        "equipment_category": equipment,
        "primary_muscle_groups": list(muscles),
        "execution_type": execution_type,
        "unilateral_mode": unilateral_mode,
        "loading_convention": loading_convention,
        "load_configuration": {"configured": False},
        "active": True,
    }


EXERCISE_CATALOGUE: tuple[dict[str, Any], ...] = (
    _exercise("lat_pulldown", "Lat Pulldown", ["lat pulldown", "pulldown"], "lat_pulldown", ["back"], "loaded_reps", loading_convention="machine_stack_kg"),
    _exercise("seated_cable_row", "Seated Cable Row", ["seated row", "cable row"], "seated_row", ["back"], "loaded_reps", loading_convention="machine_stack_kg"),
    _exercise("face_pull", "Face Pull", ["face pull"], "cable", ["rear_delts", "upper_back"], "loaded_reps", loading_convention="cable_load_kg"),
    _exercise("rear_fly", "Rear Fly", ["rear fly", "reverse fly"], "dumbbell_or_machine", ["rear_delts"], "loaded_reps", loading_convention="equipment_load_kg"),
    _exercise("dumbbell_biceps_curl", "Dumbbell Biceps Curl", ["db curl", "biceps curl"], "dumbbell", ["biceps"], "loaded_reps", loading_convention="per_dumbbell_kg"),
    _exercise("pallof_press", "Pallof Press", ["pallof"], "cable", ["core"], "side_aware_reps", "side_independent", "cable_load_kg"),
    _exercise("dead_bug", "Dead Bug", ["dead bug"], "bodyweight", ["core"], "bodyweight_reps"),
    _exercise("side_plank", "Side Plank", ["side plank"], "bodyweight", ["core"], "timed", "side_independent"),
    _exercise("chest_supported_dumbbell_row", "Chest-Supported Dumbbell Row", ["chest supported row"], "dumbbell", ["back"], "loaded_reps", loading_convention="per_dumbbell_kg"),
    _exercise("straight_arm_cable_pulldown", "Straight-Arm Cable Pulldown", ["straight arm pulldown"], "cable", ["back"], "loaded_reps", loading_convention="cable_load_kg"),
    _exercise("dumbbell_lateral_raise", "Dumbbell Lateral Raise", ["lateral raise", "side raise"], "dumbbell", ["shoulders"], "loaded_reps", loading_convention="per_dumbbell_kg"),
    _exercise("flat_dumbbell_chest_press", "Flat Dumbbell Chest Press", ["flat db press", "flat chest press"], "dumbbell", ["chest"], "loaded_reps", loading_convention="per_dumbbell_kg"),
    _exercise("incline_dumbbell_chest_press", "Incline Dumbbell Chest Press", ["incline db press", "incline chest press"], "dumbbell", ["chest"], "loaded_reps", loading_convention="per_dumbbell_kg"),
    _exercise("dumbbell_shoulder_press", "Dumbbell Shoulder Press", ["db shoulder press", "shoulder press"], "dumbbell", ["shoulders"], "loaded_reps", loading_convention="per_dumbbell_kg"),
    _exercise("triceps_rope_pressdown", "Triceps Rope Pressdown", ["rope pressdown", "triceps pushdown"], "cable", ["triceps"], "loaded_reps", loading_convention="cable_load_kg"),
)


def _option_target(
    exercise_id: str,
    *,
    execution_type: str,
    set_min: int,
    set_max: int,
    rep_min: int | None = None,
    rep_max: int | None = None,
    duration_seconds: int | None = None,
    target_note: str = "",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "exercise_id": exercise_id,
        "execution_type": execution_type,
        "set_min": set_min,
        "set_max": set_max,
    }
    if rep_min is not None:
        result["rep_min"] = rep_min
    if rep_max is not None:
        result["rep_max"] = rep_max
    if duration_seconds is not None:
        result["duration_seconds"] = duration_seconds
    if target_note:
        result["target_note"] = target_note
    return result


def _prescription(
    day_code: str,
    sequence: int,
    label: str,
    *,
    group: str,
    allowed: Iterable[str],
    default: str | None,
    set_min: int,
    set_max: int,
    rep_min: int | None,
    rep_max: int | None,
    optional: bool = False,
    notes: str = "",
    option_targets: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "entity_type": "programme_prescription",
        "prescription_id": f"{day_code.lower()}_{sequence:02d}",
        "day_code": day_code,
        "sequence": sequence,
        "display_label": label,
        "optional": optional,
        "prescription_group": group,
        "default_exercise_id": default,
        "allowed_exercise_ids": list(allowed),
        "set_min": set_min,
        "set_max": set_max,
        "rep_min": rep_min,
        "rep_max": rep_max,
        "rest_seconds": None,
        "notes": notes,
        "progression_policy_id": None,
    }
    targets = {str(item["exercise_id"]): dict(item) for item in option_targets}
    if targets:
        result["option_targets"] = targets
    return result


def _core_targets() -> tuple[dict[str, Any], ...]:
    return (
        _option_target("pallof_press", execution_type="side_aware_reps", set_min=2, set_max=3, rep_min=8, rep_max=12),
        _option_target("dead_bug", execution_type="bodyweight_reps", set_min=2, set_max=3, rep_min=8, rep_max=12),
        _option_target("side_plank", execution_type="timed", set_min=2, set_max=3, target_note="Duration target to be configured."),
    )


def _build_days() -> tuple[dict[str, Any], ...]:
    return (
        {
            "day_code": "PULL",
            "display_name": "Pull",
            "planned_weekday": "TUESDAY",
            "sequence": 1,
            "notes": "Planned Tuesday; actual sessions may occur on any date.",
            "prescriptions": (
                _prescription("PULL", 1, "Lat Pulldown", group="lat_pulldown", allowed=("lat_pulldown",), default="lat_pulldown", set_min=3, set_max=3, rep_min=8, rep_max=12),
                _prescription("PULL", 2, "Seated Cable Row", group="seated_row", allowed=("seated_cable_row",), default="seated_cable_row", set_min=3, set_max=3, rep_min=8, rep_max=12),
                _prescription("PULL", 3, "Rear-delt accessory", group="rear_delt_accessory", allowed=("rear_fly", "face_pull"), default="face_pull", set_min=2, set_max=3, rep_min=10, rep_max=15),
                _prescription("PULL", 4, "Dumbbell Biceps Curl", group="direct_biceps", allowed=("dumbbell_biceps_curl",), default="dumbbell_biceps_curl", set_min=2, set_max=3, rep_min=8, rep_max=12, notes="Approximately once weekly; may be skipped during execution when appropriate."),
                _prescription("PULL", 5, "Optional Core", group="core", allowed=("pallof_press", "dead_bug"), default="pallof_press", set_min=2, set_max=2, rep_min=None, rep_max=None, optional=True, option_targets=_core_targets()[:2]),
            ),
        },
        {
            "day_code": "SUPPORT_CORE",
            "display_name": "Support + Core",
            "planned_weekday": "FRIDAY",
            "sequence": 2,
            "notes": "Deliberately lighter support session; no chest press or direct arm isolation.",
            "prescriptions": (
                _prescription("SUPPORT_CORE", 1, "Chest-Supported Dumbbell Row", group="support_row", allowed=("chest_supported_dumbbell_row",), default="chest_supported_dumbbell_row", set_min=2, set_max=3, rep_min=8, rep_max=12),
                _prescription("SUPPORT_CORE", 2, "Straight-Arm Cable Pulldown", group="straight_arm_pulldown", allowed=("straight_arm_cable_pulldown",), default="straight_arm_cable_pulldown", set_min=2, set_max=3, rep_min=10, rep_max=15),
                _prescription("SUPPORT_CORE", 3, "Shoulder/rear-delt accessory", group="shoulder_rear_delt", allowed=("dumbbell_lateral_raise", "rear_fly"), default="dumbbell_lateral_raise", set_min=2, set_max=2, rep_min=12, rep_max=15),
                _prescription("SUPPORT_CORE", 4, "Core", group="core", allowed=("pallof_press", "dead_bug", "side_plank"), default="pallof_press", set_min=2, set_max=3, rep_min=None, rep_max=None, option_targets=_core_targets()),
            ),
        },
        {
            "day_code": "PUSH",
            "display_name": "Push",
            "planned_weekday": "SUNDAY",
            "sequence": 3,
            "notes": "Planned Sunday; direct chest pressing remains approximately once weekly.",
            "prescriptions": (
                _prescription("PUSH", 1, "Dumbbell Chest Press", group="chest_press", allowed=("flat_dumbbell_chest_press", "incline_dumbbell_chest_press"), default="flat_dumbbell_chest_press", set_min=3, set_max=3, rep_min=6, rep_max=10),
                _prescription("PUSH", 2, "Dumbbell Shoulder Press", group="shoulder_press", allowed=("dumbbell_shoulder_press",), default="dumbbell_shoulder_press", set_min=3, set_max=3, rep_min=8, rep_max=10),
                _prescription("PUSH", 3, "Dumbbell Lateral Raise", group="lateral_raise", allowed=("dumbbell_lateral_raise",), default="dumbbell_lateral_raise", set_min=2, set_max=2, rep_min=10, rep_max=15),
                _prescription("PUSH", 4, "Triceps Rope Pressdown", group="triceps_isolation", allowed=("triceps_rope_pressdown",), default="triceps_rope_pressdown", set_min=2, set_max=3, rep_min=10, rep_max=15),
                _prescription("PUSH", 5, "Optional Core", group="core", allowed=("pallof_press", "dead_bug", "side_plank"), default="pallof_press", set_min=2, set_max=3, rep_min=None, rep_max=None, optional=True, option_targets=_core_targets()),
            ),
        },
    )


def initial_programme_records() -> list[dict[str, Any]]:
    """Return deterministic DynamoDB records for the initial programme seed."""

    created_at = "2026-09-01T00:00:00Z"
    records: list[dict[str, Any]] = [
        {
            "PK": PROGRAMME_PK,
            "SK": "META",
            "entity_type": "workout_programme",
            "programme_id": PROGRAMME_ID,
            "display_name": "JavaanFitness Shared Programme",
            "created_at": created_at,
            "active_version_id": INITIAL_VERSION_ID,
            "status": "active",
        },
        {
            "PK": PROGRAMME_PK,
            "SK": "ACTIVE",
            "entity_type": "workout_programme_active_pointer",
            "programme_id": PROGRAMME_ID,
            "active_version_id": INITIAL_VERSION_ID,
            "updated_at": created_at,
        },
        {
            "PK": PROGRAMME_PK,
            "SK": f"VERSION#{INITIAL_VERSION_ID}#META",
            "entity_type": "workout_programme_version",
            "programme_id": PROGRAMME_ID,
            "version_id": INITIAL_VERSION_ID,
            "effective_at": created_at,
            "created_at": created_at,
            "status": "active",
            "notes": "Initial shared Pull, Support + Core, and Push programme.",
        },
    ]
    for exercise in EXERCISE_CATALOGUE:
        records.append({"PK": "CATALOG#EXERCISES", "SK": f"EXERCISE#{exercise['exercise_id']}", **deepcopy(exercise)})
    for day in _build_days():
        day_code = str(day["day_code"])
        day_meta = {key: deepcopy(value) for key, value in day.items() if key != "prescriptions"}
        records.append({"PK": PROGRAMME_PK, "SK": f"VERSION#{INITIAL_VERSION_ID}#DAY#{day_code}#META", "entity_type": "workout_programme_day", "programme_id": PROGRAMME_ID, "version_id": INITIAL_VERSION_ID, **day_meta})
        for prescription in day["prescriptions"]:
            records.append({"PK": PROGRAMME_PK, "SK": f"VERSION#{INITIAL_VERSION_ID}#DAY#{day_code}#PRESCRIPTION#{int(prescription['sequence']):03d}", "programme_id": PROGRAMME_ID, "version_id": INITIAL_VERSION_ID, **deepcopy(prescription)})
    return records


def programme_response(records: Iterable[Mapping[str, Any]], *, version_id: str, programme_id: str = PROGRAMME_ID) -> dict[str, Any]:
    """Assemble API-ready programme data from storage records."""

    all_records = [dict(record) for record in records]
    programme = next((item for item in all_records if item.get("entity_type") == "workout_programme"), {"programme_id": programme_id})
    version = next((item for item in all_records if item.get("entity_type") == "workout_programme_version" and item.get("version_id") == version_id), None)
    days: dict[str, dict[str, Any]] = {}
    for item in all_records:
        if item.get("entity_type") == "workout_programme_day":
            days[str(item["day_code"])] = {key: value for key, value in item.items() if key not in {"PK", "SK", "entity_type"}}
        elif item.get("entity_type") == "programme_prescription":
            day = days.setdefault(str(item["day_code"]), {"day_code": item["day_code"], "prescriptions": []})
            day.setdefault("prescriptions", []).append({key: value for key, value in item.items() if key not in {"PK", "SK", "entity_type"}})
    for day in days.values():
        day["prescriptions"] = sorted(day.get("prescriptions", []), key=lambda item: int(item.get("sequence", 0)))
        exercise_map = {str(item["exercise_id"]): item for item in all_records if item.get("entity_type") == "exercise"}
        for prescription in day["prescriptions"]:
            prescription["exercises"] = [
                exercise_map[exercise_id]
                for exercise_id in prescription.get("allowed_exercise_ids", [])
                if exercise_id in exercise_map
            ]
    ordered_days = sorted(days.values(), key=lambda item: (int(item.get("sequence", 999)), str(item.get("day_code", ""))))
    return {
        "programme": {key: value for key, value in programme.items() if key not in {"PK", "SK", "entity_type"}},
        "version": {key: value for key, value in (version or {}).items() if key not in {"PK", "SK", "entity_type"}},
        "days": ordered_days,
        "exercises": [
            {key: value for key, value in item.items() if key not in {"PK", "SK", "entity_type"}}
            for item in all_records
            if item.get("entity_type") == "exercise"
        ],
    }


def day_response(programme: Mapping[str, Any], day_code: str) -> dict[str, Any] | None:
    wanted = str(day_code or "").strip().upper()
    for day in programme.get("days", []):
        if str(day.get("day_code", "")).upper() == wanted:
            exercise_map = {str(item["exercise_id"]): item for item in programme.get("exercises", [])}
            enriched = deepcopy(day)
            for prescription in enriched.get("prescriptions", []):
                ids = prescription.get("allowed_exercise_ids", [])
                prescription["exercises"] = [exercise_map[item] for item in ids if item in exercise_map]
            return {"programme": programme.get("programme", {}), "version": programme.get("version", {}), "day": enriched}
    return None
