---
title: "Очередь и доставка Telegram"
status: draft
tags:
  - "macro-alerts"
---

## Purpose & Scope

Контракт durable outbox → Telegram Bot API. Пользователь и оператор зависят от сохранности очереди, объяснимых ошибок и происхождения сообщения.

## Surface

- `@.env.example`: bot token, chat ID и опциональный message_thread_id.
- `@config.example.yaml`: dry_run и имена переменных окружения.
- Outbox record: event_id, subscription_id, evaluation_context, destination, status, attempts, next_attempt_at, last_error, message_id; P1 storage/render: `@src/macro_event_alerts/storage.py`, `@src/macro_event_alerts/render.py`.
- Planned states: pending, sending, retry, delivered, ambiguous, dead_letter.
- Внешний интерфейс: [Telegram sendMessage](https://core.telegram.org/bots/api#sendmessage).

## Normative Behavior

1. WHEN правило выбирает событие, outbox MUST сохранить одно задание на event_id, subscription_id, chat_id и message_thread_id.
2. WHEN sender получает HTTP success и `ok:true`, sender MUST сохранить message_id и состояние delivered.
3. WHEN Telegram возвращает 429, sender MUST отложить повтор минимум на retry_after.
4. WHEN Telegram возвращает 5xx, sender MUST применить ограниченный exponential backoff с jitter.
5. WHEN Telegram возвращает 401 или 403, sender MUST перевести задание в dead_letter с диагностикой.
6. WHEN timeout оставляет результат неизвестным, sender MUST пометить попытку ambiguous перед повтором.
7. WHEN приложение перезапускается, sender MUST восстановить pending, retry и незавершённые sending из сохранённой очереди.
8. WHEN SIGTERM прерывает процесс, worker MUST завершить транзакцию либо откатить её до выхода.
9. WHEN dry_run включён, sender MUST записать подготовленное сообщение без вызова Telegram API.
10. WHEN renderer формирует сообщение, renderer MUST включить источник, ссылку, значения, единицы, период и доступное время публикации.
11. WHEN renderer выводит scenario, renderer MUST отобразить источник и оконное изменение из сохранённого evaluation context.

## Constraints & Invariants

- Sender MUST обеспечивать локальную дедупликацию через уникальный ключ задания; exactly-once внешней доставки не гарантируется.
- Renderer MUST экранировать разметку из названий источников; размер сообщения ограничивается лимитом sendMessage.
- Sender MUST учитывать общий rate limit бота и лимит назначения независимо от числа правил.
- Worker MUST исключать token, credential headers и URL с token из логов и diagnostics.
- [assumption] Retry policy: initial 2 секунды, максимум 5 минут, 10 попыток; после лимита — dead_letter.
- [assumption] Ambiguous повторяется по retry policy, поэтому возможный дубль явно документируется.
- Worker MUST сохранять event ID при ручном повторе dead_letter; новая попытка не создаёт новое доменное событие.

## Failure Behavior

1. IF HTTP 200 содержит `ok:false`, THEN sender MUST классифицировать ошибку по error_code вместо отметки delivered.
2. IF token или chat ID отсутствует при live mode, THEN config validator MUST отклонить запуск до первого запроса.
3. IF template превышает лимит, THEN renderer MUST детерминированно сократить контекст, сохранив факт, источник и ссылку.
4. IF retry исчерпан, THEN sender MUST оставить задание в dead_letter для ручного повторения.
5. IF отказал один Telegram-запрос, THEN worker MUST продолжить сбор данных и сохранение очереди.

## Conformance

Все MUST и состояния проверяются fake Telegram transport до live smoke test; список запланированных отказов хранится в `@docs/acceptance-cases.md`.
P1 реализует pending → dry_run и renderer; остальные состояния и live sender относятся к P5. P1 не читает credentials и не вызывает Telegram API.
