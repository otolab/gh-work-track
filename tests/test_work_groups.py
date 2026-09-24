from __future__ import annotations

from gh_work_track.work_groups import resolve_work_groups


def link(from_key: str, to_key: str, rel: str) -> dict[str, str]:
    return {
        "from_thread_key": from_key,
        "to_thread_key": to_key,
        "rel": rel,
        "source": "manual",
    }


def test_parent_children_roll_up_to_parent_even_without_parent_event():
    parent = "plaidev/karte-io-systems#169457"
    child_a = "plaidev/karte-io-systems#169460"
    child_b = "plaidev/karte-io-systems#169462"
    child_c = "plaidev/karte-io-systems#170998"
    ops = "plaidev/ops#8814"
    result = resolve_work_groups(
        [
            link(child_a, parent, "parent"),
            link(child_b, parent, "parent"),
            link(child_c, parent, "parent"),
            link(child_c, ops, "cross_ref"),
        ],
        thread_keys=[child_a, child_b, child_c],
    )

    assert result[parent].group_anchor == parent
    assert result[child_a].group_anchor == parent
    assert result[child_b].group_anchor == parent
    assert result[child_c].group_anchor == parent
    assert result[child_a].group_role == "child"
    assert result[child_c].related == [parent, ops]
    assert result[ops].group_anchor == ops


def test_cross_reference_does_not_merge_groups():
    first = "owner/repo#10"
    second = "owner/repo#11"
    result = resolve_work_groups([link(first, second, "cross_ref")])

    assert result[first].group_anchor == first
    assert result[second].group_anchor == second
    assert result[first].related == [second]


def test_parent_cycle_uses_watch_kind_number_tie_break_not_union_find():
    a = "owner/repo#10"
    b = "owner/repo#20"
    unrelated = "owner/repo#30"
    result = resolve_work_groups(
        [
            link(a, b, "parent"),
            link(b, a, "parent"),
            link(b, unrelated, "cross_ref"),
        ],
        watch=[{"repo": "owner/repo", "number": 20, "kind": "issue"}],
    )

    assert result[a].group_anchor == b
    assert result[b].group_anchor == b
    # An undirected component implementation would incorrectly put this
    # cross-referenced node in the A/B group.
    assert result[unrelated].group_anchor == unrelated


def test_parent_branch_is_deterministic_and_watchable():
    child = "owner/repo#50"
    first_parent = "owner/repo#30"
    watched_parent = "owner/repo#40"
    result = resolve_work_groups(
        [
            link(child, first_parent, "parent"),
            link(child, watched_parent, "parent"),
        ],
        watch=[watched_parent],
    )

    assert result[child].group_anchor == watched_parent


def test_metadata_parent_beats_watched_manual_parent():
    child = "owner/repo#50"
    metadata_parent = "owner/repo#30"
    watched_manual_parent = "owner/repo#40"
    result = resolve_work_groups(
        [
            {
                **link(child, metadata_parent, "parent"),
                "source": "metadata",
            },
            link(child, watched_manual_parent, "parent"),
        ],
        watch=[watched_manual_parent],
    )

    assert result[child].group_anchor == metadata_parent


def test_metadata_parent_wins_without_inferring_parent_from_cross_ref():
    child = "owner/repo#50"
    metadata_parent = "owner/repo#30"
    cross_ref_target = "other/repo#40"
    result = resolve_work_groups(
        [
            {
                **link(child, metadata_parent, "parent"),
                "source": "metadata",
            },
            {
                **link(child, cross_ref_target, "cross_ref"),
                "source": "timeline",
            },
        ],
        watch=[cross_ref_target],
    )

    assert result[child].group_anchor == metadata_parent
    assert result[cross_ref_target].group_anchor == cross_ref_target


def test_parent_cycle_reports_warning_before_metadata_tiebreak():
    first = "owner/repo#10"
    metadata_parent = "owner/repo#20"
    warnings: list[str] = []
    result = resolve_work_groups(
        [
            {
                **link(first, metadata_parent, "parent"),
                "source": "metadata",
            },
            link(metadata_parent, first, "parent"),
        ],
        watch=[first],
        warnings=warnings,
    )

    assert result[first].group_anchor == metadata_parent
    assert result[metadata_parent].group_anchor == metadata_parent
    assert len(warnings) == 1
    assert "parent cycle detected" in warnings[0]
