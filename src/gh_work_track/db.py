from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import lancedb
import pandas as pd
import pyarrow as pa

from gh_work_track.config import db_path
from gh_work_track.schemas import (
    ALL_TABLES,
    EVENTS_TABLE,
    SYNC_RUNS_TABLE,
    THREADS_TABLE,
    events_schema,
    sync_runs_schema,
    threads_schema,
)


def _escape_sql(value: str) -> str:
    return value.replace("'", "''")


def thread_key(repo: str, number: int) -> str:
    return f"{repo}#{number}"


def _now_ms() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC").floor("ms")


class WorkTrackDB:
    """LanceDB wrapper for gh-work-track.

    search-docs の db-engine と同様に:
    - PyArrow スキーマで create_table
    - open_table はインスタンス内でキャッシュ
    - read_only 時は read_consistency_interval を設定
    """

    def __init__(self, path: str | None = None, *, read_only: bool = False) -> None:
        resolved = db_path(path)
        resolved.mkdir(parents=True, exist_ok=True)
        self.path = resolved
        self.read_only = read_only
        connect_kwargs: dict[str, Any] = {}
        if read_only:
            connect_kwargs["read_consistency_interval"] = timedelta(seconds=5)
        self.db = lancedb.connect(str(resolved), **connect_kwargs)
        self._tables: dict[str, Any] = {}

    def init_tables(self) -> None:
        existing = set(self.db.list_tables())
        specs = {
            EVENTS_TABLE: events_schema(),
            THREADS_TABLE: threads_schema(),
            SYNC_RUNS_TABLE: sync_runs_schema(),
        }
        for name, schema in specs.items():
            if name in existing:
                continue
            try:
                self.db.create_table(name, schema=schema)
            except ValueError as exc:
                if "already exists" not in str(exc):
                    raise
        self._ensure_indexes()

    def _get_table(self, name: str):
        if name not in self._tables:
            self._tables[name] = self.db.open_table(name)
        return self._tables[name]

    def _ensure_indexes(self) -> None:
        events = self._get_table(EVENTS_TABLE)
        self._create_scalar_index_if_missing(events, "date", "BTREE")
        self._create_scalar_index_if_missing(events, "thread_key", "BTREE")
        threads = self._get_table(THREADS_TABLE)
        self._create_scalar_index_if_missing(threads, "thread_key", "BTREE")

    @staticmethod
    def _create_scalar_index_if_missing(table, column: str, index_type: str) -> None:
        existing = table.list_indices()
        if any(getattr(idx, "columns", None) == [column] for idx in existing):
            return
        try:
            table.create_scalar_index(column, index_type=index_type)
        except Exception:
            # インデックス作成失敗はクエリ性能に留まるため起動は継続
            return

    def normalize_event(self, record: dict[str, Any]) -> dict[str, Any]:
        repo = str(record.get("repo", ""))
        number = int(record.get("number", 0))
        sources = record.get("sources")
        if isinstance(sources, list):
            sources_json = json.dumps(sources, ensure_ascii=False)
        else:
            sources_json = str(sources or "")
        collected_at = record.get("collected_at")
        if collected_at is None:
            collected_at = _now_ms()
        elif not isinstance(collected_at, pd.Timestamp):
            collected_at = pd.Timestamp(collected_at).floor("ms")
        return {
            "dedup_key": str(record["dedup_key"]),
            "date": str(record.get("date", "")),
            "timestamp": str(record.get("timestamp", "")),
            "repo": repo,
            "number": number,
            "kind": str(record.get("kind", "issue")),
            "event": str(record.get("event", "")),
            "actor": str(record.get("actor", "")),
            "snippet": str(record.get("snippet", "")),
            "url": str(record.get("url", "")),
            "source": str(record.get("source", "")),
            "sources": sources_json,
            "thread_key": thread_key(repo, number),
            "collected_at": collected_at,
        }

    def upsert_events(self, records: list[dict[str, Any]]) -> tuple[int, int]:
        if not records:
            return 0, self.count_events()
        normalized = [self.normalize_event(record) for record in records]
        table = self._get_table(EVENTS_TABLE)
        before_keys = self._existing_dedup_keys({row["dedup_key"] for row in normalized})
        table.merge_insert("dedup_key").when_matched_update_all().when_not_matched_insert_all().execute(
            normalized
        )
        after_count = self.count_events()
        new_count = sum(1 for row in normalized if row["dedup_key"] not in before_keys)
        return new_count, after_count

    def _existing_dedup_keys(self, keys: set[str]) -> set[str]:
        if not keys:
            return set()
        table = self._get_table(EVENTS_TABLE)
        found: set[str] = set()
        for key in keys:
            rows = (
                table.search()
                .where(f"dedup_key = '{_escape_sql(key)}'")
                .limit(1)
                .to_list()
            )
            if rows:
                found.add(key)
        return found

    def count_events(self) -> int:
        return len(self._get_table(EVENTS_TABLE).search().to_pandas())

    def count_threads(self) -> int:
        return len(self._get_table(THREADS_TABLE).search().to_pandas())

    def count_sync_runs(self) -> int:
        return len(self._get_table(SYNC_RUNS_TABLE).search().to_pandas())

    def events_for_date(self, day: str) -> list[dict[str, Any]]:
        table = self._get_table(EVENTS_TABLE)
        df = (
            table.search()
            .where(f"date = '{_escape_sql(day)}'")
            .to_pandas()
        )
        if df.empty:
            return []
        df = df.sort_values(["timestamp", "repo", "number", "event"])
        return df.to_dict(orient="records")

    def events_between(self, start: str, end: str) -> list[dict[str, Any]]:
        table = self._get_table(EVENTS_TABLE)
        df = (
            table.search()
            .where(
                f"date >= '{_escape_sql(start)}' AND date <= '{_escape_sql(end)}'"
            )
            .to_pandas()
        )
        if df.empty:
            return []
        df = df.sort_values(["date", "timestamp", "repo", "number", "event"])
        return df.to_dict(orient="records")

    def upsert_thread(
        self,
        repo: str,
        number: int,
        *,
        kind: str = "issue",
        title: str = "",
        watch_note: str = "",
        last_seen_at: str = "",
        last_synced_at: str = "",
    ) -> None:
        row = {
            "thread_key": thread_key(repo, number),
            "repo": repo,
            "number": int(number),
            "kind": kind,
            "title": title,
            "watch_note": watch_note,
            "last_seen_at": last_seen_at,
            "last_synced_at": last_synced_at,
            "updated_at": _now_ms(),
        }
        table = self._get_table(THREADS_TABLE)
        table.merge_insert("thread_key").when_matched_update_all().when_not_matched_insert_all().execute(
            [row]
        )

    def list_watched_threads(self) -> list[dict[str, Any]]:
        table = self._get_table(THREADS_TABLE)
        df = table.search().where("watch_note != ''").to_pandas()
        if df.empty:
            return []
        return df.sort_values("thread_key").to_dict(orient="records")

    def record_sync_run(
        self,
        *,
        since_days: int,
        thread_count: int,
        event_count: int,
        new_count: int,
        warnings: list[str],
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> str:
        run_id = uuid.uuid4().hex
        started = started_at or datetime.now(timezone.utc)
        finished = finished_at or datetime.now(timezone.utc)
        row = {
            "run_id": run_id,
            "started_at": pd.Timestamp(started).floor("ms"),
            "finished_at": pd.Timestamp(finished).floor("ms"),
            "since_days": int(since_days),
            "thread_count": int(thread_count),
            "event_count": int(event_count),
            "new_count": int(new_count),
            "warnings": json.dumps(warnings, ensure_ascii=False),
        }
        self._get_table(SYNC_RUNS_TABLE).add([row])
        return run_id

    def stats(self) -> dict[str, Any]:
        return {
            "db_path": str(self.path),
            "read_only": self.read_only,
            "tables": ALL_TABLES,
            "events": self.count_events(),
            "threads": self.count_threads(),
            "sync_runs": self.count_sync_runs(),
        }
