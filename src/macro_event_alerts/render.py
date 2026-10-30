"""Compact Russian Telegram HTML; dynamic text is escaped before adding markup."""
from datetime import datetime, timezone
from decimal import Decimal
from html import escape
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .domain import decimal_text, instant

LOCAL_ZONE = ZoneInfo("Europe/Podgorica")
SERIES_LABELS = {
    "US.CPI.HEADLINE.YOY": "Инфляция CPI США · год к году",
    "US.CPI.HEADLINE.MOM": "Инфляция CPI США · месяц к месяцу",
    "US.CPI.CORE.YOY": "Базовая инфляция CPI США · год к году",
    "US.CPI.CORE.MOM": "Базовая инфляция CPI США · месяц к месяцу",
    "US.PCE.HEADLINE.YOY": "Инфляция PCE США · год к году",
    "US.PCE.HEADLINE.MOM": "Инфляция PCE США · месяц к месяцу",
    "US.PCE.CORE.YOY": "Базовая инфляция PCE США · год к году",
    "US.PCE.CORE.MOM": "Базовая инфляция PCE США · месяц к месяцу",
    "US.GDP.QOQ.ANNUALIZED": "ВВП США · кв/кв, годовой темп",
    "US.NFP.CHANGE": "Изменение занятости в США · NFP",
    "US.UNEMPLOYMENT.RATE": "Безработица в США",
    "US.ECI.QOQ": "Стоимость труда в США (ECI) · кв/кв",
    "US.FED.TARGET.MIDPOINT": "Ставка ФРС · середина диапазона",
    "US.FED.TARGET.UPPER": "Ставка ФРС · верхняя граница",
    "US.PPI.HEADLINE.MOM": "Цены производителей PPI США · месяц к месяцу",
    "US.PPI.HEADLINE.YOY": "Цены производителей PPI США · год к году",
    "US.PPI.CORE.MOM": "Базовый PPI США · месяц к месяцу",
    "US.PPI.CORE.YOY": "Базовый PPI США · год к году",
    "US.RETAIL.HEADLINE.MOM": "Розничные продажи США · месяц к месяцу",
    "US.RETAIL.EX_AUTOS.MOM": "Продажи США без автомобилей · месяц к месяцу",
    "US.JOBLESS.INITIAL.CLAIMS": "Первичные заявки на пособие в США",
    "US.ISM.MANUFACTURING.PMI": "ISM США · промышленность",
    "US.ISM.SERVICES.PMI": "ISM США · услуги",
}
MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь")
FORECAST_LABELS = {"consensus": "Консенсус", "model": "Модельный прогноз", "official_projection": "Официальный прогноз", "unknown": "Прогноз источника"}


def text(value, limit=180) -> str:
    value = " ".join(str(value).split())
    return escape(value if len(value) <= limit else value[:limit-1] + "…")


def number(value) -> str:
    return decimal_text(Decimal(value)).replace(".", ",")


def value_text(value, subject: dict, *, difference=False) -> str:
    if value is None:
        return "нет данных"
    unit = subject["unit"]
    suffix = "п.п." if difference and unit == "percent" else {
        "percent": "%", "persons": "чел.", "usd": "$", "index": "пунктов", "other": "ед.",
    }[unit]
    scale = {"units": "", "thousands": "тыс. ", "millions": "млн ", "billions": "млрд "}[subject["scale"]]
    separator = "" if suffix == "%" and not scale else " "
    return number(value) + separator + scale + suffix


def period_text(value: str) -> str:
    if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        year, month = value.split("-")
        return f"{MONTHS[int(month)-1]} {year}"
    if re.fullmatch(r"\d{4}-Q[1-4]", value):
        return f"{value[-1]}-й квартал {value[:4]}"
    return text(value, 64)


def date_text(value: str | datetime) -> str:
    date = instant(value) if isinstance(value, str) else value
    local = date.astimezone(LOCAL_ZONE)
    return local.strftime("%d.%m.%Y · %H:%M %Z")


def source_link(source: dict) -> str:
    url = source["source_url"]
    label = {"mql5_web": "MQL5", "xoomar": "XOOMAR", "polymarket": "Polymarket"}.get(source["provider_id"])
    label = label or urlsplit(url).hostname or "Источник"
    return f'<a href="{escape(url, quote=True)}">{text(label, 60)}</a>'


def macro_label(subject: dict) -> str:
    return text(SERIES_LABELS.get(subject["series_id"], subject["label"]))


def macro_context(subject: dict, payload: dict) -> list[str]:
    period = subject["reference_period"]
    lines = ["Период: " + (period_text(period) if period != "unknown" else "не указан источником")]
    if payload.get("published_at"):
        lines.append("Опубликовано: " + date_text(payload["published_at"]))
    elif payload.get("scheduled_at"):
        lines.append("По календарю: " + date_text(payload["scheduled_at"]))
    return lines


def render(event: dict, context: dict, *, dry_run: bool = True) -> str:
    subject, payload, source = event["subject"], event["payload"], event["source"]
    event_type = event["event_type"]
    lines = ["🧪 <b>Тест · синтетические данные</b>", ""] if dry_run else []
    if subject["kind"] == "macro":
        headline = {"macro.release": "📊 Вышла статистика", "macro.forecast_changed": "📝 Обновился прогноз", "macro.revised": "🔄 Пересмотр статистики"}[event_type]
        lines.extend([f"<b>{headline}</b>", macro_label(subject), ""])
        if event_type == "macro.release":
            lines.append(f"Факт: <b>{value_text(payload['actual'], subject)}</b>")
            forecast_label = FORECAST_LABELS[payload["forecast_origin"]]
            lines.append(f"{forecast_label}: {value_text(payload['forecast'], subject)}")
            previous_period = payload.get("previous_reference_period")
            previous_label = "Предыдущее" + (f" ({period_text(previous_period)})" if previous_period and previous_period != "unknown" else "")
            lines.append(f"{previous_label}: {value_text(payload['previous'], subject)}")
            if payload["actual"] is not None and payload["forecast"] is not None:
                delta = Decimal(payload["actual"]) - Decimal(payload["forecast"])
                comparison = "Выше прогноза на " if delta > 0 else "Ниже прогноза на "
                lines.append("Совпало с прогнозом" if delta == 0 else comparison + value_text(abs(delta), subject, difference=True))
        else:
            before, after = (payload["before"], payload["after"]) if event_type == "macro.forecast_changed" else (payload["before_actual"], payload["after_actual"])
            lines.append(f"{value_text(before, subject)} → <b>{value_text(after, subject)}</b>")
            if event_type == "macro.forecast_changed":
                lines.append(FORECAST_LABELS[payload["forecast_origin"]])
        lines.extend(["", *macro_context(subject, payload)])
    else:
        label = subject["label"]
        before = number(Decimal(context["before"]) * 100)
        after = number(Decimal(context["after"]) * 100)
        delta = Decimal(context["delta_pp"])
        change = ("+" if delta > 0 else "") + number(delta)
        duration = ""
        if context.get("window_start") and context.get("window_end"):
            minutes = Decimal(str((instant(context["window_end"]) - instant(context["window_start"])).total_seconds())) / 60
            duration = " за " + number(minutes) + " мин"
        basis = {"midpoint": "Середина спреда", "last_trade": "Последняя сделка", "provider_probability": "Оценка источника"}[subject["price_basis"]]
        outcome = subject.get("outcome_label") or subject["outcome_id"]
        outcome = {"yes": "Да", "no": "Нет"}.get(outcome.casefold(), outcome)
        lines.extend(["<b>🎯 Изменилась рыночная оценка</b>", text(label), "", f"Цена исхода: {before}% → <b>{after}%</b>",
                      f"Изменение: <b>{change} п.п.</b>{duration}", "Исход: " + text(outcome, 100), "", "Срок сценария: " + date_text(subject["horizon_at"]), basis])
    lines.extend(["Получено: " + date_text(context.get("current_observed_at", event["observed_at"])), "Источник: " + source_link(source)])
    if source.get("declared_delay_seconds") is not None:
        lines.append(f"Заявленная задержка источника: {source['declared_delay_seconds']} с")
    return "\n".join(lines)



def _snapshot_group(subject: dict) -> str:
    series = subject["series_id"]
    if series.startswith(("US.CPI.", "US.PCE.", "US.PPI.")):
        return "Инфляция"
    if series in {"US.NFP.CHANGE", "US.UNEMPLOYMENT.RATE", "US.ECI.QOQ", "US.JOBLESS.INITIAL.CLAIMS"}:
        return "Рынок труда"
    if series.startswith(("US.GDP.", "US.RETAIL.", "US.ISM.")):
        return "Экономический рост"
    if series.startswith("US.FED."):
        return "Ставка ФРС"
    return "Другие показатели"


def _snapshot_rank(item: dict, now: datetime, published: bool) -> tuple:
    values, subject = item["values"], item["subject"]
    date = values.get("scheduled_at") or values.get("published_at")
    if not date:
        order = (2, 0)
    else:
        when = instant(date).timestamp()
        order = (0, -when) if published else ((0, when) if instant(date) >= now else (1, -when))
    # Choose one complete source observation. Never borrow its missing fields
    # from another provider, release or reporting period.
    return (*order, -int(values.get("forecast") is not None), -int(values.get("previous") is not None),
            -int(subject["reference_period"] != "unknown"), -instant(item["observed_at"]).timestamp(),
            item["source"]["provider_id"], item["observation_id"])


def _snapshot_macro_block(item: dict, *, published: bool) -> str:
    subject, values = item["subject"], item["values"]
    fields = []
    if published:
        fields.append("Факт: <b>" + value_text(values["actual"], subject) + "</b>")
    fields.append(FORECAST_LABELS[values["forecast_origin"]] + ": " + value_text(values["forecast"], subject))
    previous_period = values.get("previous_reference_period")
    previous_label = "Предыдущее" + (f" ({period_text(previous_period)})" if previous_period and previous_period != "unknown" else "")
    fields.append(previous_label + ": " + value_text(values["previous"], subject))
    lines = [f"<b>{macro_label(subject)}</b> · {source_link(item['source'])}", " · ".join(fields)]
    if subject["reference_period"] != "unknown":
        lines.append("Период: " + period_text(subject["reference_period"]))
    release_time = values.get("published_at") or values.get("scheduled_at")
    if release_time:
        lines.append(("Опубликовано: " if values.get("published_at") else "По календарю: ") + date_text(release_time))
    return "\n".join(lines)


def _snapshot_chunks(title: str, blocks: list[tuple[str, str]], footer: str) -> list[str]:
    """Split between whole records, keeping HTML and every selected value intact."""
    chunks, current, previous_group = [], [], None
    for group, block in blocks:
        addition = ([f"<b>{text(group)}</b>"] if group != previous_group else []) + [block]
        candidate = "\n\n".join([title, *current, *addition, footer])
        if current and len(candidate.encode("utf-16-le")) // 2 > 3900:
            chunks.append("\n\n".join([title, *current, footer]))
            current = []
            addition = [f"<b>{text(group)}</b>", block]
        current.extend(addition)
        previous_group = group
    if current:
        chunks.append("\n\n".join([title, *current, footer]))
    return chunks


def render_snapshot_messages(observations: list[dict], *, at=None, dry_run=False) -> list[str]:
    """All saved macro coverage and quotes, with pending releases kept separate."""
    now = instant(at) if isinstance(at, str) else at or datetime.now(timezone.utc)
    published, calendar, scenarios = {}, {}, {}
    for item in observations:
        subject, values = item["subject"], item["values"]
        if subject["kind"] == "macro":
            if values["publication_status"] == "cancelled":
                continue
            is_published = values["actual"] is not None
            selected = published if is_published else calendar
            key = subject["series_id"]
            if key not in selected or _snapshot_rank(item, now, is_published) < _snapshot_rank(selected[key], now, is_published):
                selected[key] = item
        else:
            key = (item["source"]["provider_id"], subject["contract_id"], subject["outcome_id"],
                   subject["horizon_at"], subject["resolution_hash"], subject["price_basis"])
            if key not in scenarios or item["observed_at"] > scenarios[key]["observed_at"]:
                scenarios[key] = item
    footer = "<i>Снимок сохранённых данных, не новый релиз.</i>\nНа " + date_text(now)
    period_note = "\nГде отчётный период не указан, источник его не сообщает."
    marker = "🧪 Тест · синтетические данные" if dry_run else "📊 Макросводка США"
    messages = []
    if published or scenarios:
        blocks = []
        group_order = {"Инфляция": 0, "Рынок труда": 1, "Экономический рост": 2, "Ставка ФРС": 3, "Другие показатели": 4}
        for item in sorted(published.values(), key=lambda item: (group_order[_snapshot_group(item["subject"])], item["subject"]["series_id"])):
            blocks.append((_snapshot_group(item["subject"]), _snapshot_macro_block(item, published=True)))
        for key, item in sorted(scenarios.items()):
            subject, values = item["subject"], item["values"]
            outcome = subject.get("outcome_label") or subject["outcome_id"]
            outcome = {"yes": "Да", "no": "Нет"}.get(outcome.casefold(), outcome)
            basis = {"midpoint": "середина спреда", "last_trade": "последняя сделка", "provider_probability": "оценка источника"}[subject["price_basis"]]
            lines = [f"<b>{text(subject['label'])}</b> · {source_link(item['source'])}",
                     f"Цена исхода «{text(outcome, 100)}»: <b>{number(Decimal(values['probability']) * 100)}%</b> ({basis})",
                     "Котировка: " + date_text(values["sample_at"]), "Срок сценария: " + date_text(subject["horizon_at"])]
            if values["market_status"] != "open":
                lines.append("Рынок закрыт" if values["market_status"] == "closed" else "Результат сценария определён")
            blocks.append(("Рыночный сценарий", "\n".join(lines)))
        section_footer = footer + (period_note if any(item["subject"]["reference_period"] == "unknown" for item in published.values()) else "")
        messages.extend(_snapshot_chunks(f"<b>{marker}</b>\nОпубликованные значения и сохранённые котировки", blocks, section_footer))
    if calendar:
        blocks = []
        ordered = sorted(calendar.values(), key=lambda item: (item["values"].get("scheduled_at") or "9999", item["subject"]["series_id"]))
        for item in ordered:
            scheduled = item["values"].get("scheduled_at")
            group = date_text(scheduled).split(" · ")[0] if scheduled else "Дата не указана"
            blocks.append((group, _snapshot_macro_block(item, published=False)))
        title = f"<b>{marker} · календарь</b>\nРезультаты ещё не опубликованы. Ниже — прогнозы источников и прошлые значения."
        section_footer = footer + (period_note if any(item["subject"]["reference_period"] == "unknown" for item in calendar.values()) else "")
        messages.extend(_snapshot_chunks(title, blocks, section_footer))
    if not messages:
        messages = [f"<b>{marker}</b>\nСохранённых данных пока нет.\n\n{footer}"]
    if len(messages) > 1:
        messages = [message.replace("</b>", f" · {index}/{len(messages)}</b>", 1) for index, message in enumerate(messages, 1)]
    return messages
