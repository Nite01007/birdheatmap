"""View: Arrivals — species detected for the first time within a chosen window.

Definition of "arrival":
    A species is an arrival for a given window if it has at least one detection
    inside the window AND has ZERO detections recorded before the window opened.

    This uses the local SQLite database (not the BirdWeather API), so it only
    reflects whatever history has been synced.  If the backfill is incomplete,
    some species that look like "arrivals" may actually have earlier detections
    that simply haven't been synced yet.

Periods:
    week  — last 7 rolling days (not a calendar week)
    month — current calendar month (1st of month to now)
    year  — current calendar year (Jan 1 to now)
    all   — all time (every species' very first detection in the DB)
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Registry interface
# ---------------------------------------------------------------------------

NAME: str = "arrivals"
DISPLAY_NAME: str = "Arrivals"
DESCRIPTION: str = "Species detected for the first time within a chosen window."

# Human-readable labels for each period option (used in the UI selector).
PERIODS: dict[str, str] = {
    "week":  "This Week",
    "month": "This Month",
    "year":  "This Year",
    "all":   "All Time",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _window_start_local(period: str, now_local: datetime) -> datetime:
    """Return the start of the selected window in station local time."""
    if period == "week":
        # Rolling 7-day window, starting at midnight 7 days ago.
        return (now_local - timedelta(days=7)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    if period == "month":
        # Calendar month: midnight on the 1st of this month.
        return now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "year":
        # Calendar year: midnight Jan 1.
        return now_local.replace(
            month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    # "all" — use a date far in the past so the NOT EXISTS check is always false
    # (every detection will appear to have no prior history before 2000-01-01).
    return now_local.replace(
        year=2000, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
    )


# ---------------------------------------------------------------------------
# render_data — called by the Flask route
# ---------------------------------------------------------------------------

def render_data(db: sqlite3.Connection, **params: Any) -> dict:
    """Query the local DB for species arriving within the selected window.

    Returns a dict with:
        arrivals     — list of arrival records, newest first
        period       — the selected period key ("week", "month", etc.)
        period_label — human-readable label for the selected period
        periods      — full dict of {key: label} for building the selector UI
        count        — total number of arrivals found
    """
    period = params.get("period", "week")
    if period not in PERIODS:
        period = "week"

    # Determine station timezone for local-time window boundaries.
    station_row = db.execute("SELECT timezone FROM station LIMIT 1").fetchone()
    tz = ZoneInfo(station_row["timezone"] if station_row else "America/New_York")
    now_local = datetime.now(tz)

    ws_local = _window_start_local(period, now_local)
    # Convert the window-start boundary to UTC for DB comparison.
    ws_utc = ws_local.astimezone(timezone.utc).isoformat()

    # Find every species that:
    #   (a) has at least one detection on or after ws_utc, AND
    #   (b) has NO detection before ws_utc (i.e. it's "new" within this window)
    # For each such species, return the earliest detection in the window and a count.
    rows = db.execute(
        """
        SELECT
            s.id,
            s.common_name,
            s.scientific_name,
            MIN(d.timestamp_utc)  AS first_seen_utc,
            COUNT(d.id)           AS total_in_window
        FROM detection d
        JOIN species s ON s.id = d.species_id
        WHERE d.timestamp_utc >= :ws
          AND NOT EXISTS (
                SELECT 1
                FROM detection d2
                WHERE d2.species_id = d.species_id
                  AND d2.timestamp_utc < :ws
          )
        GROUP BY d.species_id, s.id, s.common_name, s.scientific_name
        ORDER BY first_seen_utc DESC
        """,
        {"ws": ws_utc},
    ).fetchall()

    arrivals = []
    for r in rows:
        # Convert the UTC timestamp back to local time for display.
        ts_local = datetime.fromisoformat(r["first_seen_utc"]).astimezone(tz)
        arrivals.append({
            "id":             r["id"],
            "common_name":    r["common_name"],
            "scientific_name": r["scientific_name"],
            "first_seen":     ts_local.strftime("%-I:%M %p · %b %-d, %Y"),
            "total":          r["total_in_window"],
        })

    return {
        "arrivals":     arrivals,
        "period":       period,
        "period_label": PERIODS[period],
        "periods":      PERIODS,
        "count":        len(arrivals),
    }
