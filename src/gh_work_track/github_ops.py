#!/usr/bin/env python3
"""GitHub work progress tracker: collect → daily → list → drill.

Usage:
  gh-work-track.py list [--since DAYS] [--all] [--watch] [--json]
  gh-work-track.py drill <ref> [--comments N] [--json]
  gh-work-track.py collect [--since DAYS] [--json]
  gh-work-track.py daily (--date YYYY-MM-DD | --since DAYS) [--group flat|anchor|epic] [--json]
  gh-work-track.py watch {list|add|remove} [ref ...]
  gh-work-track.py mark-seen <ref>   # update last-seen after drill

<ref> formats:
  plaidev/karte-io-systems#170102
  karte-io-systems#170102          (default repo: plaidev/karte-io-systems)
  notif:25602629003
  https://github.com/plaidev/karte-io-systems/pull/167239
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from gh_work_track.config import (
    DEFAULT_MINE_REPOS,
    DEFAULT_REPO as CONFIG_DEFAULT_REPO,
    DEFAULT_SYNC_BOOTSTRAP_DAYS,
    DEFAULT_SYNC_OVERLAP_MINUTES,
    db_path as default_db_path,
    resolve_config,
)
from gh_work_track.session import get_session
from gh_work_track.work_groups import WorkGroupAssignment, resolve_work_groups

# Kept as public aliases for callers that imported the old constants. Runtime
# behavior resolves the config file and overrides through ``resolve_config``.
DEFAULT_REPO = CONFIG_DEFAULT_REPO
SYNC_BOOTSTRAP_DAYS = DEFAULT_SYNC_BOOTSTRAP_DAYS
SYNC_OVERLAP_MINUTES = DEFAULT_SYNC_OVERLAP_MINUTES
SYNC_SINCE_HELP = (
    "バックフィル専用。省略時は前回成功 sync 以降の incremental（初回は設定された bootstrap 日数）"
)
MINE_REPOS = list(DEFAULT_MINE_REPOS)

# GitHub search returns at most 1,000 results for one query.  Ask gh for the
# largest useful page so a high result count can be surfaced to the caller.
SEARCH_RESULT_LIMIT = 1000
SEARCH_RESULT_WARNING_THRESHOLD = 900
SEARCH_INTERVAL_SECONDS = 2.0
SEARCH_MAX_RETRIES = 3
SEARCH_RETRY_BACKOFF_SECONDS = 1.0
SEARCH_MAX_RETRY_WAIT_SECONDS = 300.0
EVENTS_RESULT_LIMIT = 300
EVENTS_RESULT_WARNING_THRESHOLD = 270

# Body links are intentionally weaker than REST metadata/timeline links.  The
# values are kept as floats because ``thread_links.confidence`` is the public
# storage contract and callers can use them for filtering.
BODY_PARENT_CONFIDENCE = 0.3
BODY_REFERENCE_CONFIDENCE = 0.6
BODY_CLOSES_CONFIDENCE = 0.6

REASON_JA = {
    "review_requested": "レビュー依頼",
    "mention": "メンション",
    "assign": "アサイン",
    "state_change": "ステータス変更",
    "author": "自分の投稿への反応",
    "comment": "コメント",
    "subscribed": "購読中の更新",
    "team_mention": "チームメンション",
    "security_alert": "セキュリティ",
    "invitation": "招待",
    "manual": "手動",
}

SKIP_REASONS = {"ci_activity"}


class SyncCollectionError(RuntimeError):
    """A source failure that makes a sync unsuitable as a watermark."""


@dataclass
class ThreadRef:
    repo: str
    number: int
    kind: str = "issue"  # issue | pr

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"

    @property
    def web_url(self) -> str:
        path = "pull" if self.kind == "pr" else "issues"
        return f"https://github.com/{self.repo}/{path}/{self.number}"


@dataclass(frozen=True)
class ThreadSyncBoundary:
    """The start boundary of one thread's GitHub collection attempt."""

    ref: ThreadRef
    started_at: datetime


@dataclass
class ThreadSummary:
    ref: ThreadRef
    title: str = ""
    state: str = ""
    updated_at: str = ""
    reasons: list[str] = field(default_factory=list)
    unread: bool = False
    notif_ids: list[str] = field(default_factory=list)
    subject_type: str = ""
    latest_author: str = ""
    latest_snippet: str = ""
    review_decision: str = ""
    labels: list[str] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    is_watched: bool = False
    note: str = ""
    is_new_since_seen: bool = False
    sources: list[str] = field(default_factory=list)
    group_anchor: str = ""
    group_role: str = ""
    related: list[str] = field(default_factory=list)


def gh_api(path: str, *, paginate: bool = False) -> Any:
    cmd = ["gh", "api", path]
    if paginate:
        cmd.insert(2, "--paginate")
        cmd.insert(3, "--slurp")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"gh api timed out: {path}") from exc
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"gh api failed: {path}")
    text = result.stdout.strip()
    if not text:
        return None
    parsed = json.loads(text)
    if paginate and isinstance(parsed, list) and parsed and all(
        isinstance(page, list) for page in parsed
    ):
        return [item for page in parsed for item in page]
    return parsed


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def parse_ref(raw: str, *, default_repo: str | None = None) -> ThreadRef:
    raw = raw.strip()
    configured_default_repo = (
        default_repo if default_repo is not None else resolve_config().default_repo
    )
    if raw.startswith("notif:"):
        notif = gh_api(f"notifications/threads/{raw.split(':', 1)[1]}")
        return thread_ref_from_notification(notif)

    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 3 and parts[0] in ("plaidev", "otolab"):
            owner, repo, kind, num = parts[0], parts[1], parts[2], int(parts[3])
            return ThreadRef(f"{owner}/{repo}", num, "pr" if kind == "pull" else "issue")
        raise ValueError(f"unsupported URL: {raw}")

    m = re.fullmatch(r"(?:([A-Za-z0-9_.-]+)/)?([A-Za-z0-9_.-]+)#(\d+)", raw)
    if m:
        owner, repo, num = m.group(1), m.group(2), int(m.group(3))
        default_owner = configured_default_repo.split("/", 1)[0]
        full_repo = f"{owner}/{repo}" if owner else f"{default_owner}/{repo}"
        return ThreadRef(full_repo, num)

    if re.fullmatch(r"\d+", raw):
        return ThreadRef(configured_default_repo, int(raw))

    raise ValueError(f"cannot parse ref: {raw}")


def thread_ref_from_notification(notif: dict[str, Any]) -> ThreadRef:
    repo = notif["repository"]["full_name"]
    subject = notif.get("subject", {})
    subject_type = subject.get("type", "")
    url = subject.get("url") or ""
    if url:
        # https://api.github.com/repos/OWNER/REPO/issues/123 or pulls/123
        m = re.search(r"/repos/([^/]+/[^/]+)/(issues|pulls)/(\d+)", url)
        if m:
            return ThreadRef(m.group(1), int(m.group(3)), "pr" if m.group(2) == "pulls" else "issue")
    title = subject.get("title", "")
    m = re.search(r"#(\d+)\b", title)
    if m:
        kind = "pr" if subject_type == "PullRequest" else "issue"
        return ThreadRef(repo, int(m.group(1)), kind)
    raise ValueError(f"notification has no resolvable thread: {notif.get('id')}")


def should_skip_notification(notif: dict[str, Any]) -> bool:
    if notif.get("reason") in SKIP_REASONS:
        return True
    repo = notif["repository"]["full_name"]
    title = notif.get("subject", {}).get("title", "")
    if repo == "plaidev/web-ecosystem-research" and title.startswith("[auto] update"):
        return True
    return False


def fetch_notifications(unread_only: bool = False, since: str | None = None) -> list[dict[str, Any]]:
    """GitHub notifications API.

    Default API = unread + participating のみ（最大50件程度）。
    unread_only=False なら all=true で既読も含める。
    since を渡すと API 側で期間フィルタ（全件 paginate より高速）。
    """
    query = "notifications?per_page=100"
    if not unread_only:
        query += "&all=true"
    if since:
        query += f"&since={since}"
    return gh_api(query, paginate=True) or []


def gh_cli_json(args: list[str]) -> Any:
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"gh command timed out: {' '.join(args)}") from exc
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "gh command failed")
    return json.loads(result.stdout or "null")


SUB_ISSUES_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      subIssues(first: 100) {
        nodes {
          number
          title
          updatedAt
          repository { nameWithOwner }
        }
      }
    }
  }
}
""".strip()


def fetch_sub_issues(ref: ThreadRef) -> list[ThreadRef]:
    """Return direct sub-issues for one parent using the GraphQL API.

    REST issue metadata exposes a child's ``parent_issue_url`` but does not
    enumerate a parent's children.  This intentionally small GraphQL path is
    used only for explicit watch/backfill discovery, not for every metadata
    lookup.
    """
    try:
        owner, name = ref.repo.split("/", 1)
    except ValueError as exc:
        raise RuntimeError(f"invalid repository for sub-issues: {ref.repo}") from exc
    result = gh_cli_json([
        "api",
        "graphql",
        "-f",
        f"query={SUB_ISSUES_QUERY}",
        "-f",
        f"owner={owner}",
        "-f",
        f"name={name}",
        "-F",
        f"number={ref.number}",
    ])
    if not isinstance(result, dict):
        raise RuntimeError("expected a JSON object from GraphQL subIssues")

    errors = result.get("errors")
    if errors:
        if isinstance(errors, list):
            messages = []
            for error in errors:
                if isinstance(error, dict):
                    message = error.get("message")
                else:
                    message = error
                if message:
                    messages.append(str(message))
            detail = "; ".join(messages) or repr(errors)
        else:
            detail = str(errors)
        raise RuntimeError(f"GraphQL subIssues API error: {detail}")

    data = result.get("data", result)
    if not isinstance(data, dict):
        raise RuntimeError("GraphQL subIssues response has no data")
    repository = data.get("repository")
    if not isinstance(repository, dict):
        raise RuntimeError("GraphQL subIssues response has no repository")
    issue = repository.get("issue")
    if not isinstance(issue, dict):
        raise RuntimeError("GraphQL subIssues response has no issue")
    connection = issue.get("subIssues")
    if not isinstance(connection, dict):
        raise RuntimeError("GraphQL subIssues response has no subIssues connection")
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        raise RuntimeError("expected subIssues.nodes to be an array")

    refs: dict[str, ThreadRef] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        try:
            number = int(node.get("number"))
        except (TypeError, ValueError):
            continue
        if number <= 0:
            continue
        repository_value = node.get("repository")
        if isinstance(repository_value, dict):
            repo = str(repository_value.get("nameWithOwner", ""))
        else:
            repo = ""
        repo = repo or ref.repo
        child = ThreadRef(repo, number, "issue")
        refs[child.key] = child
    return list(refs.values())


def fetch_mine_open(repos: Sequence[str] | None = None) -> list[ThreadSummary]:
    """自分の open PR/Issue。通知が来ていない作業の補完用。list API の結果をそのまま使い API 呼び出しを抑える。"""
    selected_repos = (
        list(repos) if repos is not None else list(resolve_config().mine_repos)
    )
    summaries: list[ThreadSummary] = []
    seen: set[str] = set()
    for repo in selected_repos:
        for subcmd, kind in (("pr", "pr"), ("issue", "issue")):
            items = gh_cli_json([
                subcmd, "list", "--repo", repo, "--author", "@me",
                "--state", "open", "--limit", "50",
                "--json", "number,title,updatedAt,state",
            ])
            if not items:
                continue
            for item in items:
                ref = ThreadRef(repo, item["number"], kind)
                if ref.key in seen:
                    continue
                seen.add(ref.key)
                summaries.append(ThreadSummary(
                    ref=ref,
                    title=item.get("title", ""),
                    state=item.get("state", ""),
                    updated_at=item.get("updatedAt", ""),
                    sources=["mine"],
                    is_new_since_seen=is_new_since_seen(ref, item.get("updatedAt", "")),
                ))
    return summaries


def load_watch() -> list[dict[str, Any]]:
    return get_session().load_watch_entries()


def save_watch(threads: list[dict[str, Any]]) -> None:
    get_session().save_watch_entries(threads)


def mark_seen(ref: ThreadRef, activity_at: str | None = None) -> None:
    get_session().mark_seen(ref.repo, ref.number, ref.kind, activity_at)


def is_new_since_seen(ref: ThreadRef, updated_at: str) -> bool:
    return get_session().is_new_since_seen(ref.repo, ref.number, updated_at)


def snippet(text: str, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def json_safe_value(value: Any) -> Any:
    """Convert API/DB values into values accepted by ``json.dumps``.

    LanceDB returns timestamp columns as ``pandas.Timestamp`` instances.
    Those are datetime-like but are not JSON serializable by the standard
    library.  Keep this small recursive normalizer at the CLI boundary so
    stored link rows remain native values for markdown and DB callers.
    """
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe_value(item) for item in value]
    item_method = getattr(value, "item", None)
    if callable(item_method):
        try:
            scalar = item_method()
        except (TypeError, ValueError):
            scalar = value
        if scalar is not value:
            return json_safe_value(scalar)
    return value


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def event_date(timestamp: str) -> str:
    parsed = parse_timestamp(timestamp)
    return parsed.date().isoformat() if parsed else timestamp[:10]


def event_actor(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "?"
    if payload.get("login"):
        return str(payload["login"])
    for field_name in ("actor", "user", "author", "reviewer"):
        value = payload.get(field_name)
        if isinstance(value, dict) and value.get("login"):
            return str(value["login"])
        if isinstance(value, str) and value:
            return value
    return "?"


def event_dedup_key(record: dict[str, Any], source_id: str | None = None) -> str:
    """Return a stable key shared by timeline and comments representations."""
    identity_fields = ("repo", "number", "kind", "event")
    identity = tuple(record.get(field_name, "") for field_name in identity_fields)
    if source_id:
        return "source:" + "|".join(str(part) for part in (*identity, source_id))
    fallback = {
        **{field_name: record.get(field_name, "") for field_name in identity_fields},
        "timestamp": record.get("timestamp", ""),
        "actor": record.get("actor", ""),
        "snippet": record.get("snippet", ""),
    }
    digest = hashlib.sha256(
        json.dumps(fallback, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return "fallback:" + digest


def make_event_record(
    ref: ThreadRef,
    event: str,
    timestamp: str | None,
    actor: str,
    body: str = "",
    *,
    source: str,
    source_id: str | None = None,
) -> dict[str, Any] | None:
    if not timestamp or not parse_timestamp(timestamp):
        return None
    record: dict[str, Any] = {
        "date": event_date(timestamp),
        "timestamp": timestamp,
        "repo": ref.repo,
        "number": ref.number,
        "kind": ref.kind,
        "event": event,
        "actor": actor or "?",
        "snippet": snippet(body),
        "url": ref.web_url,
        "source": source,
    }
    record["dedup_key"] = event_dedup_key(record, source_id)
    return record


def timeline_event_record(ref: ThreadRef, payload: dict[str, Any]) -> dict[str, Any] | None:
    event = str(payload.get("event", "")).strip()
    if not event:
        return None
    timestamp = next(
        (
            str(payload[field_name])
            for field_name in ("created_at", "submitted_at", "updated_at")
            if payload.get(field_name)
        ),
        None,
    )
    body_parts = []
    if payload.get("state"):
        body_parts.append(str(payload["state"]))
    if payload.get("body"):
        body_parts.append(str(payload["body"]))
    assignee = payload.get("assignee") or {}
    if assignee.get("login"):
        body_parts.append(f"assignee @{assignee['login']}")
    label = payload.get("label") or {}
    if label.get("name"):
        body_parts.append(f"label {label['name']}")
    rename = payload.get("rename") or {}
    if rename.get("from") or rename.get("to"):
        body_parts.append(f"{rename.get('from', '')} → {rename.get('to', '')}".strip())
    if payload.get("commit_id"):
        body_parts.append(str(payload["commit_id"])[:12])
    source_id = payload.get("id") or payload.get("node_id") or payload.get("commit_id")
    return make_event_record(
        ref,
        event,
        timestamp,
        event_actor(payload),
        " — ".join(body_parts),
        source="timeline",
        source_id=str(source_id) if source_id else None,
    )


def _thread_ref_from_link_value(
    value: Any,
    *,
    default_repo: str | None = None,
) -> ThreadRef | None:
    """Resolve the issue-like objects returned by timeline link events."""
    if isinstance(value, ThreadRef):
        return value
    if isinstance(value, str):
        match = re.search(
            r"(?:https?://[^/]+/)?(?:repos/)?([^/\s]+/[^/#\s]+)/"
            r"(issues|pulls|pull)/(\d+)",
            value,
        )
        if match:
            return ThreadRef(
                match.group(1),
                int(match.group(3)),
                "pr" if match.group(2) in {"pulls", "pull"} else "issue",
            )
        return None
    if not isinstance(value, dict):
        return None
    for nested_name in ("issue", "pull_request", "subject", "target"):
        nested = value.get(nested_name)
        if isinstance(nested, dict):
            resolved = _thread_ref_from_link_value(
                nested,
                default_repo=default_repo,
            )
            if resolved:
                return resolved

    number = value.get("number")
    if number is None:
        for url_name in ("url", "html_url", "issue_url", "pull_request_url"):
            resolved = _thread_ref_from_link_value(
                value.get(url_name),
                default_repo=default_repo,
            )
            if resolved:
                return resolved
        return None
    try:
        number = int(number)
    except (TypeError, ValueError):
        return None
    repo_value = value.get("repository") or value.get("repo")
    if isinstance(repo_value, dict):
        repo = repo_value.get("full_name") or repo_value.get("name")
        if repo and "/" not in str(repo):
            owner = repo_value.get("owner") or {}
            owner_name = owner.get("login") if isinstance(owner, dict) else owner
            repo = f"{owner_name}/{repo}" if owner_name else repo
    else:
        repo = repo_value
    repo = str(repo or default_repo or "")
    if "/" not in repo:
        return None
    value_type = str(value.get("type", "")).lower()
    kind = "pr" if value.get("pull_request") or value_type in {
        "pullrequest",
        "pull_request",
        "pr",
        "pull",
    } else "issue"
    return ThreadRef(repo, number, kind)


# Only these URL forms are accepted as body references.  Keeping the host
# explicit prevents a random URL containing an ``issues/<number>`` path from
# becoming a thread link, while supporting both browser and REST URLs.
_BODY_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/"
    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pulls?)/\d+"
    r"|https?://api\.github\.com/repos/"
    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pulls?)/\d+",
    re.IGNORECASE,
)
_BODY_BARE_REF_RE = re.compile(r"(?<![A-Za-z0-9_])#([1-9]\d*)\b")
_BODY_KEYWORD_RE = re.compile(
    r"(?P<parent>\bparent\s*[:：]|親\s*[:：])"
    r"|(?P<reference>\brefs?\s*[:：]|\brelated\s*[:：]|関連\s*[:：])"
    r"|(?P<closes>\b(?:closes|fixes)\b)",
    re.IGNORECASE,
)


def _body_link_target_values(text: str, *, repo: str) -> list[ThreadRef]:
    """Return unique URL and same-repository shorthand targets in ``text``."""
    targets: list[ThreadRef] = []
    seen: set[str] = set()

    url_matches = list(_BODY_URL_RE.finditer(text))
    for match in url_matches:
        candidate = match.group(0).rstrip(".,;:!?)]}>\"'")
        target = _thread_ref_from_link_value(candidate, default_repo=repo)
        if target and target.key not in seen:
            seen.add(target.key)
            targets.append(target)

    # Do not interpret a numeric URL fragment (``.../issues/1#2``) as a
    # second same-repository shorthand reference.
    bare_text = _BODY_URL_RE.sub(" ", text)
    for match in _BODY_BARE_REF_RE.finditer(bare_text):
        target = ThreadRef(repo, int(match.group(1)), "issue")
        if target.key not in seen:
            seen.add(target.key)
            targets.append(target)
    return targets


def body_link_records(
    ref: ThreadRef,
    body: str,
    *,
    discovered_at: str | None = None,
) -> list[dict[str, Any]]:
    """Extract conservative, auditable links from issue/PR body text.

    Bare ``#N`` references are considered only on lines with an explicit
    relationship keyword.  Full GitHub Issue/PR URLs may also stand alone and
    default to ``inferred_ref``.  Fenced and indented code is ignored so code
    examples cannot accidentally create links.  The matched line is retained
    as ``evidence`` for the drill view.
    """
    if not body:
        return []

    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    in_fence = False
    for raw_line in str(body).splitlines():
        stripped = raw_line.strip()
        if re.match(r"^(?:```|~~~)", stripped):
            in_fence = not in_fence
            continue
        if in_fence or raw_line.startswith(("    ", "\t")):
            continue

        evidence = snippet(raw_line)
        # Inline code is evidence, not prose.  Mask it before keyword/URL
        # matching while retaining the original line for the audit snippet.
        parse_line = re.sub(
            r"`[^`]*`",
            lambda match: " " * len(match.group(0)),
            raw_line,
        )
        keyword_matches = list(_BODY_KEYWORD_RE.finditer(parse_line))
        segments: list[tuple[str, str]] = []
        if keyword_matches:
            for index, match in enumerate(keyword_matches):
                end = (
                    keyword_matches[index + 1].start()
                    if index + 1 < len(keyword_matches)
                    else len(parse_line)
                )
                kind = match.lastgroup or "reference"
                segments.append((kind, parse_line[match.end():end]))
        elif _BODY_URL_RE.search(parse_line):
            # A URL without a keyword is still useful as a low-risk explicit
            # cross-repository reference.  Bare #N remains disallowed here.
            segments.append(("reference", parse_line))
        else:
            continue

        for keyword, segment in segments:
            if keyword == "parent":
                rel = "inferred_parent"
                confidence = BODY_PARENT_CONFIDENCE
            elif keyword == "closes":
                # GitHub's closing syntax is meaningful for PR bodies.  Do
                # not turn an Issue's prose into a closing edge.
                if ref.kind != "pr":
                    continue
                rel = "closes"
                confidence = BODY_CLOSES_CONFIDENCE
            else:
                rel = "inferred_ref"
                confidence = BODY_REFERENCE_CONFIDENCE

            for target in _body_link_target_values(segment, repo=ref.repo):
                if target.key == ref.key:
                    continue
                key = (target.key, rel)
                if key in seen:
                    continue
                seen.add(key)
                record: dict[str, Any] = {
                    "from_thread_key": ref.key,
                    "to_thread_key": target.key,
                    "rel": rel,
                    "source": "body",
                    "confidence": confidence,
                    "evidence": evidence,
                }
                if discovered_at is not None:
                    record["discovered_at"] = discovered_at
                records.append(record)
    return records


# Public aliases keep the parser discoverable for callers/tests without
# coupling them to the storage-oriented implementation name.
extract_body_links = body_link_records
extract_body_thread_links = body_link_records
parse_body_links = body_link_records
body_thread_link_records = body_link_records


def _link_timestamp(payload: dict[str, Any]) -> str:
    timestamp = next(
        (
            str(payload[field_name])
            for field_name in ("created_at", "submitted_at", "updated_at")
            if payload.get(field_name)
        ),
        None,
    )
    return timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalise_link_relation(value: Any) -> str | None:
    relation = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if relation in {
        "parent",
        "cross_ref",
        "cross_reference",
        "cross_referenced",
        "crossreferenced",
    }:
        return "parent" if relation == "parent" else "cross_ref"
    if relation in {
        "blocks",
        "blocking",
        "blocking_issue",
        "blocking_added",
    }:
        return "blocks"
    if relation in {
        "blocked_by",
        "blockedby",
        "blocked",
        "blocked_issue",
        "blocked_by_added",
    }:
        return "blocked_by"
    if relation in {"closes", "closed_by"}:
        return "closes"
    return None


def _connected_relation(
    ref: ThreadRef,
    target: ThreadRef,
    payload: dict[str, Any],
) -> str:
    containers: list[dict[str, Any]] = [payload]
    for name in ("subject", "source", "target", "connection"):
        value = payload.get(name)
        if isinstance(value, dict):
            containers.append(value)
    for container in containers:
        for field_name in (
            "rel",
            "relation",
            "relationship",
            "connection",
            "direction",
            "kind",
            "type",
        ):
            relation = _normalise_link_relation(container.get(field_name))
            if relation in {"blocks", "blocked_by"}:
                return relation
        if container.get("blocked_by") is True or container.get("is_blocked_by") is True:
            return "blocked_by"
        if container.get("blocking") is True or container.get("is_blocking") is True:
            return "blocks"

    # GitHub's REST ``connected`` payload has historically omitted the
    # relationship label.  Keep a deterministic fallback for those payloads:
    # a PR linked to an issue is treated as the blocking side, while an issue
    # linked to a PR is treated as blocked by that PR.  Fixtures that carry an
    # explicit relationship always take precedence above.
    if ref.kind == "pr" and target.kind == "issue":
        return "blocks"
    if ref.kind == "issue" and target.kind == "pr":
        return "blocked_by"
    return "blocks"


def _connected_target(
    ref: ThreadRef,
    payload: dict[str, Any],
) -> ThreadRef | None:
    """Resolve an endpoint from an endpoint-bearing connected payload."""
    for field_name in (
        "subject",
        "target",
        "connected_issue",
        "blocking_issue",
        "blocked_issue",
        "source",
    ):
        candidate = payload.get(field_name)
        if candidate is None:
            continue
        target = _thread_ref_from_link_value(candidate, default_repo=ref.repo)
        if target:
            return target
    return None


def timeline_link_warning(
    ref: ThreadRef,
    payload: dict[str, Any],
    *,
    cutoff: datetime | None = None,
) -> str | None:
    """Explain why a recent connected event could not produce a link row.

    The documented REST ``connected`` event has no issue/PR endpoint.  Do not
    infer one from the event URL or commit fields; callers get an explicit
    warning instead.  Endpoint-bearing payloads (for example, an adapter
    using a richer API representation) remain supported by the parser.
    """
    event = str(payload.get("event", "")).strip().lower()
    if event != "connected":
        return None
    if cutoff is not None:
        timestamp = _timeline_timestamp(payload)
        if timestamp is None or timestamp < normalize_datetime(cutoff):
            return None
    if _connected_target(ref, payload) is not None:
        return None
    event_id = payload.get("id") or payload.get("node_id") or "unknown"
    return (
        f"{ref.key}: connected timeline event {event_id} has no resolvable "
        "endpoint in the documented REST payload; blocks/blocked_by were not stored"
    )


def timeline_link_records(
    ref: ThreadRef,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Extract directed link rows from one GitHub timeline event.

    The current timeline thread is the ``from`` endpoint.  The relation is
    expressed from that thread's perspective: ``blocked_by`` means the
    current thread is blocked by the target, while ``blocks`` means it blocks
    the target.  No API call is made here; all target data must be in the
    already fetched payload.  The documented REST ``connected`` shape does
    not include that endpoint, so it produces no row; use
    :func:`timeline_link_warning` to report that limitation.
    """
    event = str(payload.get("event", "")).strip().lower()
    discovered_at = _link_timestamp(payload)
    confidence = 1.0
    if event == "cross-referenced":
        source = payload.get("source")
        source_issue = source.get("issue") if isinstance(source, dict) else None
        target = _thread_ref_from_link_value(source_issue, default_repo=ref.repo)
        if not target or target.key == ref.key:
            return []
        return [{
            "from_thread_key": ref.key,
            "to_thread_key": target.key,
            "rel": "cross_ref",
            "source": "timeline",
            "confidence": confidence,
            "discovered_at": discovered_at,
        }]

    if event != "connected":
        return []
    target = _connected_target(ref, payload)
    if not target or target.key == ref.key:
        return []
    relation = _connected_relation(ref, target, payload)
    return [{
        "from_thread_key": ref.key,
        "to_thread_key": target.key,
        "rel": relation,
        "source": "timeline",
        "confidence": confidence,
        "discovered_at": discovered_at,
    }]


# Public aliases make the extraction helper discoverable without coupling
# callers to the storage-oriented name used by the sync implementation.
extract_thread_links = timeline_link_records
extract_timeline_links = timeline_link_records
thread_link_records = timeline_link_records


def timeline_link_record(
    ref: ThreadRef,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the single link represented by a timeline payload, if any."""
    records = timeline_link_records(ref, payload)
    return records[0] if records else None


thread_link_record = timeline_link_record


def comment_event_record(ref: ThreadRef, payload: dict[str, Any]) -> dict[str, Any] | None:
    timestamp = payload.get("created_at") or payload.get("updated_at")
    source_id = payload.get("id") or payload.get("node_id")
    return make_event_record(
        ref,
        "commented",
        str(timestamp) if timestamp else None,
        event_actor(payload),
        str(payload.get("body", "")),
        source="comments",
        source_id=str(source_id) if source_id else None,
    )


def deduplicate_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for event in events:
        key = str(event.get("dedup_key") or event_dedup_key(event))
        event["dedup_key"] = key
        existing = unique.get(key)
        if not existing:
            unique[key] = event
            continue
        if len(str(event.get("snippet", ""))) > len(str(existing.get("snippet", ""))):
            existing["snippet"] = event["snippet"]
        sources = set(existing.get("sources", []))
        if existing.get("source"):
            sources.add(str(existing["source"]))
        if event.get("source"):
            sources.add(str(event["source"]))
        if len(sources) > 1:
            existing["sources"] = sorted(sources)
    return sorted(
        unique.values(),
        key=lambda item: (
            item.get("timestamp", ""),
            item.get("repo", ""),
            int(item.get("number", 0)),
            item.get("event", ""),
            item.get("dedup_key", ""),
        ),
    )


def save_events(events: list[dict[str, Any]]) -> tuple[int, int]:
    return get_session().save_events(deduplicate_events(events))


def save_thread_links(links: list[dict[str, Any]]) -> tuple[int, int]:
    return get_session().save_thread_links(links)


def _thread_group_assignments(
    thread_keys: Sequence[Any] | None = None,
    *,
    warnings: list[str] | None = None,
) -> dict[str, WorkGroupAssignment]:
    session = get_session()
    refs = list(thread_keys or [])
    thread_kinds = {
        value.key: value.kind
        for value in refs
        if isinstance(value, ThreadRef)
    }
    return resolve_work_groups(
        session.load_thread_links(),
        watch=session.load_watch_entries(),
        thread_keys=refs,
        thread_kinds=thread_kinds,
        warnings=warnings,
    )


def _event_thread_key(event: dict[str, Any]) -> str | None:
    repo = str(event.get("repo", ""))
    number = event.get("number")
    try:
        number = int(number)
    except (TypeError, ValueError):
        return None
    if not repo or number <= 0:
        return None
    return f"{repo}#{number}"


def _assignment_for_key(
    key: str,
    assignments: dict[str, WorkGroupAssignment],
) -> WorkGroupAssignment:
    return assignments.get(key, WorkGroupAssignment(key, "anchor", []))


def _apply_group_assignments(
    items: Iterable[ThreadSummary],
    assignments: dict[str, WorkGroupAssignment],
) -> None:
    for item in items:
        if not item.ref.number:
            continue
        assignment = _assignment_for_key(item.ref.key, assignments)
        item.group_anchor = assignment.group_anchor
        item.group_role = assignment.group_role
        item.related = list(assignment.related)


def fetch_issue_or_pr(ref: ThreadRef) -> dict[str, Any]:
    return gh_api(f"repos/{ref.repo}/issues/{ref.number}")


def parent_ref_from_issue(ref: ThreadRef, issue: dict[str, Any]) -> ThreadRef | None:
    """Resolve the official REST ``parent_issue_url`` field, if present."""
    parent_url = issue.get("parent_issue_url") if isinstance(issue, dict) else None
    if not parent_url:
        return None
    parent = _thread_ref_from_link_value(parent_url, default_repo=ref.repo)
    if parent is None or parent.key == ref.key:
        return None
    return parent


def _metadata_thread_needs_fetch(
    ref: ThreadRef,
    *,
    backfill: bool,
    parent_only: bool = False,
) -> bool:
    """Apply the same incremental boundary idea to issue metadata.

    A normal incremental run fetches metadata for newly discovered threads
    (or rows whose title has not been populated).  Backfill deliberately
    refreshes it.  Parent-only rows have no collection watermark, so a
    populated title is the fast-path cache for them.
    """
    existing = get_session().db.get_thread(ref.repo, ref.number)
    if not existing:
        return True
    if not str(existing.get("title", "")):
        return True
    if parent_only:
        return False
    return backfill or not str(existing.get("last_synced_at", ""))


def _metadata_discovered_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _timeline_timestamp(payload: dict[str, Any]) -> datetime | None:
    value = next(
        (
            str(payload[field_name])
            for field_name in ("created_at", "submitted_at", "updated_at")
            if payload.get(field_name)
        ),
        None,
    )
    return parse_timestamp(value)


def _timeline_page_timestamps(page: list[dict[str, Any]]) -> list[datetime] | None:
    if not page:
        return None
    timestamps: list[datetime] = []
    for item in page:
        if not isinstance(item, dict):
            return None
        timestamp = _timeline_timestamp(item)
        if timestamp is None:
            return None
        timestamps.append(timestamp)
    return timestamps


def _timeline_page_order(page: list[dict[str, Any]]) -> str | None:
    """Infer a page's order without trusting undocumented API behavior."""
    timestamps = _timeline_page_timestamps(page)
    if timestamps is None or len(timestamps) < 2:
        return None
    if all(left >= right for left, right in zip(timestamps, timestamps[1:])) and any(
        left > right for left, right in zip(timestamps, timestamps[1:])
    ):
        return "descending"
    if all(left <= right for left, right in zip(timestamps, timestamps[1:])) and any(
        left < right for left, right in zip(timestamps, timestamps[1:])
    ):
        return "ascending"
    return None


def _timeline_page_is_before(page: list[dict[str, Any]], cutoff: datetime) -> bool:
    timestamps = _timeline_page_timestamps(page)
    return timestamps is not None and all(timestamp < cutoff for timestamp in timestamps)


def _timeline_has_unknown_commented_activity(
    timeline: list[dict[str, Any]],
) -> bool:
    return any(
        isinstance(item, dict)
        and str(item.get("event", "")).strip() == "commented"
        and _timeline_timestamp(item) is None
        for item in timeline
    )


def fetch_timeline(
    ref: ThreadRef,
    per_page: int = 30,
    *,
    paginate: bool = False,
    cutoff: datetime | None = None,
) -> list[dict[str, Any]]:
    """Fetch timeline pages, stopping only for proven newest-first order."""
    path = f"repos/{ref.repo}/issues/{ref.number}/timeline?per_page={per_page}"
    if not paginate or cutoff is None:
        return gh_api(path, paginate=paginate) or []

    normalized_cutoff = normalize_datetime(cutoff)
    events: list[dict[str, Any]] = []
    page_number = 1
    order: str | None = None
    while True:
        page = gh_api(f"{path}&page={page_number}") or []
        if not isinstance(page, list):
            break
        events.extend(item for item in page if isinstance(item, dict))
        page_order = _timeline_page_order(page)
        if page_order is None:
            order = "unknown"
        elif order is None:
            order = page_order
        elif order != page_order:
            order = "unknown"
        if order == "descending" and _timeline_page_is_before(page, normalized_cutoff):
            break
        if len(page) < per_page:
            break
        page_number += 1
    return events


def fetch_comments(ref: ThreadRef, limit: int) -> list[dict[str, Any]]:
    comments = gh_api(f"repos/{ref.repo}/issues/{ref.number}/comments?per_page=100") or []
    return comments[-limit:]


def fetch_all_comments(ref: ThreadRef) -> list[dict[str, Any]]:
    """Fetch every comment for collect; drill keeps its bounded output."""
    return gh_api(
        f"repos/{ref.repo}/issues/{ref.number}/comments?per_page=100",
        paginate=True,
    ) or []


def enrich_thread(
    ref: ThreadRef,
    notifs: list[dict[str, Any]] | None = None,
    watch_note: str = "",
    *,
    light: bool = False,
) -> ThreadSummary:
    issue = fetch_issue_or_pr(ref)
    if issue.get("pull_request"):
        ref.kind = "pr"
    summary = ThreadSummary(
        ref=ref,
        title=issue.get("title", ""),
        state=issue.get("state", ""),
        updated_at=issue.get("updated_at", ""),
        labels=[l["name"] for l in issue.get("labels", [])],
        assignees=[a["login"] for a in issue.get("assignees", [])],
        is_watched=bool(watch_note),
        note=watch_note,
    )
    if notifs:
        for n in notifs:
            summary.notif_ids.append(str(n["id"]))
            summary.reasons.append(REASON_JA.get(n.get("reason", ""), n.get("reason", "")))
            if n.get("unread"):
                summary.unread = True
            summary.subject_type = n.get("subject", {}).get("type", summary.subject_type)
        summary.reasons = list(dict.fromkeys(summary.reasons))

    if ref.kind == "pr" and not light:
        try:
            pr = gh_api(f"repos/{ref.repo}/pulls/{ref.number}")
            summary.review_decision = pr.get("mergeable_state", "") or ""
            if pr.get("draft"):
                summary.state = "draft"
            elif pr.get("merged_at"):
                summary.state = "merged"
            else:
                summary.state = pr.get("state", summary.state)
        except RuntimeError:
            pass
        try:
            reviews = gh_api(f"repos/{ref.repo}/pulls/{ref.number}/reviews?per_page=30") or []
            latest = [r for r in reviews if r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED")]
            if latest:
                r = latest[-1]
                summary.latest_author = r.get("user", {}).get("login", "")
                summary.latest_snippet = snippet(f"[{r.get('state')}] {r.get('body', '')}")
        except RuntimeError:
            pass

    if not summary.latest_snippet and not light:
        comments = fetch_comments(ref, 1)
        if comments:
            c = comments[-1]
            summary.latest_author = c.get("user", {}).get("login", "")
            summary.latest_snippet = snippet(c.get("body", ""))

    summary.is_new_since_seen = is_new_since_seen(ref, summary.updated_at)
    return summary


def group_notifications(notifs: list[dict[str, Any]]) -> dict[str, tuple[ThreadRef, list[dict[str, Any]]]]:
    grouped: dict[str, tuple[ThreadRef, list[dict[str, Any]]]] = {}
    for n in notifs:
        if should_skip_notification(n):
            continue
        try:
            ref = thread_ref_from_notification(n)
        except ValueError:
            # non-thread notifications keyed by notif id
            key = f"notif:{n['id']}"
            grouped.setdefault(key, (ThreadRef(n["repository"]["full_name"], 0), [n]))
            continue
        grouped.setdefault(ref.key, (ref, []))[1].append(n)
    return grouped


def normalize_datetime(value: datetime) -> datetime:
    """Return a timezone-aware UTC datetime for sync cutoff comparisons."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def since_iso(cutoff: datetime) -> str:
    """Format a cutoff for GitHub's notifications ``since`` parameter."""
    return normalize_datetime(cutoff).isoformat().replace("+00:00", "Z")


def _event_repo_name(event: dict[str, Any]) -> str:
    repository = event.get("repo")
    if isinstance(repository, str):
        return repository
    if isinstance(repository, dict):
        for field_name in ("name", "full_name", "nameWithOwner"):
            value = repository.get(field_name)
            if value:
                return str(value)

    repository = event.get("repository")
    if isinstance(repository, dict):
        for field_name in ("full_name", "nameWithOwner", "name"):
            value = repository.get(field_name)
            if value:
                return str(value)
    return ""


def _event_number(payload: dict[str, Any], field_name: str) -> int | None:
    value = payload.get(field_name)
    if isinstance(value, dict):
        value = value.get("number")
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def event_thread_ref(event: dict[str, Any]) -> ThreadRef | None:
    """Extract a thread reference from a user event when it represents one."""
    event_type = str(event.get("type", ""))
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None

    issue = payload.get("issue")
    pull_request = payload.get("pull_request")
    if event_type in {
        "PullRequestEvent",
        "PullRequestReviewEvent",
        "PullRequestReviewCommentEvent",
        "PullRequestReviewThreadEvent",
    }:
        kind = "pr"
        number = _event_number(payload, "number")
        if number is None and isinstance(pull_request, dict):
            number = _event_number(pull_request, "number")
    elif event_type in {"IssuesEvent", "IssueCommentEvent"}:
        kind = "pr" if isinstance(issue, dict) and "pull_request" in issue else "issue"
        number = _event_number(payload, "number")
        if number is None and isinstance(issue, dict):
            number = _event_number(issue, "number")
    else:
        return None

    repo = _event_repo_name(event)
    if not repo or number is None:
        return None
    return ThreadRef(repo, number, kind)


def _event_cutoff(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return normalize_datetime(value)
    parsed = parse_timestamp(value)
    if parsed is not None:
        return parsed
    try:
        return normalize_datetime(datetime.fromisoformat(value))
    except ValueError as exc:
        raise ValueError(f"invalid events cutoff: {value}") from exc


def fetch_user_event_threads(
    cutoff: datetime | str,
) -> tuple[list[ThreadRef], list[str]]:
    """Find threads from the authenticated user's recent Events API activity.

    GitHub does not provide a date filter for this endpoint, so events before
    ``cutoff`` are discarded locally.  The endpoint is a complement to search,
    not a complete activity history.
    """
    user = gh_api("user")
    if not isinstance(user, dict) or not user.get("login"):
        raise RuntimeError("authenticated user response has no login")
    username = str(user["login"])

    raw_events = gh_api(f"users/{username}/events?per_page=100", paginate=True)
    if raw_events is None:
        raw_events = []
    if not isinstance(raw_events, list):
        raise RuntimeError("expected a JSON array from user events")

    effective_cutoff = _event_cutoff(cutoff)
    refs: dict[str, ThreadRef] = {}
    warnings: list[str] = []
    old_event_count = 0
    for event in raw_events:
        if not isinstance(event, dict):
            continue
        timestamp = parse_timestamp(str(event.get("created_at", "")))
        if timestamp is not None and timestamp < effective_cutoff:
            old_event_count += 1
            continue
        if timestamp is None:
            continue
        ref = event_thread_ref(event)
        if ref is not None:
            refs.setdefault(ref.key, ref)

    event_count = len(raw_events)
    if event_count >= EVENTS_RESULT_WARNING_THRESHOLD:
        warnings.append(
            f"events: {event_count} events returned (near the ~{EVENTS_RESULT_LIMIT}-event limit; "
            "older activity may be missing)"
        )
    if old_event_count and old_event_count >= event_count / 2:
        warnings.append(
            f"events: filtered {old_event_count}/{event_count} events before cutoff; "
            "the endpoint has no server-side date filter"
        )

    return list(refs.values()), warnings


def since_days_iso(since_days: int) -> str:
    """Format the legacy day-based period used by the list command."""
    return since_iso(datetime.now(timezone.utc) - timedelta(days=since_days))


def resolve_sync_cutoff(
    *,
    since_days: int | None = None,
    last_successful_sync: datetime | None = None,
    now: datetime | None = None,
    bootstrap_days: int | None = None,
    overlap_minutes: int | None = None,
) -> tuple[datetime, str]:
    """Resolve the effective cutoff and mode for a sync run.

    An explicit ``since_days`` is a backfill and deliberately does not inspect
    the watermark.  Incremental runs use the previous successful collection
    start boundary with a small overlap so events collected during that run
    can be re-read safely; the first run uses the bootstrap period instead.
    """
    settings = resolve_config(
        bootstrap_days=bootstrap_days,
        overlap_minutes=overlap_minutes,
    )
    bootstrap_days = settings.sync.bootstrap_days
    overlap_minutes = settings.sync.overlap_minutes
    current = normalize_datetime(now or datetime.now(timezone.utc))
    if since_days is not None:
        if since_days < 1:
            raise ValueError("--since は 1 以上の日数を指定してください")
        return current - timedelta(days=since_days), "backfill"
    if last_successful_sync is not None:
        watermark = normalize_datetime(last_successful_sync)
        return watermark - timedelta(minutes=overlap_minutes), "incremental"
    return current - timedelta(days=bootstrap_days), "incremental"


def sync_output_metadata(
    *, mode: str, cutoff: datetime, watermark: datetime | None
) -> dict[str, Any]:
    """Build the public metadata shared by text and JSON sync output."""
    return {
        "mode": mode,
        "cutoff": since_iso(cutoff),
        "watermark": since_iso(watermark) if watermark is not None else None,
        "bootstrap": mode == "incremental" and watermark is None,
    }


def search_thread_ref(item: dict[str, Any], kind_hint: str) -> ThreadRef | None:
    repository = item.get("repository")
    repo = ""
    if isinstance(repository, str):
        repo = repository
    elif isinstance(repository, dict):
        repo = str(repository.get("nameWithOwner", ""))
        owner = repository.get("owner") or {}
        if not repo and owner.get("login") and repository.get("name"):
            repo = f"{owner['login']}/{repository['name']}"

    raw_url = str(item.get("url", ""))
    parsed = urlparse(raw_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 4 and parts[2] in ("issues", "pull"):
        repo = repo or f"{parts[0]}/{parts[1]}"
        kind_hint = "pr" if parts[2] == "pull" else "issue"
    if not repo or not item.get("number"):
        return None
    return ThreadRef(repo, int(item["number"]), "pr" if kind_hint == "pr" else "issue")


class _SearchRateLimiter:
    """Keep backfill search requests serial and separated by a fixed interval."""

    def __init__(self, *, interval_seconds: float) -> None:
        self.interval_seconds = max(0.0, interval_seconds)
        self.last_started_at: float | None = None

    def before_call(self) -> None:
        now = time.monotonic()
        if self.last_started_at is not None:
            remaining = self.interval_seconds - (now - self.last_started_at)
            if remaining > 0:
                time.sleep(remaining)
        self.last_started_at = time.monotonic()


def _is_search_rate_limit_error(exc: RuntimeError) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code in (403, 429, "403", "429"):
        return True
    if getattr(exc, "retry_after", None) is not None:
        return True
    message = str(exc)
    return bool(
        re.search(r"\b(?:403|429)\b", message)
        or re.search(r"rate[- ]limit|secondary rate", message, re.IGNORECASE)
    )


def _retry_after_seconds(exc: RuntimeError, attempt: int) -> float:
    """Return a bounded retry delay from an error, or exponential backoff."""
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is None:
        message = str(exc)
        match = re.search(
            r"retry[- ]after\s*(?:header\s*)?(?:[:=]\s*|\s+)(\d+(?:\.\d+)?)",
            message,
            re.IGNORECASE,
        )
        if match:
            retry_after = match.group(1)

    try:
        delay = float(retry_after) if retry_after is not None else None
    except (TypeError, ValueError):
        delay = None
    if delay is None:
        reset_match = re.search(
            r"x[- ]?ratelimit[- ]?reset\s*[:=]\s*(\d+)",
            str(exc),
            re.IGNORECASE,
        )
        if reset_match:
            delay = max(0.0, float(reset_match.group(1)) - time.time())
    if delay is None:
        delay = SEARCH_RETRY_BACKOFF_SECONDS * (2**attempt)
    return min(max(0.0, delay), SEARCH_MAX_RETRY_WAIT_SECONDS)


def _search_label(
    search_kind: str,
    qualifier: str,
    *,
    repo: str | None = None,
    org: str | None = None,
) -> str:
    if repo:
        scope = f"repo:{repo}"
    elif org:
        scope = f"org:{org}"
    else:
        scope = "global"
    return f"gh search {search_kind} --{qualifier} ({scope})"


def _search_args(
    search_kind: str,
    qualifier: str,
    since_date: str,
    *,
    repo: str | None = None,
    org: str | None = None,
) -> list[str]:
    args = ["search", search_kind]
    if org:
        # gh search exposes GitHub's global search syntax as positional query
        # terms.  ``--org`` is not available consistently across gh versions.
        args.append(f"org:{org}")
    args.extend([
        f"--{qualifier}",
        "@me",
    ])
    if repo:
        args.extend(["--repo", repo])
    args.extend([
        "--updated",
        f">={since_date}",
        "--sort",
        "updated",
        "--order",
        "desc",
        "--limit",
        str(SEARCH_RESULT_LIMIT),
        "--json",
        "number,repository,url,updatedAt",
    ])
    return args


def _run_search_query(
    args: list[str],
    label: str,
    *,
    limiter: _SearchRateLimiter,
    warnings: list[str],
) -> list[dict[str, Any]]:
    for attempt in range(SEARCH_MAX_RETRIES + 1):
        limiter.before_call()
        try:
            result = gh_cli_json(args)
        except RuntimeError as exc:
            if not _is_search_rate_limit_error(exc):
                raise SyncCollectionError(f"{label}: {exc}") from exc
            if attempt >= SEARCH_MAX_RETRIES:
                raise SyncCollectionError(
                    f"{label}: rate limited after {attempt + 1} attempts: {exc}"
                ) from exc
            delay = _retry_after_seconds(exc, attempt)
            warnings.append(
                f"{label}: rate limited; retrying in {delay:g}s "
                f"(attempt {attempt + 2}/{SEARCH_MAX_RETRIES + 1})"
            )
            time.sleep(delay)
            continue

        if result is None:
            return []
        if not isinstance(result, list):
            raise SyncCollectionError(f"{label}: expected a JSON array from gh search")
        return [item for item in result if isinstance(item, dict)]

    # The loop either returns or raises.  Keep a defensive error for static
    # type checkers and future changes to the retry policy.
    raise SyncCollectionError(f"{label}: search did not return a result")


def _append_search_count_warning(
    warnings: list[str], label: str, result_count: int
) -> None:
    warning = f"{label}: {result_count} results"
    if result_count >= SEARCH_RESULT_WARNING_THRESHOLD:
        warning += " (near GitHub's 1,000-result limit; results may be truncated)"
    warnings.append(warning)


def fetch_search_threads(
    since_date: str,
    repos: Sequence[str] | None = None,
    *,
    search_orgs: Sequence[str] | None = None,
    backfill: bool = False,
) -> tuple[list[ThreadRef], list[str]]:
    """Find recently updated threads related to the authenticated user.

    Global search is the primary discovery path.  ``repos`` remains as an
    additive per-repository fallback for configurations that still need it.
    """
    queries = [
        ("issues", "author"),
        ("issues", "assignee"),
        ("issues", "commenter"),
        ("prs", "author"),
        ("prs", "assignee"),
        ("prs", "reviewed-by"),
        ("prs", "commenter"),
    ]
    refs: dict[str, ThreadRef] = {}
    warnings: list[str] = []
    settings = resolve_config()
    selected_repos = tuple(repos) if repos is not None else settings.mine_repos
    selected_orgs = (
        tuple(search_orgs) if search_orgs is not None else settings.search_orgs
    )
    limiter = _SearchRateLimiter(
        interval_seconds=SEARCH_INTERVAL_SECONDS if backfill else 0.0,
    )

    global_scopes = selected_orgs or (None,)
    for org in global_scopes:
        for search_kind, qualifier in queries:
            label = _search_label(search_kind, qualifier, org=org)
            items = _run_search_query(
                _search_args(search_kind, qualifier, since_date, org=org),
                label,
                limiter=limiter,
                warnings=warnings,
            )
            _append_search_count_warning(warnings, label, len(items))
            for item in items:
                ref = search_thread_ref(
                    item,
                    "pr" if search_kind == "prs" else "issue",
                )
                if ref:
                    refs[ref.key] = ref

    for repo in selected_repos:
        for search_kind, qualifier in queries:
            label = _search_label(search_kind, qualifier, repo=repo)
            items = _run_search_query(
                _search_args(search_kind, qualifier, since_date, repo=repo),
                label,
                limiter=limiter,
                warnings=warnings,
            )
            _append_search_count_warning(warnings, label, len(items))
            for item in items:
                ref = search_thread_ref(
                    item,
                    "pr" if search_kind == "prs" else "issue",
                )
                if ref:
                    refs[ref.key] = ref
    return list(refs.values()), warnings


def notification_event(ref: ThreadRef, notification: dict[str, Any]) -> dict[str, Any] | None:
    subject = notification.get("subject", {})
    reason = str(notification.get("reason", "notification"))
    title = str(subject.get("title", ""))
    body = f"{reason}: {title}" if title else reason
    return make_event_record(
        ref,
        "notification",
        str(notification.get("updated_at", "")),
        "?",
        body,
        source="notifications",
        source_id=f"notification:{notification.get('id', '')}",
    )


def event_is_since(record: dict[str, Any], cutoff: datetime) -> bool:
    timestamp = parse_timestamp(str(record.get("timestamp", "")))
    return bool(timestamp and timestamp >= cutoff)


def _link_is_since(record: dict[str, Any], cutoff: datetime) -> bool:
    timestamp = parse_timestamp(str(record.get("discovered_at", "")))
    return bool(timestamp and timestamp >= cutoff)


def effective_thread_cutoff(
    ref: ThreadRef,
    global_cutoff: datetime,
) -> datetime:
    """Return the strongest safe floor known for a stored thread."""
    cutoff_candidates = [normalize_datetime(global_cutoff)]
    database = get_session().db
    thread = database.get_thread(ref.repo, ref.number)
    if thread:
        synced_at = parse_timestamp(str(thread.get("last_synced_at", "")))
        if synced_at is not None:
            cutoff_candidates.append(synced_at)
    stored_at = database.db_max_timestamp(ref)
    if stored_at is not None:
        cutoff_candidates.append(normalize_datetime(stored_at))
    return max(cutoff_candidates)


def collect_event_records(
    *,
    cutoff: datetime | None = None,
    since_days: int | None = None,
    bootstrap_days: int | None = None,
    overlap_minutes: int | None = None,
    mine_repos: Sequence[str] | None = None,
    search_orgs: Sequence[str] | None = None,
    backfill: bool = False,
    optimize_threads: bool | None = None,
    synced_threads: list[ThreadSyncBoundary] | None = None,
    thread_links: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[str], int]:
    """Collect events at or after the effective sync cutoff.

    ``cutoff`` is an already-resolved incremental cutoff.  When omitted, the
    previous successful sync is looked up from the active session and the
    overlap is applied.  When supplied, ``since_days`` selects backfill mode
    and its computed cutoff takes precedence over ``cutoff``.

    Per-thread floors are used for incremental collection.  The global cutoff
    is based on the previous successful run's start boundary.  Explicit
    ``since_days`` backfills keep their global cutoff so they can repair older
    data.
    """
    backfill = backfill or since_days is not None
    if optimize_threads is None:
        optimize_threads = since_days is None
    if since_days is not None:
        cutoff, _ = resolve_sync_cutoff(
            since_days=since_days,
            now=datetime.now(timezone.utc),
            bootstrap_days=bootstrap_days,
            overlap_minutes=overlap_minutes,
        )
    elif cutoff is None:
        cutoff, _ = resolve_sync_cutoff(
            last_successful_sync=get_session().db.last_successful_sync_started_at(),
            now=datetime.now(timezone.utc),
            bootstrap_days=bootstrap_days,
            overlap_minutes=overlap_minutes,
        )
    else:
        cutoff = normalize_datetime(cutoff)

    warnings: list[str] = []
    try:
        notifications = fetch_notifications(since=since_iso(cutoff))
    except RuntimeError as exc:
        raise SyncCollectionError(f"notifications: {exc}") from exc
    refs: dict[str, ThreadRef] = {}
    notifications_by_ref: dict[str, list[dict[str, Any]]] = {}
    fallback_notifications: list[dict[str, Any]] = []
    for notification in notifications:
        if should_skip_notification(notification):
            continue
        try:
            ref = thread_ref_from_notification(notification)
        except (KeyError, ValueError):
            fallback_notifications.append(notification)
            continue
        refs[ref.key] = ref
        notifications_by_ref.setdefault(ref.key, []).append(notification)

    search_options: dict[str, Any] = {}
    if mine_repos is not None:
        search_options["repos"] = mine_repos
    if search_orgs is not None:
        search_options["search_orgs"] = search_orgs
    if backfill:
        search_options["backfill"] = True
    search_refs, search_warnings = fetch_search_threads(
        cutoff.date().isoformat(),
        **search_options,
    )
    warnings.extend(search_warnings)
    for ref in search_refs:
        refs[ref.key] = ref
    watch_entries = load_watch()
    watch_by_key: dict[str, dict[str, Any]] = {}
    for watched in watch_entries:
        try:
            repo = str(watched["repo"])
            number = int(watched["number"])
            kind = str(watched.get("kind", "issue"))
        except (KeyError, TypeError, ValueError):
            warnings.append(f"watch: invalid entry {watched!r}")
            continue
        if not repo or number <= 0:
            warnings.append(f"watch: invalid entry {watched!r}")
            continue
        if kind not in ("issue", "pr"):
            kind = "issue"
        key = f"{repo}#{number}"
        watch_by_key[key] = {
            "repo": repo,
            "number": number,
            "kind": kind,
            "note": str(watched.get("note", "")),
            "body": watched.get("body", ""),
        }
        refs.setdefault(key, ThreadRef(repo, number, kind))

    try:
        event_refs, event_warnings = fetch_user_event_threads(cutoff)
    except Exception as exc:
        warnings.append(f"events: {exc}" if str(exc) else f"events: {exc.__class__.__name__}")
    else:
        warnings.extend(event_warnings)
        for ref in event_refs:
            refs.setdefault(ref.key, ref)

    # REST metadata is the authoritative source for child -> parent links.
    # Keep the rows pending until event collection succeeds so a failed sync
    # does not leave a half-written discovery cache.  Parent-only rows are
    # intentionally not added to ``refs`` and therefore never incur timeline
    # or comments calls in this pass.
    pending_thread_metadata: dict[str, dict[str, Any]] = {}
    metadata_queue: list[tuple[ThreadRef, bool]] = []
    metadata_queued: set[str] = set()
    metadata_seen: set[str] = set()
    metadata_links_seen: set[tuple[str, str]] = set()
    metadata_parent_refs: dict[str, ThreadRef] = {}
    official_parent_by_child: dict[str, ThreadRef] = {}
    for stored_link in get_session().db.thread_links():
        if (
            str(stored_link.get("rel", "")).strip().lower() == "parent"
            and str(stored_link.get("source", "")).strip().lower() == "metadata"
        ):
            child_key = str(stored_link.get("from_thread_key", ""))
            parent_key = str(stored_link.get("to_thread_key", ""))
            if child_key and parent_key:
                try:
                    parent_repo, parent_number = parent_key.rsplit("#", 1)
                    official_parent_by_child.setdefault(
                        child_key,
                        ThreadRef(parent_repo, int(parent_number), "issue"),
                    )
                except (ValueError, TypeError):
                    continue

    def append_body_links(
        ref: ThreadRef,
        body: Any,
        *,
        discovered_at: str | None = None,
        cutoff: datetime | None = None,
    ) -> None:
        if not body:
            return
        for link in body_link_records(
            ref,
            str(body),
            discovered_at=discovered_at,
        ):
            if cutoff is not None and not _link_is_since(link, cutoff):
                continue
            if link["rel"] == "inferred_parent":
                official_parent = official_parent_by_child.get(ref.key)
                if official_parent and official_parent.key != link["to_thread_key"]:
                    warning = (
                        f"{ref.key}: body Parent {link['to_thread_key']} conflicts with "
                        f"metadata parent {official_parent.key}; metadata parent preferred"
                    )
                    if warning not in warnings:
                        warnings.append(warning)
            if thread_links is not None:
                thread_links.append(link)

    def stage_thread_metadata(
        ref: ThreadRef,
        issue: dict[str, Any] | None = None,
        *,
        allow_empty: bool = False,
    ) -> None:
        existing = get_session().db.get_thread(ref.repo, ref.number) or {}
        if issue is None and not allow_empty and not existing:
            return
        key = ref.key
        watch = watch_by_key.get(key)
        title = str(
            (issue or {}).get("title")
            or existing.get("title", "")
        )
        pending_thread_metadata[key] = {
            "ref": ref,
            "kind": ref.kind,
            "title": title,
            "is_watched": bool(watch or existing.get("is_watched", False)),
            "watch_note": str(
                (watch or {}).get("note", existing.get("watch_note", ""))
            ),
        }

    def enqueue_metadata(ref: ThreadRef, *, parent_only: bool) -> None:
        key = ref.key
        if key in metadata_seen or key in metadata_queued:
            return
        if not _metadata_thread_needs_fetch(
            ref,
            backfill=backfill,
            parent_only=parent_only,
        ):
            return
        metadata_queue.append((ref, parent_only))
        metadata_queued.add(key)

    def append_metadata_parent(child: ThreadRef, parent: ThreadRef) -> None:
        link_key = (child.key, parent.key)
        official_parent_by_child[child.key] = parent
        if link_key not in metadata_links_seen:
            metadata_links_seen.add(link_key)
            if thread_links is not None:
                thread_links.append({
                    "from_thread_key": child.key,
                    "to_thread_key": parent.key,
                    "rel": "parent",
                    "source": "metadata",
                    "confidence": 1.0,
                    "discovered_at": _metadata_discovered_at(),
                })
        # Register the anchor even when its own event history is empty.  It
        # is not a watch entry; only an explicitly watched parent is watched.
        metadata_parent_refs[parent.key] = parent
        stage_thread_metadata(parent, allow_empty=True)
        enqueue_metadata(parent, parent_only=True)

    def collect_metadata(ref: ThreadRef, *, parent_only: bool) -> None:
        key = ref.key
        metadata_queued.discard(key)
        if key in metadata_seen:
            return
        metadata_seen.add(key)
        try:
            issue = fetch_issue_or_pr(ref)
        except RuntimeError:
            # Metadata is enrichment.  Timeline collection remains usable if
            # the optional REST fast path is unavailable; a parent URL itself
            # still results in an anchor row when it was already parsed.
            return
        if not isinstance(issue, dict):
            issue = {}
        if issue.get("pull_request"):
            ref.kind = "pr"
        parent = parent_ref_from_issue(ref, issue)
        if parent is not None:
            # A normal collected thread is materialized by the existing
            # sync boundary path.  Stage the REST title here when this
            # metadata lookup also discovered a parent, so the parent/child
            # graph has useful labels without changing the no-parent path.
            stage_thread_metadata(ref, issue)
            append_metadata_parent(ref, parent)
        elif parent_only:
            # Parent-only metadata is the one case where a thread with no
            # parent of its own must still retain its title in the cache.
            stage_thread_metadata(ref, issue)
        body = issue.get("body")
        if body:
            append_body_links(
                ref,
                body,
                # Metadata payloads carry ``updated_at`` in normal GitHub
                # responses.  Leave it unset for minimal fixtures; the DB
                # normalizer supplies the observation timestamp without
                # adding another collection-clock read.
                discovered_at=(
                    str(issue.get("updated_at"))
                    if issue.get("updated_at")
                    else None
                ),
            )

    # A custom watch provider may already have a body on an entry.  The
    # regular DB-backed watch list does not store one, but accepting it here
    # keeps that optional input on the same low-cost parsing path.
    for key, watched in watch_by_key.items():
        if watched.get("body"):
            append_body_links(
                ThreadRef(str(watched["repo"]), int(watched["number"]), str(watched["kind"])),
                watched["body"],
                discovered_at=None,
            )

    # Existing incremental rows with a populated title are already covered
    # by their collection watermark.  New and backfill rows go through REST
    # metadata before timeline collection.
    for ref in list(refs.values()):
        enqueue_metadata(ref, parent_only=False)
    while metadata_queue:
        ref, parent_only = metadata_queue.pop(0)
        collect_metadata(ref, parent_only=parent_only)

    # A parent watched by the user is an explicit request to discover its
    # children.  Backfill is the repair mode where this extra enumeration is
    # also allowed for discovered issue refs.  Incremental child metadata
    # discovery above deliberately does not fan out through GraphQL.
    graphql_targets: dict[str, ThreadRef] = {}
    for key, watched in watch_by_key.items():
        graphql_targets[key] = ThreadRef(
            str(watched["repo"]),
            int(watched["number"]),
            str(watched["kind"]),
        )
    if backfill:
        # A backfill may refresh the known anchor's sub-issue membership, but
        # must not fan GraphQL out to every discovered child.  This keeps the
        # expensive path bounded by explicit watch/anchor targets.
        graphql_targets.update(metadata_parent_refs)
    for parent in graphql_targets.values():
        try:
            sub_issues = fetch_sub_issues(parent)
        except RuntimeError as exc:
            warnings.append(f"{parent.key}: sub-issues discovery unavailable: {exc}")
            continue
        for child in sub_issues:
            if child.key == parent.key:
                continue
            refs.setdefault(child.key, child)
            append_metadata_parent(child, parent)
            enqueue_metadata(child, parent_only=False)
    while metadata_queue:
        ref, parent_only = metadata_queue.pop(0)
        collect_metadata(ref, parent_only=parent_only)

    events: list[dict[str, Any]] = []
    for notification in fallback_notifications:
        repo = str(notification.get("repository", {}).get("full_name", ""))
        if not repo:
            continue
        ref = ThreadRef(repo, 0)
        record = notification_event(ref, notification)
        if record and event_is_since(record, cutoff):
            events.append(record)

    for ref_key, ref in refs.items():
        thread_sync_started_at = datetime.now(timezone.utc)
        thread_cutoff = (
            effective_thread_cutoff(ref, cutoff)
            if optimize_threads
            else normalize_datetime(cutoff)
        )
        timeline: list[dict[str, Any]] = []
        comments: list[dict[str, Any]] = []
        try:
            timeline = fetch_timeline(
                ref,
                per_page=100,
                paginate=True,
                cutoff=thread_cutoff,
            )
        except RuntimeError as exc:
            raise SyncCollectionError(f"{ref_key} timeline: {exc}") from exc

        thread_events = [
            event
            for event in (timeline_event_record(ref, item) for item in timeline)
            if event
        ]
        if thread_links is not None:
            for item in timeline:
                warning = timeline_link_warning(ref, item, cutoff=thread_cutoff)
                if warning and warning not in warnings:
                    warnings.append(warning)
                thread_links.extend(
                    link
                    for link in timeline_link_records(ref, item)
                    if _link_is_since(link, thread_cutoff)
                )
        has_recent_timeline_activity = any(
            event_is_since(event, thread_cutoff) for event in thread_events
        )
        has_unknown_commented_activity = _timeline_has_unknown_commented_activity(timeline)
        if has_recent_timeline_activity or has_unknown_commented_activity:
            try:
                comments = fetch_all_comments(ref)
            except RuntimeError as exc:
                raise SyncCollectionError(f"{ref_key} comments: {exc}") from exc
            thread_events.extend(
                event
                for event in (comment_event_record(ref, item) for item in comments)
                if event
            )
            for comment in comments:
                comment_timestamp = comment.get("created_at") or comment.get("updated_at")
                append_body_links(
                    ref,
                    comment.get("body"),
                    discovered_at=str(comment_timestamp) if comment_timestamp else None,
                    cutoff=thread_cutoff,
                )
        thread_events = [
            event for event in thread_events if event_is_since(event, thread_cutoff)
        ]
        if not thread_events:
            thread_events.extend(
                event
                for notification in notifications_by_ref.get(ref_key, [])
                for event in [notification_event(ref, notification)]
                if event and event_is_since(event, thread_cutoff)
            )
        events.extend(thread_events)
        if synced_threads is not None:
            synced_threads.append(ThreadSyncBoundary(ref, thread_sync_started_at))

    for metadata in pending_thread_metadata.values():
        ref = metadata["ref"]
        get_session().db.upsert_thread(
            ref.repo,
            ref.number,
            kind=str(metadata["kind"]),
            title=str(metadata["title"]),
            is_watched=bool(metadata["is_watched"]),
            watch_note=str(metadata["watch_note"]),
        )

    return deduplicate_events(events), warnings, len(refs)


def format_collect_markdown(
    since_days: int | None,
    event_count: int,
    new_count: int,
    total_count: int,
    thread_count: int,
    warnings: list[str],
    *,
    mode: str = "backfill",
    cutoff: datetime | None = None,
    watermark: datetime | None = None,
    bootstrap: bool = False,
    link_count: int | None = None,
    new_link_count: int | None = None,
    total_link_count: int | None = None,
) -> str:
    lines = [f"## gh-work-track collect ({datetime.now().strftime('%Y-%m-%d %H:%M')})", ""]
    if mode == "incremental" and cutoff is not None:
        period = f"{since_iso(cutoff)} 以降"
    elif since_days is not None:
        period = f"直近 {since_days} 日"
    else:
        period = "指定された cutoff 以降"
    cutoff_text = since_iso(cutoff) if cutoff is not None else "null"
    watermark_text = since_iso(watermark) if watermark is not None else "null"
    lines.append(
        f"mode: {mode} / cutoff: {cutoff_text} / "
        f"watermark: {watermark_text} / bootstrap: {str(bootstrap).lower()}"
    )
    lines.append(f"期間: {period} / 対象スレッド: {thread_count} 件")
    lines.append(f"取得イベント: {event_count} 件 / 新規保存: {new_count} 件 / 累積: {total_count} 件")
    if link_count is not None:
        lines.append(
            f"取得リンク: {link_count} 件 / 新規保存: {new_link_count or 0} 件 / "
            f"累積: {total_link_count or 0} 件"
        )
    lines.append(f"保存先: `{default_db_path()}`")
    if warnings:
        lines.append("")
        lines.append("### 警告")
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines)


def merge_summary(summaries: dict[str, ThreadSummary], summary: ThreadSummary) -> None:
    key = summary.ref.key if summary.ref.number else f"notif:{summary.notif_ids[0]}"
    existing = summaries.get(key)
    if not existing:
        summaries[key] = summary
        return
    existing.sources = list(dict.fromkeys(existing.sources + summary.sources))
    existing.unread = existing.unread or summary.unread
    existing.notif_ids = list(dict.fromkeys(existing.notif_ids + summary.notif_ids))
    existing.reasons = list(dict.fromkeys(existing.reasons + summary.reasons))
    if summary.is_watched:
        existing.is_watched = True
        existing.note = summary.note or existing.note
    if summary.updated_at > existing.updated_at:
        existing.updated_at = summary.updated_at
        existing.title = summary.title or existing.title
        existing.state = summary.state or existing.state
        existing.latest_author = summary.latest_author or existing.latest_author
        existing.latest_snippet = summary.latest_snippet or existing.latest_snippet


def collect_threads(
    since_days: int,
    *,
    include_notifications: bool = True,
    unread_only: bool = False,
    include_watch: bool = True,
    include_mine: bool = True,
    mine_repos: Sequence[str] | None = None,
) -> list[ThreadSummary]:
    since = since_days_iso(since_days)
    watch_map = {f"{t['repo']}#{t['number']}": t.get("note", "") for t in load_watch()}
    summaries: dict[str, ThreadSummary] = {}

    if include_notifications:
        notifs = fetch_notifications(unread_only=unread_only, since=since)
        grouped = group_notifications(notifs)
        for key, (ref, ns) in grouped.items():
            if ref.number == 0:
                n = ns[0]
                merge_summary(summaries, ThreadSummary(
                    ref=ref,
                    title=n.get("subject", {}).get("title", ""),
                    updated_at=n.get("updated_at", ""),
                    reasons=[REASON_JA.get(n.get("reason", ""), n.get("reason", ""))],
                    unread=bool(n.get("unread")),
                    notif_ids=[str(n["id"])],
                    subject_type=n.get("subject", {}).get("type", ""),
                    is_new_since_seen=True,
                    sources=["notif"],
                ))
                continue
            s = enrich_thread(ref, ns, watch_map.get(key, ""), light=True)
            s.sources = ["notif"]
            if key in watch_map:
                s.is_watched = True
                s.note = watch_map[key]
                s.sources.append("watch")
            merge_summary(summaries, s)

    if include_watch:
        for key, note in watch_map.items():
            if key in summaries:
                summaries[key].is_watched = True
                summaries[key].note = note or summaries[key].note
                if "watch" not in summaries[key].sources:
                    summaries[key].sources.append("watch")
                continue
            repo, num = key.split("#", 1)
            ref = ThreadRef(repo, int(num))
            s = enrich_thread(ref, None, note, light=True)
            s.sources = ["watch"]
            merge_summary(summaries, s)

    if include_mine:
        mine_items = (
            fetch_mine_open()
            if mine_repos is None
            else fetch_mine_open(mine_repos)
        )
        for s in mine_items:
            key = s.ref.key
            if key in watch_map:
                s.is_watched = True
                s.note = watch_map[key]
                s.sources.append("watch")
            merge_summary(summaries, s)

    return sorted(summaries.values(), key=lambda s: s.updated_at, reverse=True)


def format_list_markdown(
    items: list[ThreadSummary],
    since_days: int,
    meta: str,
    *,
    assignments: dict[str, WorkGroupAssignment] | None = None,
) -> str:
    lines = [f"## gh-work-track list ({datetime.now().strftime('%Y-%m-%d %H:%M')})", ""]
    lines.append(f"期間: 直近 {since_days} 日 / 件数: {len(items)}")
    lines.append(f"ソース: {meta}")
    lines.append("")
    if not items:
        lines.append("_該当なし_")
        return "\n".join(lines)

    for i, s in enumerate(items, 1):
        flags = []
        if s.unread:
            flags.append("unread")
        if s.is_new_since_seen:
            flags.append("new")
        if s.is_watched:
            flags.append("watch")
        if "mine" in s.sources and "notif" not in s.sources:
            flags.append("no-notif")
        flag_txt = f" [{', '.join(flags)}]" if flags else ""
        kind = s.ref.kind.upper() if s.ref.number else s.subject_type
        loc = s.ref.web_url if s.ref.number else "(通知のみ)"
        lines.append(f"### {i}. {s.title or '(no title)'}{flag_txt}")
        lines.append(f"- **ref**: `{s.ref.key if s.ref.number else 'notif:' + (s.notif_ids[0] if s.notif_ids else '?')}`")
        if s.sources:
            lines.append(f"- **sources**: {', '.join(s.sources)}")
        lines.append(f"- **type**: {kind} / **state**: {s.state or '-'}")
        if s.reasons:
            lines.append(f"- **通知理由**: {', '.join(s.reasons)}")
        if s.review_decision:
            lines.append(f"- **mergeable**: {s.review_decision}")
        if s.labels:
            lines.append(f"- **labels**: {', '.join(s.labels[:6])}")
        if s.assignees:
            lines.append(f"- **assignees**: {', '.join(s.assignees)}")
        if s.note:
            lines.append(f"- **watch note**: {s.note}")
        if s.ref.number:
            assignment = (
                assignments.get(s.ref.key)
                if assignments is not None
                else None
            )
            group_anchor = assignment.group_anchor if assignment else s.group_anchor
            group_role = assignment.group_role if assignment else s.group_role
            related = assignment.related if assignment else tuple(s.related)
            if not group_anchor:
                group_anchor = s.ref.key
            inferred = bool(assignment.is_inferred) if assignment else False
            inferred_marker = " [inferred]" if inferred else ""
            lines.append(
                f"- **group anchor**: `{group_anchor}` ({group_role or 'anchor'})"
                f"{inferred_marker}"
            )
            if related:
                lines.append(f"- **related**: {', '.join(f'`{key}`' for key in related)}")
        lines.append(f"- **updated**: {s.updated_at}")
        if s.latest_author:
            lines.append(f"- **最新**: @{s.latest_author}: {s.latest_snippet}")
        lines.append(f"- **url**: {loc}")
        lines.append(f"- **drill**: `gh-work-track.py drill {s.ref.key if s.ref.number else 'notif:' + s.notif_ids[0]}`")
        lines.append("")
    return "\n".join(lines)


def list_meta_line(args: argparse.Namespace) -> str:
    parts = []
    if args.notifications_only:
        parts.append("notifications only")
    else:
        if not args.no_notifications:
            parts.append("notif" + (" (unread only)" if args.unread_only else " (all)"))
        if not args.no_watch:
            parts.append("watch")
        if not args.no_mine:
            parts.append("mine (open PR/Issue)")
    return " + ".join(parts) or "none"


def cmd_list(args: argparse.Namespace) -> int:
    include_notifications = not args.no_notifications
    include_watch = not args.no_watch and not args.notifications_only
    include_mine = not args.no_mine and not args.notifications_only
    if args.notifications_only:
        include_notifications = True
        include_watch = False
        include_mine = False

    items = collect_threads(
        args.since,
        include_notifications=include_notifications,
        unread_only=args.unread_only,
        include_watch=include_watch,
        include_mine=include_mine,
        mine_repos=getattr(args, "mine_repos", None),
    )
    meta = list_meta_line(args)
    assignments = _thread_group_assignments([s.ref for s in items])
    _apply_group_assignments(items, assignments)
    if args.json:
        payload = []
        for s in items:
            payload.append({
                "ref": s.ref.key if s.ref.number else None,
                "notif_ids": s.notif_ids,
                "sources": s.sources,
                "title": s.title,
                "state": s.state,
                "reasons": s.reasons,
                "unread": s.unread,
                "is_new_since_seen": s.is_new_since_seen,
                "is_watched": s.is_watched,
                "updated_at": s.updated_at,
                "latest_author": s.latest_author,
                "latest_snippet": s.latest_snippet,
                "url": s.ref.web_url if s.ref.number else None,
                "group_anchor": s.group_anchor or (s.ref.key if s.ref.number else None),
                "group_role": s.group_role or ("anchor" if s.ref.number else None),
                "related": s.related,
                "inferred": bool(
                    assignments.get(s.ref.key).is_inferred
                    if s.ref.number and s.ref.key in assignments
                    else False
                ),
            })
        print(json.dumps({"meta": meta, "items": payload}, ensure_ascii=False, indent=2))
    else:
        print(format_list_markdown(items, args.since, meta, assignments=assignments))
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    last_successful = (
        None if args.since is not None else get_session().db.last_successful_sync()
    )
    last_successful_started_at = (
        None
        if args.since is not None
        else get_session().db.last_successful_sync_started_at()
    )
    cutoff, mode = resolve_sync_cutoff(
        since_days=args.since,
        last_successful_sync=last_successful_started_at,
        now=now,
        bootstrap_days=getattr(args, "bootstrap_days", None),
        overlap_minutes=getattr(args, "overlap_minutes", None),
    )
    metadata = sync_output_metadata(
        mode=mode,
        cutoff=cutoff,
        watermark=last_successful,
    )
    synced_threads: list[ThreadSyncBoundary] = []
    thread_links: list[dict[str, Any]] = []
    events, warnings, thread_count = collect_event_records(
        cutoff=cutoff,
        mine_repos=getattr(args, "mine_repos", None),
        search_orgs=getattr(args, "search_orgs", None),
        backfill=mode == "backfill",
        optimize_threads=mode == "incremental",
        synced_threads=synced_threads,
        thread_links=thread_links,
    )
    new_count, total_count = save_events(events)
    new_link_count, total_link_count = save_thread_links(thread_links)
    finished_at = datetime.now(timezone.utc)
    get_session().mark_threads_synced(synced_threads, synced_at=finished_at)
    if args.json:
        print(json.dumps({
            **metadata,
            "cutoff_at": metadata["cutoff"],
            "since_days": args.since,
            "thread_count": thread_count,
            "event_count": len(events),
            "new_count": new_count,
            "total_count": total_count,
            "link_count": len(thread_links),
            "new_link_count": new_link_count,
            "total_link_count": total_link_count,
            "db_path": str(default_db_path()),
            "warnings": warnings,
        }, ensure_ascii=False, indent=2))
    else:
        print(format_collect_markdown(
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
            link_count=len(thread_links),
            new_link_count=new_link_count,
            total_link_count=total_link_count,
        ))
    return 0


def daily_date_range(args: argparse.Namespace) -> tuple[date, date]:
    if args.date:
        try:
            requested = date.fromisoformat(args.date)
        except ValueError as exc:
            raise ValueError("--date は YYYY-MM-DD 形式で指定してください") from exc
        if requested.isoformat() != args.date:
            raise ValueError("--date は YYYY-MM-DD 形式で指定してください")
        return requested, requested
    if args.since is None or args.since < 1:
        raise ValueError("--since は 1 以上の日数を指定してください")
    end = datetime.now(timezone.utc).date()
    return end - timedelta(days=args.since - 1), end


def daily_events(start: date, end: date) -> list[dict[str, Any]]:
    return get_session().load_events_between(start.isoformat(), end.isoformat())


def _daily_event_line(event: dict[str, Any], *, indent: str = "") -> str:
    actor = str(event.get("actor", ""))
    actor_text = f" by @{actor}" if actor and actor != "?" else ""
    body = f" — {event['snippet']}" if event.get("snippet") else ""
    return (
        f"{indent}- `{event.get('repo', '?')}#{event.get('number', '?')}` "
        f"({event.get('kind', 'issue')}) **{event.get('event', 'event')}**"
        f"{actor_text}{body} — {event.get('url', '')}"
    )


def format_daily_markdown(
    start: date,
    end: date,
    events: list[dict[str, Any]],
    *,
    group_assignments: dict[str, WorkGroupAssignment] | None = None,
    warnings: Sequence[str] | None = None,
) -> str:
    period = start.isoformat() if start == end else f"{start.isoformat()} ～ {end.isoformat()}"
    lines = [f"## gh-work-track daily ({period})", "", f"イベント: {len(events)} 件", ""]
    if warnings:
        lines.append("### 警告")
        lines.extend(f"- {warning}" for warning in warnings)
        lines.append("")
    if not events:
        lines.append("_該当なし（先に `sync --since N` を実行してください）_")
        return "\n".join(lines)

    if group_assignments is not None:
        grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for event in events:
            key = _event_thread_key(event) or ""
            assignment = _assignment_for_key(key, group_assignments)
            grouped.setdefault(str(event.get("date", "")), {}).setdefault(
                assignment.group_anchor, []
            ).append(event)
        for event_day, by_anchor in grouped.items():
            if event_day:
                lines.append(f"### {event_day}")
            for anchor, anchor_events in by_anchor.items():
                inferred = any(
                    _assignment_for_key(
                        _event_thread_key(event) or "", group_assignments
                    ).is_inferred
                    for event in anchor_events
                )
                marker = " [inferred]" if inferred else ""
                lines.append(f"#### Group anchor: `{anchor}`{marker}")
                for event in anchor_events:
                    lines.append(_daily_event_line(event, indent="  "))
                related = sorted({
                    related_key
                    for event in anchor_events
                    for related_key in _assignment_for_key(
                        _event_thread_key(event) or "", group_assignments
                    ).related
                    if related_key != anchor
                })
                if related:
                    lines.append(f"  - related: {', '.join(f'`{key}`' for key in related)}")
                lines.append("")
            if lines[-1] == "":
                continue
        if lines[-1] == "":
            lines.pop()
        return "\n".join(lines)

    current_date = ""
    for event in events:
        event_day = str(event.get("date", ""))
        if event_day != current_date:
            if current_date:
                lines.append("")
            lines.append(f"### {event_day}")
            current_date = event_day
        lines.append(_daily_event_line(event))
    return "\n".join(lines)


def cmd_daily(args: argparse.Namespace) -> int:
    start, end = daily_date_range(args)
    events = daily_events(start, end)
    group_mode = getattr(args, "group", "flat")
    group_warnings: list[str] = []
    assignments = (
        _thread_group_assignments(
            [event for event in events if _event_thread_key(event)],
            warnings=group_warnings,
        )
        if group_mode in {"anchor", "epic"}
        else None
    )
    if args.json:
        by_date: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            output_event = dict(event)
            if assignments is not None:
                key = _event_thread_key(event) or ""
                assignment = _assignment_for_key(key, assignments)
                output_event.update(assignment.as_dict())
            by_date.setdefault(str(event.get("date", "")), []).append(output_event)
        payload: dict[str, Any] = {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "count": len(events),
            "events": by_date,
        }
        if group_warnings:
            payload["warnings"] = group_warnings
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_daily_markdown(
            start,
            end,
            events,
            group_assignments=assignments,
            warnings=group_warnings,
        ))
    return 0


def format_drill_markdown(
    ref: ThreadRef,
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    timeline: list[dict[str, Any]],
    *,
    assignment: WorkGroupAssignment | None = None,
    links: Sequence[dict[str, Any]] | None = None,
) -> str:
    lines = [f"## drill: {ref.key}", ""]
    lines.append(f"**{issue.get('title', '')}**")
    lines.append(f"- state: {issue.get('state')}")
    lines.append(f"- url: {ref.web_url}")
    if assignment is not None:
        lines.append(f"- group anchor: `{assignment.group_anchor}` ({assignment.group_role})")
        if assignment.is_inferred:
            lines.append("- group relation: [inferred]")
        if assignment.related:
            lines.append(
                f"- related: {', '.join(f'`{key}`' for key in assignment.related)}"
            )
    labels = [l["name"] for l in issue.get("labels", [])]
    if labels:
        lines.append(f"- labels: {', '.join(labels)}")
    assignees = [a["login"] for a in issue.get("assignees", [])]
    if assignees:
        lines.append(f"- assignees: {', '.join(assignees)}")
    evidence_links = [
        link for link in (links or ())
        if str(link.get("source", "")).strip().lower() == "body"
        and link.get("evidence")
    ]
    if evidence_links:
        lines.append("")
        lines.append("### 推論リンク")
        for link in evidence_links:
            lines.append(
                f"- **{link.get('rel', 'related')}** → `"
                f"{link.get('to_thread_key', '?')}` "
                f"(confidence={link.get('confidence', '')})"
            )
            lines.append(f"  evidence: {link['evidence']}")
    lines.append(f"- updated: {issue.get('updated_at')}")
    body = snippet(issue.get("body", ""), 400)
    if body:
        lines.append("")
        lines.append("### 本文（抜粋）")
        lines.append(body)
    lines.append("")
    lines.append(f"### コメント（直近 {len(comments)} 件）")
    if not comments:
        lines.append("_なし_")
    for c in comments:
        lines.append(f"- **@{c['user']['login']}** ({c['created_at']})")
        lines.append(f"  {snippet(c.get('body', ''), 500)}")
    lines.append("")
    lines.append("### タイムライン（直近）")
    events = [e for e in timeline if e.get("event") in (
        "commented", "reviewed", "merged", "closed", "reopened", "assigned",
        "review_requested", "ready_for_review", "head_ref_force_pushed",
    )]
    if not events:
        lines.append("_なし_")
    for e in events[-15:]:
        actor = (e.get("actor") or {}).get("login", "?")
        event = e.get("event", "")
        created = e.get("created_at", "")
        body = snippet(e.get("body", ""), 200)
        extra = f" — {body}" if body else ""
        lines.append(f"- {created} @{actor} **{event}**{extra}")
    return "\n".join(lines)


def cmd_drill(args: argparse.Namespace) -> int:
    ref = parse_ref(args.ref, default_repo=getattr(args, "default_repo", None))
    if ref.number == 0:
        print("drill requires a resolvable issue/PR ref", file=sys.stderr)
        return 1
    issue = fetch_issue_or_pr(ref)
    comments = fetch_comments(ref, args.comments)
    timeline = fetch_timeline(ref, per_page=max(30, args.comments * 2))
    links = [
        json_safe_value(link)
        for link in get_session().load_thread_links(thread_key_value=ref.key)
    ]
    assignments = _thread_group_assignments([ref])
    assignment = _assignment_for_key(ref.key, assignments)
    if not args.no_mark_seen:
        mark_seen(ref, issue.get("updated_at"))
    if args.json:
        print(json.dumps({
            "ref": ref.key,
            **assignment.as_dict(),
            "issue": issue,
            "comments": comments,
            "timeline": timeline,
            "links": links,
        }, ensure_ascii=False, indent=2))
    else:
        print(format_drill_markdown(
            ref,
            issue,
            comments,
            timeline,
            assignment=assignment,
            links=links,
        ))
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    threads = load_watch()
    existing = {f"{t['repo']}#{t['number']}": t for t in threads}

    if args.action == "list":
        if args.json:
            print(json.dumps(threads, ensure_ascii=False, indent=2))
        else:
            if not threads:
                print("(watch なし)")
            for t in threads:
                note = f" — {t['note']}" if t.get("note") else ""
                print(f"- {t['repo']}#{t['number']}{note}")
        return 0

    for raw in args.refs:
        ref = parse_ref(raw, default_repo=getattr(args, "default_repo", None))
        key = ref.key
        if args.action == "add":
            existing[key] = {
                "repo": ref.repo,
                "number": ref.number,
                "kind": ref.kind,
                "note": args.note or "",
                "added": datetime.now(timezone.utc).date().isoformat(),
            }
        elif args.action == "remove":
            existing.pop(key, None)

    save_watch(list(existing.values()))
    return 0


def cmd_mark_seen(args: argparse.Namespace) -> int:
    ref = parse_ref(args.ref, default_repo=getattr(args, "default_repo", None))
    mark_seen(ref)
    print(f"marked seen: {ref.key}")
    return 0


def add_config_options(
    parser: argparse.ArgumentParser,
    *,
    suppress_defaults: bool = False,
) -> None:
    """Add configuration overrides to a root or command parser."""
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--default-repo",
        default=default,
        help="数字だけの ref に使う owner/repo（env/config より優先）",
    )
    parser.add_argument(
        "--mine-repo",
        dest="mine_repos",
        action="append",
        default=default,
        help="search discovery の追加 per-repo 対象（複数回指定可）",
    )
    parser.add_argument(
        "--search-org",
        dest="search_orgs",
        action="append",
        default=default,
        help="global search の organization 絞り込み（複数回指定可）",
    )
    parser.add_argument(
        "--bootstrap-days",
        type=int,
        default=default,
        help="初回 incremental sync の遡り日数（default: 7）",
    )
    parser.add_argument(
        "--overlap-minutes",
        type=int,
        default=default,
        help="incremental sync の overlap 分数（default: 5）",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--db",
        help="LanceDB ディレクトリ（default: ~/.local/share/gh-work-track/lance）",
    )
    add_config_options(p)
    sub = p.add_subparsers(dest="command", required=True)

    lp = sub.add_parser("list", help="作業スレッド一覧（複数ソース統合）")
    add_config_options(lp, suppress_defaults=True)
    lp.add_argument("--since", type=int, default=7, help="通知の遡り日数 (default: 7)。watch/mine は期間外も含む")
    lp.add_argument("--unread-only", action="store_true", help="通知は未読のみ（GitHub デフォルト相当）")
    lp.add_argument("--notifications-only", action="store_true", help="通知のみ（watch/mine なし）")
    lp.add_argument("--no-notifications", action="store_true", help="通知ソースを除外")
    lp.add_argument("--no-watch", action="store_true", help="watch リストを除外")
    lp.add_argument("--no-mine", action="store_true", help="自分の open PR/Issue を除外")
    lp.add_argument(
        "--related",
        action="store_true",
        help="保存済みの関連リンクと推論マーカーを表示（既定でも関連欄は表示）",
    )
    lp.add_argument("--json", action="store_true")
    lp.set_defaults(func=cmd_list)

    cp = sub.add_parser("collect", help="自分に関するイベントを取得して保存（sync の別名）")
    add_config_options(cp, suppress_defaults=True)
    cp.add_argument("--since", type=int, help=SYNC_SINCE_HELP)
    cp.add_argument("--json", action="store_true")
    cp.set_defaults(func=cmd_collect)

    sp = sub.add_parser("sync", help="GitHub からイベントを同期")
    add_config_options(sp, suppress_defaults=True)
    sp.add_argument("--since", type=int, help=SYNC_SINCE_HELP)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_collect)

    dap = sub.add_parser("daily", help="保存済みイベントを日付ごとに一覧")
    daily_period = dap.add_mutually_exclusive_group(required=True)
    daily_period.add_argument("--date", help="対象日 (YYYY-MM-DD)")
    daily_period.add_argument("--since", type=int, help="直近の日数（今日を含む）")
    dap.add_argument(
        "--group",
        choices=("flat", "anchor", "epic"),
        default="flat",
        help="表示単位（default: flat; anchor/epic で parent anchor 配下にネスト）",
    )
    dap.add_argument("--json", action="store_true")
    dap.set_defaults(func=cmd_daily)

    dp = sub.add_parser("drill", help="スレッドの深堀り")
    add_config_options(dp, suppress_defaults=True)
    dp.add_argument("ref", help="plaidev/karte-io-systems#123 / notif:ID / URL")
    dp.add_argument("--comments", type=int, default=10)
    dp.add_argument("--no-mark-seen", action="store_true")
    dp.add_argument("--json", action="store_true")
    dp.set_defaults(func=cmd_drill)

    wp = sub.add_parser("watch", help="watch リスト管理")
    add_config_options(wp, suppress_defaults=True)
    wp.add_argument("action", choices=["list", "add", "remove"])
    wp.add_argument("refs", nargs="*")
    wp.add_argument("--note", default="")
    wp.add_argument("--json", action="store_true")
    wp.set_defaults(func=cmd_watch)

    mp = sub.add_parser("mark-seen", help="最終確認済みを更新")
    add_config_options(mp, suppress_defaults=True)
    mp.add_argument("ref")
    mp.set_defaults(func=cmd_mark_seen)

    return p


def run_command(args: argparse.Namespace) -> int:
    return args.func(args)
