"""Offline ground-truth contract. Never derive labels from estimator output."""
from __future__ import annotations

import copy
import json
import math
import re
from datetime import date
from pathlib import Path

MACROS = ("calories", "protein_g", "carbs_g", "fat_g")
STRONG_SOURCES = {"weighed_meal", "cooked_weight", "packaged_food", "official_restaurant", "weighed_recipe"}
FACT_SOURCES = {"component_facts", "identity_confirmation"}
METHODS = {"human_measurement", "official_label", "official_restaurant", "weighed_recipe", "user_known_fact", "user_confirmation"}
FAILURE_MODES = {"food_misidentification", "portion_depth", "hidden_oil", "sauce_gravy", "hidden_base",
                 "bone_inedible_weight", "mixed_dish", "occlusion", "incomplete_caption", "unsupported_assumption", "other"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text_value(value, field):
    require(isinstance(value, str) and bool(value.strip()), f"{field} must be nonempty text")


def number(value, field, *, positive=False):
    require(not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value), f"{field} must be finite numeric data")
    require(value > 0 if positive else value >= 0, f"{field} must be {'positive' if positive else 'nonnegative'}")
    return value


def fields(value, allowed, required, field):
    require(isinstance(value, dict), f"{field} must be an object")
    require(not (set(value) - set(allowed)), f"Unknown fields in {field}: {sorted(set(value)-set(allowed))}")
    require(set(required) <= set(value), f"Missing fields in {field}: {sorted(set(required)-set(value))}")


def provenance(value):
    fields(value, {"date", "source", "method", "reference"}, {"date", "source", "method", "reference"}, "provenance")
    for key in ("date", "source", "reference"):
        text_value(value[key], key)
    require(date.fromisoformat(value["date"]) <= date.today(), "Annotation date cannot be in the future")
    require(value["method"] in METHODS, "Provenance must identify independent human/official evidence, never a model estimate")


def macros(value, field):
    fields(value, MACROS, MACROS, field)
    for key in MACROS:
        number(value[key], f"{field}.{key}")
    energy = 4 * value["protein_g"] + 4 * value["carbs_g"] + 9 * value["fat_g"]
    # Independent label check, not a change to production reconciliation.
    # Allows label rounding/fibre differences, but rejects inconsistent data.
    require(abs(value["calories"] - energy) <= max(20, .20 * max(energy, value["calories"])),
            f"{field} fails 4/4/9 consistency (tolerance max(20 kcal, 20%))")


def normalize_name(value):
    return " ".join(value.casefold().split())


def derive_component(component):
    """Return explicit arithmetic without overwriting a supplied consumed total."""
    ref = component.get("nutrition_reference")
    if not ref:
        return None
    fields(ref, {"source", "basis", "macros", "serving_weight_g"}, {"source", "basis", "macros"}, "nutrition_reference")
    text_value(ref["source"], "nutrition reference source")
    macros(ref["macros"], "reference macros")
    require(ref["basis"] in {"per_100g", "per_serving"}, "Unknown nutrition reference basis")
    if "serving_weight_g" in ref:
        number(ref["serving_weight_g"], "serving_weight_g", positive=True)
        if "consumed_weight_g" in component and "consumed_servings" in component:
            expected_weight = number(component["consumed_servings"], "consumed_servings", positive=True) * ref["serving_weight_g"]
            require(abs(number(component["consumed_weight_g"], "consumed_weight_g", positive=True) - expected_weight) <= max(.5, .02 * expected_weight),
                    "Consumed weight disagrees with consumed servings and reference serving weight")
    if ref["basis"] == "per_100g":
        require("consumed_weight_g" in component, "per_100g requires measured consumed weight")
        factor = number(component["consumed_weight_g"], "consumed_weight_g", positive=True) / 100
        expression = f"{component['consumed_weight_g']} g / 100 g"
    elif "consumed_servings" in component:
        factor = number(component["consumed_servings"], "consumed_servings", positive=True)
        expression = f"{factor} consumed servings"
    else:
        require("consumed_weight_g" in component and "serving_weight_g" in ref,
                "per_serving requires consumed_servings or consumed weight and reference serving weight")
        factor = number(component["consumed_weight_g"], "consumed_weight_g", positive=True) / ref["serving_weight_g"]
        expression = f"{component['consumed_weight_g']} g / {ref['serving_weight_g']} g per serving"
    return {"factor": factor, "expression": expression,
            "nutrition": {key: round(ref["macros"][key] * factor, 6) for key in MACROS}}


def approximately(actual, expected, field):
    for key in MACROS:
        tolerance = max(5 if key == "calories" else .5, .02 * expected[key])
        require(abs(actual[key] - expected[key]) <= tolerance, f"{field}.{key} disagrees with component/reference arithmetic")


def prepare_ground_truth(label):
    """Explicit label-entry step; persist reference, factor, and derived totals."""
    result = copy.deepcopy(label)
    for component in result.get("components", []):
        derived = derive_component(component)
        if derived:
            if "nutrition" in component:
                approximately(component["nutrition"], derived["nutrition"], "Supplied component total")
            else:
                component["nutrition"] = derived["nutrition"]
            component["derivation"] = derived
    if result.get("derive_total"):
        require(result.get("components_complete") is True, "Cannot derive whole-meal total from incomplete components")
        present = [c for c in result.get("components", []) if c.get("present", True)]
        require(bool(present) and all("nutrition" in c for c in present), "Every consumed component needs nutrition to derive a total")
        total = {k: round(sum(c["nutrition"][k] for c in present), 6) for k in MACROS}
        if "total" in result:
            approximately(result["total"], total, "Supplied meal total")
        else:
            result["total"] = total
    return result


def validate_ground_truth(case):
    label = case.get("ground_truth")
    if label is None:
        require(case.get("label_status") != "ground_truth", "ground_truth status requires a label")
        return
    require(case.get("label_status") == "ground_truth", "A labelled case must use ground_truth status")
    allowed = {"schema_version", "confidence_tier", "source_type", "provenance", "values_kind", "components",
               "components_complete", "total", "total_source", "derive_total", "consumed_fraction", "uncertainty", "notes", "review"}
    fields(label, allowed, {"schema_version", "confidence_tier", "source_type", "provenance", "values_kind", "components", "components_complete"}, "ground_truth")
    require(type(label["schema_version"]) is int and label["schema_version"] == 1, "Unknown ground-truth schema version")
    tier = label["confidence_tier"]
    require(tier in {"A", "B", "C"}, "Ground-truth tier must be A, B, or C")
    require(label["source_type"] in STRONG_SOURCES | FACT_SOURCES, "Unsupported ground-truth source type")
    provenance(label["provenance"])
    require(label["values_kind"] in {"measured", "official", "derived", "facts"}, "Unknown values_kind")
    require(type(label["components_complete"]) is bool, "components_complete must be boolean")
    if "derive_total" in label:
        require(type(label["derive_total"]) is bool, "derive_total must be boolean")
    if "consumed_fraction" in label:
        require(tier != "C", "Tier C cannot contain measured fractions")
        require(number(label["consumed_fraction"], "consumed_fraction", positive=True) <= 1, "consumed_fraction must be in (0,1]")
    if label.get("derive_total"):
        require(label["components_complete"], "Derived meal totals require complete components")
    components = label["components"]
    require(isinstance(components, list), "components must be a list")
    require(bool(components) or tier == "A", "Fact labels require components")
    aliases = set()
    measured = False
    for component in components:
        allowed_component = {"name", "aliases", "major", "present", "consumed_weight_g", "consumed_count", "count_unit",
                             "consumed_servings", "consumed_fraction", "preparation", "product", "nutrition", "nutrition_source",
                             "nutrition_reference", "derivation", "notes"}
        fields(component, allowed_component, {"name", "aliases", "major", "present"}, "component")
        text_value(component["name"], "component name")
        require(type(component["major"]) is bool and type(component["present"]) is bool, "major and present must be boolean")
        require(isinstance(component["aliases"], list), "aliases must be a list")
        local = set()
        for alias in [component["name"], *component["aliases"]]:
            text_value(alias, "alias")
            normalized = normalize_name(alias)
            require(normalized not in aliases, "Aliases cannot map to multiple components")
            local.add(normalized)
        aliases.update(local)
        for key in ("consumed_weight_g", "consumed_count", "consumed_servings", "consumed_fraction"):
            if key in component:
                number(component[key], key, positive=True)
                require(component["present"], "Absent components cannot have consumed quantities")
                require(tier != "C", "Tier C cannot contain measured portions or numeric labels")
                measured = True
        if "consumed_fraction" in component:
            require(component["consumed_fraction"] <= 1, "consumed_fraction must be in (0,1]")
        if "consumed_count" in component:
            text_value(component.get("count_unit"), "count_unit")
        if "preparation" in component:
            require(component["preparation"] in {"skin removed", "skin consumed"}, "Unknown confirmed preparation fact")
            measured = True
            require(tier != "C", "Preparation facts require Tier B or A")
        if "product" in component:
            text_value(component["product"], "exact product/brand")
            require(tier != "C", "Exact product facts require Tier B or A")
            measured = True
        if "nutrition" in component or "nutrition_reference" in component:
            require(tier != "C", "Tier C cannot contain numeric nutrition labels")
            require(component["present"], "Absent components cannot have nutrition")
            require(any(k in component for k in ("consumed_weight_g", "consumed_servings", "consumed_count")), "Numeric component labels require known consumed quantity")
            require(label["provenance"]["method"] in METHODS - {"user_confirmation"}, "Numeric labels require measured/known facts")
            macros(component.get("nutrition"), "component nutrition")
            measured = True
            derived = derive_component(component)
            if derived:
                require(component.get("derivation") == derived, "Missing or stale persisted derivation; use label CLI")
                approximately(component["nutrition"], derived["nutrition"], "Component nutrition")
            else:
                text_value(component.get("nutrition_source"), "component nutrition_source")
        else:
            require("derivation" not in component, "Derivation requires a nutrition reference")
    if tier == "B":
        require(measured, "Tier B requires at least one explicit quantity/preparation/product fact")
        require(label["provenance"]["method"] != "user_confirmation", "Tier B requires evidence for a known fact beyond identity")
    if tier == "A":
        require(label["source_type"] in STRONG_SOURCES and label["values_kind"] != "facts", "Tier A requires strong nutrition evidence")
        require(label["provenance"]["method"] in METHODS - {"user_confirmation"}, "Tier A needs measured or official evidence")
        macros(label.get("total"), "meal total")
        require(label.get("derive_total") or label.get("total_source"), "Meal total needs independent numeric provenance")
        if label.get("total_source"):
            text_value(label["total_source"], "total_source")
        require(measured or "consumed_fraction" in label, "Tier A requires known consumed quantity/fraction")
    else:
        require("total" not in label and not label.get("derive_total"), "Only Tier A can support whole-meal numeric accuracy")
    if label.get("components_complete") and "total" in label:
        present = [c for c in components if c["present"]]
        require(bool(present) and all("nutrition" in c for c in present), "Complete numeric components require every consumed macro label")
        approximately(label["total"], {k: sum(c["nutrition"][k] for c in present) for k in MACROS}, "Meal total")
    if "uncertainty" in label:
        uncertainty = label["uncertainty"]
        fields(uncertainty, {"low", "high", "source"}, {"low", "high", "source"}, "uncertainty")
        require(tier == "A", "Macro uncertainty requires Tier A")
        text_value(uncertainty["source"], "uncertainty source")
        for bound in ("low", "high"):
            fields(uncertainty[bound], MACROS, MACROS, "uncertainty bound")
            for key in MACROS:
                number(uncertainty[bound][key], key)
        require(all(uncertainty["low"][k] <= label["total"][k] <= uncertainty["high"][k] for k in MACROS), "Ground-truth uncertainty must enclose total")
    if "review" in label:
        review = label["review"]
        fields(review, {"category", "evidence", "provenance"}, {"category", "evidence", "provenance"}, "review")
        require(review["category"] in FAILURE_MODES, "Unknown human failure category")
        text_value(review["evidence"], "review evidence")
        provenance(review["provenance"])
    if "generic_caption" in case:
        text_value(case["generic_caption"], "generic_caption")
        require(len(case["generic_caption"]) <= 1000, "Generic caption too long")


def fact_rich_caption(case):
    """Only structured, annotated identity/quantity/preparation facts; no macros."""
    validate_ground_truth(case)
    label = case.get("ground_truth", {})
    require(label.get("confidence_tier") in {"A", "B", "C"}, "Fact-rich captions require independently annotated facts")
    parts = []
    for c in label["components"]:
        name = c.get("product", c["name"])
        require(not re.search(r"\b(kcal|calories|protein|carbs|fat|macros)\b", name, re.I), "Do not leak macro answers in fact-rich captions")
        if not c["present"]:
            parts.append(f"no {name}")
            continue
        if "consumed_weight_g" in c:
            name = f"{c['consumed_weight_g']:g}g {name}"
        elif "consumed_count" in c:
            require(c["count_unit"] in {"pieces", "eggs", "items", "slices", "cups"}, "Use a supported count unit for fact-rich captions")
            unit = "" if c["count_unit"] == "eggs" and "eggs" in name.casefold().split() else c["count_unit"] + " "
            name = f"{c['consumed_count']:g} {unit}{name}"
        elif "consumed_servings" in c:
            name = f"{c['consumed_servings']:g} servings {name}"
        if c.get("preparation"):
            name += f", {c['preparation']}"
        parts.append(name)
    caption = "; ".join(parts)
    require(bool(caption) and len(caption) <= 1000, "Fact-rich caption must contain supported facts and fit Lab limit")
    return caption


def require_private_output(path):
    """Generated DB/reports and private labels stay in the established ignored area."""
    from scripts.nutrition_corpus import ROOT
    require(Path(path).resolve().is_relative_to((ROOT / "artifacts/nutrition").resolve()),
            "Evaluation artifacts must remain under ignored artifacts/nutrition/")


def write_json(path, value):
    """Atomic, owner-only local artifact write."""
    import os
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".nutrition-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
