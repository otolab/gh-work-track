# アーキテクチャ

gh-work-track は GitHub 上の Issue/PR 活動を **ThreadRef**（`repo` + `number` + `kind`）単位で発見し、各スレッドの timeline / comments からイベントを取り込み、LanceDB に upsert する CLI です。

## データフロー

```mermaid
flowchart TB
  subgraph discovery [Discovery — ThreadRef 列挙]
    N[notifications]
    S[global + per-repo search]
    W[watch list]
    E[Events API 補完]
  end
  subgraph collect [Collection — イベント取得]
    T[timeline API]
    C[comments API]
  end
  subgraph store [Storage]
    L[(LanceDB events / threads / thread_links / sync_runs)]
  end
  N --> Union[ThreadRef union by key]
  S --> Union
  W --> Union
  E --> Union
  Union --> T
  Union --> C
  T --> Dedupe[dedup_key で統合]
  C --> Dedupe
  Dedupe --> L
  T --> Links[意味付き link 抽出]
  Links --> L
```

### 2 段階に分かれる理由

GitHub には「ユーザーが関与した全スレッドを期間指定で一覧する」単一 API がありません。そのため:

1. **Discovery** — 複数経路で `ThreadRef` を union する（詳細は [github-api-design.md](github-api-design.md)）
2. **Collection** — 各スレッドについて timeline / comments をページング取得し、`events` テーブル用レコードに変換する

Discovery で拾ったスレッド数が増えると、Collection の API 呼び出しも増えます。incremental sync と per-thread cutoff（README 参照）で Collection コストを抑えます。

## sync のモード

| モード | 起動 | cutoff | watermark |
|---|---|---|---|
| **incremental** | `sync`（引数なし） | 前回成功 run の開始時刻 − `overlap_minutes` | 成功時に更新 |
| **backfill** | `sync --since N` | 直近 N 日 | 更新しない（修復用） |

失敗時に watermark を進めるかどうかは **ソースごとに異なります**（search / notifications 失敗は sync 全体を失敗、Events 失敗は warning のみ）。理由は [github-api-design.md#失敗時ポリシー](github-api-design.md#失敗時ポリシー) を参照。

## 主要コンポーネント

| モジュール | 役割 |
|---|---|
| `github_ops.py` | `gh` / `gh api` 呼び出し、discovery、collection、CLI コマンド |
| `config.py` | `mine_repos` / `search_orgs` / sync 設定 |
| `db.py` | LanceDB、`merge_insert`、`sync_runs` watermark、thread links |
| `work_groups.py` | 有向 parent 辺から WorkGroup anchor を解決 |
| `cli.py` | 引数と設定の解決 |

## ThreadRef

```text
ThreadRef(repo="owner/name", number=42, kind="issue"|"pr")
key = "owner/name#42"   # union / dedupe に使用
```

複数 discovery 経路が同じスレッドを返しても `refs[key]` で 1 件にまとめます。

## Thread links と WorkGroup

timeline の `cross-referenced` と、端点を含む enriched payload の
`connected` は、イベントとは別に `thread_links` へ保存します。link の向きは
`from_thread_key` → `to_thread_key`、`rel` は `cross_ref` / `blocks` /
`blocked_by` など、`source` は現在のところ `timeline` です。同じ端点・関係・
source の行は upsert され、再同期しても重複しません。

ただし GitHub REST の [documented `connected` event payload](https://docs.github.com/en/rest/using-the-rest-api/issue-event-types#connected)
には、イベント自身の `id` / `url` や commit 情報はありますが、接続先 Issue/PR
の endpoint はありません。Phase 1 は追加 API を呼ばないため、通常の REST sync
では `connected` から `blocks` / `blocked_by` を推測・保存しません。端点を持たない
最近のイベントは warning に記録されます。端点を含む別経路の payload はライブラリ
の抽出器で扱えますが、REST collector が生成するものではありません。

WorkGroup の anchor 解決に使うのは意味が明確な `parent` 辺だけです。
Phase 1 の parent 辺は子 → 親の向きで、親にイベントがなくても anchor として
表示できます。Phase 1 では `parent_issue_url` を自動で parent 辺へ昇格しません。
したがって MAILGUN のようにそのフィールドが `source.issue` に存在しても、実 sync
で自動ロールアップは発生せず、Phase 2 まで待つ必要があります。手動で保存した
parent 辺は通常どおり解決できます。`cross_ref` や依存辺は `related` として表示
しますが、同じグループにはしません。parent 辺が循環する場合は watch 登録、種別、
番号の順で deterministic に anchor を選びます。無向 connected components /
union-find は使いません。

通常の `daily` は従来どおりフラットです。`daily --group anchor`（`epic` も可）
だけが日次イベントを anchor 配下へネストし、JSON では各イベントに
`group_anchor` / `group_role` / `related` を追加します。`list` と `drill` は
group anchor と関連スレッドを表示します。

## 関連

- [GitHub API 利用設計](github-api-design.md)
- [README — 使い方](../README.md)
