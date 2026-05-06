"""View: Recordings.

Fetches the 200 most recent detections from the BirdWeather REST API
(including soundscape audio URLs) and groups them by species.

Each species block shows all its recordings newest-first, each with a
timestamp, confidence score, and an HTML5 <audio> player pointing to the
BirdWeather CDN soundscape URL.

The station token used in the REST URL defaults to STATION_ID (the numeric
station number, e.g. "5114").  If your station uses a different REST token,
set BIRDWEATHER_TOKEN in /etc/birdheatmap/birdheatmap.env.
"""

import logging
import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .. import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Registry interface
# ---------------------------------------------------------------------------

NAME: str = "recordings"
DISPLAY_NAME: str = "Recordings"
DESCRIPTION: str = "Recent detections with audio playback, grouped by species."

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# REST endpoint — e.g. https://app.birdweather.com/api/v1/stations/5114/detections
_DETECTIONS_URL = (
    f"{config.BIRDWEATHER_REST_URL}/stations/{config.BIRDWEATHER_TOKEN}/detections"
)

# How many detections to request per page.
# The BirdWeather REST API returns a maximum of 100 results per request.
_PER_PAGE = 100


def _fmt_ts(ts_raw: str, tz: Any) -> str:
    """Convert an ISO-8601 timestamp string to a human-readable local-time string."""
    try:
        return (
            datetime.fromisoformat(ts_raw)
            .astimezone(tz)
            .strftime("%-I:%M %p · %b %-d, %Y")
        )
    except Exception:
        return ts_raw


# ---------------------------------------------------------------------------
# render_data — called by the Flask route
# ---------------------------------------------------------------------------

def render_data(db: sqlite3.Connection, **params: Any) -> dict:
    """Fetch recent detections from BirdWeather REST API and group by species.

    Returns a dict with:
        groups   — list of species groups, each with a list of recordings
        total    — total number of recordings returned
        error    — error message string if the API call failed (else absent)
    """
    # Get the station's local timezone so we can format timestamps.
    station_row = db.execute("SELECT timezone FROM station LIMIT 1").fetchone()
    tz_name = station_row["timezone"] if station_row else "America/New_York"
    tz = ZoneInfo(tz_name)

    # --- Call the BirdWeather REST API ---
    try:
        resp = requests.get(
            _DETECTIONS_URL,
            params={"per_page": _PER_PAGE},
            timeout=15,
        )
        resp.raise_for_status()
        api_data = resp.json()
    except requests.exceptions.Timeout:
        logger.error("Recordings API timed out")
        return {"error": "BirdWeather API timed out.  Try refreshing.", "groups": [], "total": 0}
    except requests.exceptions.HTTPError as exc:
        logger.error("Recordings API HTTP error: %s", exc)
        return {"error": f"BirdWeather API error: {exc}", "groups": [], "total": 0}
    except Exception as exc:
        logger.error("Recordings API unexpected error: %s", exc)
        return {"error": str(exc), "groups": [], "total": 0}

    # --- Group detections by species ---
    # by_species maps species_id → group dict
    by_species: dict[str, dict] = {}

    for det in api_data.get("detections", []):
        sp = det.get("species") or {}
        sp_id = str(sp.get("id", ""))
        if not sp_id:
            continue

        # Create the species group entry on first encounter.
        if sp_id not in by_species:
            by_species[sp_id] = {
                "id":             sp_id,
                "common_name":    sp.get("commonName") or "Unknown",
                "scientific_name": sp.get("scientificName") or "",
                # Prefer thumbnailUrl (small); fall back to imageUrl (full-size).
                "thumbnail_url":  sp.get("thumbnailUrl") or sp.get("imageUrl") or "",
                "recordings":     [],
                # Internal field used only for sorting — removed before returning.
                "_latest_ts":     "",
            }

        soundscape = det.get("soundscape") or {}
        ts_raw = det.get("timestamp") or ""

        by_species[sp_id]["recordings"].append({
            "id":               det.get("id"),
            "timestamp":        _fmt_ts(ts_raw, tz),
            "confidence":       round((det.get("confidence") or 0) * 100),
            "soundscape_url":   soundscape.get("url") or "",
            # startTime/endTime mark where in the soundscape the bird was detected.
            "soundscape_start": soundscape.get("startTime") or 0,
            "soundscape_end":   soundscape.get("endTime") or 3,
        })

        # Track the latest timestamp so we can sort species groups by recency.
        if ts_raw > by_species[sp_id]["_latest_ts"]:
            by_species[sp_id]["_latest_ts"] = ts_raw

    # --- Sort: species by most recent detection, recordings within each group newest-first ---
    groups = sorted(by_species.values(), key=lambda g: g["_latest_ts"], reverse=True)
    for g in groups:
        # Sort recordings by detection id descending (higher id = more recent).
        g["recordings"].sort(key=lambda r: r.get("id") or 0, reverse=True)
        del g["_latest_ts"]  # remove internal field

    total = sum(len(g["recordings"]) for g in groups)
    return {"groups": groups, "total": total}
