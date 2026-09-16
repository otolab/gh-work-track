from __future__ import annotations

import pyarrow as pa

EVENTS_TABLE = "events"
THREADS_TABLE = "threads"
SYNC_RUNS_TABLE = "sync_runs"

ALL_TABLES = [EVENTS_TABLE, THREADS_TABLE, SYNC_RUNS_TABLE]


def events_schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("dedup_key", pa.string()),
            pa.field("date", pa.string()),
            pa.field("timestamp", pa.string()),
            pa.field("repo", pa.string()),
            pa.field("number", pa.int32()),
            pa.field("kind", pa.string()),
            pa.field("event", pa.string()),
            pa.field("actor", pa.string()),
            pa.field("snippet", pa.string()),
            pa.field("url", pa.string()),
            pa.field("source", pa.string()),
            pa.field("sources", pa.string()),
            pa.field("thread_key", pa.string()),
            pa.field("collected_at", pa.timestamp("ms")),
        ]
    )


def threads_schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("thread_key", pa.string()),
            pa.field("repo", pa.string()),
            pa.field("number", pa.int32()),
            pa.field("kind", pa.string()),
            pa.field("title", pa.string()),
            pa.field("is_watched", pa.bool_()),
            pa.field("watch_note", pa.string()),
            pa.field("last_seen_at", pa.string()),
            pa.field("last_synced_at", pa.string()),
            pa.field("updated_at", pa.timestamp("ms")),
        ]
    )


def sync_runs_schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("run_id", pa.string()),
            pa.field("started_at", pa.timestamp("ms")),
            pa.field("finished_at", pa.timestamp("ms")),
            pa.field("status", pa.string()),
            pa.field("mode", pa.string()),
            pa.field("cutoff_at", pa.timestamp("ms")),
            pa.field("error", pa.string()),
            pa.field("since_days", pa.int32()),
            pa.field("thread_count", pa.int32()),
            pa.field("event_count", pa.int32()),
            pa.field("new_count", pa.int32()),
            pa.field("warnings", pa.string()),
        ]
    )
