from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from gh_work_track import cli, github_ops
from gh_work_track.db import WorkTrackDB
from gh_work_track.session import Session


@pytest.fixture(autouse=True)
def disable_user_event_discovery(monkeypatch):
    monkeypatch.setattr(github_ops, "fetch_user_event_threads", lambda cutoff: ([], []))


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


def test_sync_passes_cli_configuration_overrides(monkeypatch, tmp_path):
    db_path = tmp_path / "lance"
    cutoff_args = {}
    collect_args = {}

    def fake_resolve_sync_cutoff(**kwargs):
        cutoff_args.update(kwargs)
        return datetime(2026, 9, 17, 10, tzinfo=timezone.utc), "incremental"

    monkeypatch.setattr(cli, "resolve_sync_cutoff", fake_resolve_sync_cutoff)
    monkeypatch.setattr(
        cli,
        "collect_event_records",
        lambda **kwargs: collect_args.update(kwargs) or ([], [], 0),
    )
    monkeypatch.setattr(cli, "save_events", lambda events: (0, 0))

    assert cli.main([
        "--db", str(db_path),
        "sync",
        "--mine-repo", "cli/repo",
        "--search-org", "cli-org",
        "--bootstrap-days", "11",
        "--overlap-minutes", "1",
    ]) == 0

    assert cutoff_args["bootstrap_days"] == 11
    assert cutoff_args["overlap_minutes"] == 1
    assert collect_args["mine_repos"] == ["cli/repo"]
    assert collect_args["search_orgs"] == ["cli-org"]


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
    monkeypatch.setattr(github_ops, "SEARCH_INTERVAL_SECONDS", 0.0)

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
        monkeypatch.setattr(
            github_ops,
            "fetch_timeline",
            lambda *args, **kwargs: [
                {
                    "id": 1,
                    "event": "commented",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "user": {"login": "alice"},
                    "body": "recent activity",
                }
            ],
        )
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


def test_events_failure_is_warning_only_and_advances_watermark(
    monkeypatch, tmp_path, capsys
):
    db_path = tmp_path / "lance"
    ref = github_ops.ThreadRef("otolab/events", 7, "issue")
    monkeypatch.setattr(github_ops, "fetch_notifications", lambda **kwargs: [])
    monkeypatch.setattr(
        github_ops,
        "fetch_search_threads",
        lambda cutoff_date, **kwargs: ([ref], []),
    )
    monkeypatch.setattr(
        github_ops,
        "fetch_user_event_threads",
        raise_runtime_error("events unavailable"),
    )
    monkeypatch.setattr(github_ops, "load_watch", lambda: [])
    monkeypatch.setattr(github_ops, "fetch_timeline", lambda *args, **kwargs: [])
    monkeypatch.setattr(github_ops, "fetch_all_comments", lambda ref_arg: [])

    assert cli.main(["--db", str(db_path), "sync", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["warnings"] == ["events: events unavailable"]
    assert WorkTrackDB(str(db_path)).last_successful_sync() is not None
    assert sync_runs(db_path)["status"].tolist() == ["success"]


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
        lambda cutoff_date, **kwargs: ([ref], []),
    )
    monkeypatch.setattr(
        github_ops,
        "fetch_timeline",
        lambda *args, **kwargs: [
            {
                "id": comment["id"],
                "event": "commented",
                "created_at": comment["created_at"],
                "user": comment["user"],
                "body": comment["body"],
            }
            for comment in comments
        ],
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


def test_event_save_failure_does_not_advance_thread_watermark(
    monkeypatch, tmp_path, capsys
):
    db_path = tmp_path / "lance"
    comments = [_comment(1, datetime.now(timezone.utc), "first")]
    _mock_event_sources(monkeypatch, comments)
    monkeypatch.setattr(cli, "save_events", raise_runtime_error("save unavailable"))

    assert cli.main(["--db", str(db_path), "sync"]) == 1
    capsys.readouterr()

    database = WorkTrackDB(str(db_path))
    assert database.last_successful_sync() is None
    assert database.get_thread("otolab/my-logs", 2049) is None


def test_thread_watermark_failure_rolls_back_multiple_threads(
    monkeypatch, tmp_path, capsys
):
    db_path = tmp_path / "lance"
    refs = [
        github_ops.ThreadRef("otolab/my-logs", 2049),
        github_ops.ThreadRef("otolab/my-logs", 2050),
    ]
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    monkeypatch.setattr(github_ops, "fetch_notifications", lambda **kwargs: [])
    monkeypatch.setattr(
        github_ops,
        "fetch_search_threads",
        lambda cutoff_date, **kwargs: (refs, []),
    )
    monkeypatch.setattr(
        github_ops,
        "fetch_timeline",
        lambda ref, *args, **kwargs: [
            {
                "id": ref.number,
                "event": "commented",
                "created_at": timestamp,
                "user": {"login": "alice"},
                "body": "recent activity",
            }
        ],
    )
    monkeypatch.setattr(github_ops, "fetch_all_comments", lambda ref: [])
    monkeypatch.setattr(github_ops, "load_watch", lambda: [])

    original_mark = Session.mark_thread_synced
    mark_calls = 0

    def fail_on_second(
        session, repo, number, *, kind="issue", synced_at=None
    ):
        nonlocal mark_calls
        mark_calls += 1
        if mark_calls == 2:
            raise RuntimeError("second thread watermark unavailable")
        return original_mark(
            session,
            repo,
            number,
            kind=kind,
            synced_at=synced_at,
        )

    monkeypatch.setattr(Session, "mark_thread_synced", fail_on_second)

    assert cli.main(["--db", str(db_path), "sync"]) == 1
    capsys.readouterr()

    database = WorkTrackDB(str(db_path))
    assert mark_calls == 2
    assert database.last_successful_sync() is None
    assert all(database.get_thread(ref.repo, ref.number) is None for ref in refs)
    assert sync_runs(db_path)["status"].tolist() == ["failed"]


def test_success_run_failure_restores_thread_watermark(
    monkeypatch, tmp_path, capsys
):
    db_path = tmp_path / "lance"
    ref = github_ops.ThreadRef("otolab/my-logs", 2049)
    old_sync = "2026-09-16T12:00:00Z"
    database = WorkTrackDB(str(db_path))
    database.init_tables()
    database.upsert_thread(
        ref.repo,
        ref.number,
        title="preserve",
        last_synced_at=old_sync,
    )
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    monkeypatch.setattr(github_ops, "fetch_notifications", lambda **kwargs: [])
    monkeypatch.setattr(
        github_ops,
        "fetch_search_threads",
        lambda cutoff_date, **kwargs: ([ref], []),
    )
    monkeypatch.setattr(
        github_ops,
        "fetch_timeline",
        lambda *args, **kwargs: [
            {
                "id": 1,
                "event": "commented",
                "created_at": timestamp,
                "user": {"login": "alice"},
                "body": "recent activity",
            }
        ],
    )
    monkeypatch.setattr(github_ops, "fetch_all_comments", lambda ref_arg: [])
    monkeypatch.setattr(github_ops, "load_watch", lambda: [])

    original_update = WorkTrackDB.update_sync_run

    def fail_success_update(self, run_id, **kwargs):
        if kwargs.get("status") == "success":
            raise RuntimeError("success record unavailable")
        return original_update(self, run_id, **kwargs)

    monkeypatch.setattr(WorkTrackDB, "update_sync_run", fail_success_update)

    assert cli.main(["--db", str(db_path), "sync"]) == 1
    capsys.readouterr()

    restored = WorkTrackDB(str(db_path)).get_thread(ref.repo, ref.number)
    assert restored is not None
    assert restored["title"] == "preserve"
    assert restored["last_synced_at"] == old_sync
    assert WorkTrackDB(str(db_path)).last_successful_sync() is None
    assert sync_runs(db_path)["status"].tolist() == ["failed"]


def test_incremental_sync_same_fixture_is_idempotent(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "lance"
    comments = [_comment(1, datetime.now(timezone.utc) - timedelta(minutes=1), "first")]
    _mock_event_sources(monkeypatch, comments)

    first = _run_json_sync(db_path, capsys)
    database = WorkTrackDB(str(db_path))
    watermark = database.last_successful_sync()
    thread = database.get_thread("otolab/my-logs", 2049)
    assert watermark is not None
    assert thread is not None
    assert thread["last_synced_at"]
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

    assert second["event_count"] == 1
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
    thread_before_failure = database.get_thread("otolab/my-logs", 2049)
    assert watermark_before_failure is not None
    assert thread_before_failure is not None

    def fail_comments(ref):
        raise RuntimeError("comments unavailable")

    monkeypatch.setattr(
        github_ops,
        "fetch_timeline",
        lambda *args, **kwargs: [
            {
                "id": 1,
                "event": "commented",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "user": {"login": "alice"},
                "body": "recent activity",
            }
        ],
    )
    monkeypatch.setattr(github_ops, "fetch_all_comments", fail_comments)
    assert cli.main(["--db", str(db_path), "sync"]) == 1
    capsys.readouterr()
    assert WorkTrackDB(str(db_path)).last_successful_sync() == watermark_before_failure
    assert (
        WorkTrackDB(str(db_path)).get_thread("otolab/my-logs", 2049)["last_synced_at"]
        == thread_before_failure["last_synced_at"]
    )

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


def test_incremental_sync_uses_previous_run_start_for_long_run_overlap(
    monkeypatch, tmp_path, capsys
):
    db_path = tmp_path / "lance"
    ref = github_ops.ThreadRef("otolab/my-logs", 2049)
    previous_started = datetime(2026, 9, 17, 11, 50, tzinfo=timezone.utc)
    previous_finished = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    current_started = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    current_finished = datetime(2026, 9, 17, 12, 10, tzinfo=timezone.utc)
    next_started = datetime(2026, 9, 17, 12, 20, tzinfo=timezone.utc)
    next_finished = datetime(2026, 9, 17, 12, 21, tzinfo=timezone.utc)
    late_at = datetime(2026, 9, 17, 12, 1, tzinfo=timezone.utc)
    old = _comment(
        1,
        datetime(2026, 9, 17, 11, 59, tzinfo=timezone.utc),
        "old event",
    )
    late = _comment(2, late_at, "created during collection")

    database = WorkTrackDB(str(db_path))
    database.init_tables()
    database.upsert_thread(
        ref.repo,
        ref.number,
        last_synced_at=previous_started.isoformat().replace("+00:00", "Z"),
    )
    database.record_sync_run(
        since_days=None,
        thread_count=1,
        event_count=0,
        new_count=0,
        warnings=[],
        status="success",
        mode="incremental",
        started_at=previous_started,
        finished_at=previous_finished,
    )

    class CliClock(datetime):
        values = [current_started, current_finished, next_started, next_finished]
        calls = []

        @classmethod
        def now(cls, tz=None):
            cls.calls.append(len(cls.calls))
            value = cls.values.pop(0)
            return value.astimezone(tz) if tz is not None else value

    class CollectorClock(datetime):
        values = [current_started, current_started, next_started, next_started]
        calls = []

        @classmethod
        def now(cls, tz=None):
            cls.calls.append(len(cls.calls))
            value = cls.values.pop(0)
            return value.astimezone(tz) if tz is not None else value

    monkeypatch.setattr(cli, "datetime", CliClock)
    monkeypatch.setattr(github_ops, "datetime", CollectorClock)
    monkeypatch.setattr(github_ops, "fetch_notifications", lambda **kwargs: [])
    monkeypatch.setattr(
        github_ops,
        "fetch_search_threads",
        lambda cutoff_date, **kwargs: ([ref], []),
    )
    timeline_calls = 0
    effective_cutoffs = []

    def fetch_timeline(ref_arg, *args, **kwargs):
        nonlocal timeline_calls
        timeline_calls += 1
        effective_cutoffs.append(kwargs["cutoff"])
        return [
            {
                "id": event["id"],
                "event": "commented",
                "created_at": event["created_at"],
                "user": event["user"],
                "body": event["body"],
            }
            for event in ([old] if timeline_calls == 1 else [old, late])
        ]

    monkeypatch.setattr(github_ops, "fetch_timeline", fetch_timeline)
    monkeypatch.setattr(github_ops, "fetch_all_comments", lambda ref_arg: [])
    monkeypatch.setattr(github_ops, "load_watch", lambda: [])

    first = _run_json_sync(db_path, capsys)
    second = _run_json_sync(db_path, capsys)

    assert first["cutoff"] == "2026-09-17T11:45:00Z"
    assert first["watermark"] == "2026-09-17T12:00:00Z"
    assert second["cutoff"] == "2026-09-17T11:55:00Z"
    assert second["watermark"] == "2026-09-17T12:10:00Z"
    assert second["event_count"] == 1
    assert second["new_count"] == 1
    assert effective_cutoffs == [previous_started, current_started]
    assert len(CliClock.calls) == 4
    assert len(CollectorClock.calls) == 4
    final_database = WorkTrackDB(str(db_path))
    assert final_database.count_events() == 2
    stored = final_database.events_for_date("2026-09-17")
    assert [event["timestamp"] for event in stored] == [
        old["created_at"],
        late["created_at"],
    ]


def test_incremental_sync_uses_thread_floor_and_skips_comments_when_timeline_is_old(
    monkeypatch, tmp_path, capsys
):
    db_path = tmp_path / "lance"
    ref = github_ops.ThreadRef("otolab/my-logs", 2049)
    now = datetime.now(timezone.utc)
    database = WorkTrackDB(str(db_path))
    database.init_tables()
    database.upsert_thread(
        ref.repo,
        ref.number,
        last_synced_at=now.isoformat().replace("+00:00", "Z"),
    )
    calls = {"timeline": 0, "comments": 0}
    old = _comment(1, now - timedelta(hours=1), "old")

    monkeypatch.setattr(github_ops, "fetch_notifications", lambda **kwargs: [])
    monkeypatch.setattr(
        github_ops,
        "fetch_search_threads",
        lambda cutoff_date, **kwargs: ([ref], []),
    )

    def fetch_old_timeline(*args, **kwargs):
        calls["timeline"] += 1
        return [
            {
                "id": old["id"],
                "event": "commented",
                "created_at": old["created_at"],
                "user": old["user"],
                "body": old["body"],
            }
        ]

    def fetch_comments(ref_arg):
        calls["comments"] += 1
        return [old]

    monkeypatch.setattr(github_ops, "fetch_timeline", fetch_old_timeline)
    monkeypatch.setattr(github_ops, "fetch_all_comments", fetch_comments)
    monkeypatch.setattr(github_ops, "load_watch", lambda: [])

    result = _run_json_sync(db_path, capsys)

    assert result["event_count"] == 0
    assert result["new_count"] == 0
    assert calls == {"timeline": 1, "comments": 0}
    assert sum(calls.values()) < 2


def test_comments_are_fetched_when_timeline_has_recent_activity(
    monkeypatch, tmp_path, capsys
):
    db_path = tmp_path / "lance"
    ref = github_ops.ThreadRef("otolab/my-logs", 2049)
    recent = _comment(1, datetime.now(timezone.utc), "recent")
    calls = {"comments": 0}

    monkeypatch.setattr(github_ops, "fetch_notifications", lambda **kwargs: [])
    monkeypatch.setattr(
        github_ops,
        "fetch_search_threads",
        lambda cutoff_date, **kwargs: ([ref], []),
    )
    monkeypatch.setattr(
        github_ops,
        "fetch_timeline",
        lambda *args, **kwargs: [
            {
                "id": recent["id"],
                "event": "commented",
                "created_at": recent["created_at"],
                "user": recent["user"],
                "body": recent["body"],
            }
        ],
    )

    def fetch_comments(ref_arg):
        calls["comments"] += 1
        return [recent]

    monkeypatch.setattr(github_ops, "fetch_all_comments", fetch_comments)
    monkeypatch.setattr(github_ops, "load_watch", lambda: [])

    result = _run_json_sync(db_path, capsys)

    assert result["event_count"] == 1
    assert calls["comments"] == 1
