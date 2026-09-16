from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from gh_work_track import __version__
from gh_work_track.config import data_home, db_path
from gh_work_track.github_ops import (
    build_parser as build_github_parser,
    cmd_collect,
    cmd_daily,
    cmd_drill,
    cmd_list,
    cmd_mark_seen,
    cmd_watch,
    collect_event_records,
    format_collect_markdown,
    resolve_sync_cutoff,
    save_events,
    sync_output_metadata,
)
from gh_work_track.migrate import migrate_legacy
from gh_work_track.session import open_session

DEFAULT_LEGACY_DIR = Path.home() / "Develop/otolab/my-logs/.claude/skills/gh-work-track"


def cmd_init(args: argparse.Namespace) -> int:
    session = open_session(args.db, read_only=False)
    print(json.dumps(session.db.stats(), ensure_ascii=False, indent=2))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    session = open_session(args.db, read_only=args.read_only)
    print(json.dumps(session.db.stats(), ensure_ascii=False, indent=2))
    return 0


def cmd_paths(_: argparse.Namespace) -> int:
    print(
        json.dumps(
            {"data_home": str(data_home()), "db_path": str(db_path())},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    legacy = Path(args.legacy_dir)
    session = open_session(args.db, read_only=False)
    result = migrate_legacy(
        session,
        events_jsonl=legacy / ".events" / "events.jsonl" if args.events else None,
        watch_json=legacy / ".watch.json" if args.watch else None,
        state_json=legacy / ".state.json" if args.state else None,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("## migrate")
        print(f"- events: {result['events']} 件")
        print(f"- watch: {result['watch']} 件")
        print(f"- state: {result['state']} 件")
        print(f"- db: `{session.db.path}`")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    return cmd_collect(args)


def build_parser() -> argparse.ArgumentParser:
    parser = build_github_parser()
    parser.description = "GitHub work event tracker (LanceDB)"
    parser.add_argument("--version", action="version", version=f"gh-work-track {__version__}")

    sub = next(action for action in parser._actions if action.dest == "command")

    init_parser = sub.add_parser("init", help="LanceDB テーブルを初期化")
    init_parser.set_defaults(func=cmd_init)

    stats_parser = sub.add_parser("stats", help="DB 統計を表示")
    stats_parser.add_argument("--read-only", action="store_true")
    stats_parser.set_defaults(func=cmd_stats)

    paths_parser = sub.add_parser("paths", help="データディレクトリを表示")
    paths_parser.set_defaults(func=cmd_paths)

    migrate_parser = sub.add_parser("migrate", help="my-logs プロトタイプの JSON/JSONL を取り込む")
    migrate_parser.add_argument("--legacy-dir", default=str(DEFAULT_LEGACY_DIR))
    migrate_parser.add_argument("--events", action=argparse.BooleanOptionalAction, default=True)
    migrate_parser.add_argument("--watch", action=argparse.BooleanOptionalAction, default=True)
    migrate_parser.add_argument("--state", action=argparse.BooleanOptionalAction, default=True)
    migrate_parser.add_argument("--json", action="store_true")
    migrate_parser.set_defaults(func=cmd_migrate)

    return parser


def _read_only_for(args: argparse.Namespace) -> bool:
    if args.command == "daily":
        return True
    if args.command == "list":
        return True
    if args.command == "watch" and args.action == "list":
        return True
    if args.command == "drill" and getattr(args, "no_mark_seen", False):
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in {"collect", "sync"}:
            session = open_session(args.db, read_only=False)
            started = datetime.now(timezone.utc)
            last_successful = (
                None
                if args.since is not None
                else session.db.last_successful_sync()
            )
            cutoff, mode = resolve_sync_cutoff(
                since_days=args.since,
                last_successful_sync=last_successful,
                now=started,
            )
            metadata = sync_output_metadata(
                mode=mode,
                cutoff=cutoff,
                watermark=last_successful,
            )
            run_id = session.db.start_sync_run(
                since_days=args.since,
                mode=mode,
                cutoff_at=cutoff,
                started_at=started,
            )
            try:
                synced_threads = []
                events, warnings, thread_count = collect_event_records(
                    cutoff=cutoff,
                    optimize_threads=mode == "incremental",
                    synced_threads=synced_threads,
                )
                new_count, total_count = save_events(events)
                finished_at = datetime.now(timezone.utc)
                session.mark_threads_synced(synced_threads, synced_at=finished_at)
                payload = {
                    **metadata,
                    "cutoff_at": metadata["cutoff"],
                    "since_days": args.since,
                    "thread_count": thread_count,
                    "event_count": len(events),
                    "new_count": new_count,
                    "total_count": total_count,
                    "db_path": str(db_path(args.db)),
                    "warnings": warnings,
                }
                markdown = format_collect_markdown(
                    args.since,
                    len(events),
                    new_count,
                    total_count,
                    thread_count,
                    warnings,
                    mode=mode,
                    cutoff=cutoff,
                    watermark=last_successful,
                    bootstrap=metadata["bootstrap"],
                )
                session.db.update_sync_run(
                    run_id,
                    status="success",
                    mode=mode,
                    cutoff_at=cutoff,
                    thread_count=thread_count,
                    event_count=len(events),
                    new_count=new_count,
                    warnings=warnings,
                    error="",
                    finished_at=finished_at,
                )
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    print(markdown)
            except Exception as exc:
                session.db.update_sync_run(
                    run_id,
                    status="failed",
                    mode=mode,
                    cutoff_at=cutoff,
                    error=str(exc) or exc.__class__.__name__,
                    finished_at=datetime.now(timezone.utc),
                )
                raise
            return 0

        open_session(args.db, read_only=_read_only_for(args))
        return args.func(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
