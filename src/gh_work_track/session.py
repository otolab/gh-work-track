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

    def mark_thread_synced(
        self,
        repo: str,
        number: int,
        *,
        kind: str = "issue",
        synced_at: datetime | None = None,
    ) -> None:
        """Record a successful collection boundary without losing thread metadata."""
        existing = self.db.get_thread(repo, number) or {}
        current = synced_at or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        else:
            current = current.astimezone(timezone.utc)
        candidate = current.isoformat().replace("+00:00", "Z")
        previous = str(existing.get("last_synced_at", "") or "")
        previous_at = _parse_timestamp(previous)
        if previous_at is not None and previous_at >= current:
            candidate = previous
        self.db.upsert_thread(
            repo,
            number,
            kind=str(existing.get("kind") or kind),
            title=str(existing.get("title", "")),
            watch_note=str(existing.get("watch_note", "")),
            is_watched=bool(existing.get("is_watched", False)),
            last_seen_at=str(existing.get("last_seen_at", "")),
            last_synced_at=candidate,
        )

    def capture_thread_state(
        self,
        threads: list[Any],
    ) -> dict[tuple[str, int], dict[str, Any] | None]:
        states: dict[tuple[str, int], dict[str, Any] | None] = {}
        for thread in threads:
            details = _thread_details(thread)
            if details is None:
                continue
            repo, number, _, _ = details
            states[(repo, number)] = self.db.get_thread(repo, number)
        return states

    def restore_thread_state(
        self,
        states: dict[tuple[str, int], dict[str, Any] | None],
    ) -> None:
        for (repo, number), row in states.items():
            if row is None:
                self.db.delete_thread(repo, number)
            else:
                self.db.restore_thread(row)

    def mark_threads_synced(
        self,
        threads: list[Any],
        *,
        synced_at: datetime | None = None,
    ) -> None:
        """Record collected thread boundaries and roll back on partial failure."""
        states = self.capture_thread_state(threads)
        seen: set[tuple[str, int]] = set()
        try:
            for thread in threads:
                details = _thread_details(thread)
                if details is None:
                    continue
                repo, number, kind, boundary_at = details
                key = (repo, number)
                if key in seen:
                    continue
                seen.add(key)
                self.mark_thread_synced(
                    repo,
                    number,
                    kind=kind,
                    synced_at=boundary_at or synced_at,
                )
        except Exception:
            self.restore_thread_state(states)
            raise

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


def _thread_details(
    thread: Any,
) -> tuple[str, int, str, datetime | None] | None:
    boundary_at = getattr(thread, "started_at", None)
    ref = getattr(thread, "ref", thread)
    if isinstance(thread, dict) and "ref" in thread:
        boundary_at = thread.get("started_at")
        ref = thread["ref"]
    if isinstance(ref, dict):
        repo = str(ref.get("repo", ""))
        raw_number = ref.get("number")
        kind = str(ref.get("kind", "issue"))
    else:
        repo = str(getattr(ref, "repo", ""))
        raw_number = getattr(ref, "number", None)
        kind = str(getattr(ref, "kind", "issue"))
    if not repo or raw_number is None:
        return None
    number = int(raw_number)
    if number <= 0:
        return None
    if boundary_at is not None and not isinstance(boundary_at, datetime):
        boundary_at = None
    return repo, number, kind, boundary_at


_session: Session | None = None


def open_session(db: str | None = None, *, read_only: bool = False) -> Session:
    global _session
    _session = Session(db, read_only=read_only)
    return _session


def get_session() -> Session:
    if _session is None:
        raise RuntimeError("session not opened")
    return _session


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
