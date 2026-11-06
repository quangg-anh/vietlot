"""End-to-end check: run the real app against a stub OpenAI-compatible server.

Not part of the unit suite (it binds sockets); run it manually:

    .venv314/Scripts/python.exe backend/tests/e2e_advisor_check.py
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

REQUESTS = []


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        REQUESTS.append(body)
        assert self.headers.get("Authorization") == "Bearer stub-key", "missing auth header"
        prompt = json.loads(body["messages"][1]["content"])
        top = [item["number"] for item in prompt["model_probabilities"][:3]]
        answer = {"confidence": 0.8,
                  "boost": [{"number": number, "score": 0.7} for number in top],
                  "suppress": [{"number": prompt["model_probabilities"][-1]["number"], "score": -0.9}],
                  "rationale": "stub: reinforce the model's own top numbers"}
        payload = {"choices": [{"message": {"content": json.dumps(answer)}}],
                   "usage": {"prompt_tokens": 111, "completion_tokens": 22, "total_tokens": 133}}
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    directory = tempfile.TemporaryDirectory()
    database = Path(directory.name) / "e2e.sqlite3"
    os.environ.update({
        "DATABASE_PATH": str(database),
        "API_KEY": "e2e-key",
        "DISABLE_RUNNER": "1",
        "AUTO_SYNC": "0",
        "OPENAI_API_KEY": "stub-key",
        "OPENAI_BASE_URL": f"http://127.0.0.1:{port}/v1",
        "OPENAI_MODEL": "stub-model",
        "OPENAI_MAX_TILT": "0.3",
    })

    import uvicorn
    from backend.app import etl, ledger, llm
    from backend.app.db import initialize
    from backend.app.games import game
    from backend.app.main import app
    from backend.tests.test_research import csv_bytes, draw

    initialize()
    spec = game("power655")

    # Seed eligible crawled-style history through the real import path.
    rows = []
    for index in range(30):
        day = (datetime(2025, 1, 2) + timedelta(days=index * 2)).date()
        if day.weekday() in spec["weekdays"]:
            rows.append(draw(len(rows) + 1, day.isoformat()))
    etl.import_csv(csv_bytes(rows))
    highest = max(int(row["draw_id"]) for row in rows)

    # 1. Advisor status endpoint over real HTTP.
    server_thread = threading.Thread(
        target=uvicorn.run, args=(app,), kwargs={"host": "127.0.0.1", "port": 3399, "log_level": "warning"},
        daemon=True)
    server_thread.start()
    for _ in range(60):
        try:
            urllib.request.urlopen("http://127.0.0.1:3399/api/health", timeout=1)
            break
        except Exception:
            time.sleep(0.25)

    def get(path):
        return json.loads(urllib.request.urlopen(f"http://127.0.0.1:3399{path}", timeout=20).read())

    def post(path, key=None):
        request = urllib.request.Request(f"http://127.0.0.1:3399{path}", data=b"", method="POST")
        if key:
            request.add_header("X-API-Key", key)
        return json.loads(urllib.request.urlopen(request, timeout=90).read())

    status = get("/api/games/power655/advisor")["advisor"]
    assert status["enabled"] and status["provider"] == "127.0.0.1" and status["model"] == "stub-model", status
    print("status ok:", {key: status[key] for key in ("enabled", "provider", "model", "max_tilt")})

    preview = post("/api/games/power655/advisor/preview", "e2e-key")
    assert preview["suggestion"], "advisor was not called"
    assert preview["tilt"] and preview["tilt"]["applied"], preview["tilt"]
    assert abs(sum(item["probability"] for item in preview["tilted"]) - 6.0) < 1e-9
    boosted = {item["number"]: item["delta"] for item in preview["tilted"]}
    top = preview["top"][0]
    assert boosted[top] > 0, "top number should be nudged up"
    print("preview ok: strength", round(preview["tilt"]["strength"], 3),
          "advisor boosts", [item["number"] for item in preview["suggestion"]["boost"]])

    # 2. Full lock path with the advisor active.
    target = datetime.now().astimezone() + timedelta(days=40)
    while target.weekday() not in spec["weekdays"]:
        target += timedelta(days=1)
    hour, minute = (int(part) for part in spec["slots"][0].split(":"))
    target = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
    from unittest.mock import patch
    with patch.object(ledger, "game", return_value=spec | {"rules_verified": True}):
        result = ledger.lock("power655", highest + 1, target.isoformat(), "adaptive", 5)

    payload = result["payload"]
    assert payload["method"] == "adaptive"
    assert payload["advisor"]["model"] == "stub-model", payload["advisor"]
    assert payload["advisor"]["usage"]["total_tokens"] == 133
    assert "scores" not in payload["advisor"]
    assert len(payload["tickets"]) == 5
    assert len(payload["base_probabilities"]) == 55
    assert abs(sum(payload["probabilities"]) - 6.0) < 1e-9
    assert abs(sum(payload["base_probabilities"]) - 6.0) < 1e-9
    assert payload["probabilities"] != payload["base_probabilities"], "tilt had no effect"
    print("lock ok: version", payload["model_revision"], "coverage", round(payload["coverage"]["coverage"], 4))
    print("ledger payload keys:", sorted(key for key in payload if key in
                                         ("advisor", "tilt", "base_probabilities", "weights", "coverage", "tickets")))

    # 3. Ledger immutability still holds.
    connection = sqlite3.connect(database, timeout=30)
    try:
        connection.execute("UPDATE predictions SET payload='{}'")
        raise AssertionError("ledger allowed an update")
    except sqlite3.IntegrityError as exc:
        print("immutability ok:", exc)
    finally:
        connection.rollback()
        connection.close()

    # 5. Score the advisor prediction against the real target draw, then confirm
    #    the measured effect reaches the dashboard payload.
    from backend.app import autopilot
    from backend.app.db import connect as db_connect
    target_local = target.astimezone(autopilot.ZoneInfo(spec["timezone"]))
    with db_connect() as raw:
        raw.execute(
            "INSERT INTO draws VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("power655", highest + 1, target_local.date().isoformat(), target_local.strftime("%H:%M"),
             json.dumps([1, 2, 3, 4, 5, 6]), None, "{}", "https://vietlott.vn", 1,
             "2026-01-01T00:00:00+00:00"))
    with patch.object(ledger, "datetime", wraps=datetime) as clock:
        clock.now.return_value = target + timedelta(minutes=30)
        scored = ledger.score_pending("power655")
    assert scored.get("scored") == 1, scored
    effect = autopilot.workspace("power655")["predictions"][0]["score"]["advisor_effect"]
    print("scored ok: advisor logloss_gain", round(effect["logloss_gain"], 6), "·", effect["interpretation"][:40])

    # 6. Dashboard shell still serves and the new JS parses.
    page = urllib.request.urlopen("http://127.0.0.1:3399/?code=power655", timeout=20).read().decode()
    assert 'id="model-learning"' in page and 'id="prediction-footnote"' in page
    script = urllib.request.urlopen("http://127.0.0.1:3399/static/dashboard.js", timeout=20).read().decode()
    assert "advisorLabel" in script and "componentLabel" in script
    print("dashboard ok: new renderers served")
    assert len(REQUESTS) >= 2, f"expected advisor calls, saw {len(REQUESTS)}"
    server.shutdown()
    with patch.object(ledger, "game", return_value=spec | {"rules_verified": True}):
        offline = ledger.lock("power655", highest + 1, target.isoformat(), "adaptive", 5, once=True)
        assert offline["id"] == result["id"], "repeated lock must return the stored prediction"
        fresh = ledger.lock("power655", highest + 2, target.isoformat(), "adaptive", 4)
    assert "advisor" not in fresh["payload"], "advisor was recorded while offline"
    assert "tilt" not in fresh["payload"]
    assert len(fresh["payload"]["tickets"]) == 4
    assert abs(sum(fresh["payload"]["probabilities"]) - 6.0) < 1e-9
    print("offline lock ok: idempotent", offline["id"], "· fresh prediction", fresh["id"], "without advisor fields")

    print("\nALL E2E CHECKS PASSED")
    server.server_close()
    directory.cleanup()


if __name__ == "__main__":
    main()