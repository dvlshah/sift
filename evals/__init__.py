"""Baseline metrics eval suite for the sift pipeline.

The evaluations here are intentionally **deterministic-where-possible** and
**versioned**: every result is keyed on (run_id, eval_version) so cross-run
comparisons make sense. The `baseline` orchestrator stitches them together
into one `baseline_report.json` per index snapshot.

Each eval module exports a `run(...)` function that returns a dataclass
serializable to JSON. The CLI calls them with consistent paths, then writes
to <root>/evals/<run_id>/<eval-name>.json.
"""

EVAL_VERSION = "v1"
