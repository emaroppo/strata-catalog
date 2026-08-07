"""Split assignment: inheritance, grouping, and the ratio it aims at."""

import pytest

from strata.catalog import SplitError
from strata.catalog.split import assign, ratio


def ungrouped(n: int) -> dict[int, None]:
    return {i: None for i in range(n)}


# ----------------------------------------------------------------------
# The ratio
# ----------------------------------------------------------------------


def test_singletons_land_exactly_on_the_target():
    # With groups of one the greedy rule has no reason to overshoot
    assigned, _ = assign(ungrouped(50), val_ratio=0.2)
    assert sum(assigned.values()) == 10


def test_every_sample_gets_a_side():
    assigned, _ = assign(ungrouped(37))
    assert len(assigned) == 37


def test_the_assignment_is_deterministic_for_a_seed():
    first, _ = assign(ungrouped(40), seed=7)
    second, _ = assign(ungrouped(40), seed=7)
    assert first == second


def test_a_different_seed_assigns_differently():
    assert assign(ungrouped(40), seed=1)[0] != assign(ungrouped(40), seed=2)[0]


def test_an_empty_selection_assigns_nothing():
    assert assign({}) == ({}, 0.0)


# ----------------------------------------------------------------------
# Grouping
# ----------------------------------------------------------------------


def test_a_group_stays_on_one_side():
    # All frames of one video together, or near-duplicates leak into val and
    # the metric flatters itself
    members = {i: f"vid{i // 4}" for i in range(24)}
    assigned, _ = assign(members, val_ratio=0.25)
    for video in {f"vid{v}" for v in range(6)}:
        sides = {assigned[i] for i, g in members.items() if g == video}
        assert len(sides) == 1


def test_grouped_and_ungrouped_samples_coexist():
    # The mixed corpus that [data] kind could not express
    members = {0: "vid1", 1: "vid1", 2: "vid1", 3: None, 4: None, 5: None}
    assigned, _ = assign(members, val_ratio=0.5)
    assert len({assigned[0], assigned[1], assigned[2]}) == 1
    assert len(assigned) == 6


def test_a_null_group_cannot_collide_with_a_real_one():
    # Sample 7's own group must not merge with a group literally named "7"
    members = {7: None, 8: "7", 9: "7"}
    assigned, _ = assign(members, val_ratio=0.5)
    assert assigned[8] == assigned[9]
    assert len(assigned) == 3


def test_overshoot_is_capped_at_half_a_group():
    # Six groups of 10, target 20: takes two and stops rather than running
    # to 30
    members = {i: f"g{i // 10}" for i in range(60)}
    assigned, _ = assign(members, val_ratio=0.33)
    assert sum(assigned.values()) == 20


def test_a_group_too_large_for_the_ratio_is_still_held_out():
    # No group improves on taking nothing once one exceeds twice the target,
    # which two groups guarantee. An oversized validation set is usable; an
    # empty one is not.
    members = {i: f"vid{i // 10}" for i in range(20)}
    assigned, achieved = assign(members, val_ratio=0.2)
    assert sum(assigned.values()) == 10
    assert achieved == pytest.approx(0.5)


def test_the_achieved_ratio_matches_the_target_when_it_can():
    _, achieved = assign(ungrouped(50), val_ratio=0.2)
    assert achieved == pytest.approx(0.2)


def test_the_fallback_picks_the_group_closest_to_the_target():
    members = {0: "big", 1: "big", 2: "big", 3: "big", 4: "small", 5: "small"}
    assigned, _ = assign(members, val_ratio=0.05)
    assert assigned[4] and assigned[5]
    assert not any(assigned[i] for i in range(4))


# ----------------------------------------------------------------------
# Inheritance
# ----------------------------------------------------------------------


def test_an_inherited_side_is_never_overruled():
    inherited = {i: True for i in range(10)}
    assigned, _ = assign(ungrouped(50), inherited=inherited, val_ratio=0.2)
    assert all(assigned[i] for i in range(10))


def test_new_samples_fill_the_deficit_only():
    # 10 already in val, target for 100 is 20, so 10 of the new 50 join it
    inherited = {i: i < 10 for i in range(50)}
    assigned, _ = assign(ungrouped(100), inherited=inherited, val_ratio=0.2)
    assert sum(assigned.values()) == 20


def test_a_drifted_ratio_is_corrected_by_the_next_version():
    # Over target already: the new samples all go to train rather than
    # compounding it
    inherited = {i: True for i in range(30)}
    assigned, _ = assign(ungrouped(100), inherited=inherited, val_ratio=0.2)
    assert sum(assigned.values()) == 30
    assert all(not assigned[i] for i in range(30, 100))


def test_a_new_member_of_an_assigned_group_inherits_its_side():
    inherited = {0: True, 1: True}
    members = {0: "vid1", 1: "vid1", 2: "vid1", 3: "vid2", 4: "vid2"}
    assert assign(members, inherited=inherited, val_ratio=0.5)[0][2] is True


def test_a_straddling_group_is_forced_to_train():
    # Cannot have come from here, so something else put it there; train is
    # the side that removes the leak rather than preserving it
    inherited = {0: True, 1: False}
    members = {0: "vid1", 1: "vid1", 2: "vid1", 3: None, 4: None}
    assigned, _ = assign(members, inherited=inherited, val_ratio=0.4)
    assert not any(assigned[i] for i in (0, 1, 2))


# ----------------------------------------------------------------------
# Refusals
# ----------------------------------------------------------------------


def test_a_single_group_cannot_be_split():
    with pytest.raises(SplitError, match="1 group"):
        assign({i: "one-video" for i in range(20)})


def test_the_error_explains_why_rather_than_what():
    with pytest.raises(SplitError, match="indivisible"):
        assign({i: "one-video" for i in range(20)})


def test_two_samples_still_produce_both_sides():
    # round(2 * 0.2) is 0, so the greedy pass takes nothing and the fallback
    # is the only thing standing between this and an empty validation set
    assigned, achieved = assign(ungrouped(2), val_ratio=0.2)
    assert sum(assigned.values()) == 1
    assert achieved == pytest.approx(0.5)


def test_a_lone_sample_cannot_be_split():
    with pytest.raises(SplitError, match="1 group"):
        assign(ungrouped(1))


def test_ratio_reports_what_was_achieved():
    assert ratio([True, False, False, False]) == pytest.approx(0.25)
    assert ratio([]) == 0.0
