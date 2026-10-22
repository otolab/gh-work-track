# GitHub API 利用設計

gh-work-track が GitHub API をどう使い、制限にどう対応するかをまとめます。Issue #14（global search + Events API 補完）以降の設計判断の正本です。

## 目的

| やりたいこと | やらないこと |
|---|---|
| 認証ユーザーに関係する Issue/PR **スレッド**を見つける | GitHub 上の全操作を完全履歴として再現する |
| cutoff 以降の timeline / comments を LanceDB に蓄積 | GraphQL `contributionsCollection` ベースの discovery |

「ユーザー行動の一覧」そのものが目的ではなく、**daily / drill で引けるイベント列**を安定して作ることが目的です。

## API 呼び出しを抑える日常運用

`sync` は notifications / search による discovery の後、対象スレッドごとに timeline、必要に応じて comments と metadata を取得します。対象スレッドが多い run は API 呼び出しがまとまって発生するため、同期と調査を次のように分けます。

- 1 セッションで `sync` は 1 回に留める。過去期間の確認だけを理由に再 sync / backfill しない。
- `sync` 後の `list` は必要な場合だけ実行する。`list` は通知を取得し、`mine` では repo ごとに `gh issue list` / `gh pr list` で open 項目を列挙する。この列挙は Search API ではなく、mine の候補ごとに REST metadata を取り直すこともない。通知スレッドと、通知に含まれない watch 対象は `enrich_thread` から REST metadata を取得する。
- 保存済みイベントの確認は `daily` を使う。`daily --date YYYY-MM-DD` と `daily --since N` は LanceDB のみを読み、GitHub API を呼ばない。`--group anchor` / `--group epic` も保存済み thread links から解決する。
- 過去期間や日別件数の調査は LanceDB の read-only query を優先し、`daily` は必要な日だけ実行する。
- 恒久保存する日報は同期後の `daily` 出力からローカル export する。export のために追加取得を行わない。

### Issue #24 の観測

2026-10-22 時点で手元に保存された `sync_runs` 14 件（成功12、失敗2）のうち、成功した incremental 9 件は21〜643秒、対象11〜349スレッドでした。2026-10-21 の run は349スレッド・2,437イベントで約10分43秒です。成功した backfill 3 件は114〜726秒、76〜399スレッドで、396〜399スレッドを扱った2件は約12分でした。これは一利用者の保存履歴にある実測例で、次回の所要時間や安全な対象数を保証する見積もりではありません。

query ごとの warning が残る run では、Search の論理 `gh search` 呼び出しは各28回（global 7、repo 21）でした。これは gh コマンド数であり、gh 内部のページングを含む HTTP リクエスト総数ではありません。経路別の API ページ数は `sync_runs` に記録されていません。成功 run の warnings に retry 記録はありませんでしたが、失敗 run は開始時の空 warnings が失敗記録にも残るため、warnings が空であることは retry 不在の根拠になりません。失敗原因を Search / Core、429 / secondary rate limit、Retry-After 別に照合できる構造化記録もありません。

Issue #23 に記録された実セッションでは sync の後に `list` と複数日の `daily` が実行され、別の `gh` 作業への rate limit 波及が報告されました。`daily` はローカル DB のみを読む一方、`list` は通知と `mine` の open 一覧取得を行い、通知や watch 対象には REST metadata 取得も伴います。セッション内の正確な回数や各操作の寄与、許容できる待ち時間は分かっていません。

以上から、事前見積もり・proactive throttling・cooldown の水準はまだ決められません。固定の待ち時間も設けず、既存 retry / backoff と上記の運用指針を使います。追加抑制策を判断するには、経路別リクエスト数、同期中の rate limit 応答と待ち時間、実際のセッション頻度・許容待ち時間を分けて記録する必要があります。

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
| `repos/{repo}/issues/{n}` | `parent_issue_url` と最小 thread metadata | incremental は新規/未充足 thread、backfill は再取得 |
| GraphQL `Issue.subIssues` | watch 親・backfill の子 discovery 補完 | 通常の child metadata lookup では呼ばない |

REST metadata で子の `parent_issue_url` が見つかった場合は、子 → 親の
`thread_links`（`rel=parent`, `source=metadata`, high confidence）を保存し、親を
timeline collection なしで `threads` に登録します。親への自動 watch は行いません。
同一親に複数の子がある場合も親 metadata fetch は一度だけです。

timeline / comments のページングコストは discovery 改善後の主要ボトルネックです（Issue #14 本文参照）。

metadata fetch で得た Issue/PR body は常にキーワード行ベースで解析し、comments body
は上記の条件で comments API を取得したスレッドだけ解析します。`Parent:` / `親:`、
`refs:` / `Related:` / `関連:`、PR の `closes` / `fixes` と GitHub の full URL を
`thread_links` の `source=body` として保存し、マッチ行の `evidence` を残します。
body から見つけた別 repo の ThreadRef は link の端点になるだけで、discovery の union
へは自動投入しません。公式 `parent_issue_url` と本文の Parent が食い違うときは
warning を出し、WorkGroup では公式 metadata link を優先します。

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
