"""Plain-text Russian messages. Never reads or prints Telegram credentials."""
from decimal import Decimal

from .domain import decimal_text


def shown(value) -> str:
    return "нет данных" if value is None else str(value)


def render(event: dict, context: dict, *, dry_run: bool = True) -> str:
    subject, payload = event["subject"], event["payload"]
    event_type = event["event_type"]
    label = subject["label"][:300]
    if event_type == "macro.release":
        headline = f"Статистика опубликована: {label}"
        details = (f"Факт: {shown(payload['actual'])}; прогноз: {shown(payload['forecast'])}; "
                   f"предыдущее: {shown(payload['previous'])}")
    elif event_type == "macro.forecast_changed":
        headline = f"Прогноз обновился: {label}"
        details = f"{payload['before']} → {payload['after']} (origin: {payload['forecast_origin']})"
    elif event_type == "macro.revised":
        headline = f"Результат пересмотрен: {label}"
        details = f"{payload['before_actual']} → {payload['after_actual']}"
    else:
        headline = f"Рыночная оценка сценария: {label}"
        before = decimal_text(Decimal(context["before"]) * 100)
        after = decimal_text(Decimal(context["after"]) * 100)
        delta = Decimal(context["delta_pp"])
        sign = "+" if delta > 0 else ""
        details = f"Цена исхода: {before}% → {after}% ({sign}{decimal_text(delta)} п.п. за окно)"
    if subject["kind"] == "macro":
        scope = f"Период: {shown(None if subject['reference_period']=='unknown' else subject['reference_period'])}; единицы: {subject['unit']}, {subject['scale']}"
    else:
        scope = f"Исход: {subject['outcome_id']}; срок: {subject['horizon_at']}; метод: {subject['price_basis']}"
    source = event["source"]
    delay = source["declared_delay_seconds"]
    release_time = payload.get("published_at")
    lines = ["[DRY-RUN / СИНТЕТИЧЕСКИЙ REPLAY]" if dry_run else "Macro Event Alerts", headline, details, scope,
             f"Источник: {source['provider_id']}", f"Наблюдение: {context.get('current_observed_at', event['observed_at'])}",
             f"Публикация: {release_time or 'неизвестно / неприменимо'}",
             f"Задержка источника: {str(delay) + ' с' if delay is not None else 'неизвестна'}",
             source["source_url"][:1000], f"Событие: {event['event_id']}"]
    return "\n".join(lines)[:4000]
