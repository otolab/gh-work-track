from __future__ import annotations

import json
from datetime import date, datetime, timezone

from gh_work_track import github_ops


def _documented_connected_payload():
    """The fields documented for REST issue-timeline ``connected`` events."""
    return {
        "id": 123,
        "node_id": "MDExOlRpbWVMaW5lRXZlbnQxMjM=",
        "url": "https://api.github.com/repos/owner/repo/issues/events/123",
        "actor": {"login": "alice"},
        "event": "connected",
        "commit_id": None,
        "commit_url": None,
        "created_at": "2026-09-17T10:00:00Z",
    }


def test_cross_referenced_timeline_payload_extracts_source_issue():
    ref = github_ops.ThreadRef("plaidev/karte-io-systems", 169457, "issue")
    payload = {
        "event": "cross-referenced",
        "created_at": "2026-09-17T10:00:00Z",
        "source": {
            "type": "issue",
            "issue": {
                "number": 8814,
                "repository": {"full_name": "plaidev/ops"},
            },
        },
    }

    assert github_ops.timeline_link_records(ref, payload) == [{
        "from_thread_key": "plaidev/karte-io-systems#169457",
        "to_thread_key": "plaidev/ops#8814",
        "rel": "cross_ref",
        "source": "timeline",
        "confidence": 1.0,
        "discovered_at": "2026-09-17T10:00:00Z",
    }]


def test_endpoint_bearing_connected_payload_extracts_blocks_direction():
    ref = github_ops.ThreadRef("owner/repo", 10, "issue")
    payload = {
        "event": "connected",
        "created_at": "2026-09-17T10:00:00Z",
        "subject": {
            "type": "blocking",
            "number": 11,
            "repository": {"full_name": "owner/repo"},
        },
    }

    links = github_ops.extract_thread_links(ref, payload)

    assert links[0]["from_thread_key"] == "owner/repo#10"
    assert links[0]["to_thread_key"] == "owner/repo#11"
    assert links[0]["rel"] == "blocks"
    assert links[0]["source"] == "timeline"


def test_endpoint_bearing_connected_payload_extracts_blocked_by_direction():
    ref = github_ops.ThreadRef("owner/repo", 10, "issue")
    payload = {
        "event": "connected",
        "created_at": "2026-09-17T10:00:00Z",
        "subject": {
            "type": "blocked_by",
            "number": 11,
            "repository": {"full_name": "owner/repo"},
        },
    }

    links = github_ops.timeline_link_records(ref, payload)

    assert links[0]["from_thread_key"] == "owner/repo#10"
    assert links[0]["to_thread_key"] == "owner/repo#11"
    assert links[0]["rel"] == "blocked_by"


def test_documented_connected_payload_has_no_resolvable_endpoint():
    ref = github_ops.ThreadRef("owner/repo", 10, "issue")
    payload = _documented_connected_payload()

    assert github_ops.timeline_link_records(ref, payload) == []
    warning = github_ops.timeline_link_warning(
        ref,
        payload,
        cutoff=datetime(2026, 9, 17, 9, tzinfo=timezone.utc),
    )
    assert warning is not None
    assert "no resolvable endpoint" in warning
    assert "blocks/blocked_by were not stored" in warning


def test_collect_event_records_can_collect_links_alongside_events(monkeypatch, tmp_path):
    from gh_work_track.session import open_session

    open_session(str(tmp_path / "lance"))
    ref = github_ops.ThreadRef("owner/repo", 10)
    monkeypatch.setattr(github_ops, "fetch_notifications", lambda **kwargs: [])
    monkeypatch.setattr(
        github_ops,
        "fetch_search_threads",
        lambda cutoff_date, **kwargs: ([ref], []),
    )
    monkeypatch.setattr(github_ops, "fetch_user_event_threads", lambda cutoff: ([], []))
    monkeypatch.setattr(github_ops, "load_watch", lambda: [])
    monkeypatch.setattr(
        github_ops,
        "fetch_timeline",
        lambda *args, **kwargs: [{
            "id": 1,
            "event": "cross-referenced",
            "created_at": "2026-09-17T10:00:00Z",
            "source": {
                "issue": {
                    "number": 11,
                    "repository": {"full_name": "owner/repo"},
                }
            },
        }],
    )
    monkeypatch.setattr(github_ops, "fetch_all_comments", lambda ref_arg: [])
    links: list[dict] = []

    github_ops.collect_event_records(
        cutoff=datetime(2026, 9, 17, 9, tzinfo=timezone.utc),
        thread_links=links,
    )

    assert len(links) == 1
    assert links[0]["rel"] == "cross_ref"


def test_grouped_daily_is_nested_but_default_daily_stays_flat():
    parent = "owner/repo#10"
    child = "owner/repo#11"
    events = [{
        "date": "2026-09-17",
        "repo": "owner/repo",
        "number": 11,
        "kind": "issue",
        "event": "commented",
        "actor": "alice",
        "snippet": "progress",
        "url": "https://github.com/owner/repo/issues/11",
    }]
    assignments = github_ops.resolve_work_groups([
        {
            "from_thread_key": child,
            "to_thread_key": parent,
            "rel": "parent",
        }
    ])

    flat = github_ops.format_daily_markdown(date(2026, 9, 17), date(2026, 9, 17), events)
    grouped = github_ops.format_daily_markdown(
        date(2026, 9, 17),
        date(2026, 9, 17),
        events,
        group_assignments=assignments,
    )

    assert "#### Group anchor" not in flat
    assert "#### Group anchor: `owner/repo#10`" in grouped
    assert "  - `owner/repo#11`" in grouped


def test_sync_persists_endpoint_bearing_timeline_links(monkeypatch, tmp_path, capsys):
    from gh_work_track import cli
    from gh_work_track.db import WorkTrackDB

    ref = github_ops.ThreadRef("owner/repo", 10)
    monkeypatch.setattr(github_ops, "fetch_notifications", lambda **kwargs: [])
    monkeypatch.setattr(
        github_ops,
        "fetch_search_threads",
        lambda cutoff_date, **kwargs: ([ref], []),
    )
    monkeypatch.setattr(github_ops, "fetch_user_event_threads", lambda cutoff: ([], []))
    monkeypatch.setattr(github_ops, "load_watch", lambda: [])
    monkeypatch.setattr(
        github_ops,
        "fetch_timeline",
        lambda *args, **kwargs: [{
            "id": 1,
            "event": "connected",
            "created_at": "2026-09-17T10:00:00Z",
            "subject": {
                "type": "blocking",
                "number": 11,
                "repository": {"full_name": "owner/repo"},
            },
        }],
    )
    monkeypatch.setattr(github_ops, "fetch_all_comments", lambda ref_arg: [])

    assert cli.main(["--db", str(tmp_path / "lance"), "sync", "--since", "365", "--json"]) == 0
    capsys.readouterr()

    database = WorkTrackDB(str(tmp_path / "lance"))
    links = database.thread_links()
    assert len(links) == 1
    assert links[0]["from_thread_key"] == "owner/repo#10"
    assert links[0]["to_thread_key"] == "owner/repo#11"
    assert links[0]["rel"] == "blocks"


def test_sync_does_not_persist_documented_connected_payload_without_endpoint(
    monkeypatch,
    tmp_path,
    capsys,
):
    from gh_work_track import cli
    from gh_work_track.db import WorkTrackDB

    ref = github_ops.ThreadRef("owner/repo", 10)
    monkeypatch.setattr(github_ops, "fetch_notifications", lambda **kwargs: [])
    monkeypatch.setattr(
        github_ops,
        "fetch_search_threads",
        lambda cutoff_date, **kwargs: ([ref], []),
    )
    monkeypatch.setattr(github_ops, "fetch_user_event_threads", lambda cutoff: ([], []))
    monkeypatch.setattr(github_ops, "load_watch", lambda: [])
    monkeypatch.setattr(
        github_ops,
        "fetch_timeline",
        lambda *args, **kwargs: [_documented_connected_payload()],
    )
    monkeypatch.setattr(github_ops, "fetch_all_comments", lambda ref_arg: [])

    assert cli.main(["--db", str(tmp_path / "lance"), "sync", "--since", "365", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)

    database = WorkTrackDB(str(tmp_path / "lance"))
    assert database.thread_links() == []
    assert output["link_count"] == 0
    assert any("no resolvable endpoint" in warning for warning in output["warnings"])


def test_daily_group_anchor_reads_stored_links_and_adds_json_anchor(tmp_path, capsys):
    from gh_work_track import cli
    from gh_work_track.db import WorkTrackDB

    db_path = tmp_path / "lance"
    database = WorkTrackDB(str(db_path))
    database.init_tables()
    database.upsert_events([{
        "dedup_key": "event-1",
        "date": "2026-09-17",
        "timestamp": "2026-09-17T10:00:00Z",
        "repo": "owner/repo",
        "number": 11,
        "kind": "issue",
        "event": "commented",
        "actor": "alice",
        "snippet": "progress",
        "url": "https://github.com/owner/repo/issues/11",
        "source": "timeline",
    }])
    database.upsert_thread_links([{
        "from_thread_key": "owner/repo#11",
        "to_thread_key": "owner/repo#10",
        "rel": "parent",
        "source": "manual",
        "confidence": 1.0,
        "discovered_at": "2026-09-17T10:00:00Z",
    }])

    assert cli.main([
        "--db", str(db_path), "daily", "--date", "2026-09-17", "--group", "anchor"
    ]) == 0
    grouped_output = capsys.readouterr().out
    assert "#### Group anchor: `owner/repo#10`" in grouped_output

    assert cli.main([
        "--db", str(db_path), "daily", "--date", "2026-09-17", "--group", "anchor", "--json"
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["events"]["2026-09-17"][0]["group_anchor"] == "owner/repo#10"
