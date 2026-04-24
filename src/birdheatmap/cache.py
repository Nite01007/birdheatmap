"""Disk-based PNG render cache.

Cache key: (plot_type, species_id, year, theme, extra_params, db_last_modified)
File name:  <plot_type>__<species_id>__<year>__<theme>__<db_hash>[__<params_hash>].png

"db_last_modified" is the max(timestamp_utc) across all detections.  When a
new detection is inserted the hash changes and the cached file is stale (we
just generate a new file; old ones are cleaned up lazily).

"extra_params" covers any additional PARAMS beyond year/theme (e.g. min_detections,
sort_by).  Changing those values produces a different cache key so stale PNGs
are not served.
"""

import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _params_hash(extra: dict | None) -> str:
    """Short hash of extra params dict, or empty string if none."""
    if not extra:
        return ""
    payload = json.dumps(sorted(extra.items()), sort_keys=True)
    return hashlib.sha1(payload.encode()).hexdigest()[:8]


def _key_to_filename(
    plot_type: str,
    species_id: int,
    year: int | str,
    db_last_modified: str,
    theme: str = "dark",
    extra_params: dict | None = None,
) -> str:
    short_hash = hashlib.sha1(db_last_modified.encode()).hexdigest()[:12]
    p_hash     = _params_hash(extra_params)
    suffix     = f"__{p_hash}" if p_hash else ""
    return f"{plot_type}__{species_id}__{year}__{theme}__{short_hash}{suffix}.png"


def get_cached(
    cache_dir: Path,
    plot_type: str,
    species_id: int,
    year: int | str,
    db_last_modified: str,
    theme: str = "dark",
    extra_params: dict | None = None,
) -> bytes | None:
    """Return cached PNG bytes if the file exists, otherwise None."""
    path = cache_dir / _key_to_filename(
        plot_type, species_id, year, db_last_modified, theme, extra_params
    )
    if path.exists():
        logger.debug("Cache hit: %s", path.name)
        return path.read_bytes()
    return None


def put_cached(
    cache_dir: Path,
    plot_type: str,
    species_id: int,
    year: int | str,
    db_last_modified: str,
    png_bytes: bytes,
    theme: str = "dark",
    extra_params: dict | None = None,
) -> Path:
    """Write PNG bytes to the cache directory and return the path."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / _key_to_filename(
        plot_type, species_id, year, db_last_modified, theme, extra_params
    )
    path.write_bytes(png_bytes)
    logger.debug("Cache write: %s (%d bytes)", path.name, len(png_bytes))
    return path
