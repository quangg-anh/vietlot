from pathlib import Path

import sys

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - depends on the deployed venv
    sys.stderr.write(
        "gunicorn.conf.py: python-dotenv is missing, so .env is NOT loaded and every setting "
        "(API_KEY, OPENAI_*) falls back to its default. Run "
        "'pip install -r backend/requirements.txt' with the interpreter that runs gunicorn.\n")
    load_dotenv = None


project_root = Path(__file__).resolve().parent
if load_dotenv is not None:
    # override=False: anything already exported by the process manager wins over .env.
    load_dotenv(project_root / ".env", override=False)

chdir = str(project_root)
wsgi_app = "backend.app.main:app"
worker_class = "uvicorn_worker.UvicornWorker"
workers = 1
threads = 1
bind = "127.0.0.1:3399"
pidfile = str(project_root / "gunicorn.pid")
accesslog = "-"
errorlog = "-"
loglevel = "info"
