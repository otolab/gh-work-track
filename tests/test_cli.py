from __future__ import annotations

from gh_work_track import cli
from gh_work_track.db import WorkTrackDB


def sync_runs(db_path):
    db = WorkTrackDB(str(db_path))
    db.init_tables()
    return db._get_table("sync_runs").search().to_pandas()


def test_sync_records_success(monkeypatch, tmp_path):
    db_path = tmp_path / "lance"
    monkeypatch.setattr(
        cli,
        "collect_event_records",
        lambda since: ([{"dedup_key": "event-1"}], ["warning"], 2),
    )
    monkeypatch.setattr(cli, "save_events", lambda events: (1, 1))

    assert cli.main(["--db", str(db_path), "sync", "--since", "1", "--json"]) == 0

    rows = sync_runs(db_path)
    assert len(rows) == 1
    assert rows.iloc[0]["status"] == "success"
    assert rows.iloc[0]["mode"] == "backfill"
    assert rows.iloc[0]["thread_count"] == 2
    assert rows.iloc[0]["event_count"] == 1


def test_sync_records_failure_without_advancing_watermark(monkeypatch, tmp_path):
    db_path = tmp_path / "lance"

    def fail_collect(since):
        raise RuntimeError("GitHub API unavailable")

    monkeypatch.setattr(cli, "collect_event_records", fail_collect)

    assert cli.main(["--db", str(db_path), "sync", "--since", "1"]) == 1

    rows = sync_runs(db_path)
    assert len(rows) == 1
    assert rows.iloc[0]["status"] == "failed"
    assert rows.iloc[0]["error"] == "GitHub API unavailable"
    assert WorkTrackDB(str(db_path)).last_successful_sync() is None
