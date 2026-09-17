from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gh_work_track import github_ops
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
        lambda cutoff_date: ([ref], []),
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
        lambda cutoff_date: ([ref], []),
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
