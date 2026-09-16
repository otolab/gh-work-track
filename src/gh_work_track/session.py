from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from gh_work_track.db import WorkTrackDB, thread_key


class Session:
    """DB-backed session replacing my-logs プロトタイプの JSON ファイル群。"""

    def __init__(self, db: str | None = None, *, read_only: bool = False) -> None:
        self.db = WorkTrackDB(db, read_only=read_only)
        self.db.init_tables()

    def load_watch_entries(self) -> list[dict[str, Any]]:
        rows = self.db.list_watched_threads()
        return [
            {
                "repo": row["repo"],
                "number": int(row["number"]),
                "kind": row.get("kind", "issue"),
                "note": row.get("watch_note", ""),
            }
            for row in rows
        ]

    def save_watch_entries(self, threads: list[dict[str, Any]]) -> None:
        new_keys = {thread_key(str(t["repo"]), int(t["number"])) for t in threads}
        for old in self.load_watch_entries():
            key = thread_key(str(old["repo"]), int(old["number"]))
            if key not in new_keys:
                self.db.upsert_thread(
                    str(old["repo"]),
                    int(old["number"]),
                    kind=str(old.get("kind", "issue")),
                    watch_note="",
                    is_watched=False,
                )
        for thread in threads:
            self.db.upsert_thread(
                str(thread["repo"]),
                int(thread["number"]),
                kind=str(thread.get("kind", "issue")),
                watch_note=str(thread.get("note", "")),
                is_watched=True,
            )

    def mark_seen(self, repo: str, number: int, kind: str = "issue", activity_at: str | None = None) -> None:
        existing = self.db.get_thread(repo, number) or {}
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.db.upsert_thread(
            repo,
            number,
            kind=str(existing.get("kind") or kind),
            title=str(existing.get("title", "")),
            watch_note=str(existing.get("watch_note", "")),
            is_watched=bool(existing.get("is_watched", False)),
            last_seen_at=now,
            last_synced_at=str(existing.get("last_synced_at", activity_at or now)),
        )

    def is_new_since_seen(self, repo: str, number: int, updated_at: str) -> bool:
        return self.db.is_new_since_seen(repo, number, updated_at)

    def save_events(self, events: list[dict[str, Any]]) -> tuple[int, int]:
        return self.db.upsert_events(events)

    def load_events_between(self, start: str, end: str) -> list[dict[str, Any]]:
        return self.db.events_between(start, end)

    def load_events_for_date(self, day: str) -> list[dict[str, Any]]:
        return self.db.events_for_date(day)

    def event_count(self) -> int:
        return self.db.count_events()


_session: Session | None = None


def open_session(db: str | None = None, *, read_only: bool = False) -> Session:
    global _session
    _session = Session(db, read_only=read_only)
    return _session


def get_session() -> Session:
    if _session is None:
        raise RuntimeError("session not opened")
    return _session
