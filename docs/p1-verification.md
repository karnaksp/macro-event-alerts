# Проверка P1

Проверено 30 октября 2026. Это свидетельство выполнения offline P1; доступность live API и Telegram-доставка здесь не проверялись.

## Выполненные команды

| Команда / действие | Результат |
| --- | --- |
| `uv sync --locked` | Runtime package и зависимости установлены из lockfile |
| `uv run pytest -q` | 39 passed |
| `uv run python scripts/validate_contracts.py` | 4 schemas, 6 positive fixtures, 1 config, 23 negative cases — PASS |
| `docker compose config --quiet` | Compose валиден |
| `docker compose up --build -d` | Образ собран; `macro-event-alerts-replay-1` запущен |
| `docker compose exec -T replay macro-event-alerts health --state /data/state.db` | healthy=true |
| Повтор finite replay на одном volume | Full status, event IDs и checkpoint не изменились |
| Второй replay при работающем follow worker | Отклонён с exit code 2; state lock удерживается и во время ожидания |
| `docker compose stop replay` | SIGTERM завершился с exit code 0 |
| `docker compose up -d replay` после stop | Full status и event IDs совпали с состоянием до остановки |

Локальный Docker проверял Linux arm64 на Docker Engine 29.8.0; контейнер использует Python 3.12.16. Runtime CI отдельно выполняет pytest и Docker replay на GitHub Ubuntu runner. Его успешность видна в GitHub Actions, а не выводится из локальных проверок.

## Сохранённый результат демо

| Сущность | Количество |
| --- | ---: |
| Observations | 12 |
| Domain events | 7 |
| Outbox | 6 |
| Dry-run | 6 |
| Pending | 0 |
| Quarantine | 0 |

Семь UUID событий сравнивались между запусками; checkpoint остался на строке 12. Демо содержит два forecast changes A→B→A, два release, один revision и два probability changes. Только последнее scenario окно проходит порог +15 п.п.: алерт показывает +20 п.п. за час.

## Покрытые отказы и границы

- Холодный старт на published history и первое числовое forecast после null не создают алерты.
- Новая серия/период и равный previous actual не объединяются со старым выпуском.
- Прогноз после actual не создаёт дорелизный алерт.
- Окно может созреть на свежем снимке неизменившейся цены; evaluation context остаётся неизменным при повторе.
- Bad schema, future source/publication timestamp и младшие revisions не заменяют live baseline.
- Более старый source timestamp не отменяет старшинство большей числовой source revision.
- Исторический backfill сохраняется отдельно от live baseline.
- Авария перед commit откатывает observation, event, outbox и checkpoint вместе; replay восстанавливается.
- Некорректное событие самого detector прерывает обработку с rollback вместо продвижения cursor.
- Pending outbox после рестарта выводится без нового входного события.
- Dry-run проверен с запретом сетевых sockets, включая присутствие синтетических Telegram-переменных.

## Контейнер и локальные реквизиты

Фактически проверены UID/GID `10001:10001`, read-only root filesystem, `network_mode:none`, writable volume `/data`. В runtime нет `/app/.env` и переменных `TELEGRAM_*`. Build context использует whitelist.

Локальный `.env` исключён Git и имеет права 0600. Его значения не используются replay, не входят в образ и не публикуются в репозитории. Реквизиты будущего Telegram sender не являются доказательством доставки.

## Что остаётся после P1

P2–P4 подключают внешние API и подтверждают mappings/quotas/latency. P5 добавляет live Telegram sender, retry/dead-letter и внешнюю доставку. Health P1 проверяет SQLite schema/quick_check; monitoring свежести live источников относится к следующим этапам. Dry-run stdout может повториться при аварии после записи stdout до отметки в БД; новое логическое событие при этом не создаётся.
