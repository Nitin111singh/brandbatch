"""Render worker process: python worker.py  (run one or more alongside the web app)."""
import logging
import os
import signal
import threading

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from app.models import init_engine
from app.services import run_worker

if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    init_engine(os.environ.get("DATABASE_URL", "sqlite:///brandbatch.db"))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    run_worker(stop)
