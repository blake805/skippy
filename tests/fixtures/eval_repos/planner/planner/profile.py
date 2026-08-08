"""Plan how fast the tool may be moving at each junction of a path.

The machine has one acceleration limit. Each segment has a feed cap. The plan
is the list of junction velocities: how fast the tool is going as it crosses
from one segment into the next, plus the speed at the very start and the very
end of the path.
"""

import math
from dataclasses import dataclass
from typing import List, Sequence


@dataclass(frozen=True)
class Segment:
    length: float  # mm
    feed: float    # mm/s, this segment's velocity cap


def plan(segments: Sequence[Segment], accel: float, start: float = 0.0, end: float = 0.0) -> List[float]:
    """Velocity at each junction, as fast as the constraints allow.

    Returns len(segments) + 1 velocities: the speed entering the first segment,
    the speed at each junction between segments, and the speed at the end of
    the path. A junction between two segments never exceeds either segment's
    feed cap, and no segment is asked to change speed faster than the
    acceleration limit allows over its length.
    """
    if accel <= 0:
        raise ValueError("accel must be positive")

    velocities = [float(start)]
    for index, segment in enumerate(segments):
        # The fastest we could be going by the end of this segment, flat out.
        reachable = math.sqrt(velocities[index] ** 2 + 2.0 * accel * segment.length)
        cap = segment.feed
        if index + 1 < len(segments):
            cap = min(cap, segments[index + 1].feed)
        velocities.append(min(reachable, cap))

    if segments:
        # Arrive at the requested speed at the end of the path.
        velocities[-1] = min(velocities[-1], float(end))
    return velocities


def times(segments: Sequence[Segment], accel: float, velocities: Sequence[float]) -> List[float]:
    """Seconds spent in each segment, given the planned junction velocities."""
    return [
        _segment_time(seg.length, seg.feed, accel, v_in, v_out)
        for seg, v_in, v_out in zip(segments, velocities, velocities[1:])
    ]


def _segment_time(length: float, feed: float, accel: float, v_in: float, v_out: float) -> float:
    """Accelerate, cruise at the cap if there is room, then decelerate."""
    # The peak the tool would hit accelerating from v_in and decelerating to
    # v_out with no cruise, capped by the segment's feed.
    peak = math.sqrt((2.0 * accel * length + v_in ** 2 + v_out ** 2) / 2.0)
    peak = min(peak, feed)
    elapsed = (peak - v_in) / accel + (peak - v_out) / accel
    used = (peak ** 2 - v_in ** 2) / (2.0 * accel) + (peak ** 2 - v_out ** 2) / (2.0 * accel)
    cruise = length - used
    if cruise > 1e-12:
        elapsed += cruise / peak
    return elapsed
