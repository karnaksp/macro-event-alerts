"""Offline contract checks. This is not the runtime detector or an API smoke test."""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "contracts/v1"
MACRO_IDENTITY = (
    "kind", "country", "currency", "series_id", "release_key", "reference_period",
    "frequency", "adjustment", "unit", "scale", "transformation",
)
SCENARIO_IDENTITY = (
    "kind", "contract_id", "outcome_id", "horizon_at", "resolution_hash", "price_basis",
)


def subject_key(event: dict) -> str:
    subject = event["subject"]
    fields = MACRO_IDENTITY if subject["kind"] == "macro" else SCENARIO_IDENTITY
    identity = {
        "provider_id": event["source"]["provider_id"],
        "subject": {field: subject[field] for field in fields},
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def semantics(data: dict) -> list[str]:
    """Only static invariants; no baseline, network, database or delivery checks."""
    errors = []
    if "event_type" not in data:
        if data.get("subject", {}).get("kind") == "scenario":
            values = data["values"]
            if instant(values["sample_at"]) > instant(data["observed_at"]):
                errors.append("sample_at cannot follow observed_at")
            bid, ask = values["best_bid"], values["best_ask"]
            if bid is not None and ask is not None:
                if Decimal(bid) > Decimal(ask):
                    errors.append("best_bid cannot exceed best_ask")
                if data["subject"]["price_basis"] == "midpoint":
                    if Decimal(values["probability"]) != (Decimal(bid) + Decimal(ask)) / 2:
                        errors.append("midpoint observation must match current book")
            elif data["subject"]["price_basis"] == "midpoint":
                errors.append("midpoint observation requires both book sides")
        return errors
    if data["subject_key"] != subject_key(data):
        errors.append("subject_key does not match canonical identity")
    payload = data["payload"]
    event_type = data["event_type"]
    if event_type in ("macro.forecast_changed", "scenario.probability_changed"):
        if Decimal(payload["before"]) == Decimal(payload["after"]):
            errors.append("change event has equal before and after")
    if event_type == "macro.revised":
        if Decimal(payload["before_actual"]) == Decimal(payload["after_actual"]):
            errors.append("revision has equal before_actual and after_actual")
    if "before_observed_at" in payload:
        before = instant(payload["before_observed_at"])
        after = instant(payload["after_observed_at"])
        if before >= after:
            errors.append("before_observed_at must precede after_observed_at")
        if after != instant(data["observed_at"]):
            errors.append("event observed_at must match after_observed_at")
    if event_type == "scenario.probability_changed":
        expected_pp = (Decimal(payload["after"]) - Decimal(payload["before"])) * 100
        if Decimal(payload["delta_pp"]) != expected_pp:
            errors.append("delta_pp must equal (after - before) * 100")
        for prefix in ("before", "after"):
            if instant(payload[f"{prefix}_sample_at"]) > instant(payload[f"{prefix}_observed_at"]):
                errors.append(f"{prefix}_sample_at cannot follow observation")
        bid, ask = payload["best_bid"], payload["best_ask"]
        if bid is not None and ask is not None:
            if Decimal(bid) > Decimal(ask):
                errors.append("best_bid cannot exceed best_ask")
            if data["subject"]["price_basis"] == "midpoint":
                if Decimal(payload["after"]) != (Decimal(bid) + Decimal(ask)) / 2:
                    errors.append("midpoint must match current book")
        elif data["subject"]["price_basis"] == "midpoint":
            errors.append("midpoint requires both book sides")
    return errors


def config_semantics(config: dict) -> list[str]:
    errors = []
    known = set(config["providers"])
    seen = set()
    for provider_id, provider in config["providers"].items():
        if provider["adapter"] == "mql5_web" and not provider["experimental"]:
            errors.append(f"mql5_web must be experimental: {provider_id}")
    for rule in config["rules"]:
        if rule["id"] in seen:
            errors.append(f"duplicate rule ID: {rule['id']}")
        seen.add(rule["id"])
        if not set(rule["provider_ids"]) <= known:
            errors.append(f"unknown provider in rule: {rule['id']}")
        scenario = any(t.startswith("scenario.") for t in rule["event_types"])
        macro = any(t.startswith("macro.") for t in rule["event_types"])
        if scenario and macro:
            errors.append(f"mixed macro/scenario rule: {rule['id']}")
        if scenario and (not rule["contract_ids"] or rule["series_ids"]):
            errors.append(f"scenario rule requires only contract IDs: {rule['id']}")
        if macro and (not rule["series_ids"] or rule["contract_ids"]):
            errors.append(f"macro rule requires only series IDs: {rule['id']}")
        if scenario and (rule["min_change_pp"] is None or Decimal(rule["min_change_pp"]) <= 0):
            errors.append(f"scenario threshold must be positive: {rule['id']}")
        if macro and rule["min_change_pp"] is not None:
            errors.append(f"macro threshold in pp is unsupported: {rule['id']}")
        if rule["enabled"]:
            if any(not config["providers"][p]["enabled"] for p in rule["provider_ids"] if p in known):
                errors.append(f"enabled rule uses disabled provider: {rule['id']}")
            if any("REPLACE_" in c for c in rule["contract_ids"]):
                errors.append(f"enabled rule uses placeholder contract: {rule['id']}")
    return errors


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
