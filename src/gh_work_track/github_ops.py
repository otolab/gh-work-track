#!/usr/bin/env python3
"""GitHub work progress tracker: collect → daily → list → drill.

Usage:
  gh-work-track.py list [--since DAYS] [--all] [--watch] [--json]
  gh-work-track.py drill <ref> [--comments N] [--json]
  gh-work-track.py collect [--since DAYS] [--json]
  gh-work-track.py daily (--date YYYY-MM-DD | --since DAYS) [--json]
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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from gh_work_track.config import db_path as default_db_path
from gh_work_track.session import get_session

DEFAULT_REPO = "plaidev/karte-io-systems"
SYNC_BOOTSTRAP_DAYS = 7
SYNC_OVERLAP_MINUTES = 5
SYNC_SINCE_HELP = (
    "バックフィル専用。省略時は前回成功 sync 以降の incremental（初回は 7 日 bootstrap）"
)
MINE_REPOS = [
    "plaidev/karte-io-systems",
    "plaidev/karte-io-systems-ops",
    "otolab/my-logs",
]

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


def parse_ref(raw: str) -> ThreadRef:
    raw = raw.strip()
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
        full_repo = f"{owner}/{repo}" if owner else f"plaidev/{repo}" if "/" not in repo else repo
        if "/" not in full_repo:
            full_repo = f"{DEFAULT_REPO.split('/')[0]}/{full_repo}"
        return ThreadRef(full_repo, num)

    if re.fullmatch(r"\d+", raw):
        return ThreadRef(DEFAULT_REPO, int(raw))

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


def fetch_mine_open(repos: list[str] | None = None) -> list[ThreadSummary]:
    """自分の open PR/Issue。通知が来ていない作業の補完用。list API の結果をそのまま使い API 呼び出しを抑える。"""
    repos = repos or MINE_REPOS
    summaries: list[ThreadSummary] = []
    seen: set[str] = set()
    for repo in repos:
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


def fetch_issue_or_pr(ref: ThreadRef) -> dict[str, Any]:
    return gh_api(f"repos/{ref.repo}/issues/{ref.number}")


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


def since_days_iso(since_days: int) -> str:
    """Format the legacy day-based period used by the list command."""
    return since_iso(datetime.now(timezone.utc) - timedelta(days=since_days))


def resolve_sync_cutoff(
    *,
    since_days: int | None = None,
    last_successful_sync: datetime | None = None,
    now: datetime | None = None,
    overlap_minutes: int = SYNC_OVERLAP_MINUTES,
) -> tuple[datetime, str]:
    """Resolve the effective cutoff and mode for a sync run.

    An explicit ``since_days`` is a backfill and deliberately does not inspect
    the watermark.  Incremental runs use the previous successful completion
    with a small overlap so events near the boundary can be re-read safely;
    the first run uses the bootstrap period instead.
    """
    if overlap_minutes < 0:
        raise ValueError("overlap_minutes は 0 以上で指定してください")
    current = normalize_datetime(now or datetime.now(timezone.utc))
    if since_days is not None:
        if since_days < 1:
            raise ValueError("--since は 1 以上の日数を指定してください")
        return current - timedelta(days=since_days), "backfill"
    if last_successful_sync is not None:
        watermark = normalize_datetime(last_successful_sync)
        return watermark - timedelta(minutes=overlap_minutes), "incremental"
    return current - timedelta(days=SYNC_BOOTSTRAP_DAYS), "incremental"


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


def fetch_search_threads(since_date: str) -> tuple[list[ThreadRef], list[str]]:
    """Find recently updated threads related to the authenticated user."""
    queries = [
        ("issues", "author"),
        ("issues", "assignee"),
        ("prs", "author"),
        ("prs", "assignee"),
        ("prs", "reviewed-by"),
    ]
    refs: dict[str, ThreadRef] = {}
    warnings: list[str] = []
    fields = "number,repository,url,updatedAt"
    for search_kind, qualifier in queries:
        try:
            items = gh_cli_json([
                "search",
                search_kind,
                f"--{qualifier}",
                "@me",
                "--updated",
                f">={since_date}",
                "--sort",
                "updated",
                "--order",
                "desc",
                "--limit",
                "100",
                "--json",
                fields,
            ]) or []
        except RuntimeError as exc:
            raise SyncCollectionError(
                f"gh search {search_kind} --{qualifier}: {exc}"
            ) from exc
        for item in items:
            if not isinstance(item, dict):
                continue
            ref = search_thread_ref(item, "pr" if search_kind == "prs" else "issue")
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
    overlap_minutes: int = SYNC_OVERLAP_MINUTES,
    optimize_threads: bool | None = None,
    synced_threads: list[ThreadSyncBoundary] | None = None,
) -> tuple[list[dict[str, Any]], list[str], int]:
    """Collect events at or after the effective sync cutoff.

    ``cutoff`` is an already-resolved incremental cutoff.  When omitted, the
    previous successful sync is looked up from the active session and the
    overlap is applied.  When supplied, ``since_days`` selects backfill mode
    and its computed cutoff takes precedence over ``cutoff``.

    Per-thread floors are used for incremental collection.  Explicit
    ``since_days`` backfills keep their global cutoff so they can repair older
    data.
    """
    if optimize_threads is None:
        optimize_threads = since_days is None
    if since_days is not None:
        cutoff, _ = resolve_sync_cutoff(
            since_days=since_days,
            now=datetime.now(timezone.utc),
            overlap_minutes=overlap_minutes,
        )
    elif cutoff is None:
        cutoff, _ = resolve_sync_cutoff(
            last_successful_sync=get_session().db.last_successful_sync(),
            now=datetime.now(timezone.utc),
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

    search_refs, search_warnings = fetch_search_threads(cutoff.date().isoformat())
    warnings.extend(search_warnings)
    for ref in search_refs:
        refs[ref.key] = ref
    for watched in load_watch():
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
        refs.setdefault(f"{repo}#{number}", ThreadRef(repo, number, kind))

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
        for s in fetch_mine_open():
            key = s.ref.key
            if key in watch_map:
                s.is_watched = True
                s.note = watch_map[key]
                s.sources.append("watch")
            merge_summary(summaries, s)

    return sorted(summaries.values(), key=lambda s: s.updated_at, reverse=True)


def format_list_markdown(items: list[ThreadSummary], since_days: int, meta: str) -> str:
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
    )
    meta = list_meta_line(args)
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
            })
        print(json.dumps({"meta": meta, "items": payload}, ensure_ascii=False, indent=2))
    else:
        print(format_list_markdown(items, args.since, meta))
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    last_successful = (
        None if args.since is not None else get_session().db.last_successful_sync()
    )
    cutoff, mode = resolve_sync_cutoff(
        since_days=args.since,
        last_successful_sync=last_successful,
        now=now,
    )
    metadata = sync_output_metadata(
        mode=mode,
        cutoff=cutoff,
        watermark=last_successful,
    )
    synced_threads: list[ThreadSyncBoundary] = []
    events, warnings, thread_count = collect_event_records(
        cutoff=cutoff,
        optimize_threads=mode == "incremental",
        synced_threads=synced_threads,
    )
    new_count, total_count = save_events(events)
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


def format_daily_markdown(start: date, end: date, events: list[dict[str, Any]]) -> str:
    period = start.isoformat() if start == end else f"{start.isoformat()} ～ {end.isoformat()}"
    lines = [f"## gh-work-track daily ({period})", "", f"イベント: {len(events)} 件", ""]
    if not events:
        lines.append("_該当なし（先に `sync --since N` を実行してください）_")
        return "\n".join(lines)

    current_date = ""
    for event in events:
        event_day = str(event.get("date", ""))
        if event_day != current_date:
            if current_date:
                lines.append("")
            lines.append(f"### {event_day}")
            current_date = event_day
        actor = str(event.get("actor", ""))
        actor_text = f" by @{actor}" if actor and actor != "?" else ""
        body = f" — {event['snippet']}" if event.get("snippet") else ""
        lines.append(
            f"- `{event.get('repo', '?')}#{event.get('number', '?')}` "
            f"({event.get('kind', 'issue')}) **{event.get('event', 'event')}**"
            f"{actor_text}{body} — {event.get('url', '')}"
        )
    return "\n".join(lines)


def cmd_daily(args: argparse.Namespace) -> int:
    start, end = daily_date_range(args)
    events = daily_events(start, end)
    if args.json:
        by_date: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            by_date.setdefault(str(event.get("date", "")), []).append(event)
        print(json.dumps({
            "from": start.isoformat(),
            "to": end.isoformat(),
            "count": len(events),
            "events": by_date,
        }, ensure_ascii=False, indent=2))
    else:
        print(format_daily_markdown(start, end, events))
    return 0


def format_drill_markdown(ref: ThreadRef, issue: dict[str, Any], comments: list[dict[str, Any]], timeline: list[dict[str, Any]]) -> str:
    lines = [f"## drill: {ref.key}", ""]
    lines.append(f"**{issue.get('title', '')}**")
    lines.append(f"- state: {issue.get('state')}")
    lines.append(f"- url: {ref.web_url}")
    labels = [l["name"] for l in issue.get("labels", [])]
    if labels:
        lines.append(f"- labels: {', '.join(labels)}")
    assignees = [a["login"] for a in issue.get("assignees", [])]
    if assignees:
        lines.append(f"- assignees: {', '.join(assignees)}")
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
    ref = parse_ref(args.ref)
    if ref.number == 0:
        print("drill requires a resolvable issue/PR ref", file=sys.stderr)
        return 1
    issue = fetch_issue_or_pr(ref)
    comments = fetch_comments(ref, args.comments)
    timeline = fetch_timeline(ref, per_page=max(30, args.comments * 2))
    if not args.no_mark_seen:
        mark_seen(ref, issue.get("updated_at"))
    if args.json:
        print(json.dumps({
            "ref": ref.key,
            "issue": issue,
            "comments": comments,
            "timeline": timeline,
        }, ensure_ascii=False, indent=2))
    else:
        print(format_drill_markdown(ref, issue, comments, timeline))
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
        ref = parse_ref(raw)
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
    ref = parse_ref(args.ref)
    mark_seen(ref)
    print(f"marked seen: {ref.key}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--db",
        help="LanceDB ディレクトリ（default: ~/.local/share/gh-work-track/lance）",
    )
    sub = p.add_subparsers(dest="command", required=True)

    lp = sub.add_parser("list", help="作業スレッド一覧（複数ソース統合）")
    lp.add_argument("--since", type=int, default=7, help="通知の遡り日数 (default: 7)。watch/mine は期間外も含む")
    lp.add_argument("--unread-only", action="store_true", help="通知は未読のみ（GitHub デフォルト相当）")
    lp.add_argument("--notifications-only", action="store_true", help="通知のみ（watch/mine なし）")
    lp.add_argument("--no-notifications", action="store_true", help="通知ソースを除外")
    lp.add_argument("--no-watch", action="store_true", help="watch リストを除外")
    lp.add_argument("--no-mine", action="store_true", help="自分の open PR/Issue を除外")
    lp.add_argument("--json", action="store_true")
    lp.set_defaults(func=cmd_list)

    cp = sub.add_parser("collect", help="自分に関するイベントを取得して保存（sync の別名）")
    cp.add_argument("--since", type=int, help=SYNC_SINCE_HELP)
    cp.add_argument("--json", action="store_true")
    cp.set_defaults(func=cmd_collect)

    sp = sub.add_parser("sync", help="GitHub からイベントを同期")
    sp.add_argument("--since", type=int, help=SYNC_SINCE_HELP)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_collect)

    dap = sub.add_parser("daily", help="保存済みイベントを日付ごとに一覧")
    daily_period = dap.add_mutually_exclusive_group(required=True)
    daily_period.add_argument("--date", help="対象日 (YYYY-MM-DD)")
    daily_period.add_argument("--since", type=int, help="直近の日数（今日を含む）")
    dap.add_argument("--json", action="store_true")
    dap.set_defaults(func=cmd_daily)

    dp = sub.add_parser("drill", help="スレッドの深堀り")
    dp.add_argument("ref", help="plaidev/karte-io-systems#123 / notif:ID / URL")
    dp.add_argument("--comments", type=int, default=10)
    dp.add_argument("--no-mark-seen", action="store_true")
    dp.add_argument("--json", action="store_true")
    dp.set_defaults(func=cmd_drill)

    wp = sub.add_parser("watch", help="watch リスト管理")
    wp.add_argument("action", choices=["list", "add", "remove"])
    wp.add_argument("refs", nargs="*")
    wp.add_argument("--note", default="")
    wp.add_argument("--json", action="store_true")
    wp.set_defaults(func=cmd_watch)

    mp = sub.add_parser("mark-seen", help="最終確認済みを更新")
    mp.add_argument("ref")
    mp.set_defaults(func=cmd_mark_seen)

    return p


def run_command(args: argparse.Namespace) -> int:
    return args.func(args)
