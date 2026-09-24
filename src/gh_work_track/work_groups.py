"""Resolve semantic work groups from directed thread links.

The link table is deliberately richer than a grouping table.  In particular,
``cross_ref`` and dependency links are useful context but do not make their
endpoints members of the same work group.  Only ``parent`` links participate
in anchor resolution in Phase 1.

Parent links use the direction ``child -> parent``.  A parent endpoint is
included in the result even when it has no event of its own, which lets the
CLI render an inactive anchor above active child events.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, NamedTuple


class WorkGroupAssignment(NamedTuple):
    """The resolved group information for one thread.

    ``related`` contains the other endpoints of directly stored links.  It is
    intentionally separate from ``group_anchor``: a cross-reference is
    visible as related context but never changes the anchor by itself.
    """

    group_anchor: str
    group_role: str
    related: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "group_anchor": self.group_anchor,
            "group_role": self.group_role,
            "related": list(self.related),
        }


WorkGroup = WorkGroupAssignment
WorkGroupMember = WorkGroupAssignment


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _thread_key(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    key = _value(value, "thread_key")
    if key:
        return str(key)
    key = _value(value, "key")
    if key:
        return str(key)
    repo = _value(value, "repo")
    number = _value(value, "number")
    if repo and number is not None:
        return f"{repo}#{number}"
    return None


def _watch_info(watch: Iterable[Any] | None) -> tuple[set[str], dict[str, str]]:
    watched: set[str] = set()
    kinds: dict[str, str] = {}
    for item in watch or ():
        key = _thread_key(item)
        if not key:
            continue
        watched.add(key)
        kind = _value(item, "kind")
        if kind:
            kinds[key] = str(kind)
    return watched, kinds


def _number_for_key(key: str) -> int:
    try:
        return int(key.rsplit("#", 1)[1])
    except (IndexError, ValueError):
        return 2**63 - 1


def _anchor_sort_key(
    key: str,
    *,
    watched: set[str],
    kinds: Mapping[str, str],
) -> tuple[int, int, int, str]:
    # A watched thread is the strongest user signal.  For otherwise equal
    # candidates, prefer issues to PRs and then the stable numeric ref.
    kind = str(kinds.get(key, "issue")).lower()
    kind_rank = 0 if kind in {"issue", "epic"} else 1 if kind in {"pr", "pull_request"} else 2
    return (
        0 if key in watched else 1,
        kind_rank,
        _number_for_key(key),
        key,
    )


def _normalise_inputs(
    links: Iterable[Any],
    thread_keys: Iterable[Any] | None,
    watch: Iterable[Any] | None,
    thread_kinds: Mapping[str, str] | None,
) -> tuple[
    dict[str, set[str]],
    dict[str, set[str]],
    set[str],
    dict[str, str],
    set[str],
]:
    parents: dict[str, set[str]] = {}
    related: dict[str, set[str]] = {}
    nodes: set[str] = set()
    watched, kinds = _watch_info(watch)
    if thread_kinds:
        kinds.update({str(key): str(value) for key, value in thread_kinds.items()})
    nodes.update(watched)

    for link in links:
        from_key = _thread_key(_value(link, "from_thread_key"))
        to_key = _thread_key(_value(link, "to_thread_key"))
        if not from_key or not to_key or from_key == to_key:
            continue
        nodes.update((from_key, to_key))
        related.setdefault(from_key, set()).add(to_key)
        related.setdefault(to_key, set()).add(from_key)
        rel = str(_value(link, "rel", "")).strip().lower()
        if rel == "parent":
            parents.setdefault(from_key, set()).add(to_key)

    for item in thread_keys or ():
        key = _thread_key(item)
        if key:
            nodes.add(key)
            kind = _value(item, "kind")
            if kind:
                kinds[key] = str(kind)

    return parents, related, nodes, kinds, watched


def resolve_work_groups(
    links: Iterable[Any],
    *,
    watch: Iterable[Any] | None = None,
    thread_keys: Iterable[Any] | None = None,
    thread_kinds: Mapping[str, str] | None = None,
) -> dict[str, WorkGroupAssignment]:
    """Resolve ``thread_key -> (anchor, role, related)`` assignments.

    The resolver follows directed parent links one path at a time.  It never
    creates an undirected connected component, so a cross-reference or a
    dependency cannot accidentally merge unrelated work.  If a parent path
    enters a cycle, the cycle members are reduced to one deterministic anchor
    using the watch/kind/number tie-break order documented by
    :func:`_anchor_sort_key`.
    """

    parents, related, nodes, kinds, watched = _normalise_inputs(
        links, thread_keys, watch, thread_kinds
    )
    assignments: dict[str, WorkGroupAssignment] = {}
    anchor_cache: dict[str, str] = {}

    def resolve_anchor(start: str) -> str:
        cached = anchor_cache.get(start)
        if cached:
            return cached
        path: list[str] = []
        positions: dict[str, int] = {}
        current = start
        while True:
            cached = anchor_cache.get(current)
            if cached:
                anchor = cached
                break
            if current in positions:
                cycle = set(path[positions[current] :])
                anchor = min(
                    cycle,
                    key=lambda key: _anchor_sort_key(
                        key, watched=watched, kinds=kinds
                    ),
                )
                for member in cycle:
                    anchor_cache[member] = anchor
                break
            positions[current] = len(path)
            path.append(current)
            candidates = parents.get(current, set())
            if not candidates:
                anchor = current
                break
            current = min(
                candidates,
                key=lambda key: _anchor_sort_key(
                    key, watched=watched, kinds=kinds
                ),
            )
        for member in reversed(path):
            anchor_cache.setdefault(member, anchor)
        return anchor

    for node in sorted(nodes):
        anchor = resolve_anchor(node)
        role = "anchor" if node == anchor else "child"
        neighbours = sorted(related.get(node, set()))
        assignments[node] = WorkGroupAssignment(anchor, role, neighbours)

    # Keep the resolver total even if an unusual caller supplies a link whose
    # endpoint was filtered while iterating.  This also makes the public
    # result straightforward to consume by JSON/CLI callers.
    for node, anchor in anchor_cache.items():
        assignments.setdefault(
            node,
            WorkGroupAssignment(
                anchor,
                "anchor" if node == anchor else "child",
                sorted(related.get(node, set())),
            ),
        )
    return assignments


def resolve_groups(
    links: Iterable[Any],
    *,
    watch: Iterable[Any] | None = None,
    thread_keys: Iterable[Any] | None = None,
    thread_kinds: Mapping[str, str] | None = None,
) -> dict[str, WorkGroupAssignment]:
    """Compatibility alias with a shorter name for library callers."""

    return resolve_work_groups(
        links,
        watch=watch,
        thread_keys=thread_keys,
        thread_kinds=thread_kinds,
    )


def group_assignment_dict(assignment: WorkGroupAssignment) -> dict[str, Any]:
    """Convert an assignment into a JSON-friendly mapping."""

    return assignment.as_dict()
