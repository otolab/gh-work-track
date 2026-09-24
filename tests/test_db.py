import json
from datetime import date, datetime, timedelta, timezone

import pytest

from gh_work_track.db import WorkTrackDB, thread_key


@pytest.fixture
def db(tmp_path):
    database = WorkTrackDB(str(tmp_path / "lance"))
    database.init_tables()
    return database


def sample_event(key: str, day: str = "2026-09-15") -> dict:
    return {
        "dedup_key": key,
        "date": day,
        "timestamp": f"{day}T12:00:00Z",
        "repo": "otolab/my-logs",
        "number": 2049,
        "kind": "issue",
        "event": "commented",
        "actor": "alice",
        "snippet": "progress",
        "url": "https://github.com/otolab/my-logs/issues/2049",
        "source": "comments",
    }


def test_init_and_stats(db: WorkTrackDB):
    stats = db.stats()
    assert stats["events"] == 0
    assert stats["threads"] == 0
    assert stats["sync_runs"] == 0
    assert stats["thread_links"] == 0


def test_thread_links_upsert_is_idempotent_and_updates_observation(db: WorkTrackDB):
    first = {
        "from_thread_key": "owner/repo#10",
        "to_thread_key": "owner/repo#11",
        "rel": "cross_ref",
        "source": "timeline",
        "confidence": 0.8,
        "discovered_at": "2026-09-17T10:00:00Z",
    }
    newer = {**first, "confidence": 1.0, "discovered_at": "2026-09-17T11:00:00Z"}

    assert db.upsert_thread_links([first]) == (1, 1)
    assert db.upsert_thread_links([newer]) == (0, 1)
    rows = db.thread_links(thread_key_value="owner/repo#10")
    assert len(rows) == 1
    assert rows[0]["confidence"] == 1.0
    assert str(rows[0]["discovered_at"]).startswith("2026-09-17 11:00:00")


def test_upsert_events_is_idempotent(db: WorkTrackDB):
    event = sample_event("event-1")
    new_count, total = db.upsert_events([event])
    assert new_count == 1
    assert total == 1
    new_count, total = db.upsert_events([event])
    assert new_count == 0
    assert total == 1


def test_events_for_date_and_between(db: WorkTrackDB):
    db.upsert_events([sample_event("a", "2026-09-14"), sample_event("b", "2026-09-15")])
    assert len(db.events_for_date("2026-09-15")) == 1
    end = date(2026, 9, 15)
    start = end - timedelta(days=1)
    rows = db.events_between(start.isoformat(), end.isoformat())
    assert len(rows) == 2


def test_thread_upsert_and_watch_filter(db: WorkTrackDB):
    db.upsert_thread("otolab/my-logs", 2049, watch_note="work", is_watched=True)
    db.upsert_thread("otolab/my-logs", 1, watch_note="", is_watched=False)
    watched = db.list_watched_threads()
    assert len(watched) == 1
    assert watched[0]["thread_key"] == thread_key("otolab/my-logs", 2049)


def test_record_sync_run(db: WorkTrackDB):
    run_id = db.record_sync_run(
        since_days=1,
        thread_count=3,
        event_count=10,
        new_count=4,
        warnings=["demo"],
        status="success",
        mode="backfill",
    )
    assert run_id
    assert db.count_sync_runs() == 1


def test_record_sync_run_saves_status_fields(db: WorkTrackDB):
    cutoff_at = datetime(2026, 9, 15, 11, 30, tzinfo=timezone.utc)
    run_id = db.record_sync_run(
        since_days=0,
        thread_count=1,
        event_count=2,
        new_count=2,
        warnings=[],
        status="failed",
        mode="incremental",
        cutoff_at=cutoff_at,
        error="GitHub API unavailable",
    )

    row = db._get_table("sync_runs").search().where(f"run_id = '{run_id}'").to_list()[0]
    assert row["status"] == "failed"
    assert row["mode"] == "incremental"
    assert row["error"] == "GitHub API unavailable"
    assert row["cutoff_at"] == cutoff_at.replace(microsecond=0, tzinfo=None)


def test_last_successful_sync_ignores_failed_runs(db: WorkTrackDB):
    earlier = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)
    later_failed = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    db.record_sync_run(
        since_days=1,
        thread_count=1,
        event_count=1,
        new_count=1,
        warnings=[],
        status="success",
        finished_at=earlier,
    )
    db.record_sync_run(
        since_days=1,
        thread_count=1,
        event_count=1,
        new_count=0,
        warnings=[],
        status="failed",
        error="network error",
        finished_at=later_failed,
    )

    assert db.last_successful_sync() == earlier


def test_last_successful_sync_started_at_uses_latest_successful_run(
    db: WorkTrackDB,
):
    first_started = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)
    first_finished = datetime(2026, 9, 17, 10, 10, tzinfo=timezone.utc)
    latest_started = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    latest_finished = datetime(2026, 9, 17, 12, 10, tzinfo=timezone.utc)
    db.record_sync_run(
        since_days=None,
        thread_count=1,
        event_count=1,
        new_count=1,
        warnings=[],
        status="success",
        mode="incremental",
        started_at=first_started,
        finished_at=first_finished,
    )
    db.record_sync_run(
        since_days=None,
        thread_count=1,
        event_count=1,
        new_count=0,
        warnings=[],
        status="failed",
        mode="incremental",
        started_at=datetime(2026, 9, 17, 11, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 9, 17, 11, 10, tzinfo=timezone.utc),
    )
    db.record_sync_run(
        since_days=None,
        thread_count=1,
        event_count=2,
        new_count=1,
        warnings=[],
        status="success",
        mode="incremental",
        started_at=latest_started,
        finished_at=latest_finished,
    )

    assert db.last_successful_sync() == latest_finished
    assert db.last_successful_sync_started_at() == latest_started
