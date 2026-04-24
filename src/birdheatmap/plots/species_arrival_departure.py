"""Plot: Species Presence Calendar.

Horizontal bar chart showing when each species is present across the year.
X axis  = calendar year (Jan 1 – Dec 31), month ticks.
Y axis  = one row per species, sorted by user selection.
Each bar = dimmed full range (first → last detection); bright overlay = IQR
           (middle 50% of detection day-of-year, showing when the species is
           most reliably present).

Note: this is a station-wide plot — species_id is accepted but ignored.
Select any species in the UI to trigger the render.
"""

import io
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from matplotlib.patches import Patch
import numpy as np

# ---------------------------------------------------------------------------
# Registry interface
# ---------------------------------------------------------------------------

NAME: str = "species_arrival_departure"
DISPLAY_NAME: str = "Species Presence Calendar"
DESCRIPTION: str = (
    "When is each species present? Bars span first–last detection; bright "
    "overlay shows the middle 50% (IQR). Station-wide — species selector "
    "is ignored; pick any species to render."
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
        "name": "min_detections",
        "type": "int",
        "label": "Min detections",
        "default": 5,
        "choices": None,
    },
    {
        "name": "sort_by",
        "type": "select",
        "label": "Sort by",
        "default": "first_detection",
        "choices": ["first_detection", "last_detection", "alphabetical", "total_detections"],
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
        "bar_full": "#4a88b8",   # dim full-range bar
        "bar_iqr":  "#90d8ff",   # bright IQR overlay
        "note":     "#666680",
        "subtitle": "#888899",
        "spine":    "#333348",
    },
    "light": {
        "bg":       "#f8f8fc",
        "fg":       "#2a2a3c",
        "title":    "#0a0a18",
        "grid":     "#dcdce8",
        "bar_full": "#8ab8d8",   # muted full-range
        "bar_iqr":  "#1a6fa8",   # saturated IQR
        "note":     "#888899",
        "subtitle": "#555566",
        "spine":    "#bbbbcc",
    },
}

# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render(db: sqlite3.Connection, species_id: int, **params: Any) -> bytes:
    """Return a PNG of the species presence calendar for the given year."""
    year          = int(params.get("year") or datetime.now(tz=timezone.utc).year)
    min_det       = int(params.get("min_detections") or 5)
    sort_by       = str(params.get("sort_by") or "first_detection")
    palette       = _PALETTES["light" if params.get("theme") == "light" else "dark"]

    station = db.execute("SELECT * FROM station LIMIT 1").fetchone()
    tz_name = station["timezone"] if station else "America/New_York"
    tz = ZoneInfo(tz_name)

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

    # Accumulate day-of-year per species (local time).
    sp_doys: dict[int, tuple[str, list[int]]] = {}
    for row in rows:
        dt  = datetime.fromisoformat(row["timestamp_utc"]).astimezone(tz)
        doy = dt.timetuple().tm_yday
        sid = row["species_id"]
        if sid not in sp_doys:
            sp_doys[sid] = (row["common_name"], [])
        sp_doys[sid][1].append(doy)

    # Filter by min_detections.
    sp_doys = {k: v for k, v in sp_doys.items() if len(v[1]) >= min_det}

    # ── Empty-data guard ──────────────────────────────────────────────────
    if not sp_doys:
        fig, ax = plt.subplots(figsize=(14, 4), dpi=100)
        fig.patch.set_facecolor(palette["bg"])
        ax.set_facecolor(palette["bg"])
        ax.text(0.5, 0.5,
                f"No species with ≥ {min_det} detections in {year}",
                transform=ax.transAxes, ha="center", va="center",
                color=palette["note"], fontsize=12)
        ax.set_title(f"Species Presence Calendar: {year}",
                     color=palette["title"], fontsize=13, fontweight="bold",
                     pad=14, loc="left")
        for spine in ax.spines.values():
            spine.set_color(palette["spine"])
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor(),
                    bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    # ── Compute per-species stats ─────────────────────────────────────────
    species_stats = []
    for sid, (name, doys) in sp_doys.items():
        arr = np.array(sorted(doys), dtype=float)
        species_stats.append({
            "name":  name,
            "first": int(arr[0]),
            "last":  int(arr[-1]),
            "q1":    int(np.percentile(arr, 25)),
            "q3":    int(np.percentile(arr, 75)),
            "total": len(doys),
        })

    # ── Sort ──────────────────────────────────────────────────────────────
    if sort_by == "last_detection":
        species_stats.sort(key=lambda s: s["last"])
    elif sort_by == "alphabetical":
        species_stats.sort(key=lambda s: s["name"])
    elif sort_by == "total_detections":
        # Most detections at top → sort descending (invert_yaxis puts index-0 at top)
        species_stats.sort(key=lambda s: s["total"], reverse=True)
    else:  # first_detection (default)
        species_stats.sort(key=lambda s: s["first"])

    n = len(species_stats)

    # ── Month tick positions ──────────────────────────────────────────────
    is_leap    = (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)
    n_days     = 366 if is_leap else 365
    month_doys:   list[int] = []
    month_labels: list[str] = []
    for month in range(1, 13):
        d = date(year, month, 1)
        month_doys.append(d.timetuple().tm_yday)
        month_labels.append(d.strftime("%b"))

    # ── Figure ────────────────────────────────────────────────────────────
    fig_height = max(6, 0.2 * n)
    fig, ax = plt.subplots(figsize=(14, fig_height), dpi=100)
    fig.patch.set_facecolor(palette["bg"])
    ax.set_facecolor(palette["bg"])

    bar_h = 0.65   # bar height in y-data units

    for i, sp in enumerate(species_stats):
        # Full range (dim background bar)
        width_full = max(sp["last"] - sp["first"], 1)
        ax.barh(i, width_full, left=sp["first"],
                height=bar_h, color=palette["bar_full"], alpha=0.28, zorder=3)
        # IQR overlay (bright foreground bar)
        width_iqr = max(sp["q3"] - sp["q1"], 1)
        ax.barh(i, width_iqr, left=sp["q1"],
                height=bar_h, color=palette["bar_iqr"], alpha=0.82, zorder=4)

    # Y axis: species names, earliest at top.
    ax.set_yticks(range(n))
    ax.set_yticklabels([sp["name"] for sp in species_stats], fontsize=7.5)
    ax.invert_yaxis()
    ax.set_ylim(n - 0.5, -0.5)   # tight margins

    # X axis: month ticks.
    ax.set_xlim(1, n_days)
    ax.xaxis.set_major_locator(ticker.FixedLocator(month_doys))
    ax.xaxis.set_major_formatter(ticker.FixedFormatter(month_labels))
    ax.xaxis.set_tick_params(pad=5)

    # ── Spine / grid styling ──────────────────────────────────────────────
    for spine in ax.spines.values():
        spine.set_color(palette["spine"])
    ax.tick_params(axis="both", colors=palette["fg"], labelsize=7.5,
                   length=3, width=0.6)
    ax.grid(axis="x", color=palette["grid"], linewidth=0.5, zorder=1)

    # ── Labels & legend ───────────────────────────────────────────────────
    ax.set_title(
        f"Species Presence Calendar: {year}",
        color=palette["title"], fontsize=13, fontweight="bold", pad=14, loc="left",
    )
    sort_label = sort_by.replace("_", " ")
    ax.text(
        1.0, 1.012,
        f"sorted by: {sort_label}  ·  min detections: {min_det}  ·  {n} species",
        transform=ax.transAxes, ha="right", fontsize=9,
        color=palette["subtitle"],
    )

    legend_handles = [
        Patch(facecolor=palette["bar_full"], alpha=0.28, label="Full season (first – last detection)"),
        Patch(facecolor=palette["bar_iqr"],  alpha=0.82, label="Middle 50% of detections (IQR)"),
    ]
    ax.legend(
        handles=legend_handles, loc="lower right", fontsize=8,
        framealpha=0.25, labelcolor=palette["fg"],
        edgecolor=palette["spine"], facecolor=palette["bg"],
    )

    fig.text(
        0.5, 0.005,
        f"Station-wide  ·  {n} species with ≥ {min_det} detections  ·  Belchertown MA",
        ha="center", fontsize=7.5, color=palette["note"],
    )

    fig.tight_layout(rect=[0, 0.025, 1, 1])

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor(),
                bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
