from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gh_work_track import github_ops
from gh_work_track.config import CONFIG_ENV
from gh_work_track.db import WorkTrackDB
from gh_work_track.session import open_session


def _timeline_item(hour: int, item_id: int) -> dict:
    return {
        "id": item_id,
        "event": "commented",
        "created_at": f"2026-09-17T{hour:02d}:00:00Z",
        "user": {"login": "alice"},
        "body": f"event {item_id}",
    }


def _timeline_item_at(timestamp: datetime, item_id: int) -> dict:
    return {
        "id": item_id,
        "event": "commented",
        "created_at": timestamp.isoformat().replace("+00:00", "Z"),
        "user": {"login": "alice"},
        "body": f"event {item_id}",
    }


def test_fetch_timeline_stops_after_old_page_when_order_is_newest_first(monkeypatch):
    ref = github_ops.ThreadRef("otolab/my-logs", 2049)
    pages = {
        1: [_timeline_item(12, 1), _timeline_item(11, 2)],
        2: [_timeline_item(9, 3), _timeline_item(8, 4)],
        3: [_timeline_item(7, 5)],
    }
    calls: list[str] = []

    def fake_gh_api(path: str, *, paginate: bool = False):
        calls.append(path)
        page = int(path.rsplit("page=", 1)[1])
        assert paginate is False
        return pages[page]

    monkeypatch.setattr(github_ops, "gh_api", fake_gh_api)

    result = github_ops.fetch_timeline(
        ref,
        per_page=2,
        paginate=True,
        cutoff=datetime(2026, 9, 17, 10, tzinfo=timezone.utc),
    )

    assert [path.rsplit("page=", 1)[1] for path in calls] == ["1", "2"]
    assert [item["id"] for item in result] == [1, 2, 3, 4]


def test_fetch_timeline_does_not_early_stop_for_oldest_first_api(monkeypatch):
    ref = github_ops.ThreadRef("otolab/my-logs", 2049)
    pages = {
        1: [_timeline_item(8, 1), _timeline_item(9, 2)],
        2: [_timeline_item(11, 3), _timeline_item(12, 4)],
        3: [_timeline_item(13, 5)],
    }
    calls: list[str] = []

    def fake_gh_api(path: str, *, paginate: bool = False):
        calls.append(path)
        page = int(path.rsplit("page=", 1)[1])
        assert paginate is False
        return pages[page]

    monkeypatch.setattr(github_ops, "gh_api", fake_gh_api)

    result = github_ops.fetch_timeline(
        ref,
        per_page=2,
        paginate=True,
        cutoff=datetime(2026, 9, 17, 10, tzinfo=timezone.utc),
    )

    assert [path.rsplit("page=", 1)[1] for path in calls] == ["1", "2", "3"]
    assert [item["id"] for item in result] == [1, 2, 3, 4, 5]


def test_fetch_timeline_does_not_stop_when_page_has_unknown_timestamp(monkeypatch):
    ref = github_ops.ThreadRef("otolab/my-logs", 2049)
    pages = {
        1: [_timeline_item(12, 1), _timeline_item(11, 2)],
        2: [
            {"id": 3, "event": "committed", "commit_id": "abc"},
            _timeline_item(8, 4),
        ],
        3: [_timeline_item(7, 5), _timeline_item(6, 6)],
        4: [_timeline_item(5, 7)],
    }
    calls: list[str] = []

    def fake_gh_api(path: str, *, paginate: bool = False):
        calls.append(path)
        page = int(path.rsplit("page=", 1)[1])
        assert paginate is False
        return pages[page]

    monkeypatch.setattr(github_ops, "gh_api", fake_gh_api)

    github_ops.fetch_timeline(
        ref,
        per_page=2,
        paginate=True,
        cutoff=datetime(2026, 9, 17, 10, tzinfo=timezone.utc),
    )

    assert [path.rsplit("page=", 1)[1] for path in calls] == ["1", "2", "3", "4"]


def test_unknown_commented_activity_keeps_comments_fallback(monkeypatch, tmp_path):
    ref = github_ops.ThreadRef("otolab/my-logs", 2049)
    session = open_session(str(tmp_path / "lance"))
    now = datetime.now(timezone.utc)
    recent_comment = _timeline_item_at(now, 1)
    comments_calls = 0

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
                "created_at": "not-a-timestamp",
                "user": {"login": "alice"},
                "body": "unreadable timeline activity",
            }
        ],
    )

    def fetch_comments(ref_arg):
        nonlocal comments_calls
        comments_calls += 1
        return [recent_comment]

    monkeypatch.setattr(github_ops, "fetch_all_comments", fetch_comments)
    monkeypatch.setattr(github_ops, "load_watch", lambda: [])

    events, warnings, thread_count = github_ops.collect_event_records(
        cutoff=now - timedelta(days=1)
    )

    assert warnings == []
    assert thread_count == 1
    assert comments_calls == 1
    assert len(events) == 1
    assert events[0]["dedup_key"].endswith("|1")
    assert session.db.get_thread(ref.repo, ref.number) is None


def test_thread_start_boundary_keeps_events_created_during_collection(
    monkeypatch, tmp_path
):
    ref = github_ops.ThreadRef("otolab/my-logs", 2049)
    session = open_session(str(tmp_path / "lance"))
    global_cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    initial = _timeline_item_at(datetime.now(timezone.utc) - timedelta(hours=1), 1)
    late_events: list[dict] = []
    fetch_calls = 0

    monkeypatch.setattr(github_ops, "fetch_notifications", lambda **kwargs: [])
    monkeypatch.setattr(
        github_ops,
        "fetch_search_threads",
        lambda cutoff_date, **kwargs: ([ref], []),
    )

    def fetch_timeline(*args, **kwargs):
        nonlocal fetch_calls
        fetch_calls += 1
        if fetch_calls == 1:
            late_events.append(_timeline_item_at(datetime.now(timezone.utc), 2))
            return [initial]
        return [initial, *late_events]

    monkeypatch.setattr(github_ops, "fetch_timeline", fetch_timeline)
    monkeypatch.setattr(github_ops, "fetch_all_comments", lambda ref_arg: [])
    monkeypatch.setattr(github_ops, "load_watch", lambda: [])

    boundaries = []
    first_events, _, _ = github_ops.collect_event_records(
        cutoff=global_cutoff,
        synced_threads=boundaries,
    )
    assert len(boundaries) == 1
    assert late_events
    session.save_events(first_events)
    session.mark_threads_synced(boundaries)

    thread = session.db.get_thread(ref.repo, ref.number)
    assert thread is not None
    thread_started_at = github_ops.parse_timestamp(thread["last_synced_at"])
    late_at = github_ops.parse_timestamp(late_events[0]["created_at"])
    assert thread_started_at is not None
    assert late_at is not None
    assert thread_started_at < late_at

    second_events, _, _ = github_ops.collect_event_records(cutoff=global_cutoff)

    assert fetch_calls == 2
    assert [event["timestamp"] for event in second_events] == [late_events[0]["created_at"]]


def test_effective_thread_cutoff_uses_global_thread_and_db_floors(tmp_path):
    db_path = tmp_path / "lance"
    database = WorkTrackDB(str(db_path))
    database.init_tables()
    ref = github_ops.ThreadRef("otolab/my-logs", 2049)
    database.upsert_thread(
        ref.repo,
        ref.number,
        last_synced_at="2026-09-17T11:00:00Z",
    )
    database.upsert_events(
        [
            {
                "dedup_key": "event-1",
                "date": "2026-09-17",
                "timestamp": "2026-09-17T12:00:00Z",
                "repo": ref.repo,
                "number": ref.number,
                "kind": ref.kind,
                "event": "commented",
                "actor": "alice",
                "snippet": "stored",
                "url": ref.web_url,
                "source": "comments",
            }
        ]
    )
    open_session(str(db_path))

    assert github_ops.effective_thread_cutoff(
        ref,
        datetime(2026, 9, 17, 10, tzinfo=timezone.utc),
    ) == datetime(2026, 9, 17, 12, tzinfo=timezone.utc)


def test_parse_ref_uses_configured_default_repo(monkeypatch, tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("default_repo: example/main\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_ENV, str(config_file))

    assert github_ops.parse_ref("123").repo == "example/main"
    assert github_ops.parse_ref("repo#123").repo == "example/repo"


def test_search_discovery_uses_configured_mine_repos(monkeypatch, tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "mine_repos:\n  - example/one\n  - example/two\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(CONFIG_ENV, str(config_file))
    calls: list[list[str]] = []
    monkeypatch.setattr(
        github_ops,
        "gh_cli_json",
        lambda args: calls.append(args) or [],
    )

    refs, warnings = github_ops.fetch_search_threads("2026-09-17")

    assert refs == []
    assert len(warnings) == 21
    assert all("0 results" in warning for warning in warnings)
    assert len(calls) == 21
    assert len([args for args in calls if "--repo" not in args]) == 7
    assert {
        args[args.index("--repo") + 1]
        for args in calls
        if "--repo" in args
    } == {"example/one", "example/two"}


def test_global_search_unions_threads_from_multiple_repositories(monkeypatch, tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("mine_repos: []\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_ENV, str(config_file))
    calls: list[list[str]] = []

    def fake_search(args):
        calls.append(args)
        if args[1] == "issues":
            return [
                {
                    "number": 12,
                    "repository": {"nameWithOwner": "otolab/gh-work-track"},
                    "url": "https://github.com/otolab/gh-work-track/issues/12",
                },
                {
                    "number": 42,
                    "repository": {"nameWithOwner": "plaidev/karte-io-deploy"},
                    "url": "https://github.com/plaidev/karte-io-deploy/issues/42",
                },
            ]
        return [
            {
                "number": 13,
                "repository": {"nameWithOwner": "otolab/gh-work-track"},
                "url": "https://github.com/otolab/gh-work-track/pull/13",
            }
        ]

    monkeypatch.setattr(github_ops, "gh_cli_json", fake_search)

    refs, warnings = github_ops.fetch_search_threads("2026-09-17")

    assert {ref.key for ref in refs} == {
        "otolab/gh-work-track#12",
        "otolab/gh-work-track#13",
        "plaidev/karte-io-deploy#42",
    }
    assert {ref.kind for ref in refs} == {"issue", "pr"}
    assert len(calls) == 7
    assert all("--repo" not in args for args in calls)
    assert all(not any(arg.startswith("org:") for arg in args) for args in calls)
    qualifiers = ("--author", "--assignee", "--reviewed-by", "--commenter")
    assert {
        (args[1], next(arg for arg in args if arg in qualifiers))
        for args in calls
    } == {
        ("issues", "--author"),
        ("issues", "--assignee"),
        ("issues", "--commenter"),
        ("prs", "--author"),
        ("prs", "--assignee"),
        ("prs", "--reviewed-by"),
        ("prs", "--commenter"),
    }
    assert len(warnings) == 7


def test_global_search_applies_configured_organization_qualifier(monkeypatch, tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "mine_repos: []\nsearch_orgs:\n  - plaidev\n  - otolab\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(CONFIG_ENV, str(config_file))
    calls: list[list[str]] = []
    monkeypatch.setattr(
        github_ops,
        "gh_cli_json",
        lambda args: calls.append(args) or [],
    )

    github_ops.fetch_search_threads("2026-09-17")

    assert len(calls) == 14
    assert {args[2] for args in calls} == {"org:plaidev", "org:otolab"}
    assert all("--repo" not in args for args in calls)


def test_search_retries_rate_limit_using_retry_after(monkeypatch, tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("mine_repos: []\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_ENV, str(config_file))
    calls: list[list[str]] = []
    sleeps: list[float] = []

    def fake_search(args):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("HTTP 429: rate limit exceeded; Retry-After: 7")
        return []

    monkeypatch.setattr(github_ops, "gh_cli_json", fake_search)
    monkeypatch.setattr(github_ops.time, "sleep", sleeps.append)

    refs, warnings = github_ops.fetch_search_threads(
        "2026-09-17",
        backfill=False,
    )

    assert refs == []
    assert len(calls) == 8
    assert sleeps == [pytest.approx(7)]
    assert any("retrying" in warning for warning in warnings)


def test_search_rate_limit_exhaustion_raises_sync_collection_error(monkeypatch, tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("mine_repos: []\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_ENV, str(config_file))
    calls = 0
    monkeypatch.setattr(github_ops.time, "sleep", lambda _: None)

    def fake_search(args):
        nonlocal calls
        calls += 1
        raise RuntimeError("HTTP 403: rate limit exceeded; Retry-After: 0")

    monkeypatch.setattr(github_ops, "gh_cli_json", fake_search)

    with pytest.raises(github_ops.SyncCollectionError, match="after 4 attempts"):
        github_ops.fetch_search_threads("2026-09-17")

    assert calls == 4


def test_backfill_searches_are_spaced(monkeypatch, tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("mine_repos: []\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_ENV, str(config_file))
    sleeps: list[float] = []
    monkeypatch.setattr(github_ops, "gh_cli_json", lambda args: [])
    monkeypatch.setattr(github_ops.time, "sleep", sleeps.append)

    github_ops.fetch_search_threads("2026-09-17", backfill=True)

    assert len(sleeps) == 6
    assert all(delay == pytest.approx(2.0, abs=0.1) for delay in sleeps)


def test_sync_cutoff_uses_configured_bootstrap_and_overlap(monkeypatch, tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "sync:\n  bootstrap_days: 14\n  overlap_minutes: 2\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(CONFIG_ENV, str(config_file))
    now = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
    watermark = datetime(2026, 9, 17, 11, tzinfo=timezone.utc)

    bootstrap_cutoff, bootstrap_mode = github_ops.resolve_sync_cutoff(now=now)
    incremental_cutoff, incremental_mode = github_ops.resolve_sync_cutoff(
        now=now,
        last_successful_sync=watermark,
    )

    assert bootstrap_cutoff == now - timedelta(days=14)
    assert bootstrap_mode == "incremental"
    assert incremental_cutoff == watermark - timedelta(minutes=2)
    assert incremental_mode == "incremental"
