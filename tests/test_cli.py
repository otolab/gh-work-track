from __future__ import annotations

import pytest

from gh_work_track import cli, github_ops
from gh_work_track.db import WorkTrackDB


def sync_runs(db_path):
    db = WorkTrackDB(str(db_path))
    db.init_tables()
    return db._get_table("sync_runs").search().to_pandas()


def raise_runtime_error(message):
    def fail(*args, **kwargs):
        raise RuntimeError(message)

    return fail


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


@pytest.mark.parametrize(
    ("failure_source", "error_fragment"),
    [
        ("notifications", "notifications"),
        ("search", "gh search"),
        ("timeline", "timeline"),
        ("comments", "comments"),
    ],
)
def test_partial_collection_failure_does_not_advance_watermark(
    monkeypatch, tmp_path, failure_source, error_fragment
):
    db_path = tmp_path / "lance"

    monkeypatch.setattr(cli, "collect_event_records", lambda since: ([], [], 0))
    monkeypatch.setattr(cli, "save_events", lambda events: (0, 0))
    assert cli.main(["--db", str(db_path), "sync", "--since", "1"]) == 0
    last_successful = WorkTrackDB(str(db_path)).last_successful_sync()
    assert last_successful is not None

    monkeypatch.setattr(cli, "collect_event_records", github_ops.collect_event_records)
    monkeypatch.setattr(github_ops, "fetch_notifications", lambda *args, **kwargs: [])
    monkeypatch.setattr(github_ops, "gh_cli_json", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        github_ops,
        "load_watch",
        lambda: [{"repo": "otolab/my-logs", "number": 1, "kind": "issue"}],
    )

    if failure_source == "notifications":
        monkeypatch.setattr(
            github_ops,
            "fetch_notifications",
            raise_runtime_error("notifications unavailable"),
        )
    elif failure_source == "search":
        monkeypatch.setattr(
            github_ops,
            "gh_cli_json",
            raise_runtime_error("search unavailable"),
        )
    elif failure_source == "timeline":
        monkeypatch.setattr(
            github_ops,
            "fetch_timeline",
            raise_runtime_error("timeline unavailable"),
        )
    else:
        monkeypatch.setattr(github_ops, "fetch_timeline", lambda *args, **kwargs: [])
        monkeypatch.setattr(
            github_ops,
            "fetch_all_comments",
            raise_runtime_error("comments unavailable"),
        )

    assert cli.main(["--db", str(db_path), "sync", "--since", "1"]) == 1

    rows = sync_runs(db_path)
    assert sorted(rows["status"].tolist()) == ["failed", "success"]
    failed = rows[rows["status"] == "failed"].iloc[0]
    assert error_fragment in failed["error"]
    assert WorkTrackDB(str(db_path)).last_successful_sync() == last_successful
