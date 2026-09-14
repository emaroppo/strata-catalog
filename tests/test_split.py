"""Split assignment: inheritance, grouping, the ratios it aims at, and the holdout."""

import pytest

from strata.catalog import HOLDOUT, TRAIN, VAL, Achieved, SplitError
from strata.catalog.versions.split import assign, ratio


def ungrouped(n: int) -> dict[int, None]:
    return dict.fromkeys(range(n))


def count(assigned: dict[int, str], side: str) -> int:
    return sum(1 for s in assigned.values() if s == side)


# ----------------------------------------------------------------------
# The ratio
# ----------------------------------------------------------------------


def test_singletons_land_exactly_on_the_target():
    # With groups of one the greedy rule has no reason to overshoot
    assigned, _ = assign(ungrouped(50), val_ratio=0.2)
    assert count(assigned, VAL) == 10


def test_every_sample_gets_a_side():
    assigned, _ = assign(ungrouped(37))
    assert len(assigned) == 37
    assert set(assigned.values()) <= {TRAIN, VAL, HOLDOUT}


def test_the_assignment_is_deterministic_for_a_seed():
    first, _ = assign(ungrouped(40), seed=7)
    second, _ = assign(ungrouped(40), seed=7)
    assert first == second


def test_a_different_seed_assigns_differently():
    assert assign(ungrouped(40), seed=1)[0] != assign(ungrouped(40), seed=2)[0]


def test_an_empty_selection_assigns_nothing():
    assert assign({}) == ({}, Achieved(0.0, 0.0))


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
    assert count(assigned, VAL) == 20


def test_a_group_too_large_for_the_ratio_is_still_held_out():
    # No group improves on taking nothing once one exceeds twice the target,
    # which two groups guarantee. An oversized validation set is usable; an
    # empty one is not.
    members = {i: f"vid{i // 10}" for i in range(20)}
    assigned, achieved = assign(members, val_ratio=0.2)
    assert count(assigned, VAL) == 10
    assert achieved.val == pytest.approx(0.5)


def test_the_achieved_ratio_matches_the_target_when_it_can():
    _, achieved = assign(ungrouped(50), val_ratio=0.2)
    assert achieved.val == pytest.approx(0.2)


def test_the_fallback_picks_the_group_closest_to_the_target():
    members = {0: "big", 1: "big", 2: "big", 3: "big", 4: "small", 5: "small"}
    assigned, _ = assign(members, val_ratio=0.05)
    assert assigned[4] == assigned[5] == VAL
    assert all(assigned[i] == TRAIN for i in range(4))


# ----------------------------------------------------------------------
# Inheritance
# ----------------------------------------------------------------------


def test_an_inherited_side_is_never_overruled():
    inherited = dict.fromkeys(range(10), VAL)
    assigned, _ = assign(ungrouped(50), inherited=inherited, val_ratio=0.2)
    assert all(assigned[i] == VAL for i in range(10))


def test_new_samples_fill_the_deficit_only():
    # 10 already in val, target for 100 is 20, so 10 of the new 50 join it
    inherited = {i: VAL if i < 10 else TRAIN for i in range(50)}
    assigned, _ = assign(ungrouped(100), inherited=inherited, val_ratio=0.2)
    assert count(assigned, VAL) == 20


def test_a_drifted_ratio_is_corrected_by_the_next_version():
    # Over target already: the new samples all go to train rather than
    # compounding it
    inherited = dict.fromkeys(range(30), VAL)
    assigned, _ = assign(ungrouped(100), inherited=inherited, val_ratio=0.2)
    assert count(assigned, VAL) == 30
    assert all(assigned[i] == TRAIN for i in range(30, 100))


def test_a_new_member_of_an_assigned_group_inherits_its_side():
    inherited = {0: VAL, 1: VAL}
    members = {0: "vid1", 1: "vid1", 2: "vid1", 3: "vid2", 4: "vid2"}
    assert assign(members, inherited=inherited, val_ratio=0.5)[0][2] == VAL


def test_a_straddling_group_is_forced_to_train():
    # Cannot have come from here, so something else put it there; train is
    # the side that removes the leak rather than preserving it
    inherited = {0: VAL, 1: TRAIN}
    members = {0: "vid1", 1: "vid1", 2: "vid1", 3: None, 4: None}
    assigned, _ = assign(members, inherited=inherited, val_ratio=0.4)
    assert all(assigned[i] == TRAIN for i in (0, 1, 2))


def test_an_inherited_val_is_enough_and_forces_no_fallback():
    # Two groups, one already in val: nothing new can improve on the target,
    # and nothing needs to — the fallback is for an *empty* side
    inherited = {0: VAL, 1: VAL}
    members = {0: "a", 1: "a", 2: "b", 3: "b", 4: "b", 5: "b", 6: "b", 7: "b"}
    assigned, _ = assign(members, inherited=inherited, val_ratio=0.2)
    assert all(assigned[i] == TRAIN for i in range(2, 8))


# ----------------------------------------------------------------------
# The holdout
# ----------------------------------------------------------------------


def test_nothing_is_held_out_unless_asked():
    assigned, achieved = assign(ungrouped(50))
    assert count(assigned, HOLDOUT) == 0
    assert achieved.holdout == 0.0


def test_a_holdout_is_drawn_first_and_val_from_what_is_left():
    assigned, achieved = assign(ungrouped(100), val_ratio=0.2, holdout_ratio=0.1)
    assert count(assigned, HOLDOUT) == 10
    assert count(assigned, VAL) == 20
    assert count(assigned, TRAIN) == 70
    assert achieved == Achieved(0.2, 0.1)


def test_a_holdout_keeps_groups_whole():
    members = {i: f"vid{i // 5}" for i in range(50)}
    assigned, _ = assign(members, val_ratio=0.2, holdout_ratio=0.2)
    for video in {f"vid{v}" for v in range(10)}:
        assert len({assigned[i] for i, g in members.items() if g == video}) == 1
    assert count(assigned, HOLDOUT) == 10


def test_an_inherited_holdout_stays_held_out():
    # A sample measured on once is never trained on later, whatever the new
    # version asks for
    inherited = dict.fromkeys(range(10), HOLDOUT)
    assigned, achieved = assign(ungrouped(100), inherited=inherited, holdout_ratio=0.0)
    assert all(assigned[i] == HOLDOUT for i in range(10))
    assert achieved.holdout == pytest.approx(0.1)


def test_a_holdout_the_groups_cannot_afford_is_reported_empty():
    # Two groups: a forced holdout would leave one group for train and val
    # together, so the holdout stays empty and says so
    members = {i: f"vid{i // 10}" for i in range(20)}
    assigned, achieved = assign(members, val_ratio=0.2, holdout_ratio=0.2)
    assert count(assigned, HOLDOUT) == 0
    assert achieved.holdout == 0.0
    assert count(assigned, VAL) == 10


def test_a_holdout_too_small_for_its_groups_is_still_drawn_given_three():
    members = {i: f"vid{i // 10}" for i in range(30)}
    assigned, achieved = assign(members, val_ratio=0.34, holdout_ratio=0.05)
    assert count(assigned, HOLDOUT) == 10
    assert achieved.holdout == pytest.approx(1 / 3)
    assert count(assigned, VAL) == 10


# ----------------------------------------------------------------------
# Refusals
# ----------------------------------------------------------------------


def test_a_single_group_cannot_be_split():
    with pytest.raises(SplitError, match="1 group"):
        assign(dict.fromkeys(range(20), "one-video"))


def test_the_error_explains_why_rather_than_what():
    with pytest.raises(SplitError, match="indivisible"):
        assign(dict.fromkeys(range(20), "one-video"))


def test_two_samples_still_produce_both_sides():
    # round(2 * 0.2) is 0, so the greedy pass takes nothing and the fallback
    # is the only thing standing between this and an empty validation set
    assigned, achieved = assign(ungrouped(2), val_ratio=0.2)
    assert count(assigned, VAL) == 1
    assert achieved.val == pytest.approx(0.5)


def test_a_lone_sample_cannot_be_split():
    with pytest.raises(SplitError, match="1 group"):
        assign(ungrouped(1))


def test_ratio_reports_what_was_achieved():
    assert ratio([VAL, TRAIN, TRAIN, HOLDOUT]) == pytest.approx(0.25)
    assert ratio([VAL, TRAIN, TRAIN, HOLDOUT], HOLDOUT) == pytest.approx(0.25)
    assert ratio([]) == 0.0
