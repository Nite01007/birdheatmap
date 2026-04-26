"""Flask web application.

Single-page, server-rendered UI.  No JavaScript frameworks — the page
reloads on form submission.  The species dropdown auto-submits on change
so the year dropdown reflects only years with data for that species.

Routes:
    GET /                    main page (form + inline image if species selected)
    GET /plot/<type>/<id>.png  render or serve cached PNG
    GET /status              JSON sync status (used by nothing yet, handy for debugging)
"""

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

    # Show the image if species + plot type are both selected.
    has_year_param = any(p["name"] == "year" for p in plot_params)
    show_image = bool(species_id and plot_type and (year is not None or not has_year_param))

    # Build the image URL dynamically so all params (including extras) are included.
    plot_image_url = None
    if show_image:
        img_kwargs: dict = {
            "plot_type":  plot_type,
            "species_id": species_id,
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
    import json
    conn = _open()
    data = _sync_status(conn)
    conn.close()
    return Response(json.dumps(data), mimetype="application/json")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    from waitress import serve
    logger.info("Starting on %s:%d", config.BIND_HOST, config.BIND_PORT)
    serve(app, host=config.BIND_HOST, port=config.BIND_PORT, threads=4)
