"""View: Species Recordings.

Shows the most recent detections for a single species — up to 100 results,
which is the BirdWeather REST API's per-request maximum.

The species is identified by its numeric ID (same ID used in the local DB
and in the BirdWeather API).  The species name is looked up from the local DB.

Route: GET /recordings/species/<species_id>
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

NAME: str = "species_recordings"
DISPLAY_NAME: str = "Species Recordings"
DESCRIPTION: str = "Recent recordings for a single species, with audio playback."

# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

_DETECTIONS_URL = (
    f"{config.BIRDWEATHER_REST_URL}/stations/{config.BIRDWEATHER_TOKEN}/detections"
)


def _fmt_ts(ts_raw: str, tz: Any) -> str:
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

def render_data(db: sqlite3.Connection, species_id: int, **params: Any) -> dict:
    """Fetch the most recent recordings for one species from the BirdWeather REST API.

    Returns a dict with:
        recordings     — list of recording records, newest first
        species_id     — the species ID requested
        common_name    — species common name (from local DB)
        scientific_name — species scientific name (from local DB)
        total          — number of recordings returned
        error          — error message if the API call failed (else absent)
    """
    # Look up species name from local DB.
    sp_row = db.execute(
        "SELECT common_name, scientific_name FROM species WHERE id = ?",
        (species_id,),
    ).fetchone()
    common_name     = sp_row["common_name"]     if sp_row else f"Species {species_id}"
    scientific_name = sp_row["scientific_name"] if sp_row else ""

    station_row = db.execute("SELECT timezone FROM station LIMIT 1").fetchone()
    tz = ZoneInfo(station_row["timezone"] if station_row else "America/New_York")

    # Fetch from BirdWeather REST API with species filter.
    try:
        resp = requests.get(
            _DETECTIONS_URL,
            params={"per_page": 100, "species_id": species_id},
            timeout=15,
        )
        resp.raise_for_status()
        api_data = resp.json()
    except requests.exceptions.Timeout:
        logger.error("Species recordings API timed out for species_id=%d", species_id)
        return {
            "error": "BirdWeather API timed out.  Try refreshing.",
            "recordings": [], "total": 0,
            "species_id": species_id,
            "common_name": common_name, "scientific_name": scientific_name,
        }
    except Exception as exc:
        logger.error("Species recordings API error for species_id=%d: %s", species_id, exc)
        return {
            "error": str(exc), "recordings": [], "total": 0,
            "species_id": species_id,
            "common_name": common_name, "scientific_name": scientific_name,
        }

    recordings = []
    for det in api_data.get("detections", []):
        soundscape = det.get("soundscape") or {}
        ts_raw = det.get("timestamp") or ""
        recordings.append({
            "id":               det.get("id"),
            "timestamp":        _fmt_ts(ts_raw, tz),
            "confidence":       round((det.get("confidence") or 0) * 100),
            "soundscape_url":   soundscape.get("url") or "",
            "soundscape_start": soundscape.get("startTime") or 0,
            "soundscape_end":   soundscape.get("endTime") or 3,
        })

    return {
        "recordings":     recordings,
        "species_id":     species_id,
        "common_name":    common_name,
        "scientific_name": scientific_name,
        "total":          len(recordings),
    }
