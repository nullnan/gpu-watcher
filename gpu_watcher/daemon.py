from __future__ import annotations

import logging
import threading

from .api import serve_api
from .config import load_config
from .remote import RemoteExecutor
from .scheduler import Scheduler
from .store import Store


def run_daemon(config_path: str, log_level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config(config_path)
    store = Store(config.daemon.db_path)
    store.init()
    store.sync_workers(config.workers)

    scheduler = Scheduler(config, store, RemoteExecutor(config.daemon.remote_base_dir))
    if not config.api.enabled:
        scheduler.run_forever()
        return

    thread = threading.Thread(target=scheduler.run_forever, name="scheduler", daemon=True)
    thread.start()
    serve_api(config, store, scheduler)
