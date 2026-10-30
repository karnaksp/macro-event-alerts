# Синтетические примеры

Все файлы `events/` и `observations/` содержат вымышленные значения. `provider_id=fixture`, `example.com`, UUID и market IDs не обозначают реальный релиз или рыночный прогноз.

`invalid/cases.json` задаёт изменения положительных примеров и ожидаемый уровень отказа: `schema` либо `semantic`. Скрипт `scripts/validate_contracts.py` выполняет эти проверки без сети. Сценарии перезапуска, baseline, адаптеров и Telegram описаны отдельно и здесь не исполняются.
