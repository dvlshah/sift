"""Validate every facts/*.json against its declared $schema.

Walks <run>/facts/, reads each JSON, finds the matching schema in
<run>/facts/schemas/<$schema>.json, and validates with `jsonschema`. Reports
invalid files + per-required-field error counts.

Doesn't try to be clever about partial validity — a missing required field
or wrong type is a hard fail. Schemas are versioned (the `$schema` field
in each fact is the schema's `$id`), so a future schema bump won't
spuriously invalidate old facts.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

try:
    from jsonschema import Draft202012Validator
    _HAVE_JSONSCHEMA = True
except ImportError:  # pragma: no cover - we add jsonschema as a dep
    Draft202012Validator = None  # type: ignore[assignment]
    _HAVE_JSONSCHEMA = False


@dataclass
class FactInvalid:
    path: str
    schema_id: Optional[str]
    errors: list[str]


@dataclass
class FactsValidationMetrics:
    run_id: str
    schemas_found: int = 0
    facts_total: int = 0
    facts_valid: int = 0
    facts_invalid: int = 0
    facts_no_schema: int = 0
    by_schema_total: dict[str, int] = field(default_factory=dict)
    by_schema_invalid: dict[str, int] = field(default_factory=dict)
    invalid_examples: list[FactInvalid] = field(default_factory=list)
    # Top missing-field reasons across all invalid files
    top_error_messages: list[tuple[str, int]] = field(default_factory=list)


def _load_schemas(schemas_dir: Path) -> dict[str, dict]:
    """Map $id -> schema dict."""
    out: dict[str, dict] = {}
    if not schemas_dir.exists():
        return out
    for f in schemas_dir.glob("*.json"):
        try:
            sch = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        sid = sch.get("$id") or f.stem
        out[sid] = sch
    return out


def run(root: Path, run_id: str) -> FactsValidationMetrics:
    metrics = FactsValidationMetrics(run_id=run_id)
    facts_root = root / "runs" / run_id / "facts"
    if not facts_root.exists():
        return metrics

    schemas = _load_schemas(facts_root / "schemas")
    metrics.schemas_found = len(schemas)

    error_msgs: Counter[str] = Counter()

    for f in facts_root.rglob("*.json"):
        if "schemas" in f.parts:
            continue
        metrics.facts_total += 1
        try:
            payload = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            metrics.facts_invalid += 1
            metrics.invalid_examples.append(FactInvalid(
                path=str(f.relative_to(root)),
                schema_id=None,
                errors=[f"json-parse: {e}"],
            ))
            continue

        schema_id = payload.get("$schema")
        if not schema_id:
            metrics.facts_no_schema += 1
            metrics.facts_invalid += 1
            metrics.invalid_examples.append(FactInvalid(
                path=str(f.relative_to(root)), schema_id=None,
                errors=["missing $schema field"],
            ))
            continue

        metrics.by_schema_total[schema_id] = (
            metrics.by_schema_total.get(schema_id, 0) + 1
        )

        if schema_id not in schemas:
            metrics.facts_invalid += 1
            metrics.by_schema_invalid[schema_id] = (
                metrics.by_schema_invalid.get(schema_id, 0) + 1
            )
            if len(metrics.invalid_examples) < 20:
                metrics.invalid_examples.append(FactInvalid(
                    path=str(f.relative_to(root)),
                    schema_id=schema_id,
                    errors=[f"schema '{schema_id}' not found in facts/schemas/"],
                ))
            continue

        if not _HAVE_JSONSCHEMA:
            # Fall back to a manual required-fields check
            errs = [
                f"missing required: {k}"
                for k in schemas[schema_id].get("required", [])
                if k not in payload
            ]
        else:
            v = Draft202012Validator(schemas[schema_id])
            errs = [e.message for e in v.iter_errors(payload)]

        if errs:
            metrics.facts_invalid += 1
            metrics.by_schema_invalid[schema_id] = (
                metrics.by_schema_invalid.get(schema_id, 0) + 1
            )
            for msg in errs[:5]:
                error_msgs[msg] += 1
            if len(metrics.invalid_examples) < 20:
                metrics.invalid_examples.append(FactInvalid(
                    path=str(f.relative_to(root)),
                    schema_id=schema_id, errors=errs[:5],
                ))
        else:
            metrics.facts_valid += 1

    metrics.top_error_messages = error_msgs.most_common(10)
    return metrics


def to_dict(m: FactsValidationMetrics) -> dict:
    return asdict(m)
