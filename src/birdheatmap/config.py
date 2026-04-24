"""Runtime configuration loaded from environment variables (or a .env file).

In production the env file lives at /etc/birdheatmap/birdheatmap.env and is
declared as EnvironmentFile= in the systemd unit.  During local development
you can put the same key=value pairs in a .env file at the repo root.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from repo root when running locally; a no-op in production because
# the env vars are already present from the systemd EnvironmentFile.
load_dotenv()


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            "Check /etc/birdheatmap/birdheatmap.env (production) or .env (dev)."
        )
    return value


STATION_ID: str = os.environ.get("STATION_ID", "5114")

DB_PATH: Path = Path(os.environ.get("DB_PATH", "/var/lib/birdheatmap/birdweather.sqlite"))

CACHE_PATH: Path = Path(os.environ.get("CACHE_PATH", "/var/lib/birdheatmap/cache"))

BIND_HOST: str = os.environ.get("BIND_HOST", "0.0.0.0")

BIND_PORT: int = int(os.environ.get("BIND_PORT", "8765"))

# How often the in-process scheduler triggers an incremental sync.
SYNC_INTERVAL_MINUTES: int = int(os.environ.get("SYNC_INTERVAL_MINUTES", "60"))

# BirdWeather GraphQL endpoint (not expected to change, but overridable for tests).
BIRDWEATHER_API_URL: str = os.environ.get(
    "BIRDWEATHER_API_URL", "https://app.birdweather.com/graphql"
)

# Earliest date to request during backfill.  Set this to the date your station
# first came online (or a few days before to be safe).  Using a date long before
# the station existed wastes pages; using one after it misses early history.
BACKFILL_FROM_DATE: str = os.environ.get("BACKFILL_FROM_DATE", "2020-01-01")

# How many detections to request per GraphQL page during backfill.
BACKFILL_PAGE_SIZE: int = int(os.environ.get("BACKFILL_PAGE_SIZE", "500"))

# Approximate delay between GraphQL requests during backfill (seconds).
BACKFILL_RATE_LIMIT_SECONDS: float = float(os.environ.get("BACKFILL_RATE_LIMIT_SECONDS", "1.0"))

# Log a progress message every N pages during backfill.
BACKFILL_LOG_EVERY_N_PAGES: int = int(os.environ.get("BACKFILL_LOG_EVERY_N_PAGES", "10"))
