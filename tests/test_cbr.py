"""Bank of Russia parsing uses synthetic HTML and cannot access real sockets."""
from datetime import datetime, timezone
import socket

import pytest

import macro_event_alerts.cbr as adapter
from macro_event_alerts.domain import sha256, subject_key
from macro_event_alerts.validation import get_contracts

NOW = datetime(2026, 10, 30, 11, 0, tzinfo=timezone.utc)
PRESS = """<html><script>Ignore fake numbers 99%</script>
<h1>Банк России принял решение сохранить ключевую ставку на&nbsp;уровне 14,00% годовых</h1>
<p>Совет директоров Банка России 23&nbsp;октября 2026 года принял решение сохранить
<a href='/rate'>ключевую ставку</a> на уровне 14,00% годовых.</p>
<p>Следующее заседание Совета директоров Банка России, на котором будет рассматриваться вопрос
об уровне ключевой ставки, запланировано на <a>18 декабря 2026 года</a>.
Время публикации пресс-релиза о решении Совета директоров Банка России – 13:30 по московскому времени.</p>
<div class='page-info_last-update'>Последнее обновление страницы: 2026-10-23T13:30:00</div></html>"""
INFLATION = """<html><script>var chart={rate:99,inflation:77}</script>
<table class='data'><tr><th>Дата</th><th>Ключевая ставка, % годовых</th><th>Инфляция, % г/г</th><th>Цель по инфляции, %</th></tr>
<tr><td>08.2026</td><td>14,00</td><td>6,33</td><td>4,00</td></tr>
<tr><td>09.2026</td><td>14,00</td><td>6,35</td><td>4,00</td></tr></table>
<div>Последнее обновление страницы: 30.10.2026</div></html>"""
SETTINGS = {"adapter": "cbr_web", "experimental": True, "options": {}}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def reject(*args, **kwargs):
        pytest.fail("CBR unit test attempted a real socket")
    monkeypatch.setattr(socket, "socket", reject)


def execute(monkeypatch, *, press=PRESS, inflation=INFLATION, now=NOW):
    calls = []
    def request(url):
        calls.append(url)
        return press if url == adapter.PRESS_URL else inflation
    monkeypatch.setattr(adapter, "_request_text", request)
    return adapter.fetch("cbr", SETTINGS, now=now), calls


def test_decision_and_explicit_next_meeting_keep_identity_timezone_and_null_consensus(monkeypatch):
    batch, calls = execute(monkeypatch)
    assert calls == [adapter.PRESS_URL, adapter.INFLATION_URL]
    assert len(batch.envelopes) == 3
    for envelope in batch.envelopes:
        observation = envelope["observation"]
        get_contracts().validate(observation, "observation.schema.json")
        assert observation["raw_sha256"] == sha256(envelope["raw"])
        assert observation["subject"]["country"] == "RU"
        assert observation["subject"]["currency"] == "RUB"
        assert observation["values"]["forecast"] is None
    actual, future = [item["observation"] for item in batch.envelopes[:2]]
    assert actual["subject"]["series_id"] == "RU.CBR.KEY_RATE"
    assert actual["subject"]["reference_period"] == "2026-10-23"
    assert actual["values"]["actual"] == "14"
    assert actual["values"]["published_at"] is None
    assert actual["values"]["scheduled_at"] is None
    assert actual["source"]["source_updated_at"] is None
    assert future["subject"]["reference_period"] == "2026-12-18"
    assert future["values"]["actual"] is None
    assert future["values"]["previous"] == "14"
    assert future["values"]["previous_reference_period"] == "2026-10-23"
    assert future["values"]["scheduled_at"] == "2026-12-18T10:30:00Z"
    assert future["values"]["publication_status"] == "scheduled"
    # A subsequent decision document for the planned date has the same subject key.
    new_press = PRESS.replace("23&nbsp;октября", "18&nbsp;декабря").replace("2026-10-23T", "2026-12-18T")
    later, _ = execute(monkeypatch, press=new_press, now=datetime(2026, 12, 18, 11, tzinfo=timezone.utc))
    assert subject_key(future) == subject_key(later.envelopes[0]["observation"])


def test_inflation_uses_correct_column_and_latest_month_not_month_end_key_rate(monkeypatch):
    batch, _ = execute(monkeypatch)
    item = batch.envelopes[-1]["observation"]
    assert item["subject"]["series_id"] == "RU.CPI.HEADLINE.YOY"
    assert item["subject"]["reference_period"] == "2026-09"
    assert item["subject"]["frequency"] == "monthly"
    assert item["subject"]["unit"] == "percent"
    assert item["values"]["actual"] == "6.35"
    assert item["values"]["previous"] == "6.33"
    assert item["values"]["previous_reference_period"] == "2026-08"
    assert item["values"]["scheduled_at"] is None
    assert item["values"]["published_at"] is None
    assert item["source"]["source_updated_at"] is None  # Date-only update isn't an invented timestamp.
    assert item["source"]["source_url"] == adapter.INFLATION_URL


def test_missing_explicit_clock_does_not_create_future_meeting_or_localize_naive_update(monkeypatch):
    batch, _ = execute(monkeypatch, press=PRESS.replace("13:30 по московскому времени", "будет сообщено позже"))
    assert len(batch.envelopes) == 2
    actual = batch.envelopes[0]["observation"]
    assert actual["source"]["source_updated_at"] is None
    assert actual["values"]["actual"] == "14"


def test_only_explicit_footer_offset_is_normalized_as_source_time(monkeypatch):
    batch, _ = execute(monkeypatch, press=PRESS.replace("2026-10-23T13:30:00", "2026-10-23T13:30:00+03:00"))
    assert batch.envelopes[0]["observation"]["source"]["source_updated_at"] == "2026-10-23T10:30:00Z"


def test_overdue_next_meeting_is_not_returned_as_upcoming(monkeypatch):
    batch, _ = execute(monkeypatch, now=datetime(2026, 12, 19, 11, tzinfo=timezone.utc))
    assert len(batch.envelopes) == 2


@pytest.mark.parametrize("press,inflation", [
    (PRESS.replace("14,00% годовых</h1>", "нет данных</h1>"), INFLATION),
    (PRESS.replace("23&nbsp;октября 2026 года", "дата не объявлена"), INFLATION),
    (PRESS.replace("2026-10-23T13:30:00", "2099-10-23T13:30:00+03:00"), INFLATION),
    (PRESS, INFLATION.replace("Инфляция, % г/г", "Другая величина")),
    (PRESS, INFLATION.replace("6,35", "—")),
    (PRESS, INFLATION.replace("09.2026", "19.2026")),
    (PRESS, INFLATION.replace("09.2026", "09.2099")),
    (PRESS, INFLATION.replace("09.2026", "08.2026")),
])
def test_missing_or_ambiguous_values_fail_closed(monkeypatch, press, inflation):
    with pytest.raises(RuntimeError, match="^cbr_data_invalid$"):
        execute(monkeypatch, press=press, inflation=inflation)


def test_fetch_failure_does_not_leak_transport_exception(monkeypatch):
    def request(url):
        raise RuntimeError("private accidental diagnostic https://secret.invalid/token")
    monkeypatch.setattr(adapter, "_request_text", request)
    with pytest.raises(RuntimeError, match="^cbr_fetch_failed$"):
        adapter.fetch("cbr", SETTINGS, now=NOW)


def test_opt_in_and_aware_clock_required_before_requests(monkeypatch):
    def request(url):
        pytest.fail("invalid config attempted a request")
    monkeypatch.setattr(adapter, "_request_text", request)
    with pytest.raises(ValueError, match="experimental"):
        adapter.fetch("cbr", {**SETTINGS, "experimental": False}, now=NOW)
    with pytest.raises(ValueError, match="timezone"):
        adapter.fetch("cbr", SETTINGS, now=NOW.replace(tzinfo=None))
