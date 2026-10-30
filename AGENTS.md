# Working in this repository

P1 implements offline JSONL replay, SQLite state/outbox, detector, rules, dry-run CLI and Docker. Live providers and Telegram sender are still planned. Read docs/p1-verification.md before claiming verification.

- Read README.md, docs/architecture.md, docs/sources.md and the relevant .archcore spec before changing a boundary.
- Keep source observations separate from domain events. Preserve provider provenance, reference periods, units, forecast origin and scenario resolution rules.
- Never synthesize missing consensus or call a prediction-market price a poll of traders.
- Provider facts, pricing and licensing require current primary-source verification. Experimental MQL5 web access stays opt-in.
- No live Telegram sends, paid subscriptions or trading actions without explicit user authorization for the destination/action.
- Do not commit secrets, private chat IDs, tokens, real .env files or unredacted API responses.
- Run `uv run pytest -q` for runtime changes and `uv run python scripts/validate_contracts.py` for contract/example changes. Verify Docker when its build/runtime surface changes.
- Future runtime checks are tracked in docs/acceptance-cases.md and the implementation plan. Record the command, result and evidence when reporting them.
- Keep dependencies in pyproject.toml/uv.lock. Keep Docker offline/non-root, exclude .env from context, and persist SQLite in /data. Never load live credentials into replay.
