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


def synthetic_mql5_row(slug, actual, forecast, previous):
    """Only the declared web-calendar shape; no production row copied into new fixtures."""
    scheduled = datetime(2026, 10, 29, 12, 30, tzinfo=timezone.utc)
    return {
        "Id": 700001, "CurrencyCode": "USD", "Country": 840,
        "Url": "/en/economic-calendar/united-states/" + slug,
        "EventName": "Synthetic expansion indicator", "TimeMode": 0,
        "FullDate": scheduled.isoformat(), "ReleaseDate": int(scheduled.timestamp()) * 1000,
        "ActualValue": actual, "ForecastValue": forecast, "PreviousValue": previous,
        "Processed": 1 if actual else 0,
    }


@pytest.mark.parametrize("slug,series,frequency,unit,actual,forecast,previous,expected", [
    ("producer-price-index-mm", "US.PPI.HEADLINE.MOM", "monthly", "percent", "-0.2%", "0.1%", "0.0%", ("-0.2", "0.1", "0")),
    ("producer-price-index-yy", "US.PPI.HEADLINE.YOY", "monthly", "percent", "2.1%", "2.2%", "2.0%", ("2.1", "2.2", "2")),
    ("producer-price-index-ex-food-energy-mm", "US.PPI.CORE.MOM", "monthly", "percent", "0.0%", "0.1%", "0.2%", ("0", "0.1", "0.2")),
    ("producer-price-index-ex-food-energy-yy", "US.PPI.CORE.YOY", "monthly", "percent", "2.4%", "2.5%", "2.3%", ("2.4", "2.5", "2.3")),
    ("retail-sales-mm", "US.RETAIL.HEADLINE.MOM", "monthly", "percent", "0.4%", "0.3%", "0.2%", ("0.4", "0.3", "0.2")),
    ("retail-sales-ex-autos-mm", "US.RETAIL.EX_AUTOS.MOM", "monthly", "percent", "0.3%", "0.2%", "0.1%", ("0.3", "0.2", "0.1")),
    ("initial-jobless-claims", "US.JOBLESS.INITIAL.CLAIMS", "weekly", "persons", "210\u00a0K", "205 K", "0.2 M", ("210000", "205000", "200000")),
    ("ism-manufacturing-pmi", "US.ISM.MANUFACTURING.PMI", "monthly", "index", "49.8", "50.1", "49.5", ("49.8", "50.1", "49.5")),
    ("ism-non-manufacturing-pmi", "US.ISM.SERVICES.PMI", "monthly", "index", "52.4", "52.1", "51.8", ("52.4", "52.1", "51.8")),
])
def test_expanded_mql5_series_keep_exact_units_frequency_and_values(monkeypatch, slug, series, frequency, unit, actual, forecast, previous, expected):
    mock_response(monkeypatch, [synthetic_mql5_row(slug, actual, forecast, previous)])
    batch = providers.fetch("mql5", settings("mql5_web"), now=NOW)
    assert len(batch.envelopes) == 1
    assert_contracts(batch)
    item = batch.envelopes[0]["observation"]
    assert item["subject"]["series_id"] == series
    assert item["subject"]["frequency"] == frequency
    assert item["subject"]["unit"] == unit
    assert item["subject"]["scale"] == "units"
    assert item["subject"]["reference_period"] == "unknown"
    assert tuple(item["values"][key] for key in ("actual", "forecast", "previous")) == expected
    assert item["source"]["source_url"] == "https://www.mql5.com/en/economic-calendar/united-states/" + slug


def test_expanded_mql5_scheduled_claims_keep_forecast_without_inventing_actual(monkeypatch):
    mock_response(monkeypatch, [synthetic_mql5_row("initial-jobless-claims", "", "210 K", "205 K")])
    batch = providers.fetch("mql5", settings("mql5_web"), now=NOW)
    assert_contracts(batch)
    assert len(batch.envelopes) == 1
    item = batch.envelopes[0]["observation"]
    assert item["values"]["publication_status"] == "scheduled"
    assert item["values"]["actual"] is None
    assert item["values"]["forecast"] == "210000"


def test_retail_ex_autos_does_not_substitute_ex_autos_and_gas(monkeypatch):
    mock_response(monkeypatch, [synthetic_mql5_row("retail-sales-ex-autos-gas-mm", "0.2%", "0.1%", "0.3%")])
    assert providers.fetch("mql5", settings("mql5_web"), now=NOW).envelopes == []


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
