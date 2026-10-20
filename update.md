# Vietlott Research Platform v2 – Đặc tả tối giản (SQLite, không Redis)

**Phiên bản:** 1.2 (tối giản chi phí vận hành; thêm vòng học từ sai số và quỹ giả lập) · **Repo gốc:** github.com/quangg-anh/vietlot · **Đối tượng đọc:** dev / AI coding agent sẽ viết lại hệ thống

**Thay đổi so với bản 1.0:** bỏ Redis, bỏ PostgreSQL (dùng SQLite), bỏ Celery/RQ, bỏ MLflow, bỏ Next.js, gộp thành **một container ứng dụng + Caddy**. Mọi nguyên tắc nghiên cứu (baseline, ledger, holdout, không quảng bá) giữ nguyên.

---

## 0. Tóm tắt và nguyên tắc bắt buộc

| # | Nội dung |
| --- | --- |
| Mục tiêu | Nền tảng nghiên cứu dữ liệu đa game Vietlott: thu thập, kiểm định, thống kê, backtest, công cụ EV, dashboard |
| Game hỗ trợ | Power 6/55, Mega 6/45, Lotto 5/35, Max 3D, Max 3D Pro |
| Triển khai | 1 VPS nhỏ, Docker Compose (2 container), HTTPS |
| Nguyên tắc 1 | **Không quảng bá, không viết copy** kiểu "AI dự đoán trúng", "số chắc ăn", "đảm bảo lợi nhuận" |
| Nguyên tắc 2 | Mọi model phải so với baseline random trên dữ liệu ngoài mẫu. Không vượt thì hệ thống hiển thị kết luận đó |
| Nguyên tắc 3 | Dự đoán phải được khóa (hash) trước giờ quay, không sửa được sau |
| Nguyên tắc 4 | Dữ liệu chỉ dùng cho huấn luyện khi `verified = true` |
| Nguyên tắc 5 | Tôn trọng điều khoản, robots.txt và rate limit của nguồn dữ liệu |
| Nguyên tắc 6 (mới) | Ưu tiên ít thành phần nhất có thể: không thêm dịch vụ nền nếu một thư viện trong tiến trình làm được |

**Cơ sở khoa học:** các game đều quay số ngẫu nhiên. Kỳ vọng số trùng của một bộ ngẫu nhiên là `k × k / N` (6/55 ≈ 0,655; 6/45 = 0,8; 5/35 ≈ 0,714). Một model đôi lúc trùng 3–4 số không chứng minh model tốt. Cần hàng trăm kỳ, kiểm định thống kê và hiệu chỉnh nhiều lần thử.

---

## 1. Phạm vi

### 1.1 Trong phạm vi
- Thu thập và xác minh lịch sử kết quả 5 game.
- Kiểm định tính ngẫu nhiên từng game.
- Thống kê mô tả, Bayesian, thử nghiệm ML có kiểm soát.
- Backtest walk-forward với kiểm định ý nghĩa.
- Sổ ghi dự đoán (ledger) khóa hash.
- Công cụ tính EV, ROI, kiểm soát ngân sách.
- Dashboard web và REST API.

### 1.2 Ngoài phạm vi
- Mua vé, đặt vé hộ, kết nối tài khoản Vietlott.
- Thanh toán, nạp tiền.
- Cam kết hay gợi ý lợi nhuận.

---

## 2. Thông số game

> Mọi số liệu là **cấu hình ban đầu**. Phải đối chiếu thể lệ chính thức trên vietlott.vn trước khi code, lưu vào file cấu hình, không hard-code.

| Game | Mã | Dạng | Miền số | Lịch quay (cần xác minh) |
| --- | --- | --- | --- | --- |
| Power 6/55 | `power655` | 6 số + 1 số đặc biệt | 1–55, đặc biệt khác 6 số chính | Thứ 3, 5, 7 |
| Mega 6/45 | `mega645` | 6 số | 1–45 | Thứ 4, 6, Chủ nhật, khoảng 18h |
| Lotto 5/35 | `lotto535` | 5 số + 1 số đặc biệt | 1–35; miền số đặc biệt xác minh theo thể lệ | 2 kỳ/ngày (13h, 21h) |
| Max 3D | `max3d` | Dãy 3 chữ số theo nhóm giải | 000–999 | Thứ 2, 4, 6 |
| Max 3D Pro | `max3dpro` | Dãy 3 chữ số, cơ cấu giải khác Max 3D | 000–999 | Thứ 3, 5, 7 |

**Số tổ hợp tham chiếu:** C(55,6) = 28.989.675 · C(45,6) = 8.145.060 · C(35,5) = 324.632 (chưa tính số đặc biệt).

**Việc đầu tiên:** tạo `config/games/<code>.yaml` chứa miền số, số lượng số, có số đặc biệt hay không, lịch quay, cơ cấu giải, giá vé, nguồn dữ liệu chính và phụ.

---

## 3. Kiến trúc tối giản

```
[Nguồn chính thức] [Nguồn đối chiếu]
        │                 │
        └──► Adapters ◄───┘
                │
        ETL + Validator ──► SQLite (WAL) + thư mục data/raw/
                │
   ┌────────────┼─────────────┐
   ▼            ▼             ▼
Stats Engine  Model Engine  Payout/EV
   └────► Backtest Engine ◄───┘
                │
          Ledger (khóa hash)
                │
   FastAPI + giao diện server-render  ◄── APScheduler + job runner (cùng tiến trình)
                │
             Caddy (HTTPS)
```

| Thành phần | Công nghệ | Ghi chú |
| --- | --- | --- |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2 (hoặc `sqlite3` thuần), Alembic | Một tiến trình |
| Lịch + job nền | **APScheduler** trong tiến trình + bảng `jobs` trong SQLite | Thay Celery/RQ + Redis |
| DB | **SQLite 3** (chế độ WAL), 1 file `data/app.db` | Thay PostgreSQL |
| Cache | Cache trong bộ nhớ (`functools.lru_cache` / `cachetools` TTL) | Thay Redis |
| Rate limit | `slowapi` (bộ nhớ) hoặc cấu hình Caddy | Thay Redis |
| ML | numpy, pandas, scipy, scikit-learn, statsmodels; xgboost tùy chọn | LSTM/GRU: bỏ khỏi phạm vi mặc định |
| Tracking model | Bảng `model_versions` + `backtest_runs` + file JSON | Thay MLflow |
| Frontend | Jinja2 + HTMX + Tailwind (CDN hoặc build 1 lần) + ECharts | Thay Next.js; không cần build chain Node |
| Reverse proxy | Caddy (HTTPS tự động) | |
| Triển khai | Docker Compose, **2 container: `app`, `caddy`** | VPS 1–2 vCPU, RAM 2 GB, SSD 20 GB là đủ |
| CI | GitHub Actions | lint, test, build, scan |

**Ước tính chi phí:** VPS 2 GB RAM (~5–6 USD/tháng) là đủ. Không cần GPU, không dịch vụ DB/cache riêng.

### 3.1 Cấu trúc thư mục

```
vietlott/
├─ app/
│  ├─ api/            # routers
│  ├─ web/            # templates Jinja2, static, HTMX
│  ├─ core/           # config, security, logging
│  ├─ db/             # models, session, migrations (Alembic)
│  ├─ games/          # GameSpec, registry, loader YAML
│  ├─ adapters/       # một file mỗi game/nguồn
│  ├─ etl/            # fetch, parse, validate, upsert
│  ├─ stats/          # kiểm định ngẫu nhiên, mô tả
│  ├─ models/         # uniform, bayesian, logistic, xgboost (tùy chọn)
│  ├─ backtest/       # walk-forward, metrics, multiple-testing
│  ├─ ledger/         # khóa hash, chấm điểm
│  ├─ payout/         # EV, ROI, anti-crowd
│  └─ jobs/           # scheduler, job runner
├─ tests/
├─ config/games/*.yaml
├─ data/              # app.db, raw/ (snapshot gzip), backups/ (không commit app.db)
├─ infra/             # Caddyfile, backup script
├─ docs/
├─ Dockerfile
└─ docker-compose.yml
```

---

## 4. Mô hình dữ liệu (SQLite)

**Quy ước chuyển đổi từ PostgreSQL:**

| PostgreSQL | SQLite |
| --- | --- |
| `BIGSERIAL` / `SERIAL` | `INTEGER PRIMARY KEY AUTOINCREMENT` |
| `INT[]` | `TEXT` chứa JSON (ví dụ `[1,5,12,23,34,45]`) |
| `JSONB` | `TEXT` chứa JSON (dùng `json_extract` khi cần truy vấn) |
| `BOOL` | `INTEGER` (0/1) với `CHECK` |
| `TIMESTAMPTZ` | `TEXT` ISO 8601 UTC |
| `BYTEA` | Ghi file gzip vào `data/raw/`, DB lưu đường dẫn + sha256 |

**Thiết lập bắt buộc khi mở kết nối:**

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
```

```sql
CREATE TABLE games (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('pick_k','digits')),
  pool_min INTEGER, pool_max INTEGER, pick_count INTEGER,
  has_special INTEGER NOT NULL DEFAULT 0,
  special_min INTEGER, special_max INTEGER,
  ticket_price INTEGER,
  config TEXT                          -- bản sao YAML dạng JSON
);

CREATE TABLE draws (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_code TEXT NOT NULL REFERENCES games(code),
  draw_id INTEGER NOT NULL,            -- mã kỳ chính thức
  draw_date TEXT NOT NULL,             -- YYYY-MM-DD
  draw_slot TEXT NOT NULL DEFAULT 'main',  -- 'main' | '13h' | '21h'
  main TEXT NOT NULL DEFAULT '[]',     -- JSON, pick_k: đã sắp xếp
  special INTEGER,
  digits TEXT,                         -- JSON {tier: ["123","045",...]}
  jackpot_pool INTEGER,
  winners TEXT,                        -- JSON
  source_url TEXT,
  raw_snapshot_id INTEGER REFERENCES raw_snapshots(id),
  verified INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0,1)),
  verified_by TEXT, verified_at TEXT,
  content_hash TEXT NOT NULL,
  fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  UNIQUE (game_code, draw_id)
);
CREATE INDEX idx_draws_game_date ON draws(game_code, draw_date);

CREATE TABLE raw_snapshots (           -- nội dung gốc nằm ở file, DB giữ metadata
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_code TEXT, source TEXT, url TEXT,
  file_path TEXT NOT NULL,             -- data/raw/<game>/<yyyymmdd>/<sha256>.gz
  sha256 TEXT NOT NULL, status INTEGER,
  fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE dataset_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_code TEXT, last_draw_id INTEGER, row_count INTEGER, sha256 TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE model_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_code TEXT, name TEXT, params TEXT,
  dataset_version_id INTEGER REFERENCES dataset_versions(id),
  artifact_path TEXT,                  -- file model/JSON kết quả, nếu có
  parent_version_id INTEGER REFERENCES model_versions(id),  -- phiên bản trước khi học thêm
  trained_through_draw_id INTEGER,     -- kỳ cuối cùng được dùng để học (chống rò rỉ)
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE predictions (             -- ledger
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_code TEXT, target_draw_id INTEGER, model_version_id INTEGER,
  payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL,          -- sha256(game|target|model|payload|ts)
  locked_at TEXT NOT NULL,             -- phải < giờ quay
  scored INTEGER NOT NULL DEFAULT 0, score TEXT
);

-- Ledger bất biến: chặn sửa/xóa trường cốt lõi, chỉ cho cập nhật điểm
CREATE TRIGGER predictions_no_delete BEFORE DELETE ON predictions
BEGIN SELECT RAISE(ABORT,'ledger is append-only'); END;
CREATE TRIGGER predictions_no_edit BEFORE UPDATE OF
  game_code, target_draw_id, model_version_id, payload, payload_hash, locked_at
  ON predictions
BEGIN SELECT RAISE(ABORT,'ledger is append-only'); END;

CREATE TABLE backtest_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_code TEXT, model_version_id INTEGER,
  config TEXT, metrics TEXT,           -- mean_hits, logloss, brier, roi, ci, p_value
  holdout INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE feedback_events (         -- nhật ký "học từ sai" sau mỗi kỳ chấm điểm
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model_version_id INTEGER NOT NULL REFERENCES model_versions(id),
  prediction_id INTEGER NOT NULL REFERENCES predictions(id),
  draw_id INTEGER NOT NULL,            -- kỳ đã có kết quả
  hits INTEGER, logloss REAL, brier REAL,
  residuals TEXT,                      -- JSON: xác suất dự đoán vs kết quả từng số
  action TEXT NOT NULL,                -- updated | skipped | rolled_back | shrunk
  new_version_id INTEGER REFERENCES model_versions(id),
  detail TEXT,
  at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE sim_funds (               -- quỹ giả lập (tiền ảo)
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_code TEXT NOT NULL REFERENCES games(code),
  owner_kind TEXT NOT NULL CHECK (owner_kind IN ('model','control')),
  owner_name TEXT NOT NULL,            -- tên model, hoặc 'random' / 'no_bet'
  initial_balance INTEGER NOT NULL,
  balance INTEGER NOT NULL,
  policy TEXT NOT NULL,                -- JSON: số vé/kỳ, ngưỡng EV, trần % quỹ/kỳ
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','bankrupt','closed')),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE sim_bets (                -- chỉ thêm, không sửa/xóa (có trigger)
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fund_id INTEGER NOT NULL REFERENCES sim_funds(id),
  prediction_id INTEGER REFERENCES predictions(id),  -- bắt buộc với quỹ model
  draw_id INTEGER NOT NULL,
  tickets INTEGER NOT NULL, cost INTEGER NOT NULL,
  payout INTEGER, balance_after INTEGER,
  settled INTEGER NOT NULL DEFAULT 0,
  placed_at TEXT NOT NULL, settled_at TEXT,
  UNIQUE (fund_id, draw_id)
);
CREATE TRIGGER sim_bets_no_delete BEFORE DELETE ON sim_bets
BEGIN SELECT RAISE(ABORT,'sim_bets is append-only'); END;

CREATE TABLE jobs (                    -- hàng đợi thay Redis/Celery
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,                  -- crawl | backtest | score | freeze
  payload TEXT,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','running','done','failed')),
  result TEXT, error TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  started_at TEXT, finished_at TEXT
);
CREATE INDEX idx_jobs_status ON jobs(status, created_at);

CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT, action TEXT, detail TEXT,
  at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
```

**Ràng buộc validator (bắt buộc, kiểm trong code vì SQLite không biểu diễn được hết):**

| Game | Quy tắc |
| --- | --- |
| `pick_k` | Đủ `pick_count` số, khác nhau, trong miền; số đặc biệt (nếu có) trong miền riêng; `draw_id` tăng dần, không thủng |
| `digits` | Mỗi giá trị đúng 3 chữ số (giữ số 0 đầu, lưu chuỗi); số nhóm giải đúng theo cấu hình |
| Chung | Ngày quay khớp lịch game; không trùng `(game, draw_id)`; thay đổi bản ghi đã verified phải ghi `audit_log` |

### 4.1 Quy tắc vận hành SQLite
- **Một writer tại một thời điểm.** Job runner chạy tuần tự (1 worker thread mặc định); các tác vụ ghi dùng transaction ngắn.
- Tính toán nặng (backtest) chạy trong bộ nhớ, **chỉ ghi kết quả ở cuối** để không giữ khóa ghi lâu.
- Job `running` bị bỏ dở khi container restart sẽ được đặt lại `queued` lúc khởi động.
- Với quy mô dữ liệu này (vài chục nghìn kỳ), SQLite dư sức; không cần tối ưu thêm.
- Nếu sau này cần đa người dùng ghi đồng thời nhiều, mới cân nhắc quay lại PostgreSQL (SQLAlchemy giữ đường chuyển dễ).

---

## 5. Thu thập dữ liệu (Adapters + ETL)

### 5.1 Interface

```python
class GameAdapter(Protocol):
    code: str
    def fetch_latest(self) -> list[RawDraw]: ...
    def fetch_range(self, start_id: int, end_id: int) -> list[RawDraw]: ...
    def parse(self, snapshot: RawSnapshot) -> list[Draw]: ...
```

### 5.2 Quy tắc

| Quy tắc | Chi tiết |
| --- | --- |
| Nguồn chính | Cổng kết quả chính thức Vietlott của từng game |
| Nguồn đối chiếu | Nguồn thứ hai độc lập; chỉ đánh `verified=1` khi hai nguồn khớp 100% |
| Resume | Chỉ tải từ kỳ cuối đã lưu |
| Lưu gốc | Mỗi lần fetch ghi file gzip vào `data/raw/` + dòng `raw_snapshots`; parse lại được khi sửa parser |
| Lịch | APScheduler (cron trigger) theo lịch từng game; Lotto 5/35 chạy 2 lần/ngày |
| Lỗi | Retry mũ có jitter; cảnh báo khi 3 lần liên tiếp thất bại hoặc parse ra 0 bản ghi |
| Tuân thủ | Đọc điều khoản và robots.txt trước khi bật crawl; tối đa 1 request/giây; User-Agent rõ ràng. Không giả định có API công khai |
| Fallback | Import CSV thủ công qua `POST /api/import/csv` |

### 5.3 CSV import chung
Header: `game_code,draw_id,draw_date,draw_slot,main,special,digits,source_url,verified`. `main` là chuỗi `1|5|12|...`; `digits` là JSON.

---

## 6. Stats Engine – Randomness Lab

Chạy cho **mỗi game**, lưu kết quả (bảng `backtest_runs` hoặc bảng `stat_results` đơn giản) và hiển thị trên dashboard.

| Kiểm định | Áp dụng | Kết luận hiển thị |
| --- | --- | --- |
| Chi-square tần suất | Mọi số 1..N; từng chữ số mỗi vị trí (Max 3D) | p-value, đồng đều / lệch |
| Runs test | Chẵn/lẻ, lớn/nhỏ theo kỳ | p-value |
| Tự tương quan (lag 1..20) | Tổng 6 số; từng chữ số | ACF, ngưỡng tin cậy |
| Khoảng cách giữa 2 lần xuất hiện | Mỗi số | So với phân phối hình học |
| Phân phối tổng và khoảng cách giữa các số | pick_k | So với Monte Carlo 100k bộ ngẫu nhiên |
| Độc lập các chữ số | Max 3D / Pro | Chi-square độc lập |
| Kiểm định cặp số | pick_k | **Hiệu chỉnh Bonferroni/FDR** vì số cặp lớn |

**Quy tắc kết luận:** hiển thị "Không phát hiện sai lệch" nếu không kiểm định nào vượt ngưỡng sau hiệu chỉnh. Luôn báo số lần đã thử.

### 6.1 Illusion Simulator
Sinh chuỗi ngẫu nhiên thuần cùng kích thước với dữ liệu thật, chạy cùng bộ phân tích, để người dùng thấy "số nóng", "số lạnh", "quy luật" vẫn xuất hiện ở dữ liệu ngẫu nhiên.

---

## 7. Model Engine

### 7.1 Interface chung

```python
class Model(Protocol):
    name: str
    def fit(self, history: DrawFrame) -> None: ...
    def score(self, history: DrawFrame) -> np.ndarray:   # xác suất từng số
        ...
    def sample_tickets(self, scores, n: int, rng) -> list[Ticket]: ...
```

### 7.2 Danh sách model (rút gọn)

| Tầng | Model | Vai trò |
| --- | --- | --- |
| 1 | `uniform` | Baseline bắt buộc; mọi số xác suất `k/N` |
| 2 | `bayesian` | Beta/Dirichlet smoothing theo cửa sổ 10/30/100/300 kỳ |
| 3a | `logistic` | Giải thích được, có hiệu chỉnh xác suất |
| 3b | `xgboost` | Tùy chọn, tham số nhỏ để tránh overfit; chỉ cài khi cần |
| Phụ | `anti_crowd` | Không phải model dự đoán; chọn bộ số ít người chọn (mục 9) |

`seq` (LSTM/GRU) **loại khỏi phạm vi** bản tối giản để tránh phụ thuộc nặng; có thể thêm sau như nhánh nghiên cứu riêng.

### 7.3 Đặc trưng
Tần suất theo cửa sổ 10/30/100/300; số kỳ kể từ lần xuất hiện gần nhất; tần suất cặp; tổng, khoảng cách, chẵn/lẻ, vùng số; đặc trưng thời gian (thứ, tháng). Lotto 5/35 thêm đặc trưng theo kỳ 13h/21h.

**Ghi chú bắt buộc trong code và UI:** đây là *giả thuyết để kiểm nghiệm*, không phải quy luật. "Số lâu chưa về" không có nghĩa sắp về.

### 7.4 Hiệu chỉnh
Chuẩn hóa đầu ra sao cho tổng xác suất bằng `pick_count`. Dùng isotonic hoặc Platt scaling trên tập validation tách riêng.

### 7.5 Vòng học từ sai số (Online Learning Loop)

**Mục đích:** sau mỗi kỳ quay, model nhìn lại dự đoán đã khóa, đo sai ở đâu, cập nhật, rồi sinh dự đoán cho **kỳ kế tiếp**. Đây là cơ chế cập nhật có kiểm soát, không phải bảo đảm model sẽ giỏi lên. Với dữ liệu ngẫu nhiên, kết quả kỳ vọng là model học xong vẫn xấp xỉ `uniform`, và hệ thống phải hiển thị đúng kết luận đó.

**Chu trình mỗi kỳ (job `learn`, chạy sau job `score`):**

| Bước | Việc làm |
| --- | --- |
| 1. Chấm điểm | Lấy dự đoán đã khóa cho kỳ `t`; tính `hits`, log loss, Brier và phần dư từng số (`outcome − xác suất dự đoán`) → ghi `feedback_events` |
| 2. Chẩn đoán | So với `uniform` trên cửa sổ trượt (ví dụ 50 kỳ gần nhất): model có thật sự hơn không, hay chỉ may mắn |
| 3. Cập nhật | Chỉ dùng dữ liệu `≤ t` (xem bảng cơ chế bên dưới) → tạo `model_versions` mới, `parent_version_id` trỏ về bản cũ, `trained_through_draw_id = t` |
| 4. Kiểm tra bản mới | Chạy walk-forward nhanh trên cửa sổ validation. Không tốt hơn bản cũ (hoặc không hơn `uniform`) → **không dùng**, ghi `action = rolled_back` |
| 5. Dự đoán lại | Sinh dự đoán cho kỳ `t+1`, khóa vào ledger bằng phiên bản mới |

**Cơ chế cập nhật theo model:**

| Model | Cách học từ sai |
| --- | --- |
| `bayesian` | Cập nhật hậu nghiệm Beta/Dirichlet; cửa sổ và độ co về `uniform` (shrinkage) là tham số được chọn trên validation |
| `logistic` | Huấn luyện lại hoặc `partial_fit` với trọng số mẫu mới hơn; L2 mạnh |
| `xgboost` | Huấn luyện lại theo lịch (mỗi 20 kỳ), không cập nhật từng kỳ để tránh overfit |
| `ensemble` (mới, tùy chọn) | Trộn các model **kể cả `uniform`** bằng multiplicative weights (Hedge): model nào có log loss kém thì giảm trọng số. Nếu không model nào hơn `uniform`, trọng số dồn về `uniform` |

**Rào chắn bắt buộc (khóa cứng trong code):**

1. **Không dự đoán lại kỳ đã có kết quả.** "Dự đoán lại" luôn là cho kỳ kế tiếp. Dự đoán lại cho kỳ đã quay là rò rỉ dữ liệu và bị chặn.
2. **Không xóa phiên bản cũ.** Mọi `model_versions` và dự đoán đã khóa của chúng đều được chấm điểm và đưa vào bảng xếp hạng, kể cả bản bị `rolled_back`. Cấm chỉ giữ bản "may mắn".
3. **Mỗi lần cập nhật là một lần thử.** Số phiên bản và cấu hình đã thử được cộng vào hiệu chỉnh nhiều lần thử (Bonferroni/FDR) khi kết luận có tín hiệu hay không.
4. **Tách hai loại bằng chứng:** (a) *replay lịch sử* – mô phỏng vòng học trên dữ liệu cũ, dùng để chọn tham số trên train/validation, tuyệt đối không dùng holdout; (b) *forward live* – dự đoán thật cho kỳ chưa quay, đây mới là bằng chứng ngoài mẫu đáng tin nhất.
5. **Tự động rút lui:** nếu trên cửa sổ trượt model kém `uniform` (log loss) liên tục N kỳ, hệ thống đưa về `uniform` và ghi `action = shrunk`.
6. **Tốc độ học nhỏ và có trần:** giới hạn mức thay đổi xác suất giữa hai phiên bản liên tiếp để tránh model "đuổi theo nhiễu".
7. **"Học từ sai" không có nghĩa tăng vé hay tăng tiền sau khi thua.** Xem mục 9.1.

**Ghi chú bắt buộc trên UI:** trang Model Lab hiển thị đường log loss của model so với `uniform` theo thời gian, số phiên bản đã thử và dòng kết luận "Có tín hiệu" / "Không vượt baseline ngẫu nhiên" theo tiêu chí mục 8.3.

---

## 8. Backtest Engine

### 8.1 Quy trình walk-forward
1. Chia dữ liệu: **train / validation / holdout cuối**. Holdout không dùng để chọn model hay tham số.
2. Tại kỳ `t`: chỉ dùng dữ liệu `< t` để fit, dự đoán kỳ `t`.
3. Huấn luyện lại theo lịch (ví dụ mỗi 20 kỳ).
4. So sánh với **≥ 1000 bộ ngẫu nhiên** cho mỗi kỳ (cùng số vé).
5. Báo cáo trên holdout đúng một lần cho model được chọn.

Backtest chạy như job nền (bảng `jobs`), tải dữ liệu vào bộ nhớ một lần, ghi kết quả ở cuối.

### 8.2 Chỉ số

| Chỉ số | Mục đích |
| --- | --- |
| Số trùng trung bình so với baseline | Có hơn random không |
| Phân bố số trùng (0..6) | Nhìn cả đuôi |
| Log loss, Brier score, calibration curve | Chất lượng xác suất |
| Tỷ lệ trúng từng hạng | So với kỳ vọng lý thuyết |
| ROI sau chi phí vé theo cơ cấu giải thật | Giá trị kinh tế |
| Khoảng tin cậy bootstrap 95% | Mức bất định |
| p-value permutation test | Khác random có ý nghĩa không |
| p-value hiệu chỉnh (Bonferroni/FDR) | Tính số model và cấu hình đã thử |

### 8.3 Tiêu chí kết luận (hard-code vào báo cáo)
- **"Có tín hiệu"** chỉ khi: p-value hiệu chỉnh < 0,01 trên holdout **và** khoảng tin cậy không chứa 0 **và** ổn định qua ≥ 3 cửa sổ thời gian.
- Ngược lại ghi tự động: **"Không vượt baseline ngẫu nhiên"**.
- Ghi lại mọi model và cấu hình đã thử (`backtest_runs`), không xóa kết quả xấu.

---

## 9. Payout / EV / Anti-crowd

| Module | Chức năng |
| --- | --- |
| `payout.structure` | Đọc cơ cấu giải từ YAML từng game |
| `payout.ev` | EV mỗi vé = Σ xác suất hạng × giá trị giải − giá vé; Jackpot tích lũy tính theo giá trị hiện tại và số người trúng kỳ vọng |
| `payout.breakeven` | Giá trị Jackpot cần để EV = 0 (chia Jackpot theo số vé bán ước lượng) |
| `anti_crowd` | Điểm "độ phổ biến": phạt số ≤ 31, dãy liên tiếp, mẫu hình học. Chỉ để giảm khả năng phải chia Jackpot. **Không tăng xác suất trúng**, UI phải ghi rõ |
| `budget_guard` | Ngân sách tuần/tháng, cảnh báo khi vượt; không có "gấp thếp" |

Max 3D và Max 3D Pro: giải cố định nên anti-crowd không có ý nghĩa; chỉ hiển thị EV.

### 9.1 Quỹ giả lập (Simulated Fund)

**Mục đích:** cho mỗi model một quỹ **tiền ảo** để "chơi" theo thời gian thực trên các kỳ quay thật, nhằm đo giá trị kinh tế (ROI, sụt giảm, phá sản) trung thực hơn số trùng. Đây là công cụ nghiên cứu. **Không có tiền thật, không nối thanh toán, không nối tài khoản Vietlott.** Mọi màn hình hiển thị nhãn "Tiền giả lập".

**Cấu hình quỹ** (`policy`, lưu trong `sim_funds`, đặt khi tạo và không sửa được sau đó):

| Tham số | Ý nghĩa | Mặc định gợi ý |
| --- | --- | --- |
| `initial_balance` | Vốn ban đầu | 10.000.000 VND ảo |
| `tickets_per_draw` | Số vé cố định mỗi kỳ | 5 |
| `min_ev` (tùy chọn) | Chỉ chơi khi EV kỳ đó ≥ ngưỡng (ví dụ Jackpot ≥ điểm hòa vốn); ngược lại bỏ kỳ | tắt |
| `max_pct_per_draw` | Trần chi mỗi kỳ theo % quỹ hiện tại | 2% |

**Luật quỹ (khóa cứng):**

1. Quỹ model chỉ đặt cược bằng **dự đoán đã khóa trong ledger** (`sim_bets.prediction_id` bắt buộc). Không có dự đoán khóa trước giờ quay thì không có cược.
2. **Mức cược cố định theo policy.** Không gấp thếp, không tăng vé sau khi thua, không Kelly, không "đòi lại vốn". Tính năng nào làm tăng cược theo kết quả trước đều bị cấm.
3. Thanh toán tự động sau khi kỳ quay có `verified = 1`: chi phí = số vé × giá vé; tiền thưởng tính theo cơ cấu giải trong YAML. Với Jackpot, dùng số người trúng thực tế trong `winners` để chia; thiếu dữ liệu thì không thanh toán và đánh dấu chờ.
4. Hết tiền (không đủ trả vé kỳ tới) → `status = bankrupt`, quỹ dừng, ghi nhận phá sản. **Không nạp thêm, không reset.** Muốn thử lại phải tạo quỹ mới, quỹ cũ vẫn giữ nguyên trên bảng xếp hạng.
5. `sim_bets` chỉ thêm, không sửa/xóa (trigger SQLite).

**Quỹ đối chứng (bắt buộc, tạo tự động cùng quỹ model):**

| Quỹ | Vai trò |
| --- | --- |
| `random` | Cùng policy, vé do `uniform` sinh. Đây là baseline thật sự |
| `no_bet` | Không chơi; số dư giữ nguyên, làm mốc "không thua" |
| Monte Carlo | Mô phỏng ≥ 1000 quỹ random chạy cùng chuỗi kỳ quay, để biết quỹ model nằm ở phân vị nào |

**Chỉ số báo cáo:** số dư, ROI, drawdown tối đa, số kỳ chơi/bỏ, tỷ lệ phá sản của quỹ random mô phỏng, phân vị của model trong phân phối quỹ random, khoảng tin cậy bootstrap 95%.

**Kết luận tự động:** quỹ model chỉ được ghi "vượt baseline" khi nằm ngoài khoảng tin cậy của quỹ random **và** đạt tiêu chí mục 8.3. Nếu không, ghi: **"Không vượt quỹ ngẫu nhiên"**. Lưu ý trung thực: vì mọi vé đều có EV âm trong hầu hết trường hợp, quỹ gần như chắc chắn giảm dần theo thời gian. Mục tiêu so sánh là *giảm ít hơn quỹ random hay không*, không phải "có lời".

**Mối liên hệ với vòng học (mục 7.5):** kết quả quỹ là *đầu ra để đo*, không phải tín hiệu học. Không dùng lời/lỗ của quỹ làm phần thưởng để tối ưu model (dễ overfit vào nhiễu giải thưởng hiếm). Model học bằng log loss và Brier ở mục 7.5.

---

## 10. Ledger
- Dự đoán sinh ra → ghi `payload`, tính `payload_hash = sha256(game|target_draw|model_version|payload|timestamp)`.
- Hash công bố trên trang công khai **trước** giờ quay.
- Sau kỳ quay, job tự chấm điểm và lưu `score`.
- Không có endpoint sửa/xóa; trigger SQLite chặn thêm một lớp (mục 4).
- Trang công khai hiển thị lịch sử chấm điểm so với baseline, kể cả khi kết quả kém.
- Tùy chọn: xuất danh sách hash hằng ngày ra file `ledger-YYYY-MM-DD.txt` đặt ở nơi công khai bên ngoài (ví dụ commit vào repo) để tăng độ tin cậy về thời điểm khóa.

---

## 11. REST API

Endpoint ghi/tính toán cần header `X-API-Key`. Endpoint đọc công khai có rate limit.

| Method | Path | Mô tả |
| --- | --- | --- |
| GET | `/api/health` | Trạng thái dịch vụ, nguồn dữ liệu |
| GET | `/api/games` | Danh sách game và cấu hình |
| GET | `/api/games/{code}/draws?limit&before` | Lịch sử kỳ quay đã verified |
| GET | `/api/games/{code}/summary` | Số kỳ, kỳ mới nhất, tỉ lệ verified |
| GET | `/api/games/{code}/stats/frequency` | Tần suất, khoảng cách |
| GET | `/api/games/{code}/randomness` | Kết quả Randomness Lab |
| POST | `/api/games/{code}/predict` | Sinh bộ số tham khảo, khóa vào ledger |
| GET | `/api/ledger?game&status` | Lịch sử dự đoán và điểm |
| POST | `/api/backtest` | Tạo job backtest (`game`, `model`, `min_train`, `test_draws`) |
| GET | `/api/backtest/{id}` | Kết quả |
| GET | `/api/jobs/{id}` | Trạng thái job |
| GET | `/api/models/{id}/feedback` | Nhật ký học từ sai, cây phiên bản (`parent_version_id`) |
| POST | `/api/funds` | Tạo quỹ giả lập (`game`, `model`, `policy`); tự tạo quỹ đối chứng |
| GET | `/api/funds?game&status` | Danh sách quỹ và số dư |
| GET | `/api/funds/{id}` | Lịch sử cược, đường số dư, so với quỹ đối chứng |

Không có endpoint nạp tiền, reset hay sửa quỹ.
| GET | `/api/games/{code}/ev?jackpot=` | Tính EV |
| POST | `/api/import/csv` | Import CSV |
| POST | `/api/datasets/freeze` | Đóng băng phiên bản dữ liệu |
| GET | `/api/simulator/illusion` | Chạy Illusion Simulator |

Mọi response chứa bộ số kèm trường `disclaimer`.

---

## 12. Giao diện (server-render)

FastAPI trả HTML bằng Jinja2; HTMX cho tương tác (lọc, phân trang, polling job); ECharts cho biểu đồ. Không có bước build frontend riêng.

| Trang | Nội dung |
| --- | --- |
| Tổng quan | Thẻ từng game: kỳ mới nhất, trạng thái dữ liệu, kỳ quay kế tiếp |
| Game detail | Lịch sử, bảng/biểu đồ tần suất, heatmap, khoảng cách xuất hiện |
| Randomness Lab | Kết quả kiểm định, số lần thử, kết luận tự động |
| Illusion Simulator | So sánh dữ liệu thật và ngẫu nhiên |
| Bộ số tham khảo | Bộ số kèm điểm, **luôn hiển thị baseline và kết luận backtest mới nhất bên cạnh** |
| Backtest / Model Lab | Hiệu suất, phân bố số trùng, CI, p-value, ROI, so sánh model, dataset version |
| Ledger | Danh sách hash, điểm số công khai |
| Quỹ giả lập | Đường số dư từng quỹ model so với `random` và `no_bet`, phân vị trong Monte Carlo, drawdown, trạng thái phá sản; nhãn cố định "Tiền giả lập" |
| Nhật ký học | Mỗi phiên bản model, lý do cập nhật / rút lui, log loss so với `uniform` theo thời gian |
| EV Calculator | Nhập Jackpot, xem EV và điểm hòa vốn |
| Ngân sách | Giới hạn và chi tiêu tự khai |
| Giám sát | Nguồn lỗi, kỳ thủng, model giảm hiệu suất |

**Yêu cầu UI:** responsive, dark/light mode, i18n (vi mặc định, en). Mỗi trang có bộ số phải có banner cố định: *"Xổ số là ngẫu nhiên. Bộ số chỉ để tham khảo, không đảm bảo trúng thưởng."*

---

## 13. Bảo mật và vận hành

| Hạng mục | Yêu cầu |
| --- | --- |
| Bí mật | Chỉ trong `.env`, không commit; có `.env.example` |
| Mạng | Chỉ Caddy mở 80/443; container `app` không publish cổng ra ngoài |
| Auth | API key cho ghi/tính toán; một người dùng quản trị là đủ ở giai đoạn đầu |
| Rate limit | Theo IP trên endpoint công khai (slowapi hoặc Caddy) |
| **Backup** | Dùng `sqlite3 .backup` (hoặc `VACUUM INTO`) hằng ngày ra `data/backups/`, giữ 14 bản, **đồng bộ ra ngoài VPS** (rclone tới object storage, hoặc Litestream nếu muốn sao chép liên tục); test restore hằng tháng. Không copy thẳng file `.db` khi đang ghi |
| Log | JSON có correlation id; không log secret |
| Giám sát | Healthcheck; cảnh báo email/Telegram khi crawl lỗi hoặc model suy giảm |
| Phụ thuộc | Pin phiên bản, quét lỗ hổng trong CI |
| Riêng tư | Không thu thập dữ liệu cá nhân ngoài tài khoản cần thiết |

---

## 14. Kiểm thử

| Loại | Phạm vi |
| --- | --- |
| Unit | Validator, parser (HTML mẫu cố định mỗi game), EV, hash ledger, trigger ledger |
| Property-based | Sinh bộ số hợp lệ/không hợp lệ cho validator |
| Leakage test | Backtest không dùng dữ liệu `≥ t` khi dự đoán kỳ `t` (bắt buộc trong CI) |
| Reproducibility | Cùng `dataset_version` + seed cho cùng kết quả |
| Learning leakage | Phiên bản học sau kỳ `t` có `trained_through_draw_id ≤ t`; chặn dự đoán lại kỳ đã quay (bắt buộc trong CI) |
| Fund rules | Quỹ không tăng cược sau thua; không cược khi thiếu dự đoán khóa; phá sản thì dừng; không sửa/xóa `sim_bets` |
| Fund settlement | Thanh toán đúng theo cơ cấu giải và số người trúng mẫu; kiểm tra tổng chi/thu khớp |
| Integration | Docker Compose lên, import CSV, chạy backtest nhỏ, restart giữa job |
| E2E | Playwright cho các trang chính (có thể rút gọn) |

Test dùng SQLite `:memory:` hoặc file tạm, không cần dịch vụ ngoài. Coverage ≥ 80% cho `etl/`, `backtest/`, `ledger/`, `payout/`.

---

## 15. Lộ trình và tiêu chí nghiệm thu

| GĐ | Nội dung | Nghiệm thu |
| --- | --- | --- |
| 1 | Schema SQLite đa game, migration, GameSpec YAML, CSV import, adapter Power 6/55 và Mega 6/45 | Import đầy đủ lịch sử xác minh được, báo cáo kỳ thiếu, validator pass |
| 2 | Adapter Lotto 5/35, Max 3D, Max 3D Pro; nguồn đối chiếu; đóng băng dataset; APScheduler | 5 game verified, lịch chạy ổn định ≥ 7 ngày |
| 3 | Randomness Lab, Illusion Simulator, model uniform + bayesian | Báo cáo kiểm định 5 game, có test leakage |
| 4 | Backtest walk-forward, logistic (xgboost tùy chọn), hiệu chỉnh nhiều lần thử, holdout | Báo cáo có CI, p-value hiệu chỉnh, kết luận tự động |
| 5 | Ledger (trigger bất biến), EV, anti-crowd, budget guard | Hash công bố trước giờ quay, chấm điểm tự động |
| 5b | Vòng học từ sai số (7.5), quỹ giả lập và quỹ đối chứng (9.1) | Mỗi kỳ tự chấm điểm → học → khóa dự đoán kỳ sau; quỹ tự thanh toán; test learning leakage và fund rules xanh |
| 6 | Giao diện hoàn chỉnh, giám sát, backup + đồng bộ ngoài, CI/CD, tài liệu | Triển khai VPS có HTTPS, tài liệu vận hành |

Ước lượng cho 1 dev: **5–8 tuần** (giảm so với bản 1.0 nhờ bỏ Next.js, Celery, MLflow, Postgres), tăng nếu nguồn dữ liệu khó trích xuất.

---

## 16. Rủi ro

| Rủi ro | Xử lý |
| --- | --- |
| Cấu trúc trang nguồn thay đổi | Lưu snapshot gốc, parser có test mẫu, fallback CSV |
| Điều khoản nguồn cấm crawl | Dừng crawl, chuyển nhập thủ công hoặc nguồn được phép |
| Overfit và "phát hiện" giả | Holdout, hiệu chỉnh nhiều lần thử, Illusion Simulator |
| Hiểu nhầm là dự đoán chắc chắn | Banner cố định, kết luận backtest cạnh bộ số, không ngôn từ quảng cáo |
| Rủi ro tài chính người chơi | Budget guard, không gấp thếp, liên kết thông tin chơi có trách nhiệm |
| Pháp lý | Rà soát quy định trước khi công khai website |
| Model học theo nhiễu (overfit khi cập nhật từng kỳ) | Shrinkage về `uniform`, trần tốc độ học, tự rút lui, đếm mọi phiên bản vào hiệu chỉnh nhiều lần thử |
| Quỹ giả lập bị hiểu thành cách kiếm tiền | Nhãn "Tiền giả lập", so với quỹ random, hiển thị tỷ lệ phá sản, cấm gấp thếp và nạp thêm |
| **Mất file SQLite** (mới) | Backup hằng ngày + đồng bộ ra ngoài VPS; test restore |
| **Khóa ghi SQLite khi job dài** (mới) | Job tuần tự, tính trong bộ nhớ, ghi cuối; WAL + busy_timeout |
| **Mất job khi restart** (mới) | Bảng `jobs`, đặt lại `running → queued` lúc khởi động; job idempotent |

---

## 17. Hướng dẫn cho dev / AI viết lại
1. Đọc repo cũ để tái sử dụng: Docker, API key, import CSV, walk-forward cơ bản, dashboard.
2. Làm theo thứ tự GĐ 1 → 6; **không** xây giao diện trước khi có dữ liệu verified.
3. Mọi hằng số game nằm trong `config/games/*.yaml`; cấm hard-code `55`, `45`, `35`.
4. Mọi model phải qua baseline `uniform` và leakage test trước khi merge.
5. Mọi copy giao diện phải theo mục 12 (banner, không ngôn từ quảng cáo).
6. **Không thêm dịch vụ nền mới (DB server, cache, message broker) nếu chưa có số đo chứng minh SQLite/tiến trình đơn không đáp ứng được.**
7. Mỗi PR kèm test; CI phải xanh.
8. Điểm chưa rõ về thể lệ: **dừng và hỏi chủ dự án**, không tự suy đoán cơ cấu giải.