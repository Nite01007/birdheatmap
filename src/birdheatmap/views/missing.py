"""View: Missing (Gone Quiet).

Shows species that were actively detected in a past "comparison" window but
have had ZERO detections in the equivalent current window.

Comparison modes:
    last_week   — current = last 7 days,  comparison = 8–14 days ago
    last_month  — current = last 30 days, comparison = 31–60 days ago
    prev_year   — current = last 30 days, comparison = same 30-day window
                  exactly one year earlier (same calendar dates, prior year)

Results are sorted by last_seen descending (most recently gone quiet first),
which makes it easy to spot species that dropped out just a few days ago.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Registry interface
# ---------------------------------------------------------------------------

NAME: str = "missing"
DISPLAY_NAME: str = "Missing"
DESCRIPTION: str = "Species that were detected before but have gone silent."

# Human-readable labels shown in the comparison selector UI.
COMPARISONS: dict[str, str] = {
    "last_week":  "vs Last Week",
    "last_month": "vs Last Month",
    "prev_year":  "vs Same Period Last Year",
}

# Window length in days for each comparison mode.
_WINDOW_DAYS: dict[str, int] = {
    "last_week":  7,
    "last_month": 30,
    "prev_year":  30,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _window_boundaries(
    comparison: str, now_utc: datetime
) -> tuple[datetime, datetime, datetime, datetime]:
    """Return (current_start, current_end, comparison_start, comparison_end) in UTC.

    All four boundaries are timezone-aware UTC datetimes.
    """
    days = _WINDOW_DAYS.get(comparison, 7)

    # "Current" window: the most recent N days up to now.
    current_end = now_utc
    current_start = now_utc - timedelta(days=days)

    if comparison == "prev_year":
        # Same calendar window but shifted back exactly one year.
        comparison_end = now_utc.replace(year=now_utc.year - 1)
        comparison_start = comparison_end - timedelta(days=days)
    else:
        # "vs Last Week" or "vs Last Month": comparison is the N days immediately
        # before the current window.
        comparison_end = current_start
        comparison_start = current_start - timedelta(days=days)

    return current_start, current_end, comparison_start, comparison_end


# ---------------------------------------------------------------------------
# render_data — called by the Flask route
# ---------------------------------------------------------------------------

def render_data(db: sqlite3.Connection, **params: Any) -> dict:
    """Query the local DB for species present in the comparison window but
    absent from the current window.

    Returns a dict with:
        missing          — list of missing species records, last_seen desc
        comparison       — the selected comparison key
        comparison_label — human-readable label
        comparisons      — full dict of {key: label} for building the selector UI
        count            — total number of missing species found
    """
    comparison = params.get("comparison", "last_week")
    if comparison not in COMPARISONS:
        comparison = "last_week"

    station_row = db.execute("SELECT timezone FROM station LIMIT 1").fetchone()
    tz = ZoneInfo(station_row["timezone"] if station_row else "America/New_York")
    now_utc = datetime.now(timezone.utc)

    curr_start, _curr_end, comp_start, comp_end = _window_boundaries(
        comparison, now_utc
    )

    # Find species that:
    #   (a) have at least one detection in [comp_start, comp_end), AND
    #   (b) have NO detection in [curr_start, now)
    # Return last detection in the comparison period and the count for context.
    rows = db.execute(
        """
        SELECT
            s.id,
            s.common_name,
            s.scientific_name,
            MAX(d.timestamp_utc)  AS last_seen_utc,
            COUNT(d.id)           AS count_in_comparison
        FROM detection d
        JOIN species s ON s.id = d.species_id
        WHERE d.timestamp_utc >= :comp_start
          AND d.timestamp_utc <  :comp_end
          AND NOT EXISTS (
                SELECT 1
                FROM detection d2
                WHERE d2.species_id = d.species_id
                  AND d2.timestamp_utc >= :curr_start
          )
        GROUP BY d.species_id, s.id, s.common_name, s.scientific_name
        ORDER BY last_seen_utc DESC
        """,
        {
            "comp_start": comp_start.isoformat(),
            "comp_end":   comp_end.isoformat(),
            "curr_start": curr_start.isoformat(),
        },
    ).fetchall()

    missing = []
    for r in rows:
        ts_local = datetime.fromisoformat(r["last_seen_utc"]).astimezone(tz)
        missing.append({
            "id":               r["id"],
            "common_name":      r["common_name"],
            "scientific_name":  r["scientific_name"],
            "last_seen":        ts_local.strftime("%-I:%M %p · %b %-d, %Y"),
            "count":            r["count_in_comparison"],
        })

    return {
        "missing":          missing,
        "comparison":       comparison,
        "comparison_label": COMPARISONS[comparison],
        "comparisons":      COMPARISONS,
        "count":            len(missing),
    }
