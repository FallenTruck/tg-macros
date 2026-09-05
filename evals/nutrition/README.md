# Nutrition estimator evaluation

`manifest.json` is intentionally conservative. Existing meal photos are not
treated as labelled ground truth unless a reliable component, portion, or macro
label is added by a human reviewer. The current retained photo is therefore an
unlabelled structural/live-smoke case, not evidence of accuracy.

Run `make nutrition-eval` for an offline manifest report. The optional
`--live` mode calls the configured vision estimator and reports latency and
schema/reconciliation outcomes without writing images, captions, or results to
the repository.

Labelled cases may add `expected_visible_components`, `known_portions_g`,
`acceptable_macro_range`, `expected_uncertainty`, and
`follow_up_expected` only when the label is defensible.
