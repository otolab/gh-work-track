from __future__ import annotations

from datetime import datetime, timezone

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
        3: [_timeline_item(7, 5)],
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

    assert [path.rsplit("page=", 1)[1] for path in calls] == ["1", "2", "3"]


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
