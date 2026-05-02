"""Plot: Daily Timeline.

All detections from a single calendar day (midnight to midnight, station
local time) as a 2D heatmap: time of day on X, species on Y, color intensity
= detection count per 15-minute bin.

Station-wide — species_id is accepted but ignored.
Date defaults to yesterday when left blank.
"""

import io
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from astral import LocationInfo
from astral.sun import sun as _astral_sun

# ---------------------------------------------------------------------------
# Registry interface
# ---------------------------------------------------------------------------

NAME: str = "daily_timeline"
DISPLAY_NAME: str = "Daily Timeline"
DESCRIPTION: str = (
    "All detections from a single day (midnight–midnight, station local time) "
    "as a time-of-day × species heatmap.  Color = detections per 15-min bin.  "
    "Station-wide — species selector is ignored.  Leave date blank for yesterday."
)
PARAMS: list[dict[str, Any]] = [
    {
        "name":    "date",
        "type":    "date",
        "label":   "Date",
        "default": "",
        "choices": None,
    },
    {
        "name":    "hide_rare",
        "type":    "bool",
        "label":   "Hide rare",
        "default": True,
        "choices": None,
    },
]

REQUIRES_SPECIES: bool = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_N_BINS          = 96    # 96 × 15 min = 1440 min = 24 h
_BIN_MINUTES     = 15
_LABEL_EVERY     = 8     # label every 8 bins = every 2 hours
_HIDE_RARE_MAX   = 2     # hide species with ≤ this many total detections

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
        "zero":     "#16161f",
        "sun":      "#e8a04a",
    },
    "light": {
        "bg":       "#f8f8fc",
        "fg":       "#2a2a3c",
        "title":    "#0a0a18",
        "grid":     "#dcdce8",
        "note":     "#888899",
        "subtitle": "#555566",
        "spine":    "#bbbbcc",
        "zero":     "#e8e8f0",
        "sun":      "#b86a10",
    },
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(s: Any, fallback: date) -> date:
    """Parse a YYYY-MM-DD string; return fallback on any failure."""
    try:
        return date.fromisoformat(str(s))
    except (TypeError, ValueError):
        return fallback


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render(db: sqlite3.Connection, species_id: int, **params: Any) -> bytes:
    """Return a PNG heatmap for a single calendar day."""
    palette   = _PALETTES["light" if params.get("theme") == "light" else "dark"]
    hide_rare = params.get("hide_rare") in (True, "true", "True", "1", "yes")

    # ── Station ───────────────────────────────────────────────────────────
    station  = db.execute("SELECT * FROM station LIMIT 1").fetchone()
    tz_name  = station["timezone"] if station else "America/New_York"
    lat: float = station["lat"] if station else 42.305149
    lon: float = station["lon"] if station else -72.45105
    tz       = ZoneInfo(tz_name)

    # ── Target date (default: yesterday) ─────────────────────────────────
    today_local    = datetime.now(tz=tz).date()
    yesterday      = today_local - timedelta(days=1)
    target_date    = _parse_date(params.get("date", ""), yesterday)
    date_label     = target_date.strftime("%Y-%m-%d")

    day_start = datetime(target_date.year, target_date.month,
                         target_date.day, 0, 0, 0, tzinfo=tz)
    day_end   = day_start + timedelta(days=1)
    start_utc = day_start.astimezone(timezone.utc).isoformat()
    end_utc   = day_end.astimezone(timezone.utc).isoformat()

    # ── Sunrise / sunset ──────────────────────────────────────────────────
    observer           = LocationInfo(latitude=lat, longitude=lon, timezone=tz_name).observer
    rise_x = set_x     = None
    rise_str = set_str = ""
    try:
        s        = _astral_sun(observer, date=target_date, tzinfo=tz)
        rise_x   = (s["sunrise"].hour * 60 + s["sunrise"].minute + s["sunrise"].second / 60) / _BIN_MINUTES
        set_x    = (s["sunset"].hour  * 60 + s["sunset"].minute  + s["sunset"].second  / 60) / _BIN_MINUTES
        rise_str = s["sunrise"].strftime("%H:%M")
        set_str  = s["sunset"].strftime("%H:%M")
    except Exception:
        pass

    # ── Fetch detections ──────────────────────────────────────────────────
    rows = db.execute(
        """
        SELECT d.species_id, d.timestamp_utc, s.common_name
        FROM   detection d
        JOIN   species   s ON s.id = d.species_id
        WHERE  d.timestamp_utc >= ? AND d.timestamp_utc < ?
        ORDER  BY d.timestamp_utc
        """,
        (start_utc, end_utc),
    ).fetchall()

    # ── Aggregate per-species, per-bin ────────────────────────────────────
    sp_data: dict[int, dict] = {}
    for row in rows:
        dt      = datetime.fromisoformat(row["timestamp_utc"]).astimezone(tz)
        bin_idx = (dt.hour * 60 + dt.minute) // _BIN_MINUTES
        sid     = row["species_id"]
        if sid not in sp_data:
            sp_data[sid] = {"name": row["common_name"], "bins": {}, "first_bin": bin_idx}
        sp_data[sid]["bins"][bin_idx] = sp_data[sid]["bins"].get(bin_idx, 0) + 1
        if bin_idx < sp_data[sid]["first_bin"]:
            sp_data[sid]["first_bin"] = bin_idx

    if hide_rare:
        sp_data = {
            sid: info for sid, info in sp_data.items()
            if sum(info["bins"].values()) > _HIDE_RARE_MAX
        }

    # ── Empty-data guard ──────────────────────────────────────────────────
    if not sp_data:
        fig, ax = plt.subplots(figsize=(10, 3), dpi=100)
        fig.patch.set_facecolor(palette["bg"])
        ax.set_facecolor(palette["bg"])
        ax.set_title(f"Daily Timeline — {date_label}",
                     color=palette["title"], fontsize=13, fontweight="bold",
                     pad=14, loc="left")
        ax.text(0.5, 0.5, "No detections recorded for this date.",
                transform=ax.transAxes, ha="center", va="center",
                color=palette["note"], fontsize=12)
        for sp in ax.spines.values():
            sp.set_color(palette["spine"])
        ax.set_xticks([])
        ax.set_yticks([])
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor(),
                    bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    # ── Sort by first detection of the day ───────────────────────────────
    sorted_species = sorted(sp_data.items(), key=lambda kv: kv[1]["first_bin"])
    n_species      = len(sorted_species)
    species_names  = [info["name"] for _, info in sorted_species]

    # ── Build count array ─────────────────────────────────────────────────
    heatmap = np.zeros((n_species, _N_BINS), dtype=float)
    for row_idx, (_sid, info) in enumerate(sorted_species):
        for bin_idx, count in info["bins"].items():
            heatmap[row_idx, bin_idx] = count

    # ── Figure ────────────────────────────────────────────────────────────
    fig_height = max(4.0, min(14.0, n_species * 0.32 + 2.5))
    fig, ax    = plt.subplots(figsize=(10.0, fig_height), dpi=100)
    fig.patch.set_facecolor(palette["bg"])
    ax.set_facecolor(palette["zero"])

    cmap = plt.cm.viridis.copy()
    cmap.set_bad(color=palette["zero"])
    ax.imshow(np.ma.masked_equal(heatmap, 0), aspect="auto", cmap=cmap,
              interpolation="nearest", origin="upper")
    ax.set_xlim(-0.5, _N_BINS - 0.5)
    ax.set_ylim(n_species - 0.5, -0.5)

    # Colorbar
    sm = plt.cm.ScalarMappable(cmap=cmap,
                               norm=plt.Normalize(vmin=0, vmax=float(heatmap.max())))
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=ax, pad=0.01, shrink=0.75, aspect=22)
    cbar.set_label("Detections / 15 min", color=palette["fg"], fontsize=8, labelpad=8)
    cbar.ax.tick_params(colors=palette["fg"], labelsize=7)
    cbar.outline.set_edgecolor(palette["spine"])

    # X axis
    hour_bins   = list(range(0, _N_BINS, _LABEL_EVERY))
    hour_labels = [f"{(b * _BIN_MINUTES) // 60:02d}:00" for b in hour_bins]
    ax.set_xticks(hour_bins)
    ax.set_xticklabels(hour_labels, fontsize=8)
    ax.set_xlabel("Time of day", color=palette["fg"], fontsize=9, labelpad=8)

    # Y axis
    ax.set_yticks(range(n_species))
    ax.set_yticklabels(species_names, fontsize=8)

    # Sunrise / sunset
    if rise_x is not None:
        ax.axvline(rise_x, color=palette["sun"], lw=1.2,
                   linestyle=(0, (3, 4)), alpha=0.90, zorder=5)
    if set_x is not None:
        ax.axvline(set_x, color=palette["sun"], lw=1.2,
                   linestyle=(0, (3, 4)), alpha=0.90, zorder=5)

    # Horizontal rules every 3 species
    for yi in range(2, n_species, 3):
        ax.axhline(yi + 0.5, color=palette["grid"], linewidth=0.4, zorder=2)

    # Styling
    for spine in ax.spines.values():
        spine.set_color(palette["spine"])
    ax.tick_params(axis="both", colors=palette["fg"], length=3, width=0.6)
    ax.grid(axis="x", color=palette["grid"], linewidth=0.4, zorder=0)

    # Titles
    ax.set_title(f"Daily Timeline — {date_label}",
                 color=palette["title"], fontsize=13, fontweight="bold",
                 pad=14, loc="left")
    ax.text(1.0, 1.012, f"{len(rows):,} detections · {n_species} species",
            transform=ax.transAxes, ha="right", fontsize=9,
            color=palette["subtitle"])
    sun_note = f"  ·  sunrise {rise_str} / sunset {set_str}" if rise_str else ""
    fig.text(0.5, 0.005,
             f"Bin: 15 min.  Species sorted by first detection (earliest at top).  "
             f"Timezone: {tz_name}{sun_note}.  Dashed lines = sunrise / sunset.",
             ha="center", fontsize=7.5, color=palette["note"])

    fig.tight_layout(rect=[0, 0.025, 1, 1])

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor(),
                bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
