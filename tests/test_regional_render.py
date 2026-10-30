"""Countries stay distinct in the digest and retain their own economic meaning."""
from __future__ import annotations

import re

import pytest

from macro_event_alerts.render import render, render_snapshot_messages
from test_render import MessageHTML, fixture, macro_snapshot


AT = "2026-10-30T11:00:00Z"


def observation(country: str, series: str, *, actual="3.1", label=None):
    item = macro_snapshot(series, actual=actual, label=label)
    item["subject"].update(country=country, currency={"US": "USD", "CN": "CNY", "RU": "RUB", "EA": "EUR"}[country])
    return item


def pages(items):
    return [MessageHTML(message) for message in render_snapshot_messages(items, at=AT)]


def test_identical_series_and_values_in_different_countries_are_not_deduplicated():
    names = {"US": "США", "CN": "Китай", "RU": "Россия", "EA": "Еврозона"}
    items = [observation(country, "CUSTOM.SAME", label=f"Показатель {country}") for country in names]
    rendered = pages(items)
    assert len(rendered) == 4
    for message, (country, name) in zip(rendered, names.items()):
        assert name in message.visible.splitlines()[0]
        assert f"Показатель {country}" in message.visible and "3,1%" in message.visible
        assert all(f"Показатель {other}" not in message.visible for other in names if other != country)
        if country != "US":
            assert "США" not in message.visible


def test_each_country_has_its_own_page_numbering_and_no_lost_rows():
    china = [observation("CN", f"CUSTOM.LONG.{index}", label=f"Китайский показатель {index:02d} " + "🔥" * 65)
             for index in range(20)]
    items = [observation("US", "US.CPI.HEADLINE.YOY"), *china,
             observation("EA", "EA.HICP.HEADLINE.YOY")]
    messages = render_snapshot_messages(items, at=AT)
    china_pages = [message for message in messages if "Китай" in MessageHTML(message).visible.splitlines()[0]]
    assert len(china_pages) > 1
    for index, message in enumerate(china_pages, 1):
        assert f"{index}/{len(china_pages)}" in MessageHTML(message).visible.splitlines()[0]
    for message in messages:
        assert len(message.encode("utf-16-le")) // 2 <= 4096
        MessageHTML(message)
    visible = "\n".join(MessageHTML(message).visible for message in messages)
    assert all(visible.count(f"Китайский показатель {index:02d}") == 1 for index in range(20))
    assert messages == render_snapshot_messages(list(reversed(items)), at=AT)


def test_fed_scenario_is_rendered_once_and_only_with_us():
    scenario = fixture("scenario", observation=True)
    scenario["subject"].update(label="Сценарий ФРС", outcome_label="Yes")
    rendered = pages([scenario, observation("CN", "CN.CPI.HEADLINE.YOY")])
    scenario_pages = [message for message in rendered if "Сценарий ФРС" in message.visible]
    assert len(scenario_pages) == 1
    assert "США" in scenario_pages[0].visible.splitlines()[0]
    china_page = next(message for message in rendered if "Китай" in message.visible.splitlines()[0])
    assert "Сценарий ФРС" not in china_page.visible


def test_country_grouping_keeps_untrusted_labels_and_source_urls_escaped():
    label = '<i>Китай & "тест"</i>'
    item = observation("CN", "CUSTOM.ESCAPING", label=label)
    item["source"]["source_url"] = "https://example.com/cn?a=1&b=%22test%22"
    raw = render_snapshot_messages([item], at=AT)[0]
    message = MessageHTML(raw)
    assert label in message.visible
    assert "&lt;i&gt;" in raw and "&amp;" in raw
    assert message.links == [item["source"]["source_url"]]
    assert "США" not in message.visible


@pytest.mark.parametrize("country,series", [("CN", "CN.GDP.QOQ"), ("EA", "EA.GDP.QOQ")])
def test_non_us_gdp_label_does_not_claim_annualized_growth(country, series):
    event = fixture("macro.release")
    event["subject"].update(country=country, series_id=series,
                            currency="CNY" if country == "CN" else "EUR")
    text = MessageHTML(render(event, {}, dry_run=False)).visible
    assert "ВВП" in text
    assert "годовой темп" not in text and "ANNUALIZED" not in text
    assert "США" not in text


def test_ecb_deposit_and_refinancing_rates_are_distinct_rows():
    rendered = pages([observation("EA", "EA.ECB.DEPOSIT.RATE", actual="2.5"),
                      observation("EA", "EA.ECB.MAIN_REFINANCING.RATE", actual="2.65")])
    visible = "\n".join(message.visible for message in rendered)
    assert "Еврозона" in visible
    assert "депозит" in visible.lower() and "рефинансирован" in visible.lower()
    assert "2,5%" in visible and "2,65%" in visible
    assert len([link for message in rendered for link in message.links]) == 2


def test_russian_cpi_and_key_rate_keep_two_percentage_rows():
    rendered = pages([observation("RU", "RU.CBR.KEY_RATE", actual="15"),
                      observation("RU", "RU.CPI.HEADLINE.YOY", actual="6.6")])
    visible = "\n".join(message.visible for message in rendered)
    assert "Россия" in visible.splitlines()[0]
    assert "Ключевая ставка" in visible and "Инфляция" in visible
    assert "15%" in visible and "6,6%" in visible
    assert "США" not in visible
    assert not re.search(r"Факт:\s*(?:15|6,6)\s*(?:пунктов|чел)", visible)
