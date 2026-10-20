CREATE TABLE IF NOT EXISTS draws (
    game_code TEXT NOT NULL, draw_id INTEGER NOT NULL CHECK(draw_id > 0),
    draw_date TEXT NOT NULL, draw_slot TEXT NOT NULL DEFAULT '',
    main TEXT NOT NULL, special INTEGER, digits TEXT NOT NULL,
    source_url TEXT NOT NULL, verified INTEGER NOT NULL CHECK(verified IN (0,1)),
    imported_at TEXT NOT NULL, PRIMARY KEY(game_code, draw_id),
    UNIQUE(game_code, draw_date, draw_slot)
);
CREATE INDEX IF NOT EXISTS draws_verified ON draws(game_code, verified, draw_date, draw_slot);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY, action TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY, game_code TEXT NOT NULL, payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS datasets_no_update BEFORE UPDATE ON datasets
BEGIN SELECT RAISE(ABORT,'dataset is immutable'); END;
CREATE TRIGGER IF NOT EXISTS datasets_no_delete BEFORE DELETE ON datasets
BEGIN SELECT RAISE(ABORT,'dataset is immutable'); END;
CREATE TABLE IF NOT EXISTS model_versions (
    id INTEGER PRIMARY KEY, game_code TEXT NOT NULL, method TEXT NOT NULL,
    parent_version_id INTEGER REFERENCES model_versions(id),
    trained_through_draw_id INTEGER, parameters TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS model_versions_no_update BEFORE UPDATE ON model_versions
BEGIN SELECT RAISE(ABORT,'model version is immutable'); END;
CREATE TRIGGER IF NOT EXISTS model_versions_no_delete BEFORE DELETE ON model_versions
BEGIN SELECT RAISE(ABORT,'model version is immutable'); END;
CREATE TABLE IF NOT EXISTS model_training_state (
    game_code TEXT PRIMARY KEY, status TEXT NOT NULL,
    started_at TEXT, finished_at TEXT, last_error TEXT,
    model_version_id INTEGER REFERENCES model_versions(id)
);
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY, game_code TEXT NOT NULL, target_draw_id INTEGER NOT NULL,
    target_at TEXT NOT NULL, model_version_id INTEGER NOT NULL REFERENCES model_versions(id),
    payload TEXT NOT NULL, payload_hash TEXT NOT NULL UNIQUE, locked_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS predictions_no_update BEFORE UPDATE ON predictions
BEGIN SELECT RAISE(ABORT,'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS predictions_no_delete BEFORE DELETE ON predictions
BEGIN SELECT RAISE(ABORT,'ledger is append-only'); END;
CREATE TABLE IF NOT EXISTS scores (
    prediction_id INTEGER PRIMARY KEY REFERENCES predictions(id),
    result TEXT NOT NULL, scored_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS scores_no_update BEFORE UPDATE ON scores
BEGIN SELECT RAISE(ABORT,'score is immutable'); END;
CREATE TRIGGER IF NOT EXISTS scores_no_delete BEFORE DELETE ON scores
BEGIN SELECT RAISE(ABORT,'score is immutable'); END;
CREATE TABLE IF NOT EXISTS feedback_events (
    id INTEGER PRIMARY KEY, prediction_id INTEGER NOT NULL UNIQUE REFERENCES predictions(id),
    model_version_id INTEGER NOT NULL REFERENCES model_versions(id), draw_id INTEGER NOT NULL,
    action TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('backtest','score','crawl')),
    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued'
    CHECK(status IN ('queued','running','done','failed')),
    result TEXT, error TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status,id);
CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
    game_code TEXT NOT NULL, config TEXT NOT NULL, metrics TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sim_funds (
    id INTEGER PRIMARY KEY, group_id TEXT NOT NULL, game_code TEXT NOT NULL,
    owner_name TEXT NOT NULL, initial_balance INTEGER NOT NULL CHECK(initial_balance > 0),
    balance INTEGER NOT NULL CHECK(balance >= 0), policy TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','bankrupt','closed')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS raw_snapshots (
    id INTEGER PRIMARY KEY, game_code TEXT NOT NULL, source TEXT NOT NULL,
    url TEXT NOT NULL, content_hash TEXT NOT NULL, relative_path TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crawl_observations (
    snapshot_id INTEGER NOT NULL REFERENCES raw_snapshots(id),
    game_code TEXT NOT NULL, draw_id INTEGER NOT NULL,
    PRIMARY KEY(snapshot_id,game_code,draw_id),
    FOREIGN KEY(game_code,draw_id) REFERENCES draws(game_code,draw_id)
);
CREATE TABLE IF NOT EXISTS crawl_state (
    game_code TEXT PRIMARY KEY, next_history_page INTEGER NOT NULL DEFAULT 1,
    history_complete INTEGER NOT NULL DEFAULT 0,
    oldest_draw_id INTEGER, scheduled INTEGER NOT NULL DEFAULT 0,
    next_due TEXT, last_job_id INTEGER REFERENCES jobs(id),
    last_success_at TEXT, last_error TEXT, failures INTEGER NOT NULL DEFAULT 0
);
