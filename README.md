# gh-work-track

自分に関係する GitHub Issue/PR のイベントを **LanceDB** に蓄積し、日次・スレッド単位で問い合わせる CLI。

[my-logs](https://github.com/otolab/my-logs) 内のプロトタイプ（`collect` / `daily` + JSONL）を独立ツール化するリポジトリ。ストレージは [search-docs](https://github.com/otolab/search-docs) と同様に LanceDB + PyArrow スキーマを採用する。

## ステータス

| 機能 | 状態 |
|---|---|
| LanceDB スキーマ / `init` / `stats` / `daily` | ✅ |
| `sync` / `collect`（notifications + watch + search → events） | ✅ |
| `watch` / `list` / `drill` / `mark-seen` | ✅ |
| `migrate`（my-logs プロトタイプ JSONL → LanceDB） | ✅ |
| modular-prompt-extract 連携 | 🔜 別途 |

## インストール

```bash
cd ~/Develop/otolab/gh-work-track
uv sync
uv run gh-work-track --help
```

## データ配置

| 環境変数 | 意味 |
|---|---|
| `GH_WORK_TRACK_HOME` | データルート（default: `~/.local/share/gh-work-track`） |

LanceDB 本体: `$GH_WORK_TRACK_HOME/lance/`

## 使い方

### Quick start（incremental 前提）

```bash
# セットアップ
uv run gh-work-track init
uv run gh-work-track migrate

# 日常運用: 前回成功 sync 以降だけを同期
uv run gh-work-track sync
uv run gh-work-track daily --date 2026-09-15
```

通常の `sync` は incremental モードで動作し、前回成功した sync の `finished_at` を watermark として、その時刻の少し前から取得します。初回は bootstrap として直近 7 日を対象にします。

### 日常運用: `sync`

引数なしの `sync` を定期的に実行してください。境界付近の取りこぼしを防ぐため、前回成功 sync の時刻より 5 分前から取得する overlap buffer（`SYNC_OVERLAP_MINUTES=5`）を使います。重複したイベントは `dedup_key` で統合されます。

GitHub 側の discovery や通知が遅れてスレッドに載る場合は、次回の `sync` で拾います。

incremental sync では、スレッドごとの取得開始境界として記録した `last_synced_at` と DB 内の最新イベント時刻も cutoff の下限として使います。取得開始後に発生したイベントは次回の対象に残ります。timeline に cutoff 以降の activity がないスレッドでは comments の取得を省略します。GitHub の timeline API は返却順を保証していないため、新→古と確認できたページだけ early stop を行い、古→新または順序不明の場合は取りこぼし防止のため全ページを取得します。

### 修復・初回: `sync --since N`

任意期間を取り直すときや初回のバックフィルには `sync --since N` を使います。これは backfill 専用で、watermark を使わず直近 `N` 日（`N >= 1`）を取得します。完了後の日常運用は、引数なしの `sync` に戻してください。

`collect` は `sync` の別名です。どちらも同じ incremental / backfill の動作と出力メタデータを持ちます。

```bash
# 修復・初回のバックフィル例
uv run gh-work-track sync --since 7

# 一覧・深堀り
uv run gh-work-track list --since 7
uv run gh-work-track drill 170102
uv run gh-work-track watch list
```

## LanceDB 設計（search-docs 踏襲）

- **PyArrow スキーマ**で `create_table`
- **`open_table` は DB クラス内でキャッシュ**（毎回 open しない）
- **`merge_insert`** で `dedup_key` / `thread_key` の upsert
- **scalar index**（`date`, `thread_key`）で日次 pivot を高速化
- **read_only 接続**時は `read_consistency_interval=5s`（将来の並行読み取り向け）

### テーブル

| テーブル | 用途 |
|---|---|
| `events` | GitHub イベント（dedup_key 一意） |
| `threads` | watch / last-seen / 同期メタ |
| `sync_runs` | collect 実行ログ |

## 開発

```bash
uv sync --group dev
uv run pytest -v
```

## ライセンス

MIT
