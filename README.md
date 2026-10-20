# Power 6/55 Research Platform

A research scaffold for collecting verifiable Power 6/55 draw history, storing it in PostgreSQL, producing statistical baselines, and running walk-forward evaluations. **It does not predict winning numbers reliably and must not be represented as a guaranteed-profit system.**

## Included
- Docker Compose: PostgreSQL, Redis, FastAPI, worker, Next.js.
- API for verified draw records, summary, model-generated candidate sets, and backtest.
- Import endpoint for manually verified CSV data.
- Official-site fetcher with conservative parsing and provenance storage. Official HTML can change; confirm the parser against the current site before enabling scheduled imports.
- Baseline uniform probability and smoothed Bayesian frequency model.
- Optional XGBoost experiment if installed; it is never presumed better than random.
- Walk-forward evaluation with no future data in each training window.
- Minimal dashboard and API key protection for write/compute endpoints.

## Start locally
1. Install Docker Engine and Docker Compose v2.
2. `cp .env.example .env`
3. Edit `.env`; set strong random passwords/secrets. Keep `.env` out of version control.
4. `docker compose up --build -d`
5. API docs: `http://localhost:8000/docs`; dashboard: `http://localhost:3000`.
6. The dashboard is intentionally bound to localhost. For VPS deployment, put Caddy/Nginx in front and expose only HTTPS.

## CSV import
Prepare UTF-8 CSV with headers:
`draw_id,draw_date,n1,n2,n3,n4,n5,n6,special,source_url,verified`
Dates use `YYYY-MM-DD`. Numbers are integers 1–55; the six main numbers must be distinct and `special` must not duplicate them. Set `verified` to `true` only after independently checking that row against the official result; otherwise leave it blank or false.
Then:
`curl -X POST http://localhost:8000/api/import/csv -H "X-API-Key: YOUR_API_KEY" -F "file=@draws.csv"`

The import endpoint validates records and uses upsert on `draw_id`. Only rows explicitly marked `verified=true` are eligible for analytics; the flag is an operator attestation, not cryptographic proof. Independently reconcile the source before setting it. Check official results manually or implement a verified source adapter.

## Official source collection
The worker periodically fetches `VIETLOTT_RESULTS_URL`. Website structure may change or use dynamic rendering. It only imports rows if the parser recognizes a date plus seven numbers; review logs and verify every imported row. If parsing yields no records, import a verified CSV rather than relaxing validation. Respect the website's terms, rate limits, and robots instructions.

## API
- `GET /api/health`
- `GET /api/draws?limit=100`
- `GET /api/summary`
- `GET /api/predictions?method=bayesian&count=5` (exploratory candidate sets, not winning odds)
- `POST /api/backtest` with `{"min_train":100,"test_draws":100,"method":"bayesian"}` and `X-API-Key`
- `POST /api/import/csv` with `X-API-Key`

Methods: `uniform`, `bayesian`, and `xgboost` (optional dependency). Model scores rank candidates; they are not evidence of a real edge.

## Production checklist
- Generate strong unique secrets; restrict network access.
- Terminate TLS with Caddy/Nginx; do not expose Postgres or Redis publicly.
- Add backups and test restore.
- Pin image versions and dependencies; use a vulnerability scanner.
- Add authentication/rate limiting if multi-user access is enabled.
- Verify source data and scraper output before using it in reports.
- Run backtests on a final holdout set; log all tried models to reduce multiple-testing bias.
- Lottery is random. Set budgets and do not use model output as financial advice.
