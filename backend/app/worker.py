import os, time, logging
from .db import Base, engine, SessionLocal
from .collector import run_collection

logging.basicConfig(level=logging.INFO)
Base.metadata.create_all(bind=engine)

def main():
    interval = max(1, int(os.getenv("FETCH_INTERVAL_HOURS", "24"))) * 3600
    while True:
        try:
            with SessionLocal() as db:
                logging.info("Collector check: %s", run_collection(db))
        except Exception:
            logging.exception("Collection check failed")
        time.sleep(interval)

if __name__ == "__main__":
    main()
