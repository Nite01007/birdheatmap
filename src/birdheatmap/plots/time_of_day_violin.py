"""Plot: Time-of-Day Activity (Violin).

Horizontal violin plot showing each species' daily activity rhythm.
X axis  = hour of day 00:00–24:00.
Y axis  = one violin per species, sorted by median detection time
          (earliest at top → morning species read first).
Color   = viridis gradient keyed to sort position (blue=early, yellow=late).
Overlay = optional average sunrise/sunset dashed lines.

Date-range modes (year × season):
  year=int,   season="all"     → full calendar year
  year=int,   season=specific  → seasonal window in that year
  year="all", season="all"     → all data, all years (no filter)
  year="all", season=specific  → season pooled across ALL years (key feature)

Winter convention: "winter Y" = Dec 1 (Y-1) through Mar 1 Y.
  Example: year=2025, season="winter" → Dec 2024 + Jan/Feb 2025.

Station-wide plot — species_id is accepted by the registry but ignored.
Select any species in the UI to trigger the render.
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
import numpy as np
from astral import LocationInfo
from astral.sun import sun

# ---------------------------------------------------------------------------
# Registry interface
# ---------------------------------------------------------------------------

NAME: str = "time_of_day_violin"
DISPLAY_NAME: str = "Time-of-Day Activity (Violin)"
DESCRIPTION: str = (
    "Horizontal violin plot of each species' daily activity rhythm, sorted by "
    "median detection time. Answers 'what time does each species sing?' "
    "Station-wide — species selector is ignored; pick any species to render."
)
PARAMS: list[dict[str, Any]] = [
    {
        "name":    "year",
        "type":    "year_or_all",
        "label":   "Year",
        "default": None,
        "choices": None,
    },
    {
        "name":    "season",
        "type":    "select",
        "label":   "Season",
        "default": "all",
        "choices": ["all", "spring", "summer", "fall", "winter"],
    },
    {
        "name":    "top_n",
        "type":    "int",
        "label":   "Top N species",
        "default": 15,
        "choices": None,
    },
    {
        "name":    "min_detections",
        "type":    "int",
        "label":   "Min detections",
        "default": 30,
        "choices": None,
    },
    {
        "name":    "show_sunrise_sunset",
        "type":    "bool",
        "label":   "Sunrise/sunset",
        "default": True,
        "choices": None,
    },
]

# ---------------------------------------------------------------------------
# Theme palettes
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

# ---------------------------------------------------------------------------
# Date-range helpers
# ---------------------------------------------------------------------------

# Season bounds: (start_month, end_month exclusive).
# "winter" end_month < start_month — handled as a special case below.
_SEASON_BOUNDS: dict[str, tuple[int, int]] = {
    "spring": (3, 6),
    "summer": (6, 9),
    "fall":   (9, 12),
    "winter": (12, 3),
}

_SEASON_LABELS: dict[str, str] = {
    "spring": "Spring",
    "summer": "Summer",
    "fall":   "Fall",
    "winter": "Winter",
}


def _date_to_utc_iso(d: date, tz: ZoneInfo) -> str:
    """Local midnight on *d* as a UTC ISO-8601 string for SQL comparisons."""
    local_midnight = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tz)
    utc = local_midnight.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _season_range(year: int, season: str, tz: ZoneInfo) -> tuple[str, str]:
    """(start_utc, end_utc) for one specific year + non-"all" season.

    Winter: year=2025 → Dec 1 2024 – Mar 1 2025 (the year the winter ends in).
    """
    if season == "winter":
        start = date(year - 1, 12, 1)
        end   = date(year, 3, 1)
    else:
        sm, em = _SEASON_BOUNDS[season]
        start  = date(year, sm, 1)
        end    = date(year, em, 1)
    return (_date_to_utc_iso(start, tz), _date_to_utc_iso(end, tz))


def _date_ranges(
    year,               # int | "all"
    season: str,        # "all" | "spring" | "summer" | "fall" | "winter"
    db_years: list[int],
    tz: ZoneInfo,
) -> list[tuple[str, str]] | None:
    """Return (start_utc, end_utc) pairs for a SQL WHERE clause, or None.

    None  → no timestamp filter (fetch all rows).
    list  → OR together: WHERE (ts >= a AND ts < b) OR (ts >= c AND ts < d) …
    """
    all_years = (year == "all" or year is None)

    if all_years and season == "all":
        return None

    if all_years:
        # One disjoint range per year; together they cover every occurrence of
        # this season across the full dataset without overlap.
        return [_season_range(y, season, tz) for y in sorted(db_years)]

    yr = int(year)
    if season == "all":
        return [(_date_to_utc_iso(date(yr, 1, 1), tz),
                 _date_to_utc_iso(date(yr + 1, 1, 1), tz))]

    return [_season_range(yr, season, tz)]


def _build_title(year: int | str | None, season: str) -> str:
    all_years = (year == "all" or year is None)
    slabel = _SEASON_LABELS.get(season, "")
    if all_years and season == "all":
        return "Daily Activity Rhythms: All Data"
    if all_years:
        return f"Daily Activity Rhythms: {slabel} (All Years)"
    yr = int(year)  # type: ignore[arg-type]
    if season == "all":
        return f"Daily Activity Rhythms: {yr}"
    return f"Daily Activity Rhythms: {slabel} {yr}"


# ---------------------------------------------------------------------------
# Sunrise/sunset helper
# ---------------------------------------------------------------------------

def _avg_sun_hours(
    ranges: list[tuple[str, str]] | None,
    observer,
    tz: ZoneInfo,
    fallback_year: int,
) -> tuple[float, float] | None:
    """Return (avg_rise_h, avg_set_h) as fractional hours over the first range.

    We use only the first range — for all-years mode every range covers the
    same seasonal period, so one is representative.  Returns None on failure.
    """
    if ranges:
        start_iso, end_iso = ranges[0]
        d_start = datetime.fromisoformat(start_iso).astimezone(tz).date()
        d_end   = datetime.fromisoformat(end_iso).astimezone(tz).date()
    else:
        # No range → all data; average over a full year as a proxy.
        d_start = date(fallback_year, 1, 1)
        d_end   = date(fallback_year + 1, 1, 1)

    rises: list[float] = []
    sets:  list[float] = []
    d = d_start
    while d < d_end:
        try:
            s = sun(observer, date=d, tzinfo=tz)
            rises.append(s["sunrise"].hour + s["sunrise"].minute / 60 +
                         s["sunrise"].second / 3600)
            sets.append(s["sunset"].hour  + s["sunset"].minute  / 60 +
                        s["sunset"].second  / 3600)
        except Exception:
            pass
        d += timedelta(days=1)

    if not rises:
        return None
    return (sum(rises) / len(rises), sum(sets) / len(sets))


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render(db: sqlite3.Connection, species_id: int, **params: Any) -> bytes:
    """Return a PNG of the time-of-day violin plot."""
    year_raw = params.get("year")
    season   = str(params.get("season") or "all")
    top_n    = max(1, int(params.get("top_n") or 15))
    min_det  = max(1, int(params.get("min_detections") or 30))
    palette  = _PALETTES["light" if params.get("theme") == "light" else "dark"]

    # Accept bool or string for show_sunrise_sunset (bool when called from web
    # layer after _gather_extra_params coercion; string when called directly).
    show_sr_raw = params.get("show_sunrise_sunset", True)
    if isinstance(show_sr_raw, str):
        show_sr = show_sr_raw.lower() in ("true", "1", "yes")
    else:
        show_sr = bool(show_sr_raw)

    # ── Station info ──────────────────────────────────────────────────────────
    station  = db.execute("SELECT * FROM station LIMIT 1").fetchone()
    tz_name  = station["timezone"] if station else "America/New_York"
    lat      = station["lat"]      if station else 42.305149
    lon      = station["lon"]      if station else -72.45105
    tz       = ZoneInfo(tz_name)
    observer = LocationInfo(latitude=lat, longitude=lon, timezone=tz_name).observer

    # ── Distinct years present in DB (UTC-calendar) ───────────────────────────
    db_years = [
        r["yr"] for r in db.execute(
            "SELECT DISTINCT CAST(strftime('%Y', timestamp_utc) AS INTEGER) AS yr "
            "FROM detection ORDER BY yr"
        ).fetchall()
    ]

    # Normalise year: None → most recent year in DB (default view).
    if year_raw is None:
        year_norm: int | str = max(db_years) if db_years else datetime.now(tz=timezone.utc).year
    elif year_raw == "all":
        year_norm = "all"
    else:
        year_norm = int(year_raw)

    ranges = _date_ranges(year_norm, season, db_years, tz)

    # ── Fetch detections ──────────────────────────────────────────────────────
    if ranges is None:
        rows = db.execute(
            "SELECT d.species_id, d.timestamp_utc, s.common_name "
            "FROM detection d JOIN species s ON s.id = d.species_id"
        ).fetchall()
    elif not ranges:
        rows = []
    else:
        placeholders = " OR ".join(
            ["(d.timestamp_utc >= ? AND d.timestamp_utc < ?)"] * len(ranges)
        )
        flat = [v for r in ranges for v in r]
        rows = db.execute(
            "SELECT d.species_id, d.timestamp_utc, s.common_name "
            "FROM detection d JOIN species s ON s.id = d.species_id "
            f"WHERE {placeholders}",
            flat,
        ).fetchall()

    # ── Convert to fractional hours in local time, grouped by species ─────────
    sp_hours: dict[int, tuple[str, list[float]]] = {}
    for row in rows:
        dt     = datetime.fromisoformat(row["timestamp_utc"]).astimezone(tz)
        frac_h = dt.hour + dt.minute / 60 + dt.second / 3600
        sid    = row["species_id"]
        if sid not in sp_hours:
            sp_hours[sid] = (row["common_name"], [])
        sp_hours[sid][1].append(frac_h)

    # ── Filter by min_detections, take top_n by count, sort by median time ────
    n_eligible = sum(1 for _, hrs in sp_hours.values() if len(hrs) >= min_det)
    qualified = [
        {"name": name, "hours": hrs, "count": len(hrs)}
        for name, hrs in sp_hours.values()
        if len(hrs) >= min_det
    ]
    by_count    = sorted(qualified, key=lambda s: s["count"], reverse=True)[:top_n]
    species_sorted = sorted(by_count, key=lambda s: float(np.median(s["hours"])))
    n               = len(species_sorted)
    total_det       = sum(s["count"] for s in species_sorted)
    title_str       = _build_title(year_norm, season)
    fallback_year   = max(db_years) if db_years else datetime.now(tz=timezone.utc).year

    # ── Thin-data guard ───────────────────────────────────────────────────────
    if n < 3:
        fig, ax = plt.subplots(figsize=(12, 4), dpi=100)
        fig.patch.set_facecolor(palette["bg"])
        ax.set_facecolor(palette["bg"])
        ax.set_title(title_str, color=palette["title"], fontsize=13,
                     fontweight="bold", pad=14, loc="left")
        for sp in ax.spines.values():
            sp.set_color(palette["spine"])
        ax.tick_params(colors=palette["fg"])
        ax.text(
            0.5, 0.5,
            f"Not enough data: only {n_eligible} species "
            f"have ≥{min_det} detections in this range.\n"
            "Try widening the date range or lowering min_detections.",
            transform=ax.transAxes, ha="center", va="center",
            color=palette["note"], fontsize=11, linespacing=1.7,
        )
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor(),
                    bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    # ── Average sunrise / sunset ──────────────────────────────────────────────
    sun_lines: tuple[float, float] | None = None
    if show_sr:
        sun_lines = _avg_sun_hours(ranges, observer, tz, fallback_year)

    # ── Build figure ──────────────────────────────────────────────────────────
    fig_height = max(6, 0.4 * n)
    fig, ax = plt.subplots(figsize=(12, fig_height), dpi=100)
    fig.patch.set_facecolor(palette["bg"])
    ax.set_facecolor(palette["bg"])

    datasets = [np.array(s["hours"]) for s in species_sorted]
    # Earliest species → top of plot → highest y position.
    y_pos = list(range(n - 1, -1, -1))   # [n-1, n-2, …, 1, 0]

    # ── Violin plot ───────────────────────────────────────────────────────────
    parts = ax.violinplot(
        datasets,
        positions=y_pos,
        vert=False,
        showmedians=True,
        showextrema=False,
        widths=0.75,
    )

    # Color: viridis gradient matching species_ridge convention.
    cmap = plt.cm.viridis
    for i, body in enumerate(parts["bodies"]):
        t     = i / max(n - 1, 1)          # 0=earliest (blue), 1=latest (yellow)
        color = cmap(0.12 + 0.76 * t)
        body.set_facecolor(color)
        body.set_edgecolor(color)
        body.set_alpha(0.82)

    # Style median line.
    parts["cmedians"].set_color(palette["fg"])
    parts["cmedians"].set_linewidth(1.5)
    parts["cmedians"].set_zorder(5)

    # IQR band: thick line from Q25 to Q75 at each violin's y position.
    for i, (sp, yp) in enumerate(zip(species_sorted, y_pos)):
        t     = i / max(n - 1, 1)
        color = cmap(0.12 + 0.76 * t)
        q25   = float(np.percentile(sp["hours"], 25))
        q75   = float(np.percentile(sp["hours"], 75))
        ax.plot([q25, q75], [yp, yp], color=color, linewidth=4,
                alpha=0.95, solid_capstyle="round", zorder=4)

    # ── Sunrise/sunset lines ──────────────────────────────────────────────────
    if sun_lines is not None:
        avg_rise, avg_set = sun_lines
        ax.axvline(avg_rise, color=palette["sun"], linewidth=1.0,
                   linestyle="--", alpha=0.65, zorder=2)
        ax.axvline(avg_set,  color=palette["sun"], linewidth=1.0,
                   linestyle="--", alpha=0.65, zorder=2)
        y_label = n - 0.55
        ax.text(avg_rise + 0.1, y_label, "avg sunrise", color=palette["sun"],
                fontsize=7, va="center", style="italic")
        ax.text(avg_set  + 0.1, y_label, "avg sunset",  color=palette["sun"],
                fontsize=7, va="center", style="italic")

    # ── X axis: 00:00 – 24:00 ────────────────────────────────────────────────
    ax.set_xlim(0, 24)
    ax.xaxis.set_major_locator(ticker.MultipleLocator(2))

    def _fmt_hour(val: float, _pos: Any) -> str:
        h = int(round(val)) % 24
        return f"{h:02d}:00"

    ax.xaxis.set_major_formatter(ticker.FuncFormatter(_fmt_hour))
    ax.set_xlabel("Time of day", color=palette["fg"], fontsize=9, labelpad=8)

    # ── Y axis: species names ─────────────────────────────────────────────────
    ax.set_yticks(y_pos)
    ax.set_yticklabels([s["name"] for s in species_sorted], fontsize=8)
    ax.set_ylim(-0.6, n - 0.4)

    # ── Spine / tick / grid ───────────────────────────────────────────────────
    for sp in ax.spines.values():
        sp.set_color(palette["spine"])
    ax.spines["right"].set_visible(False)
    ax.spines["top"].set_visible(False)
    ax.tick_params(axis="both", colors=palette["fg"], labelsize=8, length=3, width=0.6)
    ax.grid(axis="x", color=palette["grid"], linewidth=0.5, zorder=1)
    ax.grid(axis="y", color=palette["grid"], linewidth=0.3, zorder=1)

    # ── Titles ────────────────────────────────────────────────────────────────
    ax.set_title(title_str, color=palette["title"], fontsize=13,
                 fontweight="bold", pad=14, loc="left")
    ax.text(
        1.0, 1.012,
        f"top {n} species  ·  min {min_det} detections  ·  Belchertown MA",
        transform=ax.transAxes, ha="right", fontsize=9, color=palette["subtitle"],
    )
    fig.text(
        0.5, 0.005,
        f"Violin widths show relative detection density by hour.  "
        f"Inner marker = median, band = IQR.  "
        f"n={total_det:,} detections across top {n} species "
        f"({n_eligible} species met min_detections={min_det}).",
        ha="center", fontsize=7.5, color=palette["note"],
    )

    fig.tight_layout(rect=[0, 0.025, 1, 1])

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor(),
                bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
