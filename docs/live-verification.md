# Live-стек и оптимизация

Проверено 30 октября 2026. Реальные API и Telegram проверялись отдельно от offline tests; credentials и chat ID исключены из этого отчёта.

## Выполнено

- `uv run --locked pytest -q`: **196 passed**. Включены fake Telegram, HTML templates/escaping, outcome labels, API mapping fixtures, durable retry/restart, quotas и v1→v2 migration.
- Contract validator: 4 schemas, 6 positive fixtures, 1 config, 23 negative cases — PASS.
- Одна Alpine-сборка и один offline smoke container: два replay-процесса сохранили те же 7 event IDs, checkpoint и 6 dry-run jobs.
- `docker compose up --no-build -d alerts`: запущен проверенный image, отдельный live volume.
- Первое live polling: XOOMAR 6, MQL5 13, Polymarket 1 наблюдение; quarantine 0, ошибки источников отсутствуют.
- `telegram-check`: token и доступ к supergroup подтверждены после добавления бота пользователем.
- `telegram-test`: **delivered**, Telegram message ID **2505**. Отправлен один явно обозначенный контрольный снимок реальных сохранённых данных.
- Live health: healthy=true, telegram_ready=true, sources_ready=true.
- После переработки шаблонов тот же message ID 2505 обновлён через editMessageText: API подтвердил HTML entities (bold, italic, text_link) и link_preview_options.is_disabled=true. Проверка через Telegram Desktop подтвердила отдельные блоки и отсутствие превью. Новое сообщение не отправлялось.
- Formatter tests проверяют десятичную запятую, %/п.п./тыс. чел., null и 0, период, CET/CEST, экранирование HTML и цену сценария меньше 1%. Fake sender проверяет plain jobs предыдущей версии и новые HTML jobs; Polymarket сохраняет название выбранного исхода.

На первом проходе domain events/outbox равны нулю: существующие данные загружаются как baseline. Контрольное сообщение не выдаётся за новый macro release. Последующие изменения автоматически проходят detector, rules и durable sender; приход нового релиза в будущем не объявляется уже проверенным фактом.

## Измерение образа

| Версия | Размер Docker image |
| --- | ---: |
| P1 Python slim + format extras | 223 313 483 байт |
| Live Alpine + только нужные зависимости | 86 553 071 байт |
| Снижение | 61,24% |

Это локальный uncompressed image size для Linux arm64. Кросс-платформенные musl wheels подтверждены; GitHub CI выполняет свой smoke на Ubuntu runner. Размер compressed registry layers отличается.

## Сокращение повторов

Раньше два workflow запускались на push ветки и PR: 4 workflow runs, до 4 Python installs и 2 image builds на одном изменении. Теперь один CI на PR, одна установка Python-зависимостей, один image build с BuildKit cache. Изменения документации не требуют тяжёлых jobs; superseded run отменяется. Отдельный main push сохраняет проверку интеграции.

В runtime config проверяется при старте, JSON Schema registry кешируется, external observations валидируются на входе. Health использует ограниченные запросы к schema/heartbeat/provider state вместо `quick_check`, count таблиц и полного списка UUID. Полная диагностика остаётся в явном `status`.

## Границы

MQL5 web экспериментален; reference period отсутствует в listing и не выводится из даты релиза. XOOMAR не предоставляет consensus. Polymarket использует настоящий book timestamp, midpoint и market-aggregate liquidity; цена исхода не является опросом трейдеров. Платные источники и торговые ключи не используются.

Telegram имеет at-least-once semantics: network timeout может дать повтор сообщения после восстановления. Старые chat/topic jobs сохраняются без перенаправления. Dead-letter учитывается и сохраняется, но отдельного operator CLI для retry пока нет.


## Расширенная сводка и новые показатели

- Добавлены 9 MQL5-серий: headline/core PPI MoM/YoY, retail/headline и ex-autos MoM, initial claims, ISM manufacturing/services. Соответствие единиц и названий проверено по первичным страницам MQL5, ссылки — в docs/sources.md. Release/revision и forecast-change правила включены.
- Лимит сводки в 3 значения удалён. Опубликованные результаты/котировки и ещё не опубликованные выпуски выбираются отдельно; источник каждой строки сохраняется целиком, без заимствования forecast/previous из другого наблюдения.
- Одноразовый read-only API запрос MQL5 за 31 день назад/0 вперёд вернул 29 исторических фактов по 21 серии. Единственный запрос предварительно учтён в существующем daily budget. Импорт выполнен в остановленном worker с существующим volume, без Telegram credentials, через _ingest(is_backfill=true).
- Проверены инварианты импорта: live subject_state, events, outbox, subscriptions и polling checkpoints не изменились; после импорта worker запущен снова. Ручная сводка читает latest backfill отдельно от live baseline.
- В момент проверки в базе были все 23 поддерживаемые макросерии, по 22 — опубликованные факты; все 9 новых рядов присутствовали. Quarantine=0, live health healthy=true.
- Сводка доставлена в 3 частях: message2505 обновлён, message2538/2539 отправлены с паузой 3 секунды. API подтвердил delivered, HTML entities и отключённые previews для каждой части. Длины в момент проверки: 3764/2550/3590 UTF-16 units, каждая меньше4096.
- Новые tests проверяют отображение более3рядов, отдельный будущий forecast без потери прошлого actual, нулевые значения, исключение cancelled, целые HTML-блоки/пагинацию, latest backfill без изменений baseline, остановку ручной отправки при ошибке, PMI в пунктах и claims в людях.
