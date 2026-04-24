"""In-process APScheduler that runs incremental syncs on a timer.

Started by `python -m birdheatmap serve`; not used by the CLI sync command.
"""

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from . import config

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def start(db_path) -> None:
    """Start the background scheduler.  db_path is passed through to the sync job."""
    global _scheduler

    from .db import open_db
    from .sync import sync

    def _job() -> None:
        logger.info("Scheduled incremental sync starting …")
        try:
            conn = open_db(db_path)
            sync(conn)
            conn.close()
        except Exception:
            logger.exception("Scheduled sync failed")

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        _job,
        "interval",
        minutes=config.SYNC_INTERVAL_MINUTES,
        id="incremental_sync",
        replace_existing=True,
        # Run once immediately at startup so the first backfill/sync doesn't
        # wait a full interval before beginning.
        next_run_time=datetime.now(tz=timezone.utc),
    )
    _scheduler.start()
    logger.info(
        "Scheduler started; first sync running now, then every %d minutes",
        config.SYNC_INTERVAL_MINUTES,
    )


def stop() -> None:
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
