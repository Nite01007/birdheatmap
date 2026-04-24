"""Plot: Annual Song Observations heatmap.

One dot per 5-minute window (binary presence) across a full calendar year.
X axis  = day of year (Jan 1 → Dec 31), month-labelled.
Y axis  = time of day 00:00–24:00, INVERTED so midnight is at the top.
Overlay = sunrise and sunset dotted curves computed from station coordinates.
"""

import io
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")  # non-interactive, safe for server use
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from astral import LocationInfo
from astral.sun import sun

# ---------------------------------------------------------------------------
# Registry interface
# ---------------------------------------------------------------------------

NAME: str = "annual_heatmap"
DISPLAY_NAME: str = "Annual Song Observations"
DESCRIPTION: str = (
    "Year-long activity grid. Each dot is a 5-minute window with at least one "
    "detection. Sunrise and sunset curves are overlaid."
)
PARAMS: list[dict[str, Any]] = [
    {
        "name": "year",
        "type": "int",
        "label": "Year",
        "default": None,   # web layer substitutes current year when None
        "choices": None,   # populated dynamically from DB years for the species
    },
]


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

_PALETTES = {
    "dark": {
        "bg":       "#0e0e14",
        "fg":       "#c8c8d4",
        "title":    "#ffffff",
        "grid":     "#1e1e2c",
        "dot":      "#90d8ff",
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
        "dot":      "#1a6fa8",
        "sun":      "#b86a10",
        "note":     "#888899",
        "subtitle": "#555566",
        "spine":    "#bbbbcc",
    },
}


def render(db: sqlite3.Connection, species_id: int, **params: Any) -> bytes:
    """Return a 1000×800 PNG of the annual heatmap for *species_id* in *year*."""
    year    = int(params.get("year") or datetime.now(tz=timezone.utc).year)
    palette = _PALETTES["light" if params.get("theme") == "light" else "dark"]

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

    # ── Detection data ────────────────────────────────────────────────────
    year_start = f"{year}-01-01T00:00:00+00:00"
    year_end   = f"{year + 1}-01-01T00:00:00+00:00"
    rows = db.execute(
        """
        SELECT timestamp_utc
        FROM detection
        WHERE species_id = ? AND timestamp_utc >= ? AND timestamp_utc < ?
        """,
        (species_id, year_start, year_end),
    ).fetchall()

    # Convert to (day-of-year, 5-minute bucket) pairs, deduplicated.
    buckets: set[tuple[int, int]] = set()
    for row in rows:
        dt = datetime.fromisoformat(row["timestamp_utc"]).astimezone(tz)
        doy    = dt.timetuple().tm_yday
        minute = (dt.hour * 60 + dt.minute) // 5 * 5
        buckets.add((doy, minute))

    plot_days = [b[0] for b in buckets]
    plot_mins = [b[1] for b in buckets]

    # ── Sunrise / sunset ──────────────────────────────────────────────────
    observer   = LocationInfo(latitude=lat, longitude=lon, timezone=tz_name).observer
    is_leap    = (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)
    n_days     = 366 if is_leap else 365
    jan1       = date(year, 1, 1)

    sun_doys:   list[float] = []
    sun_rises:  list[float] = []
    sun_sets:   list[float] = []

    for doy in range(1, n_days + 1):
        d = jan1 + timedelta(days=doy - 1)
        try:
            s = sun(observer, date=d, tzinfo=tz)
            rise = s["sunrise"].hour * 60 + s["sunrise"].minute + s["sunrise"].second / 60
            sset = s["sunset"].hour  * 60 + s["sunset"].minute  + s["sunset"].second  / 60
            sun_doys.append(doy)
            sun_rises.append(rise)
            sun_sets.append(sset)
        except Exception:
            # Polar-night / midnight-sun edge cases — skip the day.
            pass

    # ── Month tick positions ──────────────────────────────────────────────
    month_tick_doys:   list[int] = []
    month_tick_labels: list[str] = []
    for month in range(1, 13):
        d = date(year, month, 1)
        month_tick_doys.append(d.timetuple().tm_yday)
        month_tick_labels.append(d.strftime("%b"))

    # ── Build figure ──────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(10, 8), dpi=100)

    fig.patch.set_facecolor(palette["bg"])
    ax.set_facecolor(palette["bg"])

    # Sunrise / sunset curves (draw first so dots sit on top)
    ax.plot(sun_doys, sun_rises, color=palette["sun"], linewidth=0.9,
            linestyle=(0, (3, 4)), alpha=0.85, zorder=3, label="Sunrise / Sunset")
    ax.plot(sun_doys, sun_sets,  color=palette["sun"], linewidth=0.9,
            linestyle=(0, (3, 4)), alpha=0.85, zorder=3)

    # Detection dots
    if plot_days:
        ax.scatter(plot_days, plot_mins, s=2.5, color=palette["dot"], alpha=0.85,
                   linewidths=0, zorder=4)

    # ── Y axis: 00:00 at top, 24:00 at bottom ────────────────────────────
    ax.set_ylim(1440, 0)  # invert: 0 min (midnight) floats to top
    ax.yaxis.set_major_locator(ticker.MultipleLocator(120))   # every 2 hours

    def _fmt_minutes(val: float, _pos: Any) -> str:
        h = int(val) // 60
        return f"{h:02d}:00"

    ax.yaxis.set_major_formatter(ticker.FuncFormatter(_fmt_minutes))

    # ── X axis: day-of-year with month labels ─────────────────────────────
    ax.set_xlim(1, n_days)
    ax.xaxis.set_major_locator(ticker.FixedLocator(month_tick_doys))
    ax.xaxis.set_major_formatter(ticker.FixedFormatter(month_tick_labels))

    # ── Spine / tick / grid styling ───────────────────────────────────────
    for spine in ax.spines.values():
        spine.set_color(palette["spine"])

    ax.tick_params(axis="both", colors=palette["fg"], labelsize=9, length=4, width=0.6)
    ax.xaxis.set_tick_params(pad=6)

    ax.set_xticks(month_tick_doys, minor=False)
    ax.grid(axis="x", color=palette["grid"], linewidth=0.6, zorder=1)
    ax.grid(axis="y", color=palette["grid"], linewidth=0.4, zorder=1)

    # ── Titles & labels ───────────────────────────────────────────────────
    ax.set_title(
        f"Annual Song Observations: {species['common_name']}",
        color=palette["title"], fontsize=13, fontweight="bold", pad=14, loc="left",
    )
    ax.text(
        1.0, 1.012,
        f"{year}  ·  {species['scientific_name']}",
        transform=ax.transAxes, ha="right", fontsize=9,
        color=palette["subtitle"], style="italic",
    )

    ax.set_ylabel("Time of day", color=palette["fg"], fontsize=9, labelpad=8)

    if not plot_days:
        ax.text(
            0.5, 0.5, "No detections for this species / year",
            transform=ax.transAxes, ha="center", va="center",
            color=palette["note"], fontsize=12,
        )

    sun_line = plt.Line2D([0], [0], color=palette["sun"], linewidth=1.2,
                          linestyle=(0, (3, 4)), label="Sunrise / Sunset")
    ax.legend(handles=[sun_line], loc="lower right", fontsize=8,
              framealpha=0.25, labelcolor=palette["fg"],
              edgecolor=palette["spine"], facecolor=palette["bg"])

    fig.text(
        0.5, 0.005,
        "Showing sunrise & sunset times.  Scale: 5 minute buckets, max count/bucket = 1",
        ha="center", fontsize=7.5, color=palette["note"],
    )

    fig.tight_layout(rect=[0, 0.025, 1, 1])

    # ── Encode and return ─────────────────────────────────────────────────
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor(),
                bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
