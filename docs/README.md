# gh-work-track ドキュメント

| 文書 | 内容 |
|---|---|
| [architecture.md](architecture.md) | `sync` パイプライン、discovery → 収集 → 永続化の全体像 |
| [github-api-design.md](github-api-design.md) | GitHub API の制限と、それに対する設計・実装方針 |

利用者向けの Quick start と設定はリポジトリ直下の [README.md](../README.md) を参照してください。

実装の定数・分岐の正本は `src/gh_work_track/github_ops.py` です。本文書は **意図と trade-off** の説明です。
