"""Flask web application.

Single-page, server-rendered UI.  No JavaScript frameworks — the page
reloads on form submission.  The species dropdown auto-submits on change
so the year dropdown reflects only years with data for that species.

Routes:
    GET /                      main page (form + inline image if species selected)
    GET /plot/<type>/<id>.png  render or serve cached PNG
    GET /status                JSON sync status
    GET /recordings            recent detections with audio, grouped by species
    GET /recordings/data       JSON feed for recordings (no-JS fallback / testing)
    GET /arrivals              species arriving for the first time in a window
    GET /arrivals/data         JSON data for arrivals (AJAX period swap)
    GET /missing               species that went silent vs a comparison window
    GET /missing/data          JSON data for missing (AJAX comparison swap)
"""

import json
import logging
from datetime import datetime, timezone

from flask import Flask, Response, abort, redirect, render_template, request, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from . import config
from .cache import get_cached, put_cached
from .db import (
    get_db_last_modified,
    get_detection_count,
    get_detection_years,
    get_sync_state,
    list_species_with_detections,
    open_db,
)
from .plots import registry
from .views import registry as view_registry

logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates")

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["60 per minute"],
    storage_uri="memory://",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open():
    """Open a fresh DB connection for the duration of a request."""
    return open_db(config.DB_PATH)


def _sync_status(conn) -> dict:
    """Build the sync-status dict shown in the banner."""
    state   = get_sync_state(conn)
    n       = get_detection_count(conn)
    last_ts = state["last_detection_timestamp"]

    if last_ts:
        try:
            dt = datetime.fromisoformat(last_ts).astimezone(
                timezone.utc
            )
            last_str = dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            last_str = last_ts
    else:
        last_str = "never"

    if state["backfill_complete"]:
        backfill_str = "complete"
    elif state["cursor"]:
        backfill_str = "in progress"
    else:
        backfill_str = "not started"

    return {
        "last_sync":    last_str,
        "n_detections": f"{n:,}",
        "backfill":     backfill_str,
    }


def _validated_theme(raw: str | None) -> str:
    return "light" if raw == "light" else "dark"


def _parse_year(raw: str | None, year_type: str, years: list[int]) -> int | str | None:
    """Parse the year query param, honouring the year_or_all type if needed."""
    if year_type == "year_or_all":
        if raw == "all":
            return "all"
        try:
            return int(raw) or (max(years) if years else None)
        except (TypeError, ValueError):
            return max(years) if years else None
    else:
        try:
            return int(raw or 0) or (max(years) if years else None)
        except (TypeError, ValueError):
            return max(years) if years else None


def _gather_extra_params(plot_spec: list[dict], request_args) -> dict:
    """Read all non-year/non-theme params from request args, coercing types."""
    extra: dict = {}
    for p in plot_spec:
        if p["name"] in ("year", "theme"):
            continue
        val = request_args.get(p["name"])
        if val is None:
            val = p.get("default")
        if val is not None:
            if p.get("type") == "int":
                try:
                    val = int(val)
                except (TypeError, ValueError):
                    val = p.get("default")
            elif p.get("type") == "float":
                try:
                    val = float(val)
                except (TypeError, ValueError):
                    val = p.get("default")
            elif p.get("type") == "bool":
                val = val in (True, "true", "True", "1", "yes")
        extra[p["name"]] = val
    return extra


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
@limiter.limit("30 per minute")
def index():
    conn = _open()

    species_list  = list_species_with_detections(conn)
    status        = _sync_status(conn)
    station_row   = conn.execute("SELECT name, id FROM station LIMIT 1").fetchone()
    station_label = station_row["name"] if station_row else f"station {config.STATION_ID}"

    theme = _validated_theme(request.args.get("theme"))

    # Build the URL for the theme toggle link (flips theme, keeps everything else).
    toggle_args = {k: v for k, v in request.args.items()}
    toggle_args["theme"] = "dark" if theme == "light" else "light"
    theme_toggle_url = url_for("index", **toggle_args)

    # Parse selections from query string.
    plot_type  = request.args.get("plot_type") or (
        next(iter(registry)) if registry else None
    )
    try:
        species_id = int(request.args.get("species_id", 0)) or None
    except (TypeError, ValueError):
        species_id = None

    # Years available for the selected species.
    years = get_detection_years(conn, species_id) if species_id else []
    conn.close()

    # Determine the year param type for this plot (if any).
    year_spec = None
    if plot_type and plot_type in registry:
        year_spec = next(
            (p for p in registry[plot_type].params if p["name"] == "year"),
            None,
        )
    year_type = year_spec.get("type", "int") if year_spec else "int"
    year = _parse_year(request.args.get("year"), year_type, years)

    # Build parameter inputs for the active plot type.
    plot_params: list[dict] = []
    if plot_type and plot_type in registry:
        for p in registry[plot_type].params:
            spec = dict(p)
            if spec["name"] == "year":
                spec["_choices"] = years        # dynamic per-species
                spec["_value"]   = year
            else:
                spec["_choices"] = spec.get("choices")
                spec["_value"]   = request.args.get(spec["name"], spec.get("default"))
            plot_params.append(spec)

    # Show the image if the required inputs are present.
    # Station-wide plots (REQUIRES_SPECIES = False) don't need a species selection.
    has_year_param     = any(p["name"] == "year" for p in plot_params)
    plot_needs_species = registry[plot_type].requires_species if (plot_type and plot_type in registry) else True
    show_image = bool(
        plot_type
        and (species_id or not plot_needs_species)
        and (year is not None or not has_year_param)
    )

    # Build the image URL dynamically so all params (including extras) are included.
    plot_image_url = None
    if show_image:
        img_kwargs: dict = {
            "plot_type":  plot_type,
            "species_id": species_id or 0,   # 0 for station-wide plots with no species selected
            "theme":      theme,
        }
        if year is not None:
            img_kwargs["year"] = year
        for p in plot_params:
            if p["name"] not in ("year", "theme") and p.get("_value") is not None:
                img_kwargs[p["name"]] = p["_value"]
        plot_image_url = url_for("plot_image", **img_kwargs)

    return render_template(
        "index.html",
        registry         = registry,
        species_list     = species_list,
        plot_type        = plot_type,
        species_id       = species_id,
        year             = year,
        plot_params      = plot_params,
        show_image       = show_image,
        plot_image_url   = plot_image_url,
        status           = status,
        theme            = theme,
        theme_toggle_url = theme_toggle_url,
        station_label    = station_label,
    )


@app.route("/plot/<plot_type>/<int:species_id>.png")
@limiter.limit("8 per minute")
def plot_image(plot_type: str, species_id: int):
    if plot_type not in registry:
        abort(404)

    plot_spec = registry[plot_type].params
    theme     = _validated_theme(request.args.get("theme"))

    # Determine year, honouring year_or_all type.
    year_spec = next((p for p in plot_spec if p["name"] == "year"), None)
    year_type = year_spec.get("type", "int") if year_spec else "int"

    if year_type == "year_or_all":
        raw = request.args.get("year", "")
        if raw == "all":
            year: int | str = "all"
        else:
            try:
                year = int(raw) if raw else datetime.now(tz=timezone.utc).year
            except (TypeError, ValueError):
                year = datetime.now(tz=timezone.utc).year
    else:
        try:
            year = int(request.args.get("year", 0)) or datetime.now(tz=timezone.utc).year
        except (TypeError, ValueError):
            year = datetime.now(tz=timezone.utc).year

    # Gather all extra params from the plot's PARAMS spec.
    extra_params = _gather_extra_params(plot_spec, request.args)
    cache_extra  = extra_params if extra_params else None

    conn  = _open()
    db_lm = get_db_last_modified(conn)

    png = get_cached(config.CACHE_PATH, plot_type, species_id, year, db_lm, theme, cache_extra)
    if png is None:
        logger.info(
            "Cache miss — rendering %s / species=%d / year=%s / theme=%s / extra=%s",
            plot_type, species_id, year, theme, extra_params,
        )
        try:
            png = registry[plot_type].render(
                conn, species_id, year=year, theme=theme, **extra_params
            )
        except Exception:
            logger.exception("Render failed")
            conn.close()
            abort(500)
        put_cached(config.CACHE_PATH, plot_type, species_id, year, db_lm, png, theme, cache_extra)

    conn.close()
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "no-cache"})


@app.route("/status")
@limiter.limit("10 per minute")
def status_json():
    conn = _open()
    data = _sync_status(conn)
    conn.close()
    return Response(json.dumps(data), mimetype="application/json")


# ---------------------------------------------------------------------------
# Shared helper for view pages
# ---------------------------------------------------------------------------

def _view_context(conn, theme: str, toggle_endpoint: str, **toggle_extra) -> dict:
    """Build the dict of template variables shared by all three view pages."""
    status = _sync_status(conn)
    station_row = conn.execute("SELECT name FROM station LIMIT 1").fetchone()
    station_label = station_row["name"] if station_row else f"station {config.STATION_ID}"
    toggle_args = {"theme": "dark" if theme == "light" else "light", **toggle_extra}
    theme_toggle_url = url_for(toggle_endpoint, **toggle_args)
    return {
        "theme":            theme,
        "theme_toggle_url": theme_toggle_url,
        "status":           status,
        "station_label":    station_label,
    }


# ---------------------------------------------------------------------------
# Recordings
# ---------------------------------------------------------------------------

@app.route("/recordings")
@limiter.limit("20 per minute")
def recordings_page():
    theme = _validated_theme(request.args.get("theme"))
    conn = _open()
    ctx = _view_context(conn, theme, "recordings_page")
    data = view_registry["recordings"].render_data(conn, theme=theme)
    species_list = list_species_with_detections(conn)
    conn.close()
    return render_template("recordings.html", data=data, species_list=species_list, **ctx)


@app.route("/recordings/data")
@limiter.limit("30 per minute")
def recordings_data():
    theme = _validated_theme(request.args.get("theme"))
    conn = _open()
    data = view_registry["recordings"].render_data(conn, theme=theme)
    conn.close()
    return Response(json.dumps(data), mimetype="application/json")


@app.route("/recordings/species/<int:species_id>")
@limiter.limit("20 per minute")
def species_recordings_page(species_id: int):
    theme = _validated_theme(request.args.get("theme"))
    conn = _open()
    ctx = _view_context(conn, theme, "species_recordings_page", species_id=species_id)
    data = view_registry["species_recordings"].render_data(conn, species_id=species_id, theme=theme)
    conn.close()
    return render_template("species_recordings.html", data=data, **ctx)


# ---------------------------------------------------------------------------
# Arrivals
# ---------------------------------------------------------------------------

def _arrivals_html(data: dict) -> str:
    """Render the arrivals list as an HTML string (used for initial page load
    and as the return value of the AJAX endpoint — keeps both in sync)."""
    if not data.get("arrivals"):
        return f'<p class="no-selection">No new arrivals for {data["period_label"]}.</p>'

    count = data["count"]
    noun = "species" if count == 1 else "species"
    html = f'<p class="result-count"><strong>{count}</strong> new {noun} in {data["period_label"]}</p>'
    html += '<div class="card-list">'
    for a in data["arrivals"]:
        det = "1 detection" if a["total"] == 1 else f"{a['total']} detections"
        sci = a["scientific_name"] or ""
        html += (
            f'<div class="arrival-card">'
            f'<div class="sp-name">{a["common_name"]}</div>'
            f'<div class="sp-sci">{sci}</div>'
            f'<div class="sp-meta">First seen: {a["first_seen"]}'
            f' &nbsp;·&nbsp; <span class="highlight">{det}</span> since arrival</div>'
            f'</div>'
        )
    html += "</div>"
    return html


@app.route("/arrivals")
@limiter.limit("20 per minute")
def arrivals_page():
    theme  = _validated_theme(request.args.get("theme"))
    period = request.args.get("period", "week")
    conn   = _open()
    ctx    = _view_context(conn, theme, "arrivals_page")
    data   = view_registry["arrivals"].render_data(conn, period=period, theme=theme)
    conn.close()
    return render_template(
        "arrivals.html",
        data         = data,
        arrivals_html = _arrivals_html(data),
        **ctx,
    )


@app.route("/arrivals/data")
@limiter.limit("60 per minute")
def arrivals_data():
    theme  = _validated_theme(request.args.get("theme"))
    period = request.args.get("period", "week")
    conn   = _open()
    data   = view_registry["arrivals"].render_data(conn, period=period, theme=theme)
    conn.close()
    return Response(json.dumps(data), mimetype="application/json")


# ---------------------------------------------------------------------------
# Missing (Gone Quiet)
# ---------------------------------------------------------------------------

def _missing_html(data: dict) -> str:
    """Render the missing-species list as an HTML string (initial load + AJAX)."""
    if not data.get("missing"):
        return f'<p class="no-selection">No missing species for {data["comparison_label"]}.</p>'

    count = data["count"]
    html = (
        f'<p class="result-count"><strong>{count}</strong> species gone quiet '
        f'{data["comparison_label"]}</p>'
    )
    html += '<div class="card-list">'
    for m in data["missing"]:
        det = "1 detection" if m["count"] == 1 else f"{m['count']} detections"
        sci = m["scientific_name"] or ""
        html += (
            f'<div class="missing-card">'
            f'<div class="sp-name">{m["common_name"]}</div>'
            f'<div class="sp-sci">{sci}</div>'
            f'<div class="sp-meta">'
            f'Last seen: <span class="highlight">{m["last_seen"]}</span>'
            f' &nbsp;·&nbsp; {det} in comparison period'
            f'</div>'
            f'</div>'
        )
    html += "</div>"
    return html


@app.route("/missing")
@limiter.limit("20 per minute")
def missing_page():
    theme      = _validated_theme(request.args.get("theme"))
    comparison = request.args.get("comparison", "last_week")
    conn       = _open()
    ctx        = _view_context(conn, theme, "missing_page")
    data       = view_registry["missing"].render_data(conn, comparison=comparison, theme=theme)
    conn.close()
    return render_template(
        "missing.html",
        data         = data,
        missing_html = _missing_html(data),
        **ctx,
    )


@app.route("/missing/data")
@limiter.limit("60 per minute")
def missing_data():
    theme      = _validated_theme(request.args.get("theme"))
    comparison = request.args.get("comparison", "last_week")
    conn       = _open()
    data       = view_registry["missing"].render_data(conn, comparison=comparison, theme=theme)
    conn.close()
    return Response(json.dumps(data), mimetype="application/json")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    from waitress import serve
    logger.info("Starting on %s:%d", config.BIND_HOST, config.BIND_PORT)
    serve(app, host=config.BIND_HOST, port=config.BIND_PORT, threads=4)
