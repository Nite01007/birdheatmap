"""Plot: Species Portrait.

Side-by-side annual-presence violin (left) and seasonal daily-rhythm
violins (right) for a single species. All available years are pooled.

Left panel  — vertical KDE violin over day-of-year 1-365, Jan at top.
              Season bands (Spring/Summer/Fall/Winter) shaded behind.
Right panel — four horizontal KDE violins, one per season, Spring at top.
              Dashed lines mark mean season sunrise and sunset.
              Seasons with < 10 detections show a rug plot instead.
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

NAME: str = "species_portrait"
DISPLAY_NAME: str = "Species Portrait"
DESCRIPTION: str = (
    "Annual presence violin + seasonal daily-rhythm violins for a single "
    "species. All years pooled. Select a species in the main dropdown."
)
PARAMS: list[dict[str, Any]] = []

# ---------------------------------------------------------------------------
# Theme palettes
# ---------------------------------------------------------------------------

_PALETTES = {
    "dark": {
        "bg":       "#0e0e14",
        "fg":       "#c8c8d4",
        "title":    "#ffffff",
        "grid":     "#1e1e2c",
        "note":     "#666680",
        "subtitle": "#888899",
        "spine":    "#333348",
        "violin":   "#7ab8d8",   # neutral color for annual-presence violin
    },
    "light": {
        "bg":       "#f8f8fc",
        "fg":       "#2a2a3c",
        "title":    "#0a0a18",
        "grid":     "#dcdce8",
        "note":     "#888899",
        "subtitle": "#555566",
        "spine":    "#bbbbcc",
        "violin":   "#2a70a8",
    },
}

# ---------------------------------------------------------------------------
# Season configuration
# ---------------------------------------------------------------------------

# DOY bounds (inclusive)
_SEASON_DOYS = {
    "Spring": (60,  151),
    "Summer": (152, 243),
    "Fall":   (244, 334),
    # Winter wraps: DOY 1-59 and 335-365
}
_WINTER_BANDS = ((1, 59), (335, 365))

_SEASON_COLORS = {
    "dark": {
        "Spring": "#4a9e5a",
        "Summer": "#c8a030",
        "Fall":   "#c86030",
        "Winter": "#4a78b0",
    },
    "light": {
        "Spring": "#2a8e3a",
        "Summer": "#9a7a10",
        "Fall":   "#a04820",
        "Winter": "#2a58a0",
    },
}

_SEASON_BAND_ALPHA = 0.18
_SEASON_ORDER = ["Spring", "Summer", "Fall", "Winter"]
_SEASON_Y_POS = {"Spring": 3, "Summer": 2, "Fall": 1, "Winter": 0}
_THIN_THRESH  = 10

# ---------------------------------------------------------------------------
# KDE helpers
# ---------------------------------------------------------------------------

def _gaussian_kde(values: list[float], x_range: np.ndarray, bandwidth: float) -> np.ndarray:
    """Gaussian KDE. Returns raw (un-normalized) density array."""
    density = np.zeros(len(x_range))
    for v in values:
        density += np.exp(-0.5 * ((x_range - v) / bandwidth) ** 2)
    return density


def _doy_kde(doys: list[int], bandwidth: float = 12.0) -> tuple[np.ndarray, np.ndarray]:
    """KDE over DOY 1–365, normalized to peak=1. Returns (y, density)."""
    y       = np.arange(1, 366, dtype=float)
    density = _gaussian_kde([float(d) for d in doys], y, bandwidth)
    peak    = density.max()
    if peak > 0:
        density /= peak
    return y, density


def _hour_kde(hours: list[float], bandwidth: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """KDE over hours 0–24, normalized to peak=1. Returns (x, density)."""
    x       = np.linspace(0, 24, 600)
    density = _gaussian_kde(hours, x, bandwidth)
    peak    = density.max()
    if peak > 0:
        density /= peak
    return x, density

# ---------------------------------------------------------------------------
# Sunrise / sunset helper
# ---------------------------------------------------------------------------

def _mean_sun_for_doys(
    observer,
    tz: ZoneInfo,
    doy_ranges: list[tuple[int, int]],
    ref_year: int = 2023,
) -> tuple[float | None, float | None]:
    """Mean sunrise and mean sunset (fractional hours) averaged across all
    dates in the given DOY ranges, computed using *ref_year* as the calendar."""
    rises: list[float] = []
    sets:  list[float] = []
    ref_jan1 = date(ref_year, 1, 1)
    for doy_start, doy_end in doy_ranges:
        d = ref_jan1 + timedelta(days=doy_start - 1)
        stop = ref_jan1 + timedelta(days=doy_end - 1)
        while d <= stop:
            try:
                s = sun(observer, date=d, tzinfo=tz)
                rises.append(
                    s["sunrise"].hour + s["sunrise"].minute / 60
                    + s["sunrise"].second / 3600
                )
                sets.append(
                    s["sunset"].hour + s["sunset"].minute / 60
                    + s["sunset"].second / 3600
                )
            except Exception:
                pass
            d += timedelta(days=1)
    if not rises:
        return None, None
    return sum(rises) / len(rises), sum(sets) / len(sets)

# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render(db: sqlite3.Connection, species_id: int, **params: Any) -> bytes:
    """Return a 14×7-inch PNG Species Portrait for *species_id*."""
    is_light = params.get("theme") == "light"
    palette  = _PALETTES["light" if is_light else "dark"]
    sc       = _SEASON_COLORS["light" if is_light else "dark"]

    # ── Station + species info ────────────────────────────────────────────
    station  = db.execute("SELECT * FROM station LIMIT 1").fetchone()
    tz_name  = station["timezone"] if station else "America/New_York"
    lat      = station["lat"]      if station else 42.305149
    lon      = station["lon"]      if station else -72.45105
    tz       = ZoneInfo(tz_name)
    observer = LocationInfo(latitude=lat, longitude=lon, timezone=tz_name).observer

    species  = db.execute(
        "SELECT common_name, scientific_name FROM species WHERE id=?",
        (species_id,),
    ).fetchone()
    common_name = species["common_name"] if species else f"Species {species_id}"

    # ── Fetch all detections for this species ─────────────────────────────
    rows = db.execute(
        "SELECT timestamp_utc FROM detection WHERE species_id=? ORDER BY timestamp_utc",
        (species_id,),
    ).fetchall()

    if not rows:
        fig, ax = plt.subplots(figsize=(14, 7), dpi=150)
        fig.patch.set_facecolor(palette["bg"])
        ax.set_facecolor(palette["bg"])
        ax.set_title(common_name, color=palette["title"], fontsize=15, fontweight="bold",
                     pad=18)
        ax.text(0.5, 0.5, "No detections found.", transform=ax.transAxes,
                ha="center", va="center", color=palette["note"], fontsize=13)
        for sp in ax.spines.values():
            sp.set_color(palette["spine"])
        ax.tick_params(colors=palette["fg"])
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150, facecolor=fig.get_facecolor(),
                    bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    # ── Parse detections ──────────────────────────────────────────────────
    doys_all:       list[int]   = []
    hours_by_season: dict[str, list[float]] = {s: [] for s in _SEASON_ORDER}
    years_seen:     set[int]    = set()

    for row in rows:
        dt  = datetime.fromisoformat(row["timestamp_utc"]).astimezone(tz)
        doy = min(dt.timetuple().tm_yday, 365)   # clip Feb-29 → 365
        hr  = dt.hour + dt.minute / 60 + dt.second / 3600
        doys_all.append(doy)
        years_seen.add(dt.year)

        if 60 <= doy <= 151:
            hours_by_season["Spring"].append(hr)
        elif 152 <= doy <= 243:
            hours_by_season["Summer"].append(hr)
        elif 244 <= doy <= 334:
            hours_by_season["Fall"].append(hr)
        else:
            hours_by_season["Winter"].append(hr)

    total      = len(doys_all)
    year_min   = min(years_seen)
    year_max   = max(years_seen)
    year_range = str(year_min) if year_min == year_max else f"{year_min}–{year_max}"

    # ── Month labels for left-panel Y axis ───────────────────────────────
    ref_year     = 2023   # non-leap for stable DOY → date mapping
    ref_jan1     = date(ref_year, 1, 1)
    month_doys:   list[int] = []
    month_labels: list[str] = []
    for m in range(1, 13):
        d   = date(ref_year, m, 1)
        doy = d.timetuple().tm_yday
        month_doys.append(doy)
        month_labels.append(d.strftime("%b"))

    # ── Figure layout ─────────────────────────────────────────────────────
    fig = plt.figure(figsize=(14, 7), dpi=150)
    gs  = fig.add_gridspec(
        1, 2,
        width_ratios=[1, 4],
        wspace=0.06,
        left=0.055, right=0.97,
        top=0.83, bottom=0.10,
    )
    ax_left  = fig.add_subplot(gs[0])
    ax_right = fig.add_subplot(gs[1])
    fig.patch.set_facecolor(palette["bg"])
    ax_left.set_facecolor(palette["bg"])
    ax_right.set_facecolor(palette["bg"])

    # ====================================================================
    # LEFT PANEL — Annual Presence Violin
    # ====================================================================

    # Season background bands
    for season, (d_start, d_end) in _SEASON_DOYS.items():
        ax_left.axhspan(d_start, d_end, color=sc[season],
                        alpha=_SEASON_BAND_ALPHA, zorder=1, lw=0)
    for d_start, d_end in _WINTER_BANDS:
        ax_left.axhspan(d_start, d_end, color=sc["Winter"],
                        alpha=_SEASON_BAND_ALPHA, zorder=1, lw=0)

    # KDE violin (symmetric around x=0)
    y_doy, dens = _doy_kde(doys_all, bandwidth=12.0)
    half_w = 0.78   # half-width in axis x-units (axis range is -1.1 to 1.1)
    vc = palette["violin"]
    ax_left.fill_betweenx(
        y_doy, -dens * half_w, dens * half_w,
        color=vc, alpha=0.55, zorder=3,
    )
    ax_left.plot(-dens * half_w, y_doy, color=vc, lw=0.7, alpha=0.85, zorder=4)
    ax_left.plot( dens * half_w, y_doy, color=vc, lw=0.7, alpha=0.85, zorder=4)

    # Y axis: month labels, Jan at top (inverted)
    ax_left.set_yticks(month_doys)
    ax_left.set_yticklabels(month_labels, fontsize=8)
    ax_left.set_ylim(365, 1)
    ax_left.tick_params(axis="y", colors=palette["fg"], labelsize=8, length=3, width=0.6)

    # X axis: hidden (just the violin shape)
    ax_left.set_xlim(-1.1, 1.1)
    ax_left.set_xticks([])
    ax_left.set_xlabel("Annual presence", color=palette["fg"], fontsize=8, labelpad=6)

    for side, sp in ax_left.spines.items():
        if side == "left":
            sp.set_color(palette["spine"])
        else:
            sp.set_visible(False)

    ax_left.grid(axis="y", color=palette["grid"], linewidth=0.35, zorder=0)

    # ====================================================================
    # RIGHT PANEL — Daily Activity Rhythm Violins
    # ====================================================================

    violin_hw = 0.38   # half-height of horizontal violin in y-data units
    rug_hw    = 0.25

    for season in _SEASON_ORDER:
        y_pos  = _SEASON_Y_POS[season]
        color  = sc[season]
        hours  = hours_by_season[season]
        n      = len(hours)

        # Season label on right edge
        ax_right.text(
            24.15, y_pos, season, ha="left", va="center",
            color=color, fontsize=8.5, fontweight="bold",
        )

        if n == 0:
            ax_right.text(
                12, y_pos, "no data", ha="center", va="center",
                color=palette["note"], fontsize=8, style="italic",
            )
            continue

        if n < _THIN_THRESH:
            # Rug plot
            for h in hours:
                ax_right.plot(
                    [h, h], [y_pos - rug_hw, y_pos + rug_hw],
                    color=color, lw=1.2, alpha=0.75, zorder=3,
                )
            ax_right.text(
                23.5, y_pos + rug_hw + 0.04, f"n={n}",
                ha="right", va="bottom", color=palette["note"], fontsize=7,
            )
        else:
            # KDE violin
            x_kde, dens_h = _hour_kde(hours, bandwidth=1.0)
            ax_right.fill_between(
                x_kde,
                y_pos - dens_h * violin_hw,
                y_pos + dens_h * violin_hw,
                color=color, alpha=0.70, zorder=3,
            )
            ax_right.plot(x_kde, y_pos + dens_h * violin_hw,
                          color=color, lw=0.75, alpha=0.90, zorder=4)
            ax_right.plot(x_kde, y_pos - dens_h * violin_hw,
                          color=color, lw=0.75, alpha=0.90, zorder=4)
            # Median marker
            med = float(np.median(hours))
            ax_right.plot(
                [med], [y_pos], "o",
                color=palette["bg"], ms=4, mec=color, mew=1.2, zorder=5,
            )

        # Sunrise / sunset dashed reference lines
        doy_ranges = (
            list(_WINTER_BANDS)
            if season == "Winter"
            else [_SEASON_DOYS[season]]
        )
        rise_h, set_h = _mean_sun_for_doys(observer, tz, doy_ranges, ref_year)
        span = violin_hw + 0.06
        if rise_h is not None:
            ax_right.plot(
                [rise_h, rise_h], [y_pos - span, y_pos + span],
                color=color, lw=0.9, ls="--", alpha=0.50, zorder=2,
            )
        if set_h is not None:
            ax_right.plot(
                [set_h, set_h], [y_pos - span, y_pos + span],
                color=color, lw=0.9, ls="--", alpha=0.50, zorder=2,
            )

    # Y axis: hidden ticks (season labels drawn as text above)
    ax_right.set_yticks([])
    ax_right.set_ylim(-0.62, 3.62)

    # X axis: 0–24 h
    ax_right.set_xlim(0, 24)
    ax_right.xaxis.set_major_locator(ticker.MultipleLocator(6))
    ax_right.xaxis.set_minor_locator(ticker.MultipleLocator(3))

    def _fmt_hour(val: float, _pos: Any) -> str:
        h = int(round(val)) % 24
        return f"{h:02d}:00"

    ax_right.xaxis.set_major_formatter(ticker.FuncFormatter(_fmt_hour))
    ax_right.set_xlabel("Time of day", color=palette["fg"], fontsize=9, labelpad=8)
    ax_right.tick_params(axis="x", colors=palette["fg"], labelsize=8, length=3, width=0.6)

    # Spines + grid
    for side, sp in ax_right.spines.items():
        if side == "bottom":
            sp.set_color(palette["spine"])
        else:
            sp.set_visible(False)
    ax_right.grid(axis="x", color=palette["grid"], linewidth=0.5, zorder=1)
    # Light horizontal dividers between season bands
    for y in [0.5, 1.5, 2.5]:
        ax_right.axhline(y, color=palette["grid"], lw=0.3, zorder=0)

    # ── Title and labels ──────────────────────────────────────────────────
    fig.suptitle(
        common_name,
        x=0.5, y=0.97,
        color=palette["title"], fontsize=16, fontweight="bold",
    )
    fig.text(
        0.5, 0.925,
        f"{total:,} detections · {year_range}",
        ha="center", fontsize=10, color=palette["subtitle"],
    )
    fig.text(
        0.5, 0.015,
        "Violin width = detection density  ·  ○ = median  ·  "
        "dashed lines = mean season sunrise/sunset  ·  Belchertown MA",
        ha="center", fontsize=7.5, color=palette["note"],
    )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=fig.get_facecolor(),
                bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
