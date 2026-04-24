"""CLI entry point.  Run as `python -m birdheatmap <command>`.

Commands:
    sync    One-shot sync (backfill if needed, then incremental).
    serve   Start the web server (also starts the APScheduler sync job).
    render  Render a single plot to a PNG file.
    plots   List registered plot types.
    species List species in the local cache.
"""

import logging
import os
import sys
from pathlib import Path

# Point matplotlib at a writable cache dir so it never tries to write to the
# service user's (nonexistent) home directory.  Set before any matplotlib import.
if "MPLCONFIGDIR" not in os.environ:
    _mpl_dir = Path(
        os.environ.get("STATE_DIRECTORY", "/var/lib/birdheatmap")
    ) / ".matplotlib"
    _mpl_dir.mkdir(parents=True, exist_ok=True)
    os.environ["MPLCONFIGDIR"] = str(_mpl_dir)

import click

from . import config
from .db import open_db

# Basic logging setup: INFO to stdout, can be overridden by --verbose.
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable DEBUG logging.")
def cli(verbose: bool) -> None:
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------

@cli.command()
@click.option(
    "--dry-run",
    is_flag=True,
    help="Fetch 2 pages and print the raw response; do not write to the database.",
)
@click.option(
    "--max-pages",
    type=int,
    default=None,
    help="Stop after this many pages (useful for testing).",
)
def sync(dry_run: bool, max_pages: int | None) -> None:
    """Run a one-shot sync (backfill if needed, then incremental)."""
    from .sync import fetch_and_cache_station, sync as run_sync

    conn = open_db(config.DB_PATH)

    from .db import get_station
    if get_station(conn, config.STATION_ID) is None:
        fetch_and_cache_station(conn)

    run_sync(conn, dry_run=dry_run, max_pages=max_pages or (2 if dry_run else None))
    conn.close()


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------

@cli.command()
def serve() -> None:
    """Start the web server and background sync scheduler."""
    from . import scheduler
    from .sync import fetch_and_cache_station
    from .db import get_station

    conn = open_db(config.DB_PATH)
    if get_station(conn, config.STATION_ID) is None:
        fetch_and_cache_station(conn)
    conn.close()

    scheduler.start(config.DB_PATH)

    from .web import run
    run()


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--plot", "plot_type", required=True, help="Plot type slug, e.g. annual_heatmap.")
@click.option("--species", "species_name", required=True, help='Common name, e.g. "Black-capped Chickadee".')
@click.option("--year", type=int, default=None, help="Calendar year (default: current year).")
@click.option("--out", "out_path", required=True, type=click.Path(), help="Output PNG path.")
def render(plot_type: str, species_name: str, year: int | None, out_path: str) -> None:
    """Render a single plot to a PNG file."""
    import datetime

    from .plots import registry

    if plot_type not in registry:
        click.echo(f"Unknown plot type: {plot_type!r}. Run `plots` to see available types.")
        sys.exit(1)

    conn = open_db(config.DB_PATH)
    row = conn.execute(
        "SELECT id FROM species WHERE common_name = ? COLLATE NOCASE LIMIT 1",
        (species_name,),
    ).fetchone()
    if row is None:
        click.echo(f"Species not found in cache: {species_name!r}")
        sys.exit(1)

    effective_year = year or datetime.date.today().year
    png_bytes = registry[plot_type].render(conn, row["id"], year=effective_year)
    conn.close()

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png_bytes)
    click.echo(f"Saved {len(png_bytes):,} bytes → {out}")


# ---------------------------------------------------------------------------
# plots
# ---------------------------------------------------------------------------

@cli.command("plots")
def list_plots() -> None:
    """List registered plot types."""
    from .plots import registry

    if not registry:
        click.echo("No plot types registered.")
        return
    for name, pm in registry.items():
        click.echo(f"  {name:<25}  {pm.display_name}")
        click.echo(f"  {'':25}  {pm.description}")
        click.echo()


# ---------------------------------------------------------------------------
# reset-backfill
# ---------------------------------------------------------------------------

@cli.command("reset-backfill")
def reset_backfill() -> None:
    """Reset backfill state so the next sync re-fetches all history.

    Safe to run at any time — existing detections are kept and will be
    skipped (INSERT OR IGNORE) when the backfill re-runs.
    """
    from .db import transaction, update_sync_state

    conn = open_db(config.DB_PATH)
    with transaction(conn):
        conn.execute(
            "UPDATE sync_state SET backfill_complete = 0, cursor = NULL WHERE id = 1"
        )
    conn.close()
    click.echo("Backfill state reset.  Run `sync` to re-fetch missing history.")


# ---------------------------------------------------------------------------
# species
# ---------------------------------------------------------------------------

@cli.command("species")
def list_species() -> None:
    """List species in the local detection cache."""
    from .db import list_species_with_detections

    conn = open_db(config.DB_PATH)
    rows = list_species_with_detections(conn)
    conn.close()

    if not rows:
        click.echo("No species in cache yet.  Run `sync` first.")
        return
    click.echo(f"{'ID':>6}  {'Common Name':<35}  Scientific Name")
    click.echo("-" * 70)
    for r in rows:
        click.echo(f"{r['id']:>6}  {r['common_name']:<35}  {r['scientific_name']}")


if __name__ == "__main__":
    cli()
