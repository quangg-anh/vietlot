from pathlib import Path

from dotenv import load_dotenv


project_root = Path(__file__).resolve().parent
load_dotenv(project_root / ".env")

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
