# Vietlott Research Lab v2

Single-process FastAPI research workspace, SQLite WAL, server-rendered dashboard,
and Caddy. **Lottery draws are random. This project does not guarantee winning
numbers or profit.** No ticket purchases, real-money accounts, or payments.

## Release status: foundation, not full specification completion

`update.md` remains the reference specification, not a claim that every roadmap
item is complete. No real draw history is bundled and no sources were silently
marked verified.

| Area | Implemented | Deliberately unavailable / remaining |
| --- | --- | --- |
| Storage | SQLite WAL, foreign keys, busy timeout, versioned initial schema | PostgreSQL-to-SQLite migration requires export/import |
| Games | Five JSON-compatible YAML config files | Official rule/schedule review required; three newer game imports blocked |
| ETL | Atomic multi-game/legacy Power CSV, validation, provenance audit, gap ranges | Official adapters, two-source reconciliation, raw snapshots, automatic crawling |
| Statistics | Verified-only frequency/gaps, Monte Carlo frequency tests, Bonferroni within report, illusion simulator | Runs, ACF, pair/digit tests and report-wide trial registry |
| Models | Uniform and shrunk Bayesian inclusion scores | Logistic, optional XGBoost, calibration, independent holdout, multiple-model correction |
| Backtest | Walk-forward, sampled random control, Brier/log loss, paired bootstrap CI, input hash | Statistical promotion/edge claims remain disabled |
| Ledger | Deadline/schedule checks, SHA-256, SQLite immutability, idempotent scores and feedback | External timestamp publication; automatic learning/promotion intentionally skipped |
| Funds / EV | Policy validation, guarded fund/control creation schema | Betting, payouts, settlement, fund Monte Carlo and EV unavailable |
| Operations | Durable sequential jobs, restart recovery, online backup CLI, CI | APScheduler/crawl cron, daily backup scheduling and off-host sync |

All game configs start with `rules_verified=false`, `ticket_price=null`, and empty
prizes. Structural CSV research for Power/Mega uses explicitly **draft** schedules.
The other games fail closed until operators complete configuration review. Do not
flip flags to claim production readiness. Verify current official rules, sources,
schedule, special-number domains and prize groups first; record review URL/date.
Ledger locking and funds require the review flag. Funds also require price/prizes,
but this release still does not place or settle simulated bets. EV always returns
409 rather than inventing a payout calculation.

## Docker: two services

1. Copy `.env.example` to `.env` and replace `API_KEY` with a long random secret.
   Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. Run `docker compose up --build -d`.
3. Open `http://localhost` or the direct local API at `http://127.0.0.1:8000/docs`.
4. For HTTPS set `SITE_ADDRESS` to your public domain, configure DNS, and expose
   ports 80/443. Caddy obtains certificates automatically.

App runs non-root with one Uvicorn worker and persistent `research_data` volume.
Do not start extra workers/replicas: job recovery assumes one application process.
The old PostgreSQL/Redis volumes are not deleted. Export any existing verified
records before retiring the old deployment. Do not run `docker compose down -v`
on a deployment holding needed history.

## Local development (Python 3.12)

```sh
python -m venv .venv
# Activate .venv using your shell, then:
python -m pip install -r backend/requirements-dev.txt
# Set API_KEY in the environment; .env is loaded by Compose, not Python itself.
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --workers 1
python -m unittest discover -s backend/tests -v
python -m coverage run --source=backend/app -m unittest discover -s backend/tests
python -m coverage report --show-missing
```

No Node.js build, Redis, PostgreSQL, Celery, NumPy, pandas or XGBoost required.
Game files use JSON syntax, a valid YAML subset, to avoid a separate YAML parser.
SQLite path defaults to `data/research.sqlite3`; override `DATABASE_PATH` if needed.

## Verified history import

Use UTF-8 CSV with the header in `data/draws_template.csv`:

```csv
game_code,draw_id,draw_date,draw_slot,main,special,digits,source_url,verified
```

`main`: space-separated integers. `digits`: quoted JSON object containing lists of
three-character strings, preserving leading zeroes. Dates: `YYYY-MM-DD`; slots:
local `HH:MM`. Source URLs must be HTTP(S) for verified rows. `verified` accepts
only `true`, `false`, or blank. Legacy Power files with `n1` through `n6` remain
accepted. Example structure only, **not a real historical result**:

```csv
power655,1,2025-01-02,18:00,1 2 3 4 5 6,7,,https://vietlott.vn,false
```

Only set `verified=true` after independent reconciliation. This is an operator
attestation, **not** automatic two-source verification. Parser success alone is
never verification. Verified records cannot be overwritten/downgraded through
import; corrections need a separately reviewed migration. Unverified updates and
verification promotions retain audit entries. Reimporting identical rows is safe.
CSV is capped at 10 MB / 50,000 rows. One bad row rolls back the whole batch.
ID gaps are reported as ranges; they are never filled with fabricated results.

```sh
curl -X POST http://127.0.0.1:8000/api/import/csv \
  -H "X-API-Key: YOUR_API_KEY" -F "file=@draws.csv"
```

## API and dashboard

Public: `/api/health`, `/api/games`, `/api/games/{code}/draws?limit&before`,
`/api/games/{code}/summary`, `/api/games/{code}/stats/frequency`, `/api/ledger`,
`/api/jobs/{id}`, `/api/backtest/{job_id}`, `/api/models/{id}/feedback`, `/api/funds`.

All writes and expensive computations require `X-API-Key`:

- `POST /api/import/csv`: multipart upload.
- `POST /api/datasets/freeze`: `{"game":"power655"}`; immutable full snapshot + SHA-256.
- `POST /api/backtest`: `{"game":"power655","method":"bayesian","min_train":100,"test_draws":100,"seed":42}`.
  Returns 202 with `job_id`. Poll `/api/jobs/{id}` for queued/running/done/failed.
- `POST /api/games/{code}/randomness`: Monte Carlo frequency tests.
  Interactive report is limited to 2,000 draws to bound synchronous CPU work.
- `POST /api/simulator/illusion`: `{"game":"power655"}`; random history frequency.
- `POST /api/games/{code}/predict`: `target_draw_id`, timezone-aware `target_at`,
  `method`, `count`; blocked for draft rules. Only unseen future IDs accepted.
- `POST /api/funds`: `game`, `method`, `initial_balance`, optional capped `policy`;
  disabled for draft rules/unknown payouts. Creates model/random/no_bet records
  only; no betting/settlement is implemented.

Breaking changes from v1: removed legacy `/api/predictions` GET computation and
single-game `/api/draws`/`summary`. Backtests now return jobs, not inline results.
Expensive randomness/simulator endpoints use POST + authentication rather than
public GET to prevent anonymous CPU exhaustion. API docs: `/docs`.

Dashboard serves without third-party CDNs, stores no API keys in browser storage,
and renders server-side with Jinja escaping. Use HTTPS before entering secrets.
Keys authorize all writes; do not share publicly. Read requests are limited to
120/minute per direct client address. Behind Caddy, clients share the proxy IP
and therefore a conservative shared limit. Configure trusted proxy handling and
edge limits before a large public deployment; do not blindly trust forwarded IPs.

## Research safeguards

- Only verified rows reach statistics, models, snapshots and backtests.
- Walk-forward training contains strictly earlier draws; jobs capture input data
  and config at submission so restart/reimport does not silently change results.
- Bayesian scores sum to `pick_count` and shrink toward `uniform`. They are
  marginal scoring estimates, not proof of winning odds; weighted sampling without
  replacement does not preserve those marginals exactly.
- A real seeded random ticket is evaluated alongside every model ticket.
- Bootstrap intervals are exploratory, not an independent final holdout. They
  assume exchangeable draw-level differences. Repeated experimentation can create
  false discoveries. No automatic "beats random" conclusion is permitted.
- Frequency Monte Carlo simulates sampling without replacement, rather than
  applying an inappropriate independent-ball chi-square reference distribution.
- Ledger hashes use canonical JSON `[game_code,target_draw_id,model_version_id,payload,locked_at]`.
  Database triggers block updates/deletes; scoring lives separately. Local hashes
  alone do not prove public pre-draw publication or resist a DB administrator.
- Score jobs run sequentially every five minutes using the in-process runner.
  Restart resets interrupted jobs to queued. No automatic learning promotion,
  payout optimization, martingale betting, fund reset or deposits.

## Backup and restore

```sh
python -m backend.app.backup data/backups/research-manual.sqlite3
# Docker: path is inside the persistent application volume.
docker compose exec app python -m app.backup /app/data/backups/manual.sqlite3
```

Uses SQLite's online backup API, then `PRAGMA integrity_check`; never copies a
live WAL database as a lone file. Schedule daily execution externally and copy
backups off the VPS. This release does not create that external schedule for you.
Test restore on a **separate new path**: set `DATABASE_PATH` to the backup copy,
start a separate local process/port, inspect counts and ledger hashes. For actual
replacement stop the app first, preserve the original DB and its WAL/SHM files,
then restore a verified snapshot. Never delete the only copy.

## Next acceptance gates

1. Review current official game rules and permitted sources with owner approval.
2. Build fixture-tested adapters, raw snapshots and independent reconciliation;
   demonstrate stable scheduled ingestion over seven days.
3. Add registered trials, untouched holdout, logistic calibration and leakage tests.
4. Complete online learning lineage, payout/EV, immutable simulated bets,
   settlement and fund Monte Carlo tests before enabling those features.
5. Extend visual E2E, production restore drill,
   off-host backups and deployment validation. CI here is testing, not auto-deploy.

## Validation in this revision

26 unittest cases pass on Python 3.12 with the pinned dependencies. Measured
application statement coverage: 92%; analytics 96%, ETL 96%, ledger 89%. CI enforces
80% overall and 80% for these core modules combined. Backup/restore was tested
against a temporary SQLite database. Desktop and 390px mobile layouts and the
simulator/auth-error flow were manually checked in a browser. Docker Compose
configuration validates; image build/runtime was not tested locally because the
Docker daemon was unavailable. GitHub Actions still needs to run remotely.
