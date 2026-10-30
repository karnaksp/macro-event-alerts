"""Provider mapping tests use captured public responses; no sockets or secrets."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from macro_event_alerts import providers
from macro_event_alerts.domain import sha256
from macro_event_alerts.validation import Contracts

NOW = datetime(2026, 10, 30, 11, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures/providers"


def rows(name):
    return json.loads((FIXTURES / name).read_text())


def mock_response(monkeypatch, data, *, headers=None, status=200):
    calls = []
    def response(url, **kwargs):
        calls.append((url, kwargs))
        return copy.deepcopy(data), headers or {}, status
    monkeypatch.setattr(providers, "_request_json", response)
    return calls


def settings(adapter):
    return {"adapter": adapter, "experimental": adapter == "mql5_web", "options": {"look_back_days": 30, "look_ahead_days": 1}}


def assert_contracts(batch):
    contracts = Contracts()
    for envelope in batch.envelopes:
        observation = envelope["observation"]
        contracts.validate(observation, "observation.schema.json")
        assert observation["raw_sha256"] == sha256(envelope["raw"])
        assert observation["source"]["source_updated_at"] is None
        assert observation["values"]["published_at"] is None


def test_mql5_maps_actual_forecast_pending_and_rate_upper_without_invented_period(monkeypatch):
    calls = mock_response(monkeypatch, rows("mql5_calendar.json"))
    batch = providers.fetch("mql5", settings("mql5_web"), now=NOW)
    assert len(calls) == 1
    assert_contracts(batch)
    subjects = {e["observation"]["subject"]["series_id"]: e["observation"] for e in batch.envelopes}
    core = subjects["US.PCE.CORE.MOM"]
    assert core["values"]["actual"] == "0.3"
    assert core["values"]["forecast"] == "0.2"
    assert core["subject"]["reference_period"] == "unknown"
    assert core["subject"]["release_key"].startswith("mql5:")
    assert subjects["US.ECI.QOQ"]["values"]["publication_status"] == "scheduled"
    assert subjects["US.ECI.QOQ"]["values"]["actual"] is None
    assert subjects["US.FED.TARGET.UPPER"]["values"]["actual"] == "4"
    assert "US.FED.TARGET.MIDPOINT" not in subjects


def test_mql5_opt_in_checked_before_network(monkeypatch):
    calls = mock_response(monkeypatch, [])
    with pytest.raises(ValueError, match="experimental"):
        providers.fetch("mql5", {"adapter": "mql5_web", "experimental": False}, now=NOW)
    assert calls == []


@pytest.mark.parametrize("patch", [
    {"ActualValue": "not-a-number"},
    {"ActualValue": "0.3"},  # MQL5 percent measurements require its explicit % basis.
    {"ActualValue": "NaN%"},
    {"TimeMode": 1},
    {"ReleaseDate": 1},
    {"CurrencyCode": "EUR"},
    {"Url": "https://attacker.example/path"},
    {"Url": 123},
])
def test_invalid_mql5_row_cannot_become_a_misleading_observation(monkeypatch, patch):
    row = next(row for row in rows("mql5_calendar.json") if row["EventName"] == "Core PCE Price Index m/m")
    row.update(patch)
    mock_response(monkeypatch, [row])
    assert providers.fetch("mql5", settings("mql5_web"), now=NOW).envelopes == []


def test_web_number_scale_is_display_units_not_mt5_million_multiplier():
    assert providers._number("0.0%", "percent", suffixed=True) == "0"
    assert providers._number("29\u00a0K", "persons", suffixed=True) == "29000"
    assert providers._number("-1.2 M", "persons", suffixed=True) == "-1200000"
    assert providers._number("", "percent", suffixed=True) is None


def test_calendar_filter_is_applied_locally_and_query_window_bounded(monkeypatch):
    mock_response(monkeypatch, rows("mql5_calendar.json"))
    config = settings("mql5_web")
    config["options"] = {"look_back_days": 0, "look_ahead_days": 0}
    batch = providers.fetch("mql5", config, now=NOW)
    assert len(batch.envelopes) == 1  # ECI scheduled today; old PCE/GDP/FOMC filtered locally.
    config["options"] = {"look_back_days": 32}
    with pytest.raises(ValueError, match="31"):
        providers.fetch("mql5", config, now=NOW)


def test_xoomar_explicit_periods_units_and_one_request(monkeypatch):
    calls = mock_response(monkeypatch, rows("xoomar_calendar.json"), headers={"ETag": '"calendar-v1"'})
    batch = providers.fetch("xoomar", settings("xoomar"), now=NOW)
    assert len(calls) == 1
    assert_contracts(batch)
    values = {e["observation"]["subject"]["series_id"]: e["observation"] for e in batch.envelopes}
    assert values["US.GDP.QOQ.ANNUALIZED"]["subject"]["reference_period"] == "2026-Q3"
    assert values["US.CPI.HEADLINE.YOY"]["subject"]["reference_period"] == "2026-09"
    payrolls = values["US.NFP.CHANGE"]
    assert payrolls["subject"]["scale"] == "thousands"
    assert payrolls["values"]["actual"] == "29"
    assert values["US.FED.TARGET.MIDPOINT"]["values"]["actual"] == "3.875"
    assert all(item["values"]["forecast"] is None for item in values.values())
    assert "macro.forecast_changed" not in providers.capabilities("xoomar")
    assert batch.checkpoint["etag"] == '"calendar-v1"'


def test_xoomar_etag_reused_only_for_identical_window(monkeypatch):
    calls = mock_response(monkeypatch, rows("xoomar_calendar.json"), headers={"ETag": '"calendar-v1"'})
    first = providers.fetch("xoomar", settings("xoomar"), now=NOW)
    providers.fetch("xoomar", settings("xoomar"), now=NOW, checkpoint=first.checkpoint)
    assert calls[-1][1]["headers"]["If-None-Match"] == '"calendar-v1"'
    different = settings("xoomar")
    different["options"] = {}
    providers.fetch("xoomar", different, now=NOW, checkpoint=first.checkpoint)
    assert calls[-1][1]["headers"] == {}
    mock_response(monkeypatch, None, status=304)
    assert providers.fetch("xoomar", settings("xoomar"), now=NOW, checkpoint=first.checkpoint).envelopes == []


def test_unknown_units_or_missing_reference_period_are_not_guessed(monkeypatch):
    data = rows("xoomar_calendar.json")
    cpi = next(row for row in data["data"] if row["eventName"].startswith("CPI"))
    cpi["unit"] = "% m/m"
    cpi["periodLabel"] = None
    data["data"] = [cpi]
    mock_response(monkeypatch, data)
    assert providers.fetch("xoomar", settings("xoomar"), now=NOW).envelopes == []
