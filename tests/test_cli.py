from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

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
        lambda **kwargs: ([{"dedup_key": "event-1"}], ["warning"], 2),
    )
    monkeypatch.setattr(cli, "save_events", lambda events: (1, 1))

    assert cli.main(["--db", str(db_path), "sync", "--since", "1", "--json"]) == 0

    rows = sync_runs(db_path)
    assert len(rows) == 1
    assert rows.iloc[0]["status"] == "success"
    assert rows.iloc[0]["mode"] == "backfill"
    assert rows.iloc[0]["thread_count"] == 2
    assert rows.iloc[0]["event_count"] == 1


def test_sync_help_explains_incremental_and_backfill_modes(capsys):
    for command in ("sync", "collect"):
        with pytest.raises(SystemExit) as exc_info:
            cli.main([command, "--help"])

        assert exc_info.value.code == 0
        output = capsys.readouterr().out
        assert "バックフィル専用" in output
        assert "前回成功 sync 以降の incremental" in output


def test_sync_since_uses_backfill_without_reading_watermark(monkeypatch, tmp_path):
    db_path = tmp_path / "lance"
    collected = {}

    def fail_if_watermark_read(self):
        pytest.fail("backfill must not read the successful watermark")

    monkeypatch.setattr(WorkTrackDB, "last_successful_sync", fail_if_watermark_read)
    monkeypatch.setattr(
        cli,
        "collect_event_records",
        lambda **kwargs: (collected.update(kwargs) or ([], [], 0)),
    )
    monkeypatch.setattr(cli, "save_events", lambda events: (0, 0))

    assert cli.main(["--db", str(db_path), "sync", "--since", "2"]) == 0

    assert collected["cutoff"] is not None
    rows = sync_runs(db_path)
    assert rows.iloc[0]["mode"] == "backfill"
    assert rows.iloc[0]["since_days"] == 2


def test_sync_records_failure_without_advancing_watermark(monkeypatch, tmp_path):
    db_path = tmp_path / "lance"

    def fail_collect(**kwargs):
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

    monkeypatch.setattr(cli, "collect_event_records", lambda **kwargs: ([], [], 0))
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


def _comment(comment_id: int, timestamp: datetime, body: str) -> dict:
    return {
        "id": comment_id,
        "created_at": timestamp.isoformat().replace("+00:00", "Z"),
        "user": {"login": "alice"},
        "body": body,
    }


def _mock_event_sources(monkeypatch, comments: list[dict]) -> None:
    ref = github_ops.ThreadRef("otolab/my-logs", 2049)
    monkeypatch.setattr(github_ops, "fetch_notifications", lambda **kwargs: [])
    monkeypatch.setattr(
        github_ops,
        "fetch_search_threads",
        lambda cutoff_date: ([ref], []),
    )
    monkeypatch.setattr(
        github_ops,
        "fetch_timeline",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        github_ops,
        "fetch_all_comments",
        lambda ref_arg: list(comments),
    )
    monkeypatch.setattr(github_ops, "load_watch", lambda: [])


def _run_json_sync(db_path, capsys) -> dict:
    assert cli.main(["--db", str(db_path), "sync", "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_incremental_sync_same_fixture_is_idempotent(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "lance"
    comments = [_comment(1, datetime.now(timezone.utc) - timedelta(minutes=1), "first")]
    _mock_event_sources(monkeypatch, comments)

    first = _run_json_sync(db_path, capsys)
    watermark = WorkTrackDB(str(db_path)).last_successful_sync()
    assert watermark is not None
    second = _run_json_sync(db_path, capsys)

    assert first["mode"] == "incremental"
    assert first["cutoff"] == first["cutoff_at"]
    assert first["watermark"] is None
    assert first["bootstrap"] is True
    assert first["new_count"] == 1
    assert second["mode"] == "incremental"
    assert second["cutoff"] == second["cutoff_at"]
    assert second["watermark"] == github_ops.since_iso(watermark)
    assert second["bootstrap"] is False
    assert second["new_count"] == 0
    assert second["total_count"] == 1


def test_incremental_sync_text_includes_sync_metadata(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "lance"
    monkeypatch.setattr(
        cli,
        "collect_event_records",
        lambda **kwargs: ([], [], 0),
    )
    monkeypatch.setattr(cli, "save_events", lambda events: (0, 0))

    assert cli.main(["--db", str(db_path), "sync"]) == 0

    output = capsys.readouterr().out
    assert "mode: incremental" in output
    assert "cutoff: " in output
    assert "watermark: null" in output
    assert "bootstrap: true" in output


def test_backfill_output_has_null_watermark_and_no_bootstrap(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "lance"
    monkeypatch.setattr(
        cli,
        "collect_event_records",
        lambda **kwargs: ([], [], 0),
    )
    monkeypatch.setattr(cli, "save_events", lambda events: (0, 0))

    assert cli.main(["--db", str(db_path), "sync", "--since", "2", "--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "backfill"
    assert output["cutoff"] == output["cutoff_at"]
    assert output["watermark"] is None
    assert output["bootstrap"] is False


def test_incremental_sync_saves_only_new_fixture_event(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "lance"
    comments = [_comment(1, datetime.now(timezone.utc) - timedelta(minutes=1), "first")]
    _mock_event_sources(monkeypatch, comments)

    _run_json_sync(db_path, capsys)
    comments.append(_comment(2, datetime.now(timezone.utc), "second"))
    second = _run_json_sync(db_path, capsys)

    assert second["event_count"] == 2
    assert second["new_count"] == 1
    assert second["total_count"] == 2


def test_failed_incremental_sync_keeps_watermark_for_recovery(
    monkeypatch, tmp_path, capsys
):
    db_path = tmp_path / "lance"
    comments = [_comment(1, datetime.now(timezone.utc) - timedelta(minutes=1), "first")]
    _mock_event_sources(monkeypatch, comments)

    _run_json_sync(db_path, capsys)
    database = WorkTrackDB(str(db_path))
    watermark_before_failure = database.last_successful_sync()
    assert watermark_before_failure is not None

    def fail_comments(ref):
        raise RuntimeError("comments unavailable")

    monkeypatch.setattr(github_ops, "fetch_all_comments", fail_comments)
    assert cli.main(["--db", str(db_path), "sync"]) == 1
    capsys.readouterr()
    assert WorkTrackDB(str(db_path)).last_successful_sync() == watermark_before_failure

    comments.append(_comment(2, datetime.now(timezone.utc), "recovered"))
    monkeypatch.setattr(
        github_ops,
        "fetch_all_comments",
        lambda ref_arg: list(comments),
    )
    recovered = _run_json_sync(db_path, capsys)

    assert recovered["new_count"] == 1
    assert recovered["total_count"] == 2
    rows = sync_runs(db_path)
    assert sorted(rows["status"].tolist()) == ["failed", "success", "success"]
