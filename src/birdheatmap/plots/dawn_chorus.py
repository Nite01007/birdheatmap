"""Plot: Dawn Chorus — who sings first relative to sunrise.

Each detection's timestamp is converted to "minutes from local sunrise" using
astral.  Panels are faceted small-multiples (one per species), shared x-axis
−60 to +180 minutes.  Panels are sorted by median singing time so the plot
reads left-to-right, top-to-bottom in sequence from first to last singer.

Note: station-wide plot — species_id is accepted but ignored.
Select any species in the UI to trigger the render.
"""

import io
import math
import sqlite3
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
from astral import LocationInfo
from astral.sun import sun

# ---------------------------------------------------------------------------
# Registry interface
# ---------------------------------------------------------------------------

NAME: str = "dawn_chorus"
DISPLAY_NAME: str = "Dawn Chorus"
DESCRIPTION: str = (
    "Who sings first? Small-multiple histograms of detection time relative to "
    "local sunrise, one panel per species, sorted earliest-to-latest singer. "
    "Station-wide — species selector is ignored; pick any species to render."
)
PARAMS: list[dict[str, Any]] = [
    {
        "name": "year",
        "type": "int",
        "label": "Year",
        "default": None,
        "choices": None,
    },
    {
        "name": "season",
        "type": "select",
        "label": "Season",
        "default": "spring",
        "choices": ["all", "spring", "summer", "fall", "winter"],
    },
    {
        "name": "top_n",
        "type": "int",
        "label": "Top N species",
        "default": 12,
        "choices": None,
    },
    {
        "name": "bin_minutes",
        "type": "int",
        "label": "Bin (minutes)",
        "default": 5,
        "choices": None,
    },
]

# ---------------------------------------------------------------------------
# Season → months mapping (calendar-year slice)
# ---------------------------------------------------------------------------

_SEASON_MONTHS: dict[str, tuple[int, ...]] = {
    "spring": (3, 4, 5),
    "summer": (6, 7, 8),
    "fall":   (9, 10, 11),
    "winter": (12, 1, 2),
    "all":    (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12),
}

# ---------------------------------------------------------------------------
# Theme palettes
# ---------------------------------------------------------------------------

_PALETTES = {
    "dark": {
        "bg":       "#0e0e14",
        "panel_bg": "#16161f",
        "fg":       "#c8c8d4",
        "title":    "#ffffff",
        "grid":     "#1e1e2c",
        "bar":      "#90d8ff",
        "sunrise":  "#e8a04a",
        "note":     "#666680",
        "subtitle": "#888899",
        "spine":    "#333348",
    },
    "light": {
        "bg":       "#f8f8fc",
        "panel_bg": "#eeeef6",
        "fg":       "#2a2a3c",
        "title":    "#0a0a18",
        "grid":     "#dcdce8",
        "bar":      "#1a6fa8",
        "sunrise":  "#b86a10",
        "note":     "#888899",
        "subtitle": "#555566",
        "spine":    "#bbbbcc",
    },
}

# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

_WINDOW_MIN = -60
_WINDOW_MAX = 180

def render(db: sqlite3.Connection, species_id: int, **params: Any) -> bytes:
    """Return a PNG of the dawn chorus small-multiples for the given year/season."""
    year       = int(params.get("year") or datetime.now(tz=timezone.utc).year)
    season     = str(params.get("season") or "spring")
    top_n      = max(1, int(params.get("top_n") or 12))
    bin_min    = max(1, int(params.get("bin_minutes") or 5))
    palette    = _PALETTES["light" if params.get("theme") == "light" else "dark"]
    season_months = _SEASON_MONTHS.get(season, _SEASON_MONTHS["spring"])

    station = db.execute("SELECT * FROM station LIMIT 1").fetchone()
    tz_name = station["timezone"] if station else "America/New_York"
    lat     = station["lat"]      if station else 42.305149
    lon     = station["lon"]      if station else -72.45105
    tz      = ZoneInfo(tz_name)

    observer = LocationInfo(latitude=lat, longitude=lon, timezone=tz_name).observer

    # ── Fetch year's detections ───────────────────────────────────────────
    year_start = f"{year}-01-01T00:00:00+00:00"
    year_end   = f"{year + 1}-01-01T00:00:00+00:00"
    rows = db.execute(
        """
        SELECT d.species_id, d.timestamp_utc, s.common_name
        FROM detection d
        JOIN species s ON s.id = d.species_id
        WHERE d.timestamp_utc >= ? AND d.timestamp_utc < ?
        """,
        (year_start, year_end),
    ).fetchall()

    # ── Precompute sunrise by date ────────────────────────────────────────
    sunrise_cache: dict[date, datetime | None] = {}

    def _sunrise(d: date) -> datetime | None:
        if d not in sunrise_cache:
            try:
                s = sun(observer, date=d, tzinfo=tz)
                sunrise_cache[d] = s["sunrise"]
            except Exception:
                sunrise_cache[d] = None
        return sunrise_cache[d]

    # ── Compute delta_min per detection in the window ────────────────────
    # sp_deltas: species_id → (common_name, [delta_min, ...])
    sp_deltas: dict[int, tuple[str, list[float]]] = {}
    for row in rows:
        dt    = datetime.fromisoformat(row["timestamp_utc"]).astimezone(tz)
        month = dt.month
        if month not in season_months:
            continue
        sr = _sunrise(dt.date())
        if sr is None:
            continue
        delta = (dt - sr).total_seconds() / 60.0
        if _WINDOW_MIN <= delta <= _WINDOW_MAX:
            sid = row["species_id"]
            if sid not in sp_deltas:
                sp_deltas[sid] = (row["common_name"], [])
            sp_deltas[sid][1].append(delta)

    # ── Select top_n species by count in window ──────────────────────────
    qualified = [
        {"name": name, "deltas": deltas, "count": len(deltas),
         "median": float(np.median(deltas))}
        for sid, (name, deltas) in sp_deltas.items()
        if len(deltas) >= 5
    ]
    by_count = sorted(qualified, key=lambda s: s["count"], reverse=True)[:top_n]
    # Sort panels by median singing time (earliest → top-left)
    species_plot = sorted(by_count, key=lambda s: s["median"])
    n = len(species_plot)

    total_detections = sum(s["count"] for s in species_plot)
    season_label     = season.capitalize()

    # ── Empty-data guard ──────────────────────────────────────────────────
    def _blank(msg: str) -> bytes:
        fig, ax = plt.subplots(figsize=(14, 4), dpi=100)
        fig.patch.set_facecolor(palette["bg"])
        ax.set_facecolor(palette["bg"])
        ax.text(0.5, 0.5, msg,
                transform=ax.transAxes, ha="center", va="center",
                color=palette["note"], fontsize=12)
        ax.set_title(f"Dawn Chorus: who sings first ({season_label} {year})",
                     color=palette["title"], fontsize=13, fontweight="bold",
                     pad=14, loc="left")
        for sp in ax.spines.values():
            sp.set_color(palette["spine"])
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor(),
                    bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    if n == 0:
        return _blank(
            f"No detections in the −60…+180 min sunrise window "
            f"({season_label} {year})"
        )

    # ── Grid layout ───────────────────────────────────────────────────────
    n_cols = 4 if n >= 8 else 3
    n_rows = math.ceil(n / n_cols)
    panel_h = 2.6
    fig_height = max(5, n_rows * panel_h + 1.2)   # +1.2 for title + footer

    bins = np.arange(_WINDOW_MIN, _WINDOW_MAX + bin_min, bin_min)

    fig, axes = plt.subplots(
        n_rows, n_cols,
        figsize=(14, fig_height),
        dpi=100,
        sharex=True,
        sharey=False,
    )
    fig.patch.set_facecolor(palette["bg"])

    # Flatten axes array for easy indexing; guard n_rows=1 edge case.
    if n_rows == 1:
        axes_flat = list(axes) if n_cols > 1 else [axes]
    else:
        axes_flat = [ax for row in axes for ax in (row if n_cols > 1 else [row])]

    for idx, ax in enumerate(axes_flat):
        ax.set_facecolor(palette["panel_bg"])
        for spine in ax.spines.values():
            spine.set_color(palette["spine"])
        ax.tick_params(axis="both", colors=palette["fg"], labelsize=7.5,
                       length=2, width=0.5)

        if idx < n:
            sp   = species_plot[idx]
            hist, edges = np.histogram(sp["deltas"], bins=bins)
            centers      = (edges[:-1] + edges[1:]) / 2

            ax.bar(centers, hist, width=bin_min * 0.85,
                   color=palette["bar"], alpha=0.8, zorder=3)
            ax.axvline(0, color=palette["sunrise"], linewidth=1.0,
                       linestyle="--", alpha=0.9, zorder=4)

            ax.set_title(sp["name"], color=palette["fg"], fontsize=7.5,
                         fontweight="bold", pad=3)
            # Median tick annotation
            ax.axvline(sp["median"], color=palette["bar"], linewidth=0.8,
                       linestyle=":", alpha=0.7, zorder=4)

            ax.grid(axis="y", color=palette["grid"], linewidth=0.4, zorder=1)
            ax.set_ylim(bottom=0)
            ax.yaxis.set_major_locator(ticker.MaxNLocator(3, integer=True))
            ax.yaxis.set_tick_params(labelsize=6.5)

            # X label only on bottom row
            if idx >= (n_rows - 1) * n_cols:
                ax.set_xlabel("min from sunrise", color=palette["note"],
                              fontsize=7, labelpad=3)
            ax.set_xlim(_WINDOW_MIN, _WINDOW_MAX)
            ax.xaxis.set_major_locator(ticker.MultipleLocator(60))
        else:
            # Hide unused panels
            ax.set_visible(False)

    # ── Figure titles & footer ────────────────────────────────────────────
    fig.suptitle(
        f"Dawn Chorus: who sings first ({season_label} {year})",
        color=palette["title"], fontsize=13, fontweight="bold",
        x=0.02, ha="left", y=0.99,
    )

    fig.text(
        0.5, 0.005,
        f"Minutes relative to local sunrise.  n={total_detections:,} detections "
        f"across top {n} species.  Dashed line = sunrise (0 min).  "
        f"Dotted line = species median.",
        ha="center", fontsize=7.5, color=palette["note"],
    )

    fig.tight_layout(rect=[0, 0.025, 1, 0.97])

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor(),
                bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
