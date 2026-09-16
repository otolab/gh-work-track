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

```bash
# 初回
uv run gh-work-track init
uv run gh-work-track migrate

# 同期・日次
uv run gh-work-track sync --since 1
uv run gh-work-track daily --date 2026-09-15

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
