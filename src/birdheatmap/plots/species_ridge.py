"""Plot: Seasonal Succession Ridge Plot.

Ridge (joy) plot showing when each species peaks across the calendar year.
X axis  = day of year (Jan 1 – Dec 31), month ticks.
Y axis  = one ridge per species; stacked with slight overlap.
Each ridge = Gaussian-smoothed KDE of detection day-of-year, peak-normalized
             so shape shows WHEN, not how much.
Sort    = by peak detection date (earliest at top → cascade reads Jan→Dec).
Color   = viridis gradient keyed to peak day-of-year (blue=early, yellow=late).

Note: station-wide plot — species_id is accepted but ignored.
Select any species in the UI to trigger the render.
"""

import io
import sqlite3
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np

# ---------------------------------------------------------------------------
# Registry interface
# ---------------------------------------------------------------------------

NAME: str = "species_ridge"
DISPLAY_NAME: str = "Seasonal Succession (Ridge)"
DESCRIPTION: str = (
    "Ridge plot of seasonal activity peaks. Each ridge is a peak-normalized KDE "
    "of detection day-of-year, sorted by timing so the cascade reads Jan→Dec. "
    "Station-wide — species selector is ignored; pick any species to render."
)
PARAMS: list[dict[str, Any]] = [
    {
        "name": "year",
        "type": "year_or_all",
        "label": "Year",
        "default": None,
        "choices": None,
    },
    {
        "name": "min_detections",
        "type": "int",
        "label": "Min detections",
        "default": 20,
        "choices": None,
    },
    {
        "name": "top_n",
        "type": "int",
        "label": "Top N species",
        "default": 30,
        "choices": None,
    },
    {
        "name": "smoothing_bandwidth",
        "type": "float",
        "label": "Smoothing (days)",
        "default": 7.0,
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
        "note":     "#666680",
        "subtitle": "#888899",
        "spine":    "#333348",
    },
    "light": {
        "bg":       "#f8f8fc",
        "fg":       "#2a2a3c",
        "title":    "#0a0a18",
        "grid":     "#dcdce8",
        "note":     "#888899",
        "subtitle": "#555566",
        "spine":    "#bbbbcc",
    },
}

# ---------------------------------------------------------------------------
# KDE helper
# ---------------------------------------------------------------------------

def _gaussian_kde(doys: list[int], n_days: int, bandwidth: float) -> np.ndarray:
    """Gaussian-smoothed, peak-normalized density over days 1..n_days.

    Returns a length-n_days array with values in [0, 1] (peak = 1).
    """
    hist = np.zeros(n_days, dtype=float)
    for d in doys:
        idx = d - 1
        if 0 <= idx < n_days:
            hist[idx] += 1.0

    radius = max(int(bandwidth * 3), 3)
    kx     = np.arange(-radius, radius + 1, dtype=float)
    kernel = np.exp(-0.5 * (kx / bandwidth) ** 2)
    kernel /= kernel.sum()

    smooth = np.convolve(hist, kernel, mode="same")
    smooth = np.maximum(smooth, 0.0)
    peak   = smooth.max()
    if peak > 0:
        smooth /= peak
    return smooth

# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render(db: sqlite3.Connection, species_id: int, **params: Any) -> bytes:
    """Return a PNG of the seasonal succession ridge plot."""
    year_raw  = params.get("year")
    min_det   = int(params.get("min_detections") or 20)
    top_n     = max(1, int(params.get("top_n") or 30))
    bandwidth = float(params.get("smoothing_bandwidth") or 7.0)
    palette   = _PALETTES["light" if params.get("theme") == "light" else "dark"]

    station = db.execute("SELECT * FROM station LIMIT 1").fetchone()
    tz_name = station["timezone"] if station else "America/New_York"
    tz = ZoneInfo(tz_name)

    # ── Determine year mode ───────────────────────────────────────────────
    all_years_mode = (year_raw == "all" or year_raw is None)
    if all_years_mode:
        n_days     = 365
        title_year = "All Years Combined"
        ref_year   = datetime.now(tz=timezone.utc).year
    else:
        year     = int(year_raw)
        is_leap  = (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)
        n_days   = 366 if is_leap else 365
        title_year = str(year)
        ref_year = year

    # ── Fetch detections ──────────────────────────────────────────────────
    if all_years_mode:
        rows = db.execute(
            """
            SELECT d.species_id, d.timestamp_utc, s.common_name
            FROM detection d
            JOIN species s ON s.id = d.species_id
            """
        ).fetchall()
    else:
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

    # ── Group doys by species ─────────────────────────────────────────────
    sp_doys: dict[int, tuple[str, list[int]]] = {}
    for row in rows:
        dt  = datetime.fromisoformat(row["timestamp_utc"]).astimezone(tz)
        doy = dt.timetuple().tm_yday
        doy = min(doy, n_days)    # clip Feb-29 to 365 in all-years mode
        sid = row["species_id"]
        if sid not in sp_doys:
            sp_doys[sid] = (row["common_name"], [])
        sp_doys[sid][1].append(doy)

    # ── Compute KDE + peak; filter; select top_n ──────────────────────────
    qualified = []
    for sid, (name, doys) in sp_doys.items():
        if len(doys) < min_det:
            continue
        kde      = _gaussian_kde(doys, n_days, bandwidth)
        peak_doy = int(np.argmax(kde)) + 1   # 1-based
        qualified.append({"name": name, "kde": kde, "peak_doy": peak_doy, "total": len(doys)})

    # Take top_n by total detections, then re-sort by peak_doy for the cascade.
    by_count    = sorted(qualified, key=lambda s: s["total"], reverse=True)[:top_n]
    species_all = sorted(by_count, key=lambda s: s["peak_doy"])
    n           = len(species_all)

    # ── Empty-data guard ──────────────────────────────────────────────────
    if n == 0:
        fig, ax = plt.subplots(figsize=(14, 4), dpi=100)
        fig.patch.set_facecolor(palette["bg"])
        ax.set_facecolor(palette["bg"])
        ax.text(0.5, 0.5,
                f"No species with ≥ {min_det} detections",
                transform=ax.transAxes, ha="center", va="center",
                color=palette["note"], fontsize=12)
        ax.set_title(f"Seasonal Succession: {title_year}",
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

    # ── Layout constants ──────────────────────────────────────────────────
    ridge_step = 1.0   # y-distance between consecutive baselines
    ridge_amp  = 1.5   # max ridge height in y-units (>1 → ridges overlap)

    # species_all[0] = earliest peak → top of plot → highest y value
    y_bases = [(n - 1 - i) * ridge_step for i in range(n)]

    # Viridis colormap keyed to peak_doy so color encodes season.
    min_doy   = species_all[0]["peak_doy"]
    max_doy   = species_all[-1]["peak_doy"]
    doy_range = max(max_doy - min_doy, 1)
    cmap      = plt.cm.viridis

    # ── Month tick positions ──────────────────────────────────────────────
    month_doys:   list[int] = []
    month_labels: list[str] = []
    for month in range(1, 13):
        d   = date(ref_year, month, 1)
        doy = d.timetuple().tm_yday
        if doy <= n_days:
            month_doys.append(doy)
            month_labels.append(d.strftime("%b"))

    x = np.arange(1, n_days + 1, dtype=float)

    # ── Figure ────────────────────────────────────────────────────────────
    fig_height = max(6, 0.3 * n)
    fig, ax = plt.subplots(figsize=(14, fig_height), dpi=100)
    fig.patch.set_facecolor(palette["bg"])
    ax.set_facecolor(palette["bg"])

    # Draw ridges from bottom to top so earlier (higher) species occlude later ones.
    for i in range(n - 1, -1, -1):
        sp     = species_all[i]
        y_base = y_bases[i]
        y_top  = y_base + sp["kde"] * ridge_amp

        # Viridis t: 0.0 = earliest (blue-purple), 1.0 = latest (yellow)
        t     = (sp["peak_doy"] - min_doy) / doy_range
        color = cmap(0.12 + 0.76 * t)

        z = (n - i) * 3   # z-stack: earlier species drawn last → on top

        # Background strip at baseline masks ridge fill from species below.
        ax.fill_between(x, y_base - 0.15, y_base,
                        color=palette["bg"], linewidth=0, zorder=z)
        # Colored ridge fill.
        ax.fill_between(x, y_base, y_top,
                        color=color, alpha=0.82, linewidth=0, zorder=z + 1)
        # Contour line.
        ax.plot(x, y_top, color=color, alpha=0.95, linewidth=0.65, zorder=z + 2)

    # ── Y axis: species names at ridge baselines ──────────────────────────
    ax.set_yticks(y_bases)
    ax.set_yticklabels([sp["name"] for sp in species_all], fontsize=7.5)
    ax.tick_params(axis="y", length=0, colors=palette["fg"], pad=4)
    ax.set_ylim(-0.4, (n - 1) * ridge_step + ridge_amp + 0.2)

    # ── X axis ────────────────────────────────────────────────────────────
    ax.set_xlim(1, n_days)
    ax.xaxis.set_major_locator(ticker.FixedLocator(month_doys))
    ax.xaxis.set_major_formatter(ticker.FixedFormatter(month_labels))
    ax.xaxis.set_tick_params(pad=5)

    # ── Spine / grid styling ──────────────────────────────────────────────
    for spine in ax.spines.values():
        spine.set_color(palette["spine"])
    ax.spines["left"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["top"].set_visible(False)
    ax.tick_params(axis="x", colors=palette["fg"], labelsize=9, length=3, width=0.6)
    ax.grid(axis="x", color=palette["grid"], linewidth=0.5, zorder=0)

    # ── Titles & labels ───────────────────────────────────────────────────
    ax.set_title(
        f"Seasonal Succession: {title_year}",
        color=palette["title"], fontsize=13, fontweight="bold", pad=14, loc="left",
    )
    ax.text(
        1.0, 1.012,
        f"top {n} species  ·  min {min_det} detections  ·  smoothing {bandwidth:.1f} days",
        transform=ax.transAxes, ha="right", fontsize=9, color=palette["subtitle"],
    )
    fig.text(
        0.5, 0.005,
        "Each ridge: peak-normalized KDE of detection day-of-year  ·  sorted by peak date  ·  Belchertown MA",
        ha="center", fontsize=7.5, color=palette["note"],
    )

    fig.tight_layout(rect=[0, 0.025, 1, 1])

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor(),
                bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
