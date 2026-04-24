"""Disk-based PNG render cache.

Cache key: (plot_type, species_id, year, db_last_modified)
File name:  <plot_type>__<species_id>__<year>__<db_hash>.png

"db_last_modified" is the max(timestamp_utc) across all detections.  When a
new detection is inserted the hash changes and the cached file is stale (we
just generate a new file; old ones are cleaned up lazily).
"""

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _key_to_filename(
    plot_type: str,
    species_id: int,
    year: int,
    db_last_modified: str,
    theme: str = "dark",
) -> str:
    short_hash = hashlib.sha1(db_last_modified.encode()).hexdigest()[:12]
    return f"{plot_type}__{species_id}__{year}__{theme}__{short_hash}.png"


def get_cached(
    cache_dir: Path,
    plot_type: str,
    species_id: int,
    year: int,
    db_last_modified: str,
    theme: str = "dark",
) -> bytes | None:
    """Return cached PNG bytes if the file exists, otherwise None."""
    path = cache_dir / _key_to_filename(plot_type, species_id, year, db_last_modified, theme)
    if path.exists():
        logger.debug("Cache hit: %s", path.name)
        return path.read_bytes()
    return None


def put_cached(
    cache_dir: Path,
    plot_type: str,
    species_id: int,
    year: int,
    db_last_modified: str,
    png_bytes: bytes,
    theme: str = "dark",
) -> Path:
    """Write PNG bytes to the cache directory and return the path."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / _key_to_filename(plot_type, species_id, year, db_last_modified, theme)
    path.write_bytes(png_bytes)
    logger.debug("Cache write: %s (%d bytes)", path.name, len(png_bytes))
    return path
