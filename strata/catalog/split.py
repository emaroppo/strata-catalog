"""Deciding which samples are validation, once.

Membership is written into a dataset version rather than recomputed, and a
new version inherits every side its predecessor already decided. That is the
whole point: rounds warm-start from the previous checkpoint, so a sample
that migrates into validation between rounds is scored by a model that has
already trained on it, and the number comes out flattering.

Grouping is not a special case. A standalone sample is a group of one, so
the same code keeps a video's frames together and splits plain images
individually — which is why one catalog can hold both.
"""

import random
from collections.abc import Iterable


class SplitError(ValueError):
    """A split that cannot produce both sides."""


def assign(
    members: dict[int, str | None],
    inherited: dict[int, bool] | None = None,
    val_ratio: float = 0.2,
    seed: int = 42,
) -> tuple[dict[int, bool], float]:
    """Decide a val flag per sample id, and report the ratio actually reached.

    ``members`` maps sample id to group id, where ``None`` means the sample
    is its own group. ``inherited`` carries the previous version's
    decisions, which are never overruled.

    The achieved ratio is returned rather than assumed because grouping can
    make the target unreachable — see the fallback below — and a caller that
    asked for 20% deserves to know it got 50%.
    """
    if not members:
        return {}, 0.0

    inherited = inherited or {}
    groups: dict[str, list[int]] = {}
    for sample_id, group_id in members.items():
        # A null group id is the sample's own group, and has to be namespaced
        # or two samples could collide with a real group's name
        key = f"g:{group_id}" if group_id is not None else f"s:{sample_id}"
        groups.setdefault(key, []).append(sample_id)

    assigned: dict[int, bool] = {}
    in_val = 0
    undecided: list[str] = []

    for key, ids in groups.items():
        sides = {inherited[i] for i in ids if i in inherited}
        if sides == {True, False}:
            # Half a group held out is the leak this exists to prevent. It
            # cannot have come from here, so something else put it there;
            # train removes the leak rather than preserving it, and this is
            # the only case where an inherited decision is overruled.
            sides = {False}
        if not sides:
            undecided.append(key)
            continue
        side = sides.pop()
        for i in ids:
            assigned[i] = side
        if side:
            in_val += len(ids)

    # Aim at the deficit rather than flipping a coin per sample, so a ratio
    # knocked off target by an earlier version is corrected by this one
    target = round(len(members) * val_ratio)
    rng = random.Random(seed)
    rng.shuffle(undecided)
    for key in undecided:
        ids = groups[key]
        # Take the group only while doing so lands closer to the target than
        # skipping it would, which caps the overshoot at half a group
        take = abs(in_val + len(ids) - target) < abs(in_val - target)
        for i in ids:
            assigned[i] = take
        if take:
            in_val += len(ids)

    if in_val == 0 and undecided and len(groups) > 1:
        # No group could improve on taking nothing, which happens whenever
        # one group is larger than twice the target — guaranteed with two
        # groups, since one of them holds at least half. Overshooting the
        # ratio beats an empty validation set: too large is usable, empty is
        # not. The achieved ratio is returned so a caller can say so.
        closest = min(undecided, key=lambda k: abs(len(groups[k]) - target))
        for i in groups[closest]:
            assigned[i] = True
        in_val = len(groups[closest])

    _refuse_a_one_sided_split(assigned, len(groups))
    return assigned, in_val / len(members)


def _refuse_a_one_sided_split(assigned: dict[int, bool], group_count: int) -> None:
    val = sum(1 for v in assigned.values() if v)
    if val and val != len(assigned):
        return
    empty, full = ("validation", "train") if not val else ("train", "validation")
    raise SplitError(
        f"Cannot split {len(assigned)} sample(s): every one is in {full} and "
        f"{empty} is empty. They form {group_count} group(s), and a group is "
        f"indivisible, so there must be at least two before one can be held out."
    )


def ratio(assigned: Iterable[bool]) -> float:
    flags = list(assigned)
    return sum(flags) / len(flags) if flags else 0.0
