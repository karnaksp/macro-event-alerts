# Working in this repository

Runtime supports offline replay and live XOOMAR/MQL5/Polymarket ingestion with durable Telegram delivery. Read docs/live-verification.md and docs/p1-verification.md before claiming verification.

- Read README.md, docs/architecture.md, docs/sources.md and the relevant .archcore spec before changing a boundary.
- Keep source observations separate from domain events. Preserve provider provenance, reference periods, units, forecast origin and scenario resolution rules.
- Never synthesize missing consensus or call a prediction-market price a poll of traders.
- Provider facts, pricing and licensing require current primary-source verification. Experimental MQL5 web access stays opt-in.
- No live Telegram sends, paid subscriptions or trading actions without explicit user authorization for the destination/action.
- Do not commit secrets, private chat IDs, tokens, real .env files or unredacted API responses.
- Minimize repeated checks: run only affected tests while developing; run the full runtime/contract suite once after final functional changes. Repeat a suite only for a new change/failure/unresolved concern. Build/smoke Docker when runtime/dependencies/build inputs change; docs-only changes do not need another local build.
- Future runtime checks are tracked in docs/acceptance-cases.md and the implementation plan. Record the command, result and evidence when reporting them.
- Keep dependencies in pyproject.toml/uv.lock. Keep images non-root and minimal; live needs outbound HTTPS, replay stays offline. Exclude .env from builds; persist SQLite in /data. Never load live credentials into replay. CI uses one PR workflow with path gating/caches; do not reintroduce duplicate push+PR suites.
