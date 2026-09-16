from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gh_work_track.github_ops import deduplicate_events
from gh_work_track.session import Session


def load_jsonl_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("dedup_key"):
            events.append(item)
    return deduplicate_events(events)


def migrate_legacy(
    session: Session,
    *,
    events_jsonl: Path | None = None,
    watch_json: Path | None = None,
    state_json: Path | None = None,
) -> dict[str, int]:
    result = {"events": 0, "watch": 0, "state": 0}

    if events_jsonl and events_jsonl.exists():
        events = load_jsonl_events(events_jsonl)
        _, total = session.save_events(events)
        result["events"] = total

    if watch_json and watch_json.exists():
        data = json.loads(watch_json.read_text())
        threads = data.get("threads", [])
        session.save_watch_entries(
            [
                {
                    "repo": thread["repo"],
                    "number": int(thread["number"]),
                    "kind": thread.get("kind", "issue"),
                    "note": thread.get("note", ""),
                }
                for thread in threads
            ]
        )
        result["watch"] = len(threads)

    if state_json and state_json.exists():
        data = json.loads(state_json.read_text())
        for key, payload in data.get("threads", {}).items():
            if "#" not in key:
                continue
            repo, number_text = key.split("#", 1)
            try:
                number = int(number_text)
            except ValueError:
                continue
            existing = session.db.get_thread(repo, number) or {}
            session.db.upsert_thread(
                repo,
                number,
                kind=str(existing.get("kind", "issue")),
                title=str(existing.get("title", "")),
                watch_note=str(existing.get("watch_note", "")),
                is_watched=bool(existing.get("is_watched", False)),
                last_seen_at=str(payload.get("last_seen_at", "")),
                last_synced_at=str(payload.get("last_activity_at", "")),
            )
            result["state"] += 1

    return result
