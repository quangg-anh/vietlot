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
| ETL | Atomic CSV imports, Power/Mega crawler, compressed raw snapshots, provenance audit, operator review, gap ranges | Official adapters and automatic two-source reconciliation |
| Statistics | Verified-only frequency/gaps, Monte Carlo frequency tests, Bonferroni within report, illusion simulator | Runs, ACF, pair/digit tests and report-wide trial registry |
| Models | Automatic source-data Bayesian training, versioned previews and walk-forward evaluation; uniform baseline | Logistic, optional XGBoost, calibration, independent holdout, multiple-model correction |
| Backtest | Walk-forward, sampled random control, Brier/log loss, paired bootstrap CI, input hash | Statistical promotion/edge claims remain disabled |
| Ledger | Deadline/schedule checks, SHA-256, SQLite immutability, idempotent scores and feedback | External timestamp publication; automatic model promotion intentionally skipped |
| Funds / EV | Policy validation, guarded fund/control creation schema | Betting, payouts, settlement, fund Monte Carlo and EV unavailable |
| Operations | Durable sequential jobs, restart recovery, default automatic crawl scheduler with backoff, online backup CLI, CI | Daily backup scheduling and off-host sync |

All game configs start with `rules_verified=false`, `ticket_price=null`, and empty
prizes. Structural imports and crawling use explicitly **draft** schedules for
Power/Mega, Lotto 5/35 and Max 3D/Pro. Reviewed-rule operations stay blocked. Do not
flip flags to claim production readiness. Verify current official rules, sources,
schedule, special-number domains and prize groups first; record review URL/date.
Ledger locking and funds require the review flag. Funds also require price/prizes,
but this release still does not place or settle simulated bets. EV always returns
409 rather than inventing a payout calculation.

## Docker: two services

1. Copy `.env.example` to `.env` and replace `API_KEY` with a long random secret.
   Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. Run `docker compose up --build -d`.
3. Open `http://localhost` or the direct local API at `http://127.0.0.1:3399/docs`.
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
# Configure API_KEY in .env; the launcher loads it automatically.
python -m backend.app
# Default: 127.0.0.1:3399, one worker. Explicit alternative:
# python -m uvicorn backend.app.main:app --env-file .env --host 127.0.0.1 --port 3399 --workers 1
python -m unittest discover -s backend/tests -v
python -m coverage run --source=backend/app -m unittest discover -s backend/tests
python -m coverage report --show-missing
```

No Node.js build, Redis, PostgreSQL, Celery, NumPy, pandas or XGBoost required.
Game files use JSON syntax, a valid YAML subset, to avoid a separate YAML parser.
SQLite path defaults to `data/research.sqlite3`; override `DATABASE_PATH` if needed.

## Linux / aaPanel ASGI startup

Install `requirements.txt` using the service's Python interpreter, then run:

```sh
PYTHON_BIN=/www/server/pyporject_evn/vietlot/bin/python sh start-linux.sh
```

Stop the existing aaPanel Gunicorn project first. Configure the process manager
to use this command instead of its generated sync/gthread command. The launcher
explicitly selects the ASGI worker, one process, and `127.0.0.1:3399`; Nginx must
proxy to that address. It loads `.env` through `gunicorn.conf.py`. Do not run two
instances. `FastAPI.__call__() missing ... send` indicates a WSGI worker is still
being used. On Windows, continue using `python -m backend.app`.

## Automatic result crawling

For production, install runtime dependencies with the same Python interpreter
used to start Uvicorn, from the repository root:

```sh
python3.12 -m pip install -r requirements.txt
python3.12 -m pip check
python3.12 -c "from bs4 import BeautifulSoup; import backend.app.main"
```

Restart the backend after installation. If the service uses a virtual environment,
replace `python3.12` with that environment's Python executable in all commands.
The `bs4` import comes from `beautifulsoup4`, already pinned in
`backend/requirements.txt`; installing dependencies into another Python environment
does not fix the running service.

Power 6/55 and Mega 6/45 fetch public results from `xoso.com.vn`.
Lotto 5/35 fetches latest results from Minh Chinh's live Lotto page, with
`ketquaxoso.net` retained for paginated history and as a fallback if the latest
source cannot be fetched or parsed. Each snapshot records its actual source;
fallbacks are logged and returned as `source_warning`, never silently relabeled.
The Minh Chinh parser requires an explicit draw ID, date, slot, five main balls
and a separate special ball; prize-table decorations are ignored. A latest-page
sync does not mark historical backfill complete or reset its page cursor.
Max 3D and Max 3D Pro use the `ketquaxoso.net` adapter.
Lotto retains both daily slots and its separate special number. Max games retain
all prize groups as three-character strings, including leading zeroes.
These imports remain unverified; rule/prize review gates are unchanged.
Max games use a separate three-digit model; they are not treated as pick-k games.
The dashboard has one operational action: **Sync results**. It also shows
automatic-sync status, model picks, result/prediction history, and per-number
probabilities. Training and backtest controls are no longer exposed there.
Manual Sync requires the configured API key, held only in tab memory.

The single job runner enables automatic sync on startup by default. Every
60 minutes (30 for Lotto) it resumes a 20-page history batch until backfill is complete.
After backfill, latest-result sync runs 45 and 60 minutes after each configured
draw slot, using each game's timezone and draw weekdays (including both Lotto
slots). Existing interval timers are rescheduled outside these windows. Failed
requests retain their retry backoff; manual Sync still runs immediately.
If the worker misses a persisted post-draw sync time (for example during a
restart), it queues one catch-up sync instead of skipping to the next draw.
The dashboard shows the next sync time, queued/running status, history page,
and whether the worker or schedule is disabled. Worker logs report automatic
queueing and each crawl's start/completion with added and unchanged counts.
After checking the latest page, the crawler compares stored results with the
latest configured draw slot that ended at least 45 minutes ago. If results are
still missing, it records `source_stale`, the source's latest draw ID and the
expected slot, and retries within 15 minutes, including outside normal sync
windows. The dashboard warns that the source is behind. A successful HTTP
request alone does not mean the latest draw has been published. Existing
results are retained; missing results are never fabricated.
Set `AUTO_SYNC=0` to turn schedules off on startup;
a manual Sync explicitly enables the selected game's schedule again.
`DISABLE_RUNNER=1` disables both job execution and automatic model updates.
Model updates run in a separate background thread immediately at startup and
every minute, so a slow history crawl cannot block training on imported data.
Power/Mega and Lotto show the saved Bayesian model in the MODEL PICKS panel;
Max 3D/Pro train separate probability distributions over `000`..`999` for every
configured prize group. New eligible results trigger a new immutable model version
within the next model cycle (at most about a minute, plus training time). Unchanged
history reuses the current version. Changes to data, rules/config or algorithm
revision trigger refitting; a version trained on a stale snapshot is not published.

The digit learner compares uniform, smoothed frequency (60/300 draws), and
position-wise Bayesian (120 draws) candidates by past-only categorical log loss.
Configuration selection stops before the trailing evaluation segment; walk-forward
evaluation refits on each earlier prefix, never on the draw being scored. The
selected configuration is then refit on all eligible past results. Repeated rolling
evaluation is exploratory, not an untouched independent holdout or a proven edge.
Uniform selection is a valid outcome when alternatives do worse.

The dashboard shows training/error state, recent version lineage, candidate numbers,
and a prize-group probability selector. Probabilities refer to one prize value,
not ticket/pair win probability. Max 3D candidates target the first-prize group;
Max 3D Pro pairs target the special-prize group. Scores count candidate matches in
that group, not payouts. Predictions are locked only for the next scheduled draw
when the immediately preceding result is available. Once actual results arrive,
predictions are scored once and the new result enters the next refit. Old predictions
and scores are never rewritten. No betting or financial settlement is enabled.

Queue a manual run from the project root:

```sh
python -m backend.app.crawler power655 --mode history --pages 20
python -m backend.app.crawler mega645 --mode latest
python -m backend.app.crawler lotto535 --mode history --pages 20
python -m backend.app.crawler max3d --mode history --pages 20
python -m backend.app.crawler max3dpro --mode latest
python -m backend.app.crawler power655 --schedule on
python -m backend.app.crawler power655 --schedule off
```

`DATABASE_PATH` must match the running application. `--pages` limits fetched
result pages per job (1..50). History runs resume their persisted cursor;
with a one-page budget they fetch the next historical page directly. Larger
budgets refresh the latest page first. Incomplete historical pages stop the
job without advancing that page's cursor, allowing a later retry.

The crawler respects robots.txt, throttles requests, retries transient failures,
rejects redirects and unconfigured hosts, and stores temporary gzip snapshots.
The background worker checks every five minutes and deletes raw HTML files older
than one hour only when every snapshot referencing that file has committed crawl
observations in the database. Existing imported raw files are also eligible.
Failed or unprocessed snapshots are retained for diagnosis. Draws, snapshot
metadata (source URL and SHA-256), observations and audit records are not deleted.
Cleanup runs when the worker is enabled; file errors are logged and retried on
the next pass without blocking crawling.
Conflicting stored results are never overwritten. Scheduled failures use
exponential backoff rather than tight retries.

**Crawled results are automatically accepted for the automatic model workflow.**
No manual verification step is required. Successfully parsed and validated rows
with crawl provenance are included in training, prediction and scoring. Raw
snapshot metadata, source URLs and audit records remain available; imported raw
HTML files are automatically cleaned up as described above. `verified` is not
silently set to true: source-only data is distinct from independent review.
Optional operator review remains available through the authenticated
`/api/games/{code}/draws/verify` endpoint, but does not block automatic operation.
Other games remain unsupported by this adapter.

## Automatic model workflow

The model thread checks inputs every minute, independently of queued crawl work. New crawled or
verified data triggers Bayesian shrinkage training, a persisted model version, five
reproducible reference tickets, and an exploratory walk-forward evaluation
once there are more than 10 eligible draws. Unchanged inputs do not retrain.
The dashboard refreshes itself without reloading the page.

Reference tickets are explicitly **not locked predictions** and never receive
retroactive scores. Automatic pre-draw locking uses the configured game schedule
and requires eligible results through the immediately preceding draw; no manual
rules-review flag is required for this source-data workflow. Stale history still
blocks locking. Locked predictions are immutable and automatically scored against
matching crawler or verified results. Prediction and score metadata identify the
`verified_or_crawled` policy and whether results were independently reviewed.
Arbitrary unreviewed CSV imports without crawler provenance are not automatically
accepted. Legacy research APIs and manually locked predictions retain their
verified-only defaults; funds/payout restrictions are unchanged.
No automatic model promotion or claim of an edge over random selection is made.

`GET /api/games/{code}/workspace?before=ID` returns the combined dashboard state
and paginated result history, including clearly labeled unverified rows.
`POST /api/games/{code}/sync` queues a deduplicated crawl and enables its schedule;
this endpoint requires `X-API-Key`. Existing research/admin APIs remain available.

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
curl -X POST http://127.0.0.1:3399/api/import/csv \
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

### Crawl and scoring regression checks

Run `python -m unittest discover -s backend/tests -q` from the repository root.
The regression fixtures cover all five Minh Chinh game layouts, backup sources,
incomplete live results, immediate scoring after ingestion, and draw #959.
Lotto 5/35 displays main matches and the special-number match separately;
history summarizes one best ticket, never combines matches from different tickets.
Missing predicted special numbers have unknown scores, not automatic misses.
Pending predictions expose a scoring status for missing results, ineligible
sources, mismatched draw slots, invalid lock times, or ledger hash mismatches.
Predictions and stored scores remain immutable. Crawler data is not independent
verification and match counts are not prize or payout calculations.

Draw ingestion checks both game/draw ID and game/date/slot before inserting.
If another source uses a different ID for the same slot and identical main,
special, and digit results, the stored draw ID and metadata remain unchanged.
The new snapshot observation references that stored ID; the import audit records
the source-to-stored ID mapping. Predictions and scores are never renumbered.
Unresolved differences or an ID already assigned to another slot stop the entire
page with an explicit conflict; history progress is retained for retry.
This does not establish which source's draw numbering is authoritative.

Mega history can resolve a source date error using the configured Minh Chinh
reconcile_url. On conflict, it fetches the daily page for the disputed date and
requires an exact match for the incoming draw ID and numbers on a different
date, plus corroboration of every stored conflicting record. Only the incoming
row is replaced; existing draws, predictions and scores are not rewritten.
The corroborating result for the disputed slot is also retained. Corrected rows
reference the Minh Chinh snapshot, not the erroneous source snapshot, and the
import audit and job result retain both versions and their snapshot IDs.
Historical Minh Chinh pages without ball IDs are accepted only in the explicit
historical parser mode; main/special classes and ball counts remain validated.
Reconciliation is limited to three attempts per page. Missing, incomplete or
contradictory evidence leaves the entire page and its history cursor unchanged.
This is a source-backed date correction, not official verification: newly
imported results remain unverified. Deploy both crawler modules and the updated
Mega game configuration to enable this path. No database deletion is needed.

Schedule validation failures are reconciled before persistence when a separate
configured source confirms the exact draw ID and full result on a valid date
and slot. CSV imports still use strict schedule validation. Unresolved errors
include game, draw ID, date, weekday, slot, expected schedule, source URL and
snapshot ID. No historical schedule exceptions are inferred automatically.
Deploy the updated ETL, crawler and source parser modules together.

For a conflict, inspect both stored results and retained unprocessed source
snapshots without changing the database:

```bash
python -m backend.app.inspect_draw --database data/research.sqlite3 --game mega645 --date 2019-11-06
```

Use the production DATABASE_PATH as --database if it differs from this example.
The report includes nearby stored draws and matching results from the last five
unprocessed snapshots. New conflict logs also include both result payloads,
source URLs and the snapshot ID. Do not delete or renumber draws from the error
message alone: establish which result is correct before repairing stored data.
