from __future__ import annotations

from typing import Dict, List

from .models import MacroTotal, QuestionnaireAnswers

ACTIVITY_LEVEL_OPTIONS: List[Dict[str, object]] = [
    {
        "value": "sedentary",
        "label": "Sedentary (little or no exercise)",
        "description": "Mostly seated lifestyle, minimal training, low day-to-day movement.",
        "multiplier": 1.2,
    },
    {
        "value": "light",
        "label": "Lightly active (exercise 1-2 days/week)",
        "description": "Light training or decent walking, but not consistently active most days.",
        "multiplier": 1.375,
    },
    {
        "value": "moderate",
        "label": "Moderately active (exercise 3-4 days/week)",
        "description": "Regular moderate training and average day-to-day movement.",
        "multiplier": 1.55,
    },
    {
        "value": "active",
        "label": "Active (exercise 5-6 days/week)",
        "description": "Hard training most days or a physically active routine/job.",
        "multiplier": 1.725,
    },
    {
        "value": "very_active",
        "label": "Very active (daily intense training or physical job)",
        "description": "Very high activity from intense daily exercise, double sessions, or sustained physical work.",
        "multiplier": 1.9,
    },
]

GOAL_OPTIONS: List[Dict[str, str]] = [
    {"value": "lose", "label": "Lose fat"},
    {"value": "maintain", "label": "Maintain"},
    {"value": "gain", "label": "Gain muscle"},
]

ACTIVITY_MULTIPLIERS = {
    item["value"]: float(item["multiplier"])
    for item in ACTIVITY_LEVEL_OPTIONS
}

GOAL_CALORIE_ADJUSTMENTS = {
    "lose": -300.0,
    "maintain": 0.0,
    "gain": 300.0,
}


def derive_daily_target(answers: QuestionnaireAnswers) -> MacroTotal:
    sex_offset = 5.0 if answers.sex == "male" else -161.0
    bmr = (
        (10.0 * answers.weight_kg)
        + (6.25 * answers.height_cm)
        - (5.0 * answers.age_years)
        + sex_offset
    )
    maintenance_calories = bmr * ACTIVITY_MULTIPLIERS[answers.activity_level]
    target_calories = _round_to_nearest_25(
        maintenance_calories + GOAL_CALORIE_ADJUSTMENTS[answers.goal]
    )

    protein_per_kg = 2.0 if answers.goal == "lose" else 1.8
    protein_g = round(answers.weight_kg * protein_per_kg)
    fat_g = round(answers.weight_kg * 0.8)

    remaining_calories = target_calories - (protein_g * 4.0) - (fat_g * 9.0)
    carbs_g = max(0, round(remaining_calories / 4.0))

    return MacroTotal(
        calories=float(target_calories),
        protein_g=float(protein_g),
        carbs_g=float(carbs_g),
        fat_g=float(fat_g),
    )


def questionnaire_meta_payload() -> Dict[str, object]:
    return {
        "activity_options": [
            {
                "value": str(item["value"]),
                "label": str(item["label"]),
                "description": str(item["description"]),
            }
            for item in ACTIVITY_LEVEL_OPTIONS
        ],
        "goal_options": list(GOAL_OPTIONS),
        "activity_guidance": (
            "Choose based on both exercise frequency and overall daily movement, not gym days alone."
        ),
    }


def _round_to_nearest_25(value: float) -> int:
    return int(round(value / 25.0) * 25)
