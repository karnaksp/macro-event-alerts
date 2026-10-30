"""Offline contract examples and negative cases using shared runtime validation."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from macro_event_alerts.validation import semantics, config_semantics

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "contracts/v1"


def main() -> None:
    schemas = {}
    registry = Registry()
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        data = json.loads(path.read_text())
        Draft202012Validator.check_schema(data)
        schemas[path.name] = data
        registry = registry.with_resource(data["$id"], Resource.from_contents(data))
    validators = {
        name: Draft202012Validator(data, registry=registry, format_checker=FormatChecker())
        for name, data in schemas.items()
    }

    def check(data: dict, schema: str) -> tuple[str | None, list[str]]:
        schema_errors = list(validators[schema].iter_errors(data))
        if schema_errors:
            return "schema", [f"{list(e.absolute_path)}: {e.message}" for e in schema_errors]
        errors = config_semantics(data) if schema == "config.schema.json" else semantics(data)
        return ("semantic" if errors else None), errors

    positives = 0
    for directory, schema in (("events", "event.schema.json"), ("observations", "observation.schema.json")):
        for path in sorted((ROOT / "examples" / directory).glob("*.json")):
            category, errors = check(json.loads(path.read_text()), schema)
            if category:
                raise SystemExit(f"Invalid positive fixture {path.relative_to(ROOT)}: {errors}")
            positives += 1
    config = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    category, errors = check(config, "config.schema.json")
    if category:
        raise SystemExit(f"Invalid example config: {errors}")
    cases = json.loads((ROOT / "examples/invalid/cases.json").read_text())
    for case in cases:
        path = ROOT / case["fixture"]
        data = copy.deepcopy(yaml.safe_load(path.read_text()) if path.suffix == ".yaml" else json.loads(path.read_text()))
        for patch in case["patches"]:
            target = data
            keys = patch["path"]
            for key in keys[:-1]:
                target = target[key]
            if patch.get("remove"):
                del target[keys[-1]]
            else:
                target[keys[-1]] = patch["value"]
        category, errors = check(data, case["schema"])
        if category != case["expected"]:
            raise SystemExit(f"Negative case {case['name']!r}: expected {case['expected']}, got {category}: {errors}")
    print(f"PASS: {len(schemas)} schemas, {positives} positive fixtures, 1 config, {len(cases)} negative cases.")
    print("No live APIs, runtime transitions, Docker build or Telegram delivery were tested.")


if __name__ == "__main__":
    main()
