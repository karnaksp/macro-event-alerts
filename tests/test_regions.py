"""Synthetic regional calendar routing; country, basis and currency cannot mix."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import socket

import pytest

from macro_event_alerts import providers
from macro_event_alerts.catalog import MQL5_REGIONS
from macro_event_alerts.domain import sha256
from macro_event_alerts.validation import Contracts

NOW = datetime(2026, 10, 30, 11, 0, tzinfo=timezone.utc)
SOURCE_REGIONS = {
    "US": (840, "USD", "united-states"),
    "CN": (156, "CNY", "china"),
    "EA": (999, "EUR", "european-union"),
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def reject(*args, **kwargs):
        pytest.fail("regional calendar test attempted a real socket")
    monkeypatch.setattr(socket, "socket", reject)


def settings(countries=None):
    options = {"look_back_days": 1, "look_ahead_days": 2}
    if countries is not None:
        options["countries"] = countries
    return {"adapter": "mql5_web", "experimental": True, "options": options}


def row(region, slug, *, actual="0.5%", forecast="0.4%", previous="0.3%", identifier=710001):
    country_id, currency, path = SOURCE_REGIONS[region]
    scheduled = NOW - timedelta(hours=1) if actual is not None else NOW + timedelta(hours=1)
    return {
        "Id": identifier, "Country": country_id, "CurrencyCode": currency,
        "Url": f"/en/economic-calendar/{path}/{slug}", "EventName": "Synthetic regional indicator",
        "TimeMode": 0, "FullDate": scheduled.isoformat(), "ReleaseDate": int(scheduled.timestamp() * 1000),
        "ActualValue": actual, "ForecastValue": forecast, "PreviousValue": previous,
        "Processed": 0 if actual is None else 1,
    }


def transport(monkeypatch, rows):
    calls = []
    def request(url, **kwargs):
        calls.append((url, kwargs))
        return copy.deepcopy(rows), {}, 200
    monkeypatch.setattr(providers, "_request_json", request)
    return calls


def slug_for(region, series_id):
    return next(slug for slug, series in MQL5_REGIONS[region]["series"].items() if series[0] == series_id)


def assert_valid(batch):
    contracts = Contracts()
    for envelope in batch.envelopes:
        item = envelope["observation"]
        contracts.validate(item, "observation.schema.json")
        assert item["raw_sha256"] == sha256(envelope["raw"])
        assert item["subject"]["reference_period"] == "unknown"
        assert item["source"]["source_updated_at"] is None
        assert item["values"]["published_at"] is None


def test_one_response_routes_same_slug_to_three_independent_regions(monkeypatch):
    rows = []
    for index, region in enumerate(SOURCE_REGIONS):
        monkeypatch.setitem(MQL5_REGIONS[region], "series", {"shared-cpi": (f"{region}.CPI.HEADLINE.MOM", "monthly", "percent")})
        rows.append(row(region, "shared-cpi", identifier=710010 + index))
    calls = transport(monkeypatch, rows)
    batch = providers.fetch("mql5", settings(["US", "CN", "EA"]), now=NOW)
    assert len(calls) == 1
    assert_valid(batch)
    items = {envelope["observation"]["subject"]["country"]: envelope["observation"] for envelope in batch.envelopes}
    assert set(items) == set(SOURCE_REGIONS)
    for region, (_, currency, path) in SOURCE_REGIONS.items():
        item = items[region]
        assert item["subject"]["currency"] == currency
        assert item["subject"]["series_id"] == f"{region}.CPI.HEADLINE.MOM"
        assert item["source"]["source_url"] == f"https://www.mql5.com/en/economic-calendar/{path}/shared-cpi"
        assert item["values"]["actual"] == "0.5"


def test_default_country_option_preserves_us_only_behavior(monkeypatch):
    us_slug = slug_for("US", "US.CPI.HEADLINE.MOM")
    cn_slug = slug_for("CN", "CN.CPI.HEADLINE.MOM")
    transport(monkeypatch, [row("US", us_slug), row("CN", cn_slug, identifier=710002)])
    batch = providers.fetch("mql5", settings(), now=NOW)
    assert [item["observation"]["subject"]["country"] for item in batch.envelopes] == ["US"]


@pytest.mark.parametrize("region,series_id", [
    ("US", "US.GDP.QOQ.ANNUALIZED"),
    ("CN", "CN.GDP.QOQ"),
    ("EA", "EA.GDP.QOQ"),
])
def test_quarterly_gdp_keeps_its_own_country_and_annualization_basis(monkeypatch, region, series_id):
    transport(monkeypatch, [row(region, slug_for(region, series_id))])
    batch = providers.fetch("mql5", settings([region]), now=NOW)
    assert_valid(batch)
    assert len(batch.envelopes) == 1
    subject = batch.envelopes[0]["observation"]["subject"]
    assert subject["series_id"] == series_id
    assert subject["country"] == region
    assert subject["frequency"] == "quarterly"
    assert subject["unit"] == "percent"
    if region != "US":
        assert "ANNUALIZED" not in subject["series_id"]


@pytest.mark.parametrize("patch", [
    {"Country": 840},
    {"CurrencyCode": "USD"},
    {"Url": "/en/economic-calendar/united-states/cpi-mm"},
])
def test_chinese_row_rejects_wrong_country_currency_or_path(monkeypatch, patch):
    item = row("CN", slug_for("CN", "CN.CPI.HEADLINE.MOM"))
    item.update(patch)
    transport(monkeypatch, [item])
    assert providers.fetch("mql5", settings(["US", "CN", "EA"]), now=NOW).envelopes == []


def test_euro_currency_alone_cannot_turn_a_member_country_into_euro_area(monkeypatch):
    item = row("EA", slug_for("EA", "EA.GDP.QOQ"))
    item["Country"] = 250  # France also reports EUR; it is not the area aggregate.
    transport(monkeypatch, [item])
    assert providers.fetch("mql5", settings(["EA"]), now=NOW).envelopes == []


@pytest.mark.parametrize("countries", [["XX"], ["US", "XX"], "CN"])
def test_unsupported_country_option_is_rejected_before_request(monkeypatch, countries):
    calls = transport(monkeypatch, [])
    with pytest.raises(ValueError):
        providers.fetch("mql5", settings(countries), now=NOW)
    assert calls == []


def test_pending_chinese_release_keeps_forecast_without_fabricating_actual(monkeypatch):
    slug = slug_for("CN", "CN.CPI.HEADLINE.YOY")
    transport(monkeypatch, [row("CN", slug, actual=None, forecast="0.0%", previous="0.9%")])
    batch = providers.fetch("mql5", settings(["CN"]), now=NOW)
    assert_valid(batch)
    assert len(batch.envelopes) == 1
    values = batch.envelopes[0]["observation"]["values"]
    assert values["actual"] is None
    assert values["forecast"] == "0"
    assert values["previous"] == "0.9"
    assert values["publication_status"] == "scheduled"


def test_nbs_pmi_is_an_index_and_not_a_percent_series(monkeypatch):
    slug = slug_for("CN", "CN.NBS.MANUFACTURING.PMI")
    transport(monkeypatch, [row("CN", slug, actual="49.8", forecast="49.7", previous="50.1")])
    batch = providers.fetch("mql5", settings(["CN"]), now=NOW)
    assert_valid(batch)
    assert len(batch.envelopes) == 1
    item = batch.envelopes[0]["observation"]
    assert item["subject"]["unit"] == "index"
    assert item["subject"]["series_id"] == "CN.NBS.MANUFACTURING.PMI"
    assert item["values"]["actual"] == "49.8"
