# Working in this repository

The repository currently contains planning documents, JSON Schema contracts and synthetic examples. Do not describe the runtime, adapters or Docker image as implemented until runnable code and verification evidence exist.

- Read README.md, docs/architecture.md, docs/sources.md and the relevant .archcore spec before changing a boundary.
- Keep source observations separate from domain events. Preserve provider provenance, reference periods, units, forecast origin and scenario resolution rules.
- Never synthesize missing consensus or call a prediction-market price a poll of traders.
- Provider facts, pricing and licensing require current primary-source verification. Experimental MQL5 web access stays opt-in.
- No live Telegram sends, paid subscriptions or trading actions without explicit user authorization for the destination/action.
- Do not commit secrets, private chat IDs, tokens, real .env files or unredacted API responses.
- Run `uv run python scripts/validate_contracts.py` after contract or example changes. This verifies artifacts, not live providers or runtime delivery.
- Future runtime checks are tracked in docs/acceptance-cases.md and the implementation plan. Record the command, result and evidence when reporting them.
- Keep runtime dependencies in pyproject.toml and lock them with uv; Docker runtime files arrive with the first runnable replay worker.
