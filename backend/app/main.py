import os, io, csv, logging
from datetime import datetime
from fastapi import FastAPI, Depends, Header, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert
from .db import Base, engine, get_db
from .models import Draw
from .schemas import DrawIn, BacktestRequest
from .analytics import number_scores, candidate_sets, backtest

logging.basicConfig(level=logging.INFO)
Base.metadata.create_all(bind=engine)
app = FastAPI(title="Power 6/55 Research API", version="0.1.0",
              description="Statistical research only; not a winning-number prediction service.")
origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET","POST"],
                   allow_headers=["Content-Type","X-API-Key"])

def require_key(x_api_key: str | None = Header(default=None)):
    expected = os.getenv("API_KEY", "")
    if not expected or not x_api_key or x_api_key != expected:
        raise HTTPException(status_code=401, detail="Missing or invalid API key")

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "power655-api"}

@app.get("/api/draws")
def list_draws(limit: int = 100, db: Session = Depends(get_db)):
    limit = max(1, min(limit, 1000))
    rows = db.query(Draw).order_by(Draw.draw_date.desc(), Draw.draw_id.desc()).limit(limit).all()
    return [{"draw_id": r.draw_id, "draw_date": str(r.draw_date),
             "numbers": [r.n1,r.n2,r.n3,r.n4,r.n5,r.n6], "special": r.special,
             "verified": r.verified, "source_url": r.source_url} for r in rows]

@app.get("/api/summary")
def summary(db: Session = Depends(get_db)):
    rows = db.query(Draw).order_by(Draw.draw_date, Draw.draw_id).all()
    if not rows:
        return {"draw_count": 0, "message": "Import verified draw history to begin."}
    import numpy as np
    nums = [getattr(r, f"n{i}") for r in rows for i in range(1,7)]
    counts = np.bincount(nums, minlength=56)[1:]
    return {"draw_count": len(rows), "verified_count": sum(bool(r.verified) for r in rows),
            "first_date": str(rows[0].draw_date), "latest_date": str(rows[-1].draw_date),
            "frequency": [{"number": i+1, "count": int(counts[i])} for i in range(55)]}

@app.get("/api/predictions")
def predictions(method: str = "bayesian", count: int = 5, db: Session = Depends(get_db)):
    if method not in {"uniform","bayesian","xgboost"}:
        raise HTTPException(400, "method must be uniform, bayesian, or xgboost")
    rows = db.query(Draw).filter(Draw.verified == True).order_by(Draw.draw_date, Draw.draw_id).all()
    if not rows:
        raise HTTPException(409, "No verified draws; import history first")
    try:
        scores = number_scores(rows, method)
        sets = candidate_sets(scores, count=count, seed=len(rows))
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))
    return {"method": method, "training_draws": len(rows), "sets": sets,
            "warning": "Candidate sets are exploratory, not more likely to win than a fair random draw."}

@app.post("/api/backtest", dependencies=[Depends(require_key)])
def run_backtest(req: BacktestRequest, db: Session = Depends(get_db)):
    rows = db.query(Draw).filter(Draw.verified == True).order_by(Draw.draw_date, Draw.draw_id).all()
    try:
        return backtest(rows, req.min_train, req.test_draws, req.method)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))

@app.post("/api/import/csv", dependencies=[Depends(require_key)])
async def import_csv(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Upload a .csv file")
    raw = await file.read()
    if len(raw) > 10_000_000:
        raise HTTPException(413, "CSV too large (max 10 MB)")
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        required = {"draw_id","draw_date","n1","n2","n3","n4","n5","n6","special"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError("Missing required CSV headers")
        count = 0
        for item in reader:
            data = DrawIn(**{k: item[k] for k in required} | {"source_url": item.get("source_url","")})
            vals = data.model_dump()
            # Do not mark a record verified merely because its structure is valid.
            # A human must independently reconcile it with the official result.
            vals["verified"] = str(item.get("verified", "")).strip().lower() == "true"
            existing = db.get(Draw, data.draw_id)
            if existing:
                for k, v in vals.items():
                    setattr(existing, k, v)
            else:
                db.add(Draw(**vals))
            count += 1
        db.commit()
        return {"imported": count, "note": "Only rows with verified=true are used by analytics. Set this flag only after independently reconciling each row against official results."}
    except Exception as exc:
        db.rollback()
        raise HTTPException(400, f"CSV import failed: {exc}")

@app.post("/api/collector/run", dependencies=[Depends(require_key)])
def collector_run():
    from .collector import fetch_candidate_rows
    try:
        rows = fetch_candidate_rows()
        return {"parsed_candidates": len(rows), "candidates": [
            {"date": str(r["date"]), "numbers": r["numbers"], "source_url": r["source_url"]}
            for r in rows[:50]],
            "note": "Candidates are not imported automatically. Verify against the official result before importing."}
    except Exception as exc:
        raise HTTPException(502, f"Source fetch/parse failed: {exc}")
