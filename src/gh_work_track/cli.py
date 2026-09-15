from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta

from gh_work_track import __version__
from gh_work_track.config import data_home, db_path
from gh_work_track.db import WorkTrackDB


def cmd_init(args: argparse.Namespace) -> int:
    db = WorkTrackDB(args.db, read_only=False)
    db.init_tables()
    print(json.dumps(db.stats(), ensure_ascii=False, indent=2))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    db = WorkTrackDB(args.db, read_only=args.read_only)
    db.init_tables()
    print(json.dumps(db.stats(), ensure_ascii=False, indent=2))
    return 0


def cmd_paths(_: argparse.Namespace) -> int:
    print(
        json.dumps(
            {
                "data_home": str(data_home()),
                "db_path": str(db_path()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_daily(args: argparse.Namespace) -> int:
    db = WorkTrackDB(args.db, read_only=True)
    db.init_tables()
    if args.date:
        events = db.events_for_date(args.date)
        period = args.date
    else:
        end = date.today()
        start = end - timedelta(days=args.since - 1)
        events = db.events_between(start.isoformat(), end.isoformat())
        period = f"{start.isoformat()} ～ {end.isoformat()}"
    if args.json:
        print(json.dumps({"period": period, "count": len(events), "events": events}, ensure_ascii=False, indent=2))
        return 0
    print(f"## gh-work-track daily ({period})")
    print()
    print(f"イベント: {len(events)} 件")
    print()
    if not events:
        print("_該当なし（先に `sync` を実行してください — 未実装）_")
        return 0
    current_date = ""
    for event in events:
        event_day = str(event.get("date", ""))
        if event_day != current_date:
            if current_date:
                print()
            print(f"### {event_day}")
            current_date = event_day
        actor = str(event.get("actor", ""))
        actor_text = f" by @{actor}" if actor and actor != "?" else ""
        body = f" — {event['snippet']}" if event.get("snippet") else ""
        print(
            f"- `{event.get('repo', '?')}#{event.get('number', '?')}` "
            f"({event.get('kind', 'issue')}) **{event.get('event', 'event')}**"
            f"{actor_text}{body} — {event.get('url', '')}"
        )
    return 0


def cmd_sync(_: argparse.Namespace) -> int:
    print(
        "error: `sync` は未実装です。GitHub API 同期は次フェーズで my-logs プロトタイプから移植します。",
        file=sys.stderr,
    )
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="GitHub work event tracker (LanceDB)",
    )
    parser.add_argument("--version", action="version", version=f"gh-work-track {__version__}")
    parser.add_argument(
        "--db",
        help="LanceDB ディレクトリ（default: ~/.local/share/gh-work-track/lance）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="LanceDB テーブルを初期化")
    init_parser.set_defaults(func=cmd_init)

    stats_parser = sub.add_parser("stats", help="DB 統計を表示")
    stats_parser.add_argument("--read-only", action="store_true", help="read_consistency_interval=5s で接続")
    stats_parser.set_defaults(func=cmd_stats)

    paths_parser = sub.add_parser("paths", help="データディレクトリを表示")
    paths_parser.set_defaults(func=cmd_paths)

    daily_parser = sub.add_parser("daily", help="保存済みイベントを日付 pivot")
    daily_period = daily_parser.add_mutually_exclusive_group(required=True)
    daily_period.add_argument("--date", help="対象日 (YYYY-MM-DD, UTC 暦日)")
    daily_period.add_argument("--since", type=int, help="直近 N 暦日（今日を含む）")
    daily_parser.add_argument("--json", action="store_true")
    daily_parser.set_defaults(func=cmd_daily)

    sync_parser = sub.add_parser("sync", help="GitHub からイベントを同期（未実装）")
    sync_parser.set_defaults(func=cmd_sync)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
