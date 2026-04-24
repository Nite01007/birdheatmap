"""SQLite database — schema creation and low-level helpers.

All timestamps stored in UTC (ISO-8601 strings).  Timezone-aware datetime
objects are converted to UTC before insert; callers get back naive UTC datetimes.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS station (
    id              TEXT PRIMARY KEY,
    name            TEXT,
    lat             REAL,
    lon             REAL,
    timezone        TEXT,
    last_full_sync  TEXT,       -- ISO-8601 UTC timestamp
    last_incremental_sync TEXT  -- ISO-8601 UTC timestamp
);

CREATE TABLE IF NOT EXISTS species (
    id              INTEGER PRIMARY KEY,
    common_name     TEXT NOT NULL,
    scientific_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS detection (
    id              INTEGER PRIMARY KEY,
    species_id      INTEGER NOT NULL REFERENCES species(id),
    timestamp_utc   TEXT NOT NULL,  -- ISO-8601 UTC (Z suffix)
    confidence      REAL,
    probability     REAL,
    score           REAL            -- BirdWeather composite score (always present)
);

-- Speeds up per-species year/day/time aggregations used by all plots.
CREATE INDEX IF NOT EXISTS idx_detection_species_ts
    ON detection (species_id, timestamp_utc);

CREATE TABLE IF NOT EXISTS sync_state (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton row
    cursor                  TEXT,   -- opaque GraphQL pagination cursor
    last_detection_timestamp TEXT,  -- ISO-8601 UTC; newest detection seen so far
    backfill_complete       INTEGER NOT NULL DEFAULT 0  -- 0 = in progress, 1 = done
);

-- Ensure the singleton row exists.
INSERT OR IGNORE INTO sync_state (id, backfill_complete) VALUES (1, 0);
"""


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def open_db(path: Path) -> sqlite3.Connection:
    """Open (and if necessary create) the SQLite database at *path*.

    Runs the schema migration so new installations bootstrap automatically.
    Returns a connection with row_factory set to sqlite3.Row for dict-like access.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection, None, None]:
    """Context manager that commits on success and rolls back on exception."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Sync-state helpers
# ---------------------------------------------------------------------------

def get_sync_state(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM sync_state WHERE id = 1").fetchone()


def update_sync_state(
    conn: sqlite3.Connection,
    *,
    cursor: str | None = None,
    last_detection_timestamp: str | None = None,
    backfill_complete: bool | None = None,
) -> None:
    """Partially update the singleton sync_state row."""
    fields: dict[str, object] = {}
    if cursor is not None:
        fields["cursor"] = cursor
    if last_detection_timestamp is not None:
        fields["last_detection_timestamp"] = last_detection_timestamp
    if backfill_complete is not None:
        fields["backfill_complete"] = 1 if backfill_complete else 0
    if not fields:
        return
    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    conn.execute(f"UPDATE sync_state SET {set_clause} WHERE id = 1", fields)


# ---------------------------------------------------------------------------
# Station helpers
# ---------------------------------------------------------------------------

def upsert_station(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    name: str,
    lat: float,
    lon: float,
    timezone: str,
) -> None:
    conn.execute(
        """
        INSERT INTO station (id, name, lat, lon, timezone)
        VALUES (:id, :name, :lat, :lon, :timezone)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            lat  = excluded.lat,
            lon  = excluded.lon,
            timezone = excluded.timezone
        """,
        {"id": station_id, "name": name, "lat": lat, "lon": lon, "timezone": timezone},
    )


def get_station(conn: sqlite3.Connection, station_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM station WHERE id = ?", (station_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# Species helpers
# ---------------------------------------------------------------------------

def upsert_species(
    conn: sqlite3.Connection,
    species_id: int,
    common_name: str,
    scientific_name: str,
) -> None:
    conn.execute(
        """
        INSERT INTO species (id, common_name, scientific_name)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            common_name     = excluded.common_name,
            scientific_name = excluded.scientific_name
        """,
        (species_id, common_name, scientific_name),
    )


def list_species_with_detections(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all species that have at least one detection, sorted by common name."""
    return conn.execute(
        """
        SELECT s.id, s.common_name, s.scientific_name
        FROM species s
        WHERE EXISTS (SELECT 1 FROM detection d WHERE d.species_id = s.id)
        ORDER BY s.common_name
        """
    ).fetchall()


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def insert_detections(
    conn: sqlite3.Connection,
    rows: list[dict],
) -> int:
    """Bulk-insert detection rows; silently skips duplicates.  Returns inserted count."""
    # executemany's cursor.rowcount is the total affected rows across all statements.
    cur = conn.executemany(
        """
        INSERT OR IGNORE INTO detection (id, species_id, timestamp_utc, confidence, probability, score)
        VALUES (:id, :species_id, :timestamp_utc, :confidence, :probability, :score)
        """,
        rows,
    )
    return cur.rowcount


def get_detection_years(conn: sqlite3.Connection, species_id: int) -> list[int]:
    """Return a sorted list of calendar years (local time) that have detections."""
    rows = conn.execute(
        """
        SELECT DISTINCT CAST(strftime('%Y', timestamp_utc) AS INTEGER) AS yr
        FROM detection
        WHERE species_id = ?
        ORDER BY yr
        """,
        (species_id,),
    ).fetchall()
    return [r["yr"] for r in rows]


def get_detection_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM detection").fetchone()[0]


def get_db_last_modified(conn: sqlite3.Connection) -> str:
    """Return the max timestamp_utc string across all detections (or empty string)."""
    row = conn.execute("SELECT MAX(timestamp_utc) FROM detection").fetchone()
    return row[0] or ""
