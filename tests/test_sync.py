"""Tests for sync logic — cursor resumability and high-water mark tracking.

These tests mock _gql() so no network calls are made.
"""

import sqlite3
from datetime import timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from birdheatmap.db import get_sync_state, open_db
from birdheatmap.sync import _run_backfill, _to_utc_str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path: Path) -> sqlite3.Connection:
    return open_db(tmp_path / "test.sqlite")


def _detection_node(det_id: int, ts: str, species_id: str = "111") -> dict:
    return {
        "id": str(det_id),
        "timestamp": ts,
        "confidence": 0.75,
        "probability": 0.30,
        "score": 7.0,
        "species": {
            "id": species_id,
            "commonName": "Test Warbler",
            "scientificName": "Testus warblericus",
        },
    }


def _page(nodes: list, cursor: str, has_next: bool, total: int = 100) -> dict:
    return {
        "detections": {
            "totalCount": total,
            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            "nodes": nodes,
        }
    }


# ---------------------------------------------------------------------------
# _to_utc_str
# ---------------------------------------------------------------------------

class TestToUtcStr:
    def test_converts_eastern_offset(self):
        result = _to_utc_str("2025-06-01T06:00:00-04:00")
        assert result == "2025-06-01T10:00:00+00:00"

    def test_already_utc_passthrough(self):
        result = _to_utc_str("2025-01-15T12:00:00+00:00")
        assert result == "2025-01-15T12:00:00+00:00"

    def test_naive_treated_as_utc(self):
        result = _to_utc_str("2025-03-01T08:30:00")
        assert result == "2025-03-01T08:30:00+00:00"

    def test_winter_offset(self):
        # EST = UTC-5
        result = _to_utc_str("2025-01-01T07:00:00-05:00")
        assert result == "2025-01-01T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Backfill cursor persistence and resumability
# ---------------------------------------------------------------------------

class TestBackfillCursorPersistence:
    """Cursor is written to DB after each page so restarts resume correctly."""

    def test_cursor_saved_after_first_page(self, tmp_path):
        conn = _make_db(tmp_path)

        pages = [
            _page([_detection_node(1, "2025-06-01T12:00:00-04:00")], cursor="cursor_A", has_next=True),
            _page([_detection_node(2, "2025-05-31T12:00:00-04:00")], cursor="cursor_B", has_next=False),
        ]
        page_iter = iter(pages)

        with patch("birdheatmap.sync._gql", side_effect=lambda q, v: next(page_iter)):
            _run_backfill(conn, cursor=None, dry_run=False, max_pages=1)

        state = get_sync_state(conn)
        assert state["cursor"] == "cursor_A"
        assert state["backfill_complete"] == 0

    def test_resume_uses_stored_cursor(self, tmp_path):
        conn = _make_db(tmp_path)

        # Simulate a restart: cursor_A was already saved.
        from birdheatmap.db import transaction, update_sync_state
        with transaction(conn):
            update_sync_state(conn, cursor="cursor_A")

        captured_variables: list[dict] = []

        def fake_gql(query, variables):
            captured_variables.append(variables)
            return _page(
                [_detection_node(2, "2025-05-31T12:00:00-04:00")],
                cursor="cursor_B",
                has_next=False,
            )

        with patch("birdheatmap.sync._gql", side_effect=fake_gql):
            _run_backfill(conn, cursor="cursor_A", dry_run=False, max_pages=1)

        # The API must have been called with the stored cursor, not None.
        assert captured_variables[0]["after"] == "cursor_A"

    def test_backfill_complete_set_on_last_page(self, tmp_path):
        conn = _make_db(tmp_path)

        with patch("birdheatmap.sync._gql", return_value=_page(
            [_detection_node(1, "2025-06-01T08:00:00-04:00")],
            cursor="final_cursor",
            has_next=False,
        )):
            _run_backfill(conn, cursor=None, dry_run=False, max_pages=None)

        state = get_sync_state(conn)
        assert state["backfill_complete"] == 1

    def test_empty_page_marks_backfill_complete(self, tmp_path):
        conn = _make_db(tmp_path)

        with patch("birdheatmap.sync._gql", return_value=_page([], cursor=None, has_next=False)):
            _run_backfill(conn, cursor=None, dry_run=False, max_pages=None)

        assert get_sync_state(conn)["backfill_complete"] == 1


# ---------------------------------------------------------------------------
# High-water mark (last_detection_timestamp) tracking
# ---------------------------------------------------------------------------

class TestHighWaterMark:
    """last_detection_timestamp must track the NEWEST detection ever seen,
    not drift backwards as pages go newest→oldest."""

    def test_hwm_set_from_first_page(self, tmp_path):
        conn = _make_db(tmp_path)

        with patch("birdheatmap.sync._gql", return_value=_page(
            [_detection_node(1, "2025-08-15T10:00:00-04:00")],
            cursor="c1",
            has_next=False,
        )):
            _run_backfill(conn, cursor=None, dry_run=False, max_pages=None)

        state = get_sync_state(conn)
        # 10:00 EDT = 14:00 UTC
        assert state["last_detection_timestamp"] == "2025-08-15T14:00:00+00:00"

    def test_hwm_not_overwritten_by_older_page(self, tmp_path):
        conn = _make_db(tmp_path)

        pages = [
            # Page 1 — newest
            _page([_detection_node(1, "2025-08-15T10:00:00-04:00")], cursor="c1", has_next=True),
            # Page 2 — older
            _page([_detection_node(2, "2025-07-01T08:00:00-04:00")], cursor="c2", has_next=False),
        ]
        page_iter = iter(pages)

        with patch("birdheatmap.sync._gql", side_effect=lambda q, v: next(page_iter)):
            _run_backfill(conn, cursor=None, dry_run=False, max_pages=None)

        state = get_sync_state(conn)
        # Must still reflect the newest detection (page 1), not the older one (page 2).
        assert state["last_detection_timestamp"] == "2025-08-15T14:00:00+00:00"

    def test_hwm_preserved_across_restart(self, tmp_path):
        conn = _make_db(tmp_path)

        # First run: page 1 sets HWM.
        with patch("birdheatmap.sync._gql", return_value=_page(
            [_detection_node(1, "2025-08-15T10:00:00-04:00")],
            cursor="c1",
            has_next=True,
        )):
            _run_backfill(conn, cursor=None, dry_run=False, max_pages=1)

        assert get_sync_state(conn)["last_detection_timestamp"] == "2025-08-15T14:00:00+00:00"

        # Restart (cursor=c1): page 2 has older detections — HWM must not regress.
        with patch("birdheatmap.sync._gql", return_value=_page(
            [_detection_node(2, "2025-07-01T08:00:00-04:00")],
            cursor="c2",
            has_next=False,
        )):
            _run_backfill(conn, cursor="c1", dry_run=False, max_pages=None)

        assert get_sync_state(conn)["last_detection_timestamp"] == "2025-08-15T14:00:00+00:00"


# ---------------------------------------------------------------------------
# Detection and species persistence
# ---------------------------------------------------------------------------

class TestDetectionPersistence:
    def test_detections_written_to_db(self, tmp_path):
        conn = _make_db(tmp_path)

        nodes = [
            _detection_node(101, "2025-06-01T07:00:00-04:00", species_id="55"),
            _detection_node(102, "2025-06-01T07:01:00-04:00", species_id="55"),
        ]

        with patch("birdheatmap.sync._gql", return_value=_page(nodes, cursor="c", has_next=False)):
            _run_backfill(conn, cursor=None, dry_run=False, max_pages=None)

        count = conn.execute("SELECT COUNT(*) FROM detection").fetchone()[0]
        assert count == 2

    def test_duplicate_detections_not_double_inserted(self, tmp_path):
        conn = _make_db(tmp_path)

        page = _page(
            [_detection_node(99, "2025-06-01T07:00:00-04:00")],
            cursor="c",
            has_next=False,
        )

        with patch("birdheatmap.sync._gql", return_value=page):
            _run_backfill(conn, cursor=None, dry_run=False, max_pages=None)

        with patch("birdheatmap.sync._gql", return_value=page):
            _run_backfill(conn, cursor=None, dry_run=False, max_pages=None)

        count = conn.execute("SELECT COUNT(*) FROM detection").fetchone()[0]
        assert count == 1

    def test_species_upserted(self, tmp_path):
        conn = _make_db(tmp_path)

        with patch("birdheatmap.sync._gql", return_value=_page(
            [_detection_node(1, "2025-06-01T07:00:00-04:00", species_id="42")],
            cursor="c",
            has_next=False,
        )):
            _run_backfill(conn, cursor=None, dry_run=False, max_pages=None)

        row = conn.execute("SELECT * FROM species WHERE id = 42").fetchone()
        assert row is not None
        assert row["common_name"] == "Test Warbler"
