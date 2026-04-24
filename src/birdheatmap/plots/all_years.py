"""Plot: All-years overlay — every available year for one species on one grid.

Same axes as annual_heatmap (day-of-year × time-of-day), but each calendar
year is drawn in a distinct colour so activity patterns can be compared
directly across years.  Sunrise/sunset curves use the most recent year's
geometry (they shift only a minute or two year-to-year at this latitude).
"""

import io
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from astral import LocationInfo
from astral.sun import sun

# ---------------------------------------------------------------------------
# Registry interface
# ---------------------------------------------------------------------------

NAME: str = "all_years"
DISPLAY_NAME: str = "All Years (overlay)"
DESCRIPTION: str = (
    "Every available year for one species overlaid on the same grid, "
    "each year in a distinct colour."
)
PARAMS: list[dict[str, Any]] = []   # no per-render parameters


# ---------------------------------------------------------------------------
# Colour palette (edit here to restyle)
# ---------------------------------------------------------------------------

_PALETTES = {
    "dark": {
        "bg":       "#0e0e14",
        "fg":       "#c8c8d4",
        "title":    "#ffffff",
        "grid":     "#1e1e2c",
        "sun":      "#e8a04a",
        "note":     "#666680",
        "subtitle": "#888899",
        "spine":    "#333348",
    },
    "light": {
        "bg":       "#f8f8fc",
        "fg":       "#2a2a3c",
        "title":    "#0a0a18",
        "grid":     "#dcdce8",
        "sun":      "#b86a10",
        "note":     "#888899",
        "subtitle": "#555566",
        "spine":    "#bbbbcc",
    },
}

_YEAR_COLOURS = {
    "dark": [
        "#60c8f8",  # bright cyan-blue
        "#ff9070",  # bright coral
        "#70e882",  # bright sage green
        "#e080f8",  # bright lavender
        "#ffd050",  # bright amber
        "#ff8888",  # bright rose
        "#80d8f8",  # bright sky
        "#a0f090",  # bright mint
    ],
    "light": [
        "#1a6fa8",  # dark blue
        "#c04020",  # dark coral
        "#1a7830",  # dark green
        "#7020a0",  # dark purple
        "#906010",  # dark amber
        "#b03050",  # dark rose
        "#105898",  # dark sky
        "#208040",  # dark mint
    ],
}


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render(db: sqlite3.Connection, species_id: int, **_params: Any) -> bytes:
    """Return a 1000×800 PNG showing all available years overlaid."""
    theme        = "light" if _params.get("theme") == "light" else "dark"
    palette      = _PALETTES[theme]
    year_colours = _YEAR_COLOURS[theme]

    # ── DB lookups ────────────────────────────────────────────────────────
    station = db.execute("SELECT * FROM station LIMIT 1").fetchone()
    species = db.execute(
        "SELECT common_name, scientific_name FROM species WHERE id = ?", (species_id,)
    ).fetchone()
    if species is None:
        raise ValueError(f"Species id {species_id} not found in database")

    tz_name: str = station["timezone"] if station else "America/New_York"
    lat: float   = station["lat"]      if station else 42.305149
    lon: float   = station["lon"]      if station else -72.45105
    tz = ZoneInfo(tz_name)

    # ── Fetch all detections, grouped by year ────────────────────────────
    rows = db.execute(
        "SELECT timestamp_utc FROM detection WHERE species_id = ? ORDER BY timestamp_utc",
        (species_id,),
    ).fetchall()

    # year → set of (day-of-year, 5-min bucket) pairs
    by_year: dict[int, set[tuple[int, int]]] = {}
    for row in rows:
        dt     = datetime.fromisoformat(row["timestamp_utc"]).astimezone(tz)
        yr     = dt.year
        doy    = dt.timetuple().tm_yday
        minute = (dt.hour * 60 + dt.minute) // 5 * 5
        by_year.setdefault(yr, set()).add((doy, minute))

    years_sorted = sorted(by_year.keys())
    if not years_sorted:
        latest_year = datetime.now(tz=timezone.utc).year
    else:
        latest_year = years_sorted[-1]

    # ── Sunrise / sunset for the most recent year ─────────────────────────
    observer = LocationInfo(latitude=lat, longitude=lon, timezone=tz_name).observer
    is_leap  = (latest_year % 4 == 0 and latest_year % 100 != 0) or (latest_year % 400 == 0)
    n_days   = 366 if is_leap else 365
    jan1     = date(latest_year, 1, 1)

    sun_doys, sun_rises, sun_sets = [], [], []
    for doy in range(1, n_days + 1):
        d = jan1 + timedelta(days=doy - 1)
        try:
            s    = sun(observer, date=d, tzinfo=tz)
            rise = s["sunrise"].hour * 60 + s["sunrise"].minute + s["sunrise"].second / 60
            sset = s["sunset"].hour  * 60 + s["sunset"].minute  + s["sunset"].second  / 60
            sun_doys.append(doy)
            sun_rises.append(rise)
            sun_sets.append(sset)
        except Exception:
            pass

    # ── Month tick positions ──────────────────────────────────────────────
    month_tick_doys:   list[int] = []
    month_tick_labels: list[str] = []
    for month in range(1, 13):
        d = date(latest_year, month, 1)
        month_tick_doys.append(d.timetuple().tm_yday)
        month_tick_labels.append(d.strftime("%b"))

    # ── Figure ────────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(10, 8), dpi=100)
    fig.patch.set_facecolor(palette["bg"])
    ax.set_facecolor(palette["bg"])

    # Sunrise / sunset
    ax.plot(sun_doys, sun_rises, color=palette["sun"], linewidth=0.9,
            linestyle=(0, (3, 4)), alpha=0.7, zorder=3)
    ax.plot(sun_doys, sun_sets,  color=palette["sun"], linewidth=0.9,
            linestyle=(0, (3, 4)), alpha=0.7, zorder=3)

    # One scatter per year
    legend_handles = []
    for i, yr in enumerate(years_sorted):
        colour  = year_colours[i % len(year_colours)]
        buckets = by_year[yr]
        days    = [b[0] for b in buckets]
        mins    = [b[1] for b in buckets]
        ax.scatter(days, mins, s=2.0, color=colour, alpha=0.80,
                   linewidths=0, zorder=4 + i)
        handle = plt.Line2D(
            [0], [0], marker="o", color="none",
            markerfacecolor=colour, markersize=6, label=str(yr),
        )
        legend_handles.append(handle)

    # Sunrise/sunset legend entry
    legend_handles.append(
        plt.Line2D([0], [0], color=palette["sun"], linewidth=1.2,
                   linestyle=(0, (3, 4)), label="Sunrise / Sunset")
    )

    # ── Axes ──────────────────────────────────────────────────────────────
    ax.set_ylim(1440, 0)
    ax.yaxis.set_major_locator(ticker.MultipleLocator(120))

    def _fmt_min(val: float, _pos: Any) -> str:
        return f"{int(val) // 60:02d}:00"

    ax.yaxis.set_major_formatter(ticker.FuncFormatter(_fmt_min))

    ax.set_xlim(1, n_days)
    ax.xaxis.set_major_locator(ticker.FixedLocator(month_tick_doys))
    ax.xaxis.set_major_formatter(ticker.FixedFormatter(month_tick_labels))

    for spine in ax.spines.values():
        spine.set_color(palette["spine"])
    ax.tick_params(axis="both", colors=palette["fg"], labelsize=9, length=4, width=0.6)
    ax.xaxis.set_tick_params(pad=6)
    ax.grid(axis="x", color=palette["grid"], linewidth=0.6, zorder=1)
    ax.grid(axis="y", color=palette["grid"], linewidth=0.4, zorder=1)

    # ── Labels ────────────────────────────────────────────────────────────
    ax.set_title(
        f"Annual Song Observations: {species['common_name']}",
        color=palette["title"], fontsize=13, fontweight="bold", pad=14, loc="left",
    )
    ax.text(
        1.0, 1.012,
        f"All years  ·  {species['scientific_name']}",
        transform=ax.transAxes, ha="right", fontsize=9,
        color=palette["subtitle"], style="italic",
    )
    ax.set_ylabel("Time of day", color=palette["fg"], fontsize=9, labelpad=8)

    if not years_sorted:
        ax.text(0.5, 0.5, "No detections for this species",
                transform=ax.transAxes, ha="center", va="center",
                color=palette["note"], fontsize=12)

    ax.legend(
        handles=legend_handles, loc="lower right", fontsize=8,
        framealpha=0.3, labelcolor=palette["fg"],
        edgecolor=palette["spine"], facecolor=palette["bg"],
    )

    fig.text(
        0.5, 0.005,
        "Showing sunrise & sunset times.  Scale: 5 minute buckets, max count/bucket = 1 per year",
        ha="center", fontsize=7.5, color=palette["note"],
    )

    fig.tight_layout(rect=[0, 0.025, 1, 1])

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor(),
                bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
