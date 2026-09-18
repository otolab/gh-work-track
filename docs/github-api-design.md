# GitHub API 利用設計

gh-work-track が GitHub API をどう使い、制限にどう対応するかをまとめます。Issue #14（global search + Events API 補完）以降の設計判断の正本です。

## 目的

| やりたいこと | やらないこと |
|---|---|
| 認証ユーザーに関係する Issue/PR **スレッド**を見つける | GitHub 上の全操作を完全履歴として再現する |
| cutoff 以降の timeline / comments を LanceDB に蓄積 | GraphQL `contributionsCollection` ベースの discovery |

「ユーザー行動の一覧」そのものが目的ではなく、**daily / drill で引けるイベント列**を安定して作ることが目的です。

## Discovery 経路

`collect_event_records()`（`github_ops.py`）が次を `ThreadRef.key` で union します。

| 経路 | 優先度 | API | 役割 |
|---|---|---|---|
| **notifications** | 標準 | `GET /notifications` | 通知が来たスレッド |
| **global search** | **主軸** | `gh search issues/prs` | author / assignee / commenter / reviewed-by |
| **per-repo search** | 追加 | 同上 + `--repo` | `mine_repos` 後方互換 |
| **watch** | 追加 | ローカル watch リスト | 明示 watch |
| **Events API** | **補完** | `GET /users/{username}/events` | search で拾えなかったスレッド |

`search_orgs` 未設定時は org 制限なし。設定時は global search に `org:` を付与します。

### global search（主軸）

**qualifier（Issue / PR 共通パターン）**

| kind | qualifier |
|---|---|
| issues | `author`, `assignee`, `commenter` |
| prs | `author`, `assignee`, `reviewed-by`, `commenter` |

**GitHub の制限**

| 制限 | 値 | 影響 |
|---|---|---|
| 1 クエリの結果上限 | **1,000 件** | それ以上は切り捨て |
| レート | Search API 制限 | 429 / secondary rate limit |

**実装での対応**（定数は `github_ops.py`）

| 対応 | 内容 |
|---|---|
| 件数 warning | 900 件以上で truncation 警告（`SEARCH_RESULT_WARNING_THRESHOLD`） |
| backfill 直列化 | `sync --since N` 時は search を直列 + **2 秒間隔**（`SEARCH_INTERVAL_SECONDS`） |
| 429 / 403 リトライ | Retry-After または backoff、最大 3 回（`SEARCH_MAX_RETRIES`） |
| 失敗時 | `SyncCollectionError` → **sync 失敗・watermark 未更新** |

search は discovery の主軸のため、失敗時に watermark を進めません。

### Events API（補完）

**エンドポイント:** `users/{username}/events?per_page=100`（`gh api --paginate`）

PullRequest / Issue / comment / review 系イベントから `ThreadRef` を抽出します。

**GitHub の制限**

| 制限 | 値 | 影響 |
|---|---|---|
| **取得できる総数** | **最新 ~300 イベント** | paginate してもこれ以上は返らない |
| 日付フィルタ | **なし** | cutoff より前はクライアント側で破棄 |
| レート | Core API（5,000 req/h 等） | search より余裕が大きいことが多い |

#### 「300 件」の意味（重要）

- **1 回の API 呼び出しの上限ではない**
- 認証ユーザーの **公開イベント履歴のうち、最新側から最大約 300 件だけ** が API から見える
- `per_page=100` × 3 ページ相当で打ち切られる（GitHub REST の仕様）
- **1 コメント ≒ 1 イベント** として枠を消費する（IssueCommentEvent 等）
- 枠は **全 GitHub 活動で共有**（他 repo の push、大量 bot コメントも同じ 300 件に含まれる）

高活動量アカウント（CI bot、agents-ensemble のような大量 Issue/PR コメントなど）では、**数時間〜1 日未満で 300 件窓が埋まり**、それより古いイベントは Events 経路では永久に見えません。

**実装での対応**

| 対応 | 内容 |
|---|---|
| cutoff フィルタ | `created_at < cutoff` をローカルで除外 |
| 上限 warning | 270 件以上で警告（`EVENTS_RESULT_WARNING_THRESHOLD` / `EVENTS_RESULT_LIMIT`） |
| cutoff 除外が半分以上 | 「サーバー側日付フィルタなし」warning |
| 失敗時 | **warning のみ** — search / notifications が成功していれば sync 継続・watermark 更新 |

Events は **完全な activity history ではない** ため、主軸の search とセットで使います。incremental sync を定期的に回し、窓から落ちる前に search + watermark で取り込む運用を前提とします。

## Collection（discovery 後）

各 `ThreadRef` について:

| API | 用途 | 注意 |
|---|---|---|
| `repos/{repo}/issues/{n}/timeline` | レビュー・状態変更等 | 返却順非保証 → 順序不明時は全ページ取得 |
| `repos/{repo}/issues/{n}/comments` | コメント本文 | timeline に recent activity がなければ省略可 |

timeline / comments のページングコストは discovery 改善後の主要ボトルネックです（Issue #14 本文参照）。

## 失敗時ポリシー

| ソース | 失敗時 | watermark |
|---|---|---|
| notifications | `SyncCollectionError` | 進めない |
| search | `SyncCollectionError`（429 枯渇等） | 進めない |
| Events | warning のみ | search 等が成功なら **進める** |
| 個別スレッド timeline / comments | `SyncCollectionError` | 進めない |

Events は補完経路なので、search が成功している run を Events 失敗だけで無効化しません。

## 採用していない API

### GraphQL `contributionsCollection`

Issue #14 **非目標**として明示。

| 観点 | 説明 |
|---|---|
| 向いている用途 | 期間内の commit / 作成 Issue・PR / review 等の **contribution 集計**（プロフィール graph と同系） |
| discovery に弱い理由 | **comment 主体**の関与を ThreadRef 単位で安定列挙しにくい |
| 採用しない理由 | 既存の `gh search` + REST Events で Issue #14 の受け入れ条件を満たせる。GraphQL スタック追加のコストに見合わない |

将来、古い期間の commit / open / review を period バックフィルしたい **別 Issue** なら検討余地はあります。commenter discovery の代替にはなりません。

### `extra_repos`

新キーは追加せず、`mine_repos` を per-repo search の追加経路として維持します。

## 設定との対応

| 設定 | discovery への効果 |
|---|---|
| `search_orgs` | global search に `org:` 付与（未設定 = 制限なし） |
| `mine_repos` | per-repo search の追加 union（全 repo 列挙は不要） |
| `sync.overlap_minutes` | incremental cutoff の巻き戻し（境界取りこぼし防止） |
| `sync.bootstrap_days` | 初回 watermark なし時の遡り日数 |

## sync 出力の warning を読む

| warning の例 | 意味 | 推奨 |
|---|---|---|
| `near the ~300-event limit` | Events 窓が満杯に近い | incremental sync 頻度を見直す。search が主経路であることを確認 |
| `filtered N/M events before cutoff` | 300 件の大半が cutoff より前 | backfill では Events だけに頼らない |
| `900+ results`（search） | 1,000 件 truncation の可能性 | 期間を狭める、`search_orgs` で絞る |
| `events: ...`（エラー文字列） | Events 取得失敗 | search 結果は保存される。原因調査は任意 |

## コード参照

| 定数 / 関数 | ファイル |
|---|---|
| `EVENTS_RESULT_LIMIT`, `fetch_user_event_threads` | `github_ops.py` |
| `SEARCH_*`, `fetch_search_threads`, `_run_search_query` | `github_ops.py` |
| `collect_event_records` | `github_ops.py` |
| `SyncCollectionError` | `github_ops.py` |

## 関連 Issue

- [#14 — global search discovery + Events API 補完](https://github.com/otolab/gh-work-track/issues/14)
- [#15 — 実装 PR（マージ済み）](https://github.com/otolab/gh-work-track/pull/15)
