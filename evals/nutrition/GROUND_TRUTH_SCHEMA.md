# Ground-truth contract, version 1

The executable schema and arithmetic validator is
`scripts/nutrition_groundtruth.py`. `load_cases` invokes it after validating the
image bytes and hash. Labels are optional. `label_status: ground_truth` requires
a valid `ground_truth` object. Unknown label fields fail rather than being ignored.

| Field | Required meaning |
| --- | --- |
| `schema_version` | Integer `1` |
| `confidence_tier` | `A`: whole-meal numeric evidence; `B`: independent component facts; `C`: confirmed identity/presence only |
| `source_type` | `weighed_meal`, `cooked_weight`, `packaged_food`, `official_restaurant`, `weighed_recipe`, `component_facts`, `identity_confirmation` |
| `provenance` | `date` (ISO date), `source` (annotator/evidence origin), `method`, `reference` (independent evidence location or exact known fact) |
| `values_kind` | `measured`, `official`, `derived`, or `facts` |
| `components` | List of labelled components, possibly incomplete |
| `components_complete` | Explicit boolean: whether every consumed major component is independently labelled; false is the conservative default |
| `total` | A complete calories/protein/carbs/fat object, allowed only at Tier A |
| `total_source` | Independent source for a manually supplied meal total |
| `derive_total` | Optional boolean; derive a total only if every consumed component has numeric evidence |
| `consumed_fraction` | Optional known fraction of the labelled meal/recipe, greater than zero and at most one |
| `uncertainty` | Optional `low`, `high` macro objects enclosing `total`, plus an independent `source` |
| `notes` | Annotation limitations; never a substitute for required evidence |
| `review` | Optional human-assigned `category`, `evidence`, and `provenance` |

Provenance methods: `human_measurement`, `official_label`, `official_restaurant`,
`weighed_recipe`, `user_known_fact`, `user_confirmation`. A model answer, visual
guess, source dish title, logged estimate, or generic database entry without
known consumed quantity is never admissible numeric evidence. The CLI does not
accept model result objects and cannot promote runs to labels. Validation checks
structure and arithmetic; a human remains responsible for the truth of the
source declaration. Falsely describing a model answer as a measurement cannot
be detected from a JSON object alone.

Each component requires `name`, `aliases` (possibly empty), `major` and `present`
(booleans). Optional measured fields are `consumed_weight_g`, `consumed_count`
with `count_unit`, `consumed_servings`, `consumed_fraction`, and `preparation`
(`skin removed` or `skin consumed`), and `product` (independently known exact
brand/product). Tier C forbids quantities and nutrition.
Tier B requires an explicit quantity, preparation or product fact. Absent components
cannot have consumed quantities or nutrition.

Numeric components require a known consumed quantity and either:

- `nutrition`: the consumed macros, plus `nutrition_source`; or
- `nutrition_reference`: `source`, `basis` (`per_100g` or `per_serving`), and
  reference `macros`. Per-serving references may include `serving_weight_g`.

All macro objects contain `calories`, `protein_g`, `carbs_g`, and `fat_g`.
Quantities must be finite and positive; macros must be finite and nonnegative.
Booleans are not numbers. All four macros are required for a numeric object.

## Transparent arithmetic

The label-entry step persists the reference, `derivation.factor`,
`derivation.expression`, `derivation.nutrition`, and consumed `nutrition`.
Validation recomputes them and rejects stale arithmetic. For example, the
arithmetic **180 g × 130 kcal / 100 g = 234 kcal** is deterministic; this example
is not a label for a real corpus photograph.

- Per 100 g: `consumed_weight_g / 100 × reference macros`.
- Per serving: `consumed_servings × reference macros`, or
  `consumed_weight_g / serving_weight_g × reference macros`.
- Whole meal: sum all consumed component totals, only when complete.
- A recipe's ingredient quantities must already represent the consumed
  fraction. Fraction fields record provenance; they are **not multiplied a
  second time**. The user supplies the consumed quantities explicitly.
- Official supplied totals are preserved. Disagreement with component/reference
  arithmetic fails; it never silently replaces an official value.

The 4/4/9 check allows `max(20 kcal, 20% of the larger energy value)` for label
rounding and fibre. Component/reference and meal-sum checks allow
`max(5 kcal or 0.5 g, 2% of the expected value)` per nutrient. These are benchmark
label checks, independent of the unchanged production reconciliation rules.
Unusual official labels outside these tolerances require investigation and a
documented schema change; they cannot bypass validation with a free-text note.

## Matching and scoring

Aliases use exact case-folded names with collapsed whitespace. No fuzzy,
substring, semantic, or LLM matching occurs. Overlapping aliases across components
are rejected. Multiple matching estimator items are ambiguous, so their portions
are not summed or force-matched. Keep a changed alias label as a new revision;
do not silently rematch a frozen baseline after seeing its answers.

Presence checks count expected major components found, missed from the reported
items, or ambiguously matched. Missing labels can reflect wording differences;
they do not automatically establish a food-identification failure. Introduced
components require explicit absence labels or exhaustive component coverage.
Other items are unverified. For exhaustive labels, an unmatched item is considered
major at `max(25 kcal, 5% of estimated meal calories)`; minor garnish is unscored.
This threshold is a transparent evaluation convention, not a production rule.

Portion error needs consumed edible grams and a unique matched item. The current
estimator has no structured item count; known egg/serving counts alone cannot
produce gram errors. They remain useful facts for caption experiments.

Tier A yields signed error, absolute error and signed/absolute percentage error
for all four macros. Percentages are null below 10 actual kcal or 1 actual gram.
Low/high coverage is inclusive and reported beside interval width. Where truth
has documented uncertainty, per-run reports also show whether the model interval
overlaps or fully contains that uncertainty. There is no nominal confidence
level associated with these intervals, so coverage is descriptive calibration.

Across cases, metrics give each case equal weight: average errors over its
successful repeats first, then aggregate case means. Medians are across those
case means; MAPE denominators and scored counts are explicit. Worst cases remain
listed. Missing data stays null. Failures remain visible and never count as hits.
Caption comparisons use the shared case/image/label set per model and application
version, within one fixed user-context batch. Every raw run retains its label
snapshot. Reports flag CV <= 10% with absolute signed bias >= 20%, explicitly
separating repeatability from accuracy.

Human review categories: `food_misidentification`, `portion_depth`, `hidden_oil`,
`sauce_gravy`, `hidden_base`, `bone_inedible_weight`, `mixed_dish`, `occlusion`,
`incomplete_caption`, `unsupported_assumption`, `other`. Require evidence and
human provenance. Estimator assumptions and model-written failure explanations
are not authoritative reviews.
