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

## 設定

設定ファイルは `~/.config/gh-work-track/config.yaml` から読み込みます。`XDG_CONFIG_HOME` が設定されている場合は `$XDG_CONFIG_HOME/gh-work-track/config.yaml` を使い、`GH_WORK_TRACK_CONFIG` で任意のパスに変更できます。ファイルがない場合は、従来のデフォルト値を使います。

```yaml
default_repo: owner/repo
search_orgs:
  - owner
mine_repos:
  - owner/repo-a
  - owner/repo-b
sync:
  bootstrap_days: 7
  overlap_minutes: 5
```

`default_repo` は数字だけの ref（例: `drill 123`）の解決先です。`search_orgs` は sync のグローバル search を指定 organization に絞る任意設定で、未設定なら organization を限定しません。`mine_repos` は `list` の自分の open Issue/PR と、グローバル search を補完する追加の per-repo search の対象です。グローバル search が主経路なので、活動を見つけるためだけに全 repo を `mine_repos` へ列挙する必要はありません。`bootstrap_days` は watermark がない初回の incremental sync、`overlap_minutes` は前回成功 sync からの巻き戻し幅に使います。

設定値ごとの環境変数と CLI フラグは次のとおりです。`--mine-repo` と `--search-org` は複数回指定できます。リスト型の環境変数はカンマ区切りで指定してください。

| 設定 | 環境変数 | CLI フラグ |
|---|---|---|
| `default_repo` | `GH_WORK_TRACK_DEFAULT_REPO` | `--default-repo OWNER/REPO` |
| `search_orgs` | `GH_WORK_TRACK_SEARCH_ORGS` | `--search-org ORG` |
| `mine_repos`（追加 per-repo 経路） | `GH_WORK_TRACK_MINE_REPOS` | `--mine-repo OWNER/REPO` |
| `sync.bootstrap_days` | `GH_WORK_TRACK_SYNC_BOOTSTRAP_DAYS` | `--bootstrap-days N` |
| `sync.overlap_minutes` | `GH_WORK_TRACK_SYNC_OVERLAP_MINUTES` | `--overlap-minutes N` |

値の優先順位は **フラグ > env > config > default** です。`sync --since N` は設定された bootstrap 日数ではなく、明示的な N 日の backfill として動作します。

### sync の discovery とレート制限

`sync` は `gh search` のグローバル検索を主経路として、次の条件を直近の更新日時に適用します。

- Issue: `author` / `assignee` / `commenter`
- Pull request: `author` / `assignee` / `reviewed-by` / `commenter`

`search_orgs` を設定すると organization ごとに検索し、設定しない場合は全 organization が対象です。グローバル検索の結果に加えて、`mine_repos` に設定した repo も従来型の per-repo 検索で確認し、重複を統合します。

追加 repo 用に `extra_repos` は導入しません。既存の `mine_repos` を追加経路として利用できます。

検索結果は 1 クエリあたり GitHub の上限 1,000 件までです。各 qualifier の取得件数を sync の warning に出力し、900 件以上の場合は結果が切り捨てられる可能性を警告します。backfill（`sync --since N`）の検索は直列に実行し、呼び出し間隔を空けます。検索が 429/403 になった場合は Retry-After を使って上限回数まで再試行し、解消しなければ sync を失敗させます。失敗した sync は watermark を進めません。

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

通常の `sync` は incremental モードで動作し、前回成功した sync の取得開始時刻を global cutoff の基準として、設定された overlap 分前から取得します。前回成功 run の完了時刻は出力 metadata の `watermark` に保持します。初回は設定された bootstrap 日数を対象にします。

### 日常運用: `sync`

引数なしの `sync` を定期的に実行してください。境界付近の取りこぼしを防ぐため、前回成功 sync の取得開始時刻より設定された分数前から取得する overlap buffer（デフォルト `overlap_minutes=5`）を使います。重複したイベントは `dedup_key` で統合されます。

GitHub 側の discovery や通知が遅れてスレッドに載る場合は、次回の `sync` で拾います。

incremental sync では、前回成功 run の取得開始時刻を global cutoff の基準にし、スレッドごとの取得開始境界として記録した `last_synced_at` と DB 内の最新イベント時刻も cutoff の下限として使います。run が overlap を超えて長時間かかっても、スレッド取得開始後から run 完了までに発生したイベントは次回の対象に残ります。timeline に cutoff 以降の activity がないスレッドでは comments の取得を省略します。GitHub の timeline API は返却順を保証していないため、新→古と確認できたページだけ early stop を行い、古→新または順序不明の場合は取りこぼし防止のため全ページを取得します。

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
