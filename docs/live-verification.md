# Live-стек и оптимизация

Проверено 30 октября 2026. Реальные API и Telegram проверялись отдельно от offline tests; credentials и chat ID исключены из этого отчёта.

## Выполнено

- `uv run --locked pytest -q`: **123 passed**. Включены fake Telegram, API mapping fixtures, durable retry/restart, quotas и v1→v2 migration.
- Contract validator: 4 schemas, 6 positive fixtures, 1 config, 23 negative cases — PASS.
- Одна Alpine-сборка и один offline smoke container: два replay-процесса сохранили те же 7 event IDs, checkpoint и 6 dry-run jobs.
- `docker compose up --no-build -d alerts`: запущен проверенный image, отдельный live volume.
- Первое live polling: XOOMAR 6, MQL5 13, Polymarket 1 наблюдение; quarantine 0, ошибки источников отсутствуют.
- `telegram-check`: token и доступ к supergroup подтверждены после добавления бота пользователем.
- `telegram-test`: **delivered**, Telegram message ID **2505**. Отправлен один явно обозначенный контрольный снимок реальных сохранённых данных.
- Live health: healthy=true, telegram_ready=true, sources_ready=true.

На первом проходе domain events/outbox равны нулю: существующие данные загружаются как baseline. Контрольное сообщение не выдаётся за новый macro release. Последующие изменения автоматически проходят detector, rules и durable sender; приход нового релиза в будущем не объявляется уже проверенным фактом.

## Измерение образа

| Версия | Размер Docker image |
| --- | ---: |
| P1 Python slim + format extras | 223 313 483 байт |
| Live Alpine + только нужные зависимости | 86 522 131 байт |
| Снижение | 61,26% |

Это локальный uncompressed image size для Linux arm64. Кросс-платформенные musl wheels подтверждены; GitHub CI выполняет свой smoke на Ubuntu runner. Размер compressed registry layers отличается.

## Сокращение повторов

Раньше два workflow запускались на push ветки и PR: 4 workflow runs, до 4 Python installs и 2 image builds на одном изменении. Теперь один CI на PR, одна установка Python-зависимостей, один image build с BuildKit cache. Изменения документации не требуют тяжёлых jobs; superseded run отменяется. Отдельный main push сохраняет проверку интеграции.

В runtime config проверяется при старте, JSON Schema registry кешируется, external observations валидируются на входе. Health использует ограниченные запросы к schema/heartbeat/provider state вместо `quick_check`, count таблиц и полного списка UUID. Полная диагностика остаётся в явном `status`.

## Границы

MQL5 web экспериментален; reference period отсутствует в listing и не выводится из даты релиза. XOOMAR не предоставляет consensus. Polymarket использует настоящий book timestamp, midpoint и market-aggregate liquidity; цена исхода не является опросом трейдеров. Платные источники и торговые ключи не используются.

Telegram имеет at-least-once semantics: network timeout может дать повтор сообщения после восстановления. Старые chat/topic jobs сохраняются без перенаправления. Dead-letter учитывается и сохраняется, но отдельного operator CLI для retry пока нет.
