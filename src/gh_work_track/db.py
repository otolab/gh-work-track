from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import lancedb
import pandas as pd
import pyarrow as pa
from lancedb.index import BTree

from gh_work_track.config import db_path
from gh_work_track.schemas import (
    ALL_TABLES,
    EVENTS_TABLE,
    SYNC_RUNS_TABLE,
    THREAD_LINKS_TABLE,
    THREADS_TABLE,
    events_schema,
    sync_runs_schema,
    thread_links_schema,
    threads_schema,
)

SYNC_STATUSES = {"running", "success", "failed"}
SYNC_MODES = {"incremental", "backfill"}


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
        table_list = self.db.list_tables()
        if hasattr(table_list, "tables"):
            existing = set(table_list.tables)
        elif isinstance(table_list, (list, tuple)):
            existing = {name for name in table_list if isinstance(name, str)}
        else:
            existing = set(table_list)
        specs = {
            EVENTS_TABLE: events_schema(),
            THREADS_TABLE: threads_schema(),
            THREAD_LINKS_TABLE: thread_links_schema(),
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
        self._ensure_sync_runs_schema()
        self._ensure_indexes()

    def _ensure_sync_runs_schema(self) -> None:
        """Add status fields to databases created before the sync-run schema.

        LanceDB tables keep their schema after creation, so merely changing the
        schema returned by ``sync_runs_schema`` is not enough for an existing
        database.  The old implementation only recorded completed runs; those
        rows are therefore migrated as successful backfill runs.
        """
        table = self._get_table(SYNC_RUNS_TABLE)
        expected = sync_runs_schema()
        existing = set(table.schema.names)
        missing = [field for field in expected if field.name not in existing]
        if not missing or self.read_only:
            return

        table.add_columns(missing)
        defaults: dict[str, Any] = {}
        missing_names = {field.name for field in missing}
        if "status" in missing_names:
            defaults["status"] = "success"
        if "mode" in missing_names:
            defaults["mode"] = "backfill"
        if "error" in missing_names:
            defaults["error"] = ""
        if defaults and not table.search().to_pandas().empty:
            table.update(where="run_id IS NOT NULL", values=defaults)

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
        links = self._get_table(THREAD_LINKS_TABLE)
        self._create_scalar_index_if_missing(links, "from_thread_key", "BTREE")
        self._create_scalar_index_if_missing(links, "to_thread_key", "BTREE")

    @staticmethod
    def _create_scalar_index_if_missing(table, column: str, index_type: str) -> None:
        existing = table.list_indices()
        if any(getattr(idx, "columns", None) == [column] for idx in existing):
            return
        try:
            table.create_index(column, config=BTree())
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

    def count_thread_links(self) -> int:
        return len(self._get_table(THREAD_LINKS_TABLE).search().to_pandas())

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
        kind: str | None = None,
        title: str | None = None,
        watch_note: str | None = None,
        is_watched: bool | None = None,
        last_seen_at: str | None = None,
        last_synced_at: str | None = None,
    ) -> None:
        existing = self.get_thread(repo, number)
        row = {
            "thread_key": thread_key(repo, number),
            "repo": repo,
            "number": int(number),
            "kind": str(kind if kind is not None else (existing or {}).get("kind", "issue")),
            "title": str(title if title is not None else (existing or {}).get("title", "")),
            "is_watched": bool(
                is_watched
                if is_watched is not None
                else (existing or {}).get("is_watched", False)
            ),
            "watch_note": str(
                watch_note
                if watch_note is not None
                else (existing or {}).get("watch_note", "")
            ),
            "last_seen_at": str(
                last_seen_at
                if last_seen_at is not None
                else (existing or {}).get("last_seen_at", "")
            ),
            "last_synced_at": str(
                last_synced_at
                if last_synced_at is not None
                else (existing or {}).get("last_synced_at", "")
            ),
            "updated_at": _now_ms(),
        }
        table = self._get_table(THREADS_TABLE)
        table.merge_insert("thread_key").when_matched_update_all().when_not_matched_insert_all().execute(
            [row]
        )

    def get_thread(self, repo: str, number: int) -> dict[str, Any] | None:
        key = thread_key(repo, number)
        rows = (
            self._get_table(THREADS_TABLE)
            .search()
            .where(f"thread_key = '{_escape_sql(key)}'")
            .limit(1)
            .to_list()
        )
        return rows[0] if rows else None

    def delete_thread(self, repo: str, number: int) -> None:
        self._get_table(THREADS_TABLE).delete(
            where=f"thread_key = '{_escape_sql(thread_key(repo, number))}'"
        )

    def restore_thread(self, row: dict[str, Any]) -> None:
        """Restore a previously captured row after a failed watermark update."""
        self._get_table(THREADS_TABLE).merge_insert("thread_key").when_matched_update_all().when_not_matched_insert_all().execute(
            [row]
        )

    def db_max_timestamp(
        self,
        thread: Any,
        number: int | None = None,
    ) -> datetime | None:
        """Return the newest stored event timestamp for a thread.

        ``thread`` may be a ``ThreadRef``-like object or mapping.  Passing a
        repository string together with ``number`` is also supported so this
        DB layer does not need to import the GitHub API types.
        """
        if number is None:
            if isinstance(thread, dict):
                repo = str(thread.get("repo", ""))
                raw_number = thread.get("number")
            else:
                repo = str(getattr(thread, "repo", ""))
                raw_number = getattr(thread, "number", None)
            if raw_number is None:
                return None
            number = int(raw_number)
        else:
            repo = str(thread)

        if not repo or number <= 0:
            return None
        rows = (
            self._get_table(EVENTS_TABLE)
            .search()
            .where(
                f"thread_key = '{_escape_sql(thread_key(repo, number))}'"
            )
            .to_pandas()
        )
        if rows.empty or "timestamp" not in rows:
            return None
        timestamps = pd.to_datetime(rows["timestamp"], utc=True, errors="coerce").dropna()
        if timestamps.empty:
            return None
        timestamp = pd.Timestamp(timestamps.max())
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        return timestamp.to_pydatetime()

    # Descriptive alias for callers that do not use the Issue terminology.
    max_event_timestamp = db_max_timestamp

    def is_new_since_seen(self, repo: str, number: int, updated_at: str) -> bool:
        existing = self.get_thread(repo, number)
        if not existing or not existing.get("last_seen_at"):
            return True
        return updated_at > str(existing["last_seen_at"])

    def list_watched_threads(self) -> list[dict[str, Any]]:
        table = self._get_table(THREADS_TABLE)
        df = table.search().where("is_watched = true").to_pandas()
        if df.empty:
            return []
        return df.sort_values("thread_key").to_dict(orient="records")

    def normalize_thread_link(self, record: dict[str, Any]) -> dict[str, Any]:
        discovered_at = record.get("discovered_at")
        if discovered_at is None:
            discovered_at = _now_ms()
        elif not isinstance(discovered_at, pd.Timestamp):
            discovered_at = pd.Timestamp(discovered_at).floor("ms")
        return {
            "from_thread_key": str(record.get("from_thread_key", "")),
            "to_thread_key": str(record.get("to_thread_key", "")),
            "rel": str(record.get("rel", "")),
            "source": str(record.get("source", "")),
            "confidence": float(record.get("confidence", 1.0)),
            "discovered_at": discovered_at,
        }

    def upsert_thread_links(self, records: list[dict[str, Any]]) -> tuple[int, int]:
        if not records:
            return 0, self.count_thread_links()
        unique: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for record in records:
            normalized = self.normalize_thread_link(record)
            key = (
                normalized["from_thread_key"],
                normalized["to_thread_key"],
                normalized["rel"],
                normalized["source"],
            )
            if all(key):
                unique[key] = normalized
        normalized_records = list(unique.values())
        if not normalized_records:
            return 0, self.count_thread_links()
        table = self._get_table(THREAD_LINKS_TABLE)
        existing_df = table.search().to_pandas()
        existing = {
            (
                str(row["from_thread_key"]),
                str(row["to_thread_key"]),
                str(row["rel"]),
                str(row["source"]),
            )
            for _, row in existing_df.iterrows()
        }
        table.merge_insert(
            ["from_thread_key", "to_thread_key", "rel", "source"]
        ).when_matched_update_all().when_not_matched_insert_all().execute(
            normalized_records
        )
        return (
            sum(key not in existing for key in unique),
            self.count_thread_links(),
        )

    def thread_links(
        self,
        *,
        thread_key_value: str | None = None,
    ) -> list[dict[str, Any]]:
        table = self._get_table(THREAD_LINKS_TABLE)
        query = table.search()
        if thread_key_value:
            escaped = _escape_sql(thread_key_value)
            query = query.where(
                f"from_thread_key = '{escaped}' OR to_thread_key = '{escaped}'"
            )
        df = query.to_pandas()
        if df.empty:
            return []
        return df.sort_values(
            ["from_thread_key", "to_thread_key", "rel", "source"]
        ).to_dict(orient="records")

    # Descriptive alias for callers that prefer an explicit collection name.
    list_thread_links = thread_links
    get_thread_links = thread_links

    def links_for_thread(self, thread_key_value: str) -> list[dict[str, Any]]:
        return self.thread_links(thread_key_value=thread_key_value)

    def record_sync_run(
        self,
        *,
        since_days: int | None,
        thread_count: int,
        event_count: int,
        new_count: int,
        warnings: list[str],
        status: str = "success",
        mode: str = "backfill",
        cutoff_at: datetime | None = None,
        error: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> str:
        self._validate_sync_run_values(status=status, mode=mode)
        run_id = uuid.uuid4().hex
        started = started_at or datetime.now(timezone.utc)
        finished = (
            None
            if status == "running" and finished_at is None
            else finished_at or datetime.now(timezone.utc)
        )
        row = {
            "run_id": run_id,
            "started_at": pd.Timestamp(started).floor("ms"),
            "finished_at": self._timestamp(finished),
            "status": status,
            "mode": mode,
            "cutoff_at": self._timestamp(cutoff_at),
            "error": str(error) if error is not None else "",
            "since_days": None if since_days is None else int(since_days),
            "thread_count": int(thread_count),
            "event_count": int(event_count),
            "new_count": int(new_count),
            "warnings": json.dumps(warnings, ensure_ascii=False),
        }
        self._get_table(SYNC_RUNS_TABLE).add([row])
        return run_id

    def start_sync_run(
        self,
        *,
        since_days: int | None,
        mode: str = "backfill",
        cutoff_at: datetime | None = None,
        started_at: datetime | None = None,
    ) -> str:
        return self.record_sync_run(
            since_days=since_days,
            thread_count=0,
            event_count=0,
            new_count=0,
            warnings=[],
            status="running",
            mode=mode,
            cutoff_at=cutoff_at,
            started_at=started_at,
        )

    def update_sync_run(
        self,
        run_id: str,
        *,
        status: str,
        thread_count: int | None = None,
        event_count: int | None = None,
        new_count: int | None = None,
        warnings: list[str] | None = None,
        mode: str | None = None,
        cutoff_at: datetime | None = None,
        error: str | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        self._validate_sync_run_values(status=status, mode=mode)
        values: dict[str, Any] = {"status": status}
        if thread_count is not None:
            values["thread_count"] = int(thread_count)
        if event_count is not None:
            values["event_count"] = int(event_count)
        if new_count is not None:
            values["new_count"] = int(new_count)
        if warnings is not None:
            values["warnings"] = json.dumps(warnings, ensure_ascii=False)
        if mode is not None:
            values["mode"] = mode
        if cutoff_at is not None:
            values["cutoff_at"] = self._timestamp(cutoff_at)
        if error is not None:
            values["error"] = str(error)
        if finished_at is not None or status != "running":
            values["finished_at"] = self._timestamp(
                finished_at or datetime.now(timezone.utc)
            )

        result = self._get_table(SYNC_RUNS_TABLE).update(
            where=f"run_id = '{_escape_sql(run_id)}'",
            values=values,
        )
        if result.rows_updated != 1:
            raise ValueError(f"sync run not found: {run_id}")

    def last_successful_sync(self) -> datetime | None:
        rows = self._successful_sync_rows()
        if rows.empty:
            return None
        finished = rows["finished_at"].dropna()
        if finished.empty:
            return None
        timestamp = pd.Timestamp(finished.max())
        return self._to_utc_datetime(timestamp)

    def last_successful_sync_started_at(self) -> datetime | None:
        """Return the start of the latest successful sync run.

        Incremental collection uses this as its global discovery boundary.
        A run may last longer than the overlap window, so using its completion
        time could move the next cutoff past per-thread acquisition starts.
        """
        rows = self._successful_sync_rows()
        if rows.empty or "started_at" not in rows or "finished_at" not in rows:
            return None
        rows = rows.dropna(subset=["finished_at"]).sort_values("finished_at")
        if rows.empty:
            return None
        started = rows.iloc[-1]["started_at"]
        if pd.isna(started):
            return None
        return self._to_utc_datetime(pd.Timestamp(started))

    def _successful_sync_rows(self):
        table = self._get_table(SYNC_RUNS_TABLE)
        columns = set(table.schema.names)
        rows = table.search().to_pandas()
        if rows.empty:
            return rows
        if "status" in columns:
            rows = rows[rows["status"] == "success"]
        return rows

    @staticmethod
    def _to_utc_datetime(timestamp: pd.Timestamp) -> datetime:
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        return timestamp.to_pydatetime()

    @staticmethod
    def _timestamp(value: datetime | None) -> pd.Timestamp | None:
        if value is None:
            return None
        return pd.Timestamp(value).floor("ms")

    @staticmethod
    def _validate_sync_run_values(*, status: str, mode: str | None) -> None:
        if status not in SYNC_STATUSES:
            raise ValueError(f"invalid sync run status: {status}")
        if mode is not None and mode not in SYNC_MODES:
            raise ValueError(f"invalid sync run mode: {mode}")

    def stats(self) -> dict[str, Any]:
        return {
            "db_path": str(self.path),
            "read_only": self.read_only,
            "tables": ALL_TABLES,
            "events": self.count_events(),
            "threads": self.count_threads(),
            "thread_links": self.count_thread_links(),
            "sync_runs": self.count_sync_runs(),
        }
