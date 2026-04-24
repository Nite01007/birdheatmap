"""BirdWeather GraphQL client and sync logic (backfill + incremental).

Schema facts confirmed against the live API (station 5114, 2026-04-23):

  station(id: ID!) → Station
    Station.coords { lat, lon }       (nested object, not flat fields)
    Station.timezone                  (NON_NULL String)
    Station.earliestDetectionAt       (ISO8601DateTime, nullable)

  detections(
      stationIds: [ID!],
      first: Int, after: String,
      period: InputDuration { from: ISO8601Date, to: ISO8601Date }
  ) → DetectionConnection
    DetectionConnection.totalCount    (Int, NON_NULL)
    DetectionConnection.pageInfo      { hasNextPage, endCursor }
    DetectionConnection.nodes[]
      Detection.id                    (ID, NON_NULL) — comes back as a string
      Detection.timestamp             (ISO8601DateTime) — tz-aware, e.g. "…-04:00"
      Detection.confidence            (Float, NON_NULL)
      Detection.probability           (Float, nullable)
      Detection.score                 (Float, NON_NULL)
      Detection.species               (Species, NON_NULL)
        Species.id                    (ID, NON_NULL) — comes back as a string
        Species.commonName            (String, NON_NULL)
        Species.scientificName        (String, nullable)

  Cursor format: Unix timestamp encoded as base64.
    Stable across new inserts (unlike an offset cursor).
    After: cursor  means "detections with timestamp < cursor_timestamp".
    Default sort: newest-first.

  Root detections WITHOUT an explicit period returns only a short recent window.
  Always pass period: { from: ... } for full-history queries.

Backfill strategy:
  Use detections(stationIds, period: { from: earliestDate }, first: N, after: cursor).
  Pages go newest → oldest.  After each page, persist endCursor + newest seen timestamp
  so a restart resumes from the last successfully fetched page.

Incremental strategy:
  Same query but period: { from: lastSyncDate } to fetch only new detections.
"""

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import requests

from . import config
from .db import (
    get_sync_state,
    insert_detections,
    transaction,
    update_sync_state,
    upsert_species,
    upsert_station,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GraphQL queries
# ---------------------------------------------------------------------------

_STATION_QUERY = """
query StationMeta($id: ID!) {
    station(id: $id) {
        id
        name
        timezone
        coords { lat lon }
        earliestDetectionAt
        latestDetectionAt
    }
}
"""

_DETECTIONS_QUERY = """
query Detections(
    $stationIds: [ID!],
    $after:      String,
    $first:      Int,
    $period:     InputDuration
) {
    detections(
        stationIds: $stationIds,
        after:      $after,
        first:      $first,
        period:     $period
    ) {
        totalCount
        pageInfo {
            hasNextPage
            endCursor
        }
        nodes {
            id
            timestamp
            confidence
            probability
            score
            species {
                id
                commonName
                scientificName
            }
        }
    }
}
"""


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _gql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """Execute one GraphQL request; raises on HTTP error or GraphQL errors."""
    response = requests.post(
        config.BIRDWEATHER_API_URL,
        json={"query": query, "variables": variables},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if "errors" in payload:
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload["data"]


# ---------------------------------------------------------------------------
# Timestamp normalization
# ---------------------------------------------------------------------------

def _to_utc_str(ts: str) -> str:
    """Convert an ISO-8601 timestamp (possibly tz-aware) to a UTC ISO-8601 string.

    BirdWeather returns timestamps like "2026-04-23T06:44:19-04:00".
    We store them as "2026-04-23T10:44:19+00:00" (UTC, explicit offset).
    """
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        # Treat naive timestamps as UTC (shouldn't happen but be safe).
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Station metadata
# ---------------------------------------------------------------------------

def fetch_and_cache_station(conn) -> dict[str, Any]:
    """Fetch station metadata and persist it; return the station dict."""
    logger.info("Fetching station metadata for station %s …", config.STATION_ID)
    data = _gql(_STATION_QUERY, {"id": config.STATION_ID})
    raw = data["station"]

    with transaction(conn):
        upsert_station(
            conn,
            station_id=str(raw["id"]),
            name=raw["name"],
            lat=raw["coords"]["lat"],
            lon=raw["coords"]["lon"],
            timezone=raw["timezone"],
        )

    logger.info(
        "Station cached: %s  (%.5f, %.5f)  tz=%s  earliest=%s",
        raw["name"],
        raw["coords"]["lat"],
        raw["coords"]["lon"],
        raw["timezone"],
        raw.get("earliestDetectionAt"),
    )
    return raw


# ---------------------------------------------------------------------------
# Main sync entry point
# ---------------------------------------------------------------------------

def sync(conn, *, dry_run: bool = False, max_pages: int | None = None) -> None:
    """Dispatch to backfill or incremental sync based on current state.

    dry_run=True fetches one page and logs what it got without writing to the DB.
    max_pages limits how many pages are fetched (useful for smoke-testing).
    """
    state = get_sync_state(conn)

    if not state["backfill_complete"]:
        logger.info(
            "Backfill mode  cursor=%s  last_ts=%s",
            state["cursor"],
            state["last_detection_timestamp"],
        )
        _run_backfill(conn, cursor=state["cursor"], dry_run=dry_run, max_pages=max_pages)
    else:
        logger.info(
            "Incremental mode  since=%s", state["last_detection_timestamp"]
        )
        _run_incremental(conn, since=state["last_detection_timestamp"], dry_run=dry_run)


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------

def _run_backfill(
    conn,
    *,
    cursor: str | None,
    dry_run: bool,
    max_pages: int | None,
) -> None:
    """Page through all station detections (newest → oldest) and store them.

    Persists endCursor after every page so a crash/restart picks up where
    it left off.
    """
    from_date = config.BACKFILL_FROM_DATE

    page_num = 0
    total_inserted = 0
    # High-water mark: the newest timestamp we've ever seen across all pages.
    # Pages go newest→oldest, so only page 1 (or the initial resume state) sets
    # this.  We must NOT overwrite it with older values from later pages.
    current_hwm = get_sync_state(conn)["last_detection_timestamp"] or ""

    # to_date for the current pagination session.  Using today on fresh start;
    # reset to the oldest stored detection when a gap fill is triggered.
    session_to_date = datetime.now(tz=timezone.utc).date().isoformat()
    # Guard against looping on a second gap if the API keeps truncating.
    _gap_fill_attempted = False

    while True:
        if max_pages is not None and page_num >= max_pages:
            logger.info("Stopping after %d pages (max_pages limit)", page_num)
            break

        variables: dict[str, Any] = {
            "stationIds": [config.STATION_ID],
            "after": cursor,
            "first": config.BACKFILL_PAGE_SIZE,
            "period": {"from": from_date, "to": session_to_date},
        }

        logger.debug("Fetching page %d (cursor=%s) …", page_num + 1, cursor)
        data = _gql(_DETECTIONS_QUERY, variables)
        conn_data = data["detections"]
        page_info = conn_data["pageInfo"]
        nodes = conn_data["nodes"]
        total_count = conn_data["totalCount"]

        if dry_run:
            _log_dry_run_page(page_num + 1, nodes, page_info, total_count)
            break

        if not nodes:
            # Same gap check as in the hasNextPage=false branch.
            oldest_row = conn.execute(
                "SELECT MIN(timestamp_utc) FROM detection"
            ).fetchone()
            oldest_date = (oldest_row[0] or session_to_date)[:10]
            from datetime import date as _date
            gap_days = (
                _date.fromisoformat(oldest_date) - _date.fromisoformat(from_date)
            ).days
            if gap_days > 7 and not _gap_fill_attempted:
                logger.warning(
                    "Empty page but gap detected: oldest=%s from_date=%s (%d days). "
                    "Starting targeted gap-fill.",
                    oldest_date, from_date, gap_days,
                )
                _gap_fill_attempted = True
                cursor = None
                session_to_date = oldest_date
                with transaction(conn):
                    update_sync_state(conn, cursor=None)
                continue
            logger.info("Empty page — backfill complete.")
            with transaction(conn):
                update_sync_state(conn, backfill_complete=True)
            break

        rows, newest_ts_on_page = _parse_nodes(nodes)
        new_cursor = page_info.get("endCursor")

        # Only advance the high-water mark when we see something genuinely newer.
        new_hwm = newest_ts_on_page if newest_ts_on_page > current_hwm else None
        if new_hwm:
            current_hwm = new_hwm

        with transaction(conn):
            for row in rows:
                upsert_species(conn, int(row["species_id"]), row["common_name"], row["scientific_name"])
            inserted = insert_detections(conn, rows)
            update_sync_state(
                conn,
                cursor=new_cursor,
                last_detection_timestamp=new_hwm,  # None = don't overwrite
            )

        total_inserted += inserted
        cursor = new_cursor
        page_num += 1

        if page_num % config.BACKFILL_LOG_EVERY_N_PAGES == 0:
            logger.info(
                "Backfill: page %d  inserted=%d  total_so_far=%d  api_total=%d",
                page_num,
                inserted,
                total_inserted,
                total_count,
            )

        if not page_info["hasNextPage"]:
            # Before marking done, check whether history is actually covered.
            # The API occasionally returns hasNextPage=false prematurely.
            oldest_row = conn.execute(
                "SELECT MIN(timestamp_utc) FROM detection"
            ).fetchone()
            oldest_date = (oldest_row[0] or session_to_date)[:10]
            from datetime import date as _date
            gap_days = (
                _date.fromisoformat(oldest_date) - _date.fromisoformat(from_date)
            ).days

            if gap_days > 7 and not _gap_fill_attempted:
                logger.warning(
                    "Premature end-of-pagination detected: oldest stored=%s but "
                    "from_date=%s (%d day gap). Starting targeted gap-fill.",
                    oldest_date, from_date, gap_days,
                )
                _gap_fill_attempted = True
                cursor = None
                session_to_date = oldest_date  # fetch only the missing window
                with transaction(conn):
                    update_sync_state(conn, cursor=None)
                continue

            with transaction(conn):
                update_sync_state(conn, backfill_complete=True)
            logger.info(
                "Backfill complete: %d pages, %d detections inserted, %d total in API",
                page_num,
                total_inserted,
                total_count,
            )
            break

        time.sleep(config.BACKFILL_RATE_LIMIT_SECONDS)


# ---------------------------------------------------------------------------
# Incremental sync
# ---------------------------------------------------------------------------

def _run_incremental(conn, *, since: str | None, dry_run: bool) -> None:
    """Fetch detections newer than *since* and append them."""
    if since is None:
        # No prior sync recorded — use yesterday as a safe window.
        since = (datetime.now(tz=timezone.utc) - timedelta(days=1)).date().isoformat()
        logger.warning("No last_detection_timestamp found; using since=%s", since)

    # period.from only accepts ISO8601Date (YYYY-MM-DD), not a datetime string.
    since_date = since[:10]  # strip time component if present

    page_num = 0
    total_inserted = 0
    cursor: str | None = None
    newest_ts = since

    while True:
        today = datetime.now(tz=timezone.utc).date().isoformat()
        variables: dict[str, Any] = {
            "stationIds": [config.STATION_ID],
            "after": cursor,
            "first": config.BACKFILL_PAGE_SIZE,
            "period": {"from": since_date, "to": today},
        }

        data = _gql(_DETECTIONS_QUERY, variables)
        conn_data = data["detections"]
        page_info = conn_data["pageInfo"]
        nodes = conn_data["nodes"]

        if dry_run:
            _log_dry_run_page(page_num + 1, nodes, page_info, conn_data["totalCount"])
            break

        if not nodes:
            break

        rows, newest_on_page = _parse_nodes(nodes)
        if newest_on_page > newest_ts:
            newest_ts = newest_on_page

        with transaction(conn):
            for row in rows:
                upsert_species(conn, int(row["species_id"]), row["common_name"], row["scientific_name"])
            inserted = insert_detections(conn, rows)
            update_sync_state(conn, last_detection_timestamp=newest_ts)

        total_inserted += inserted
        cursor = page_info.get("endCursor")
        page_num += 1

        if not page_info["hasNextPage"]:
            break

        time.sleep(config.BACKFILL_RATE_LIMIT_SECONDS)

    logger.info(
        "Incremental sync done: %d page(s), %d new detections inserted",
        page_num,
        total_inserted,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_nodes(nodes: list[dict]) -> tuple[list[dict], str]:
    """Convert raw GraphQL detection nodes into DB-insert-ready dicts.

    Returns (rows, newest_timestamp_utc_str).
    Newest is across all nodes on this page, used to track the high-water mark.
    """
    rows: list[dict] = []
    newest_ts = ""

    for node in nodes:
        sp = node["species"]
        raw_ts = node.get("timestamp") or ""
        ts_utc = _to_utc_str(raw_ts) if raw_ts else ""

        if ts_utc > newest_ts:
            newest_ts = ts_utc

        rows.append({
            "id": int(node["id"]),
            "species_id": str(sp["id"]),   # kept as str for upsert; cast at insert site
            "common_name": sp["commonName"],
            "scientific_name": sp.get("scientificName") or "",
            "timestamp_utc": ts_utc,
            "confidence": node.get("confidence"),
            "probability": node.get("probability"),
            "score": node.get("score"),
        })

    return rows, newest_ts


def _log_dry_run_page(
    page_num: int,
    nodes: list[dict],
    page_info: dict,
    total_count: int,
) -> None:
    logger.info(
        "[dry-run] Page %d: %d detections  hasNextPage=%s  endCursor=%s  totalCount=%s",
        page_num,
        len(nodes),
        page_info.get("hasNextPage"),
        page_info.get("endCursor"),
        total_count,
    )
    if nodes:
        first = nodes[0]
        last = nodes[-1]
        logger.info(
            "[dry-run] Newest: id=%s  ts=%s  species=%s",
            first["id"],
            first.get("timestamp"),
            first["species"]["commonName"],
        )
        logger.info(
            "[dry-run] Oldest: id=%s  ts=%s  species=%s",
            last["id"],
            last.get("timestamp"),
            last["species"]["commonName"],
        )
