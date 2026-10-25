"""Deciding which side each sample is on, once: holdout drawn first, inherited sides
kept, samples with the same bytes one group. See ``docs/adr/0003``, ``docs/adr/0052``.
"""

import random
from collections.abc import Iterable, Mapping
from typing import NamedTuple

TRAIN, VAL, HOLDOUT = "train", "val", "holdout"
SIDES = (TRAIN, VAL, HOLDOUT)


class SplitError(ValueError):
    """A split that cannot produce both sides a round needs."""


class Achieved(NamedTuple):
    """The share of samples each held-out side actually got."""

    val: float
    holdout: float


def assign(
    members: dict[int, str | None],
    inherited: dict[int, str] | None = None,
    val_ratio: float = 0.2,
    holdout_ratio: float = 0.0,
    seed: int = 42,
    given: dict[int, str] | None = None,
    checksums: Mapping[int, str] | None = None,
) -> tuple[dict[int, str], Achieved]:
    """Decide a side per sample id, and report the ratios actually reached.

    ``members`` maps sample id to group id (``None`` is a group of one); ``inherited``
    sides are kept and ``given`` ones fixed first; samples sharing a ``checksums``
    entry are one group. See ``docs/adr/0024``, ``docs/adr/0052``.
    """
    if not members:
        return {}, Achieved(0.0, 0.0)

    inherited = inherited or {}
    given = given or {}
    assigned: dict[int, str] = {}
    counts: dict[str, int] = dict.fromkeys(SIDES, 0)
    for sample_id, side in given.items():
        if sample_id in members:
            assigned[sample_id] = side
            counts[side] += 1

    groups: dict[str, list[int]] = {}
    for sample_id, group_id in members.items():
        if sample_id in assigned:
            continue
        # Namespaced, or a sample's own group could collide with a real group's name
        key = f"g:{group_id}" if group_id is not None else f"s:{sample_id}"
        groups.setdefault(key, []).append(sample_id)
    if checksums:
        groups = _joined(groups, checksums)

    undecided: list[str] = []

    for key, ids in groups.items():
        sides = {inherited[i] for i in ids if i in inherited}
        if len(sides) > 1:
            # A straddling group leaks; train removes it, overruling inheritance. docs/adr/0003
            sides = {TRAIN}
        if not sides:
            undecided.append(key)
            continue
        side = sides.pop()
        for i in ids:
            assigned[i] = side
        counts[side] += len(ids)

    rng = random.Random(seed)
    rng.shuffle(undecided)
    total = len(members)

    # Holdout first, forced only with three groups; given sides count towards it. docs/adr/0024
    undecided = _fill(
        HOLDOUT,
        round(total * holdout_ratio),
        counts,
        undecided,
        groups,
        assigned,
        force=holdout_ratio > 0 and len(groups) > 2,
    )
    undecided = _fill(
        VAL, round(total * val_ratio), counts, undecided, groups, assigned, force=len(groups) > 1
    )
    for key in undecided:
        for i in groups[key]:
            assigned[i] = TRAIN
        counts[TRAIN] += len(groups[key])

    _refuse_a_one_sided_split(assigned, len(groups) + len(given))
    return assigned, Achieved(counts[VAL] / total, counts[HOLDOUT] / total)


def _joined(groups: dict[str, list[int]], checksums: Mapping[int, str]) -> dict[str, list[int]]:
    """``groups`` with every two that hold the same bytes made one, under the earliest part's key.

    See ``docs/adr/0052``.
    """
    order = {key: n for n, key in enumerate(groups)}
    parent = {key: key for key in groups}

    def root(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    first: dict[str, str] = {}
    for key, ids in groups.items():
        for sample_id in ids:
            checksum = checksums.get(sample_id)
            if checksum is None:
                continue
            a, b = root(first.setdefault(checksum, key)), root(key)
            if a != b:
                early, late = sorted((a, b), key=order.__getitem__)
                parent[late] = early

    joined: dict[str, list[int]] = {}
    for key, ids in groups.items():
        joined.setdefault(root(key), []).extend(ids)
    return joined


def _fill(
    side: str,
    target: int,
    counts: dict[str, int],
    undecided: list[str],
    groups: dict[str, list[int]],
    assigned: dict[int, str],
    *,
    force: bool,
) -> list[str]:
    """Move whole groups from ``undecided`` onto ``side`` while doing so lands closer to the target.

    See ``docs/adr/0024``.
    """
    have = counts[side]
    remaining: list[str] = []
    for key in undecided:
        ids = groups[key]
        take = abs(have + len(ids) - target) < abs(have - target)
        if take:
            for i in ids:
                assigned[i] = side
            have += len(ids)
        else:
            remaining.append(key)

    if have == 0 and remaining and force:
        # Overshooting the ratio beats an empty side, and the achieved ratio says so. docs/adr/0024
        closest = min(remaining, key=lambda k: abs(len(groups[k]) - target))
        for i in groups[closest]:
            assigned[i] = side
        have += len(groups[closest])
        remaining.remove(closest)

    counts[side] = have
    return remaining


def _refuse_a_one_sided_split(assigned: dict[int, str], group_count: int) -> None:
    val = sum(1 for s in assigned.values() if s == VAL)
    train = sum(1 for s in assigned.values() if s == TRAIN)
    if val and train:
        return
    empty, full = ("validation", "train") if not val else ("train", "validation")
    raise SplitError(
        f"Cannot split {len(assigned)} sample(s): every one is in {full} and "
        f"{empty} is empty. They form {group_count} group(s), and a group is "
        f"indivisible, so there must be at least two before one can be held out."
    )


def ratio(assigned: Iterable[str], side: str = VAL) -> float:
    sides = list(assigned)
    return sum(1 for s in sides if s == side) / len(sides) if sides else 0.0
