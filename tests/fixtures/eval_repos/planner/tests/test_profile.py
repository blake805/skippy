"""The physics the planner must satisfy.

A junction velocity the tool cannot actually reach, or cannot shed before the
next constraint, is not a slow plan — it is an impossible one, and the
controller faults mid-cut. So the invariant tested here is symmetric: over any
segment, the speed may rise by at most what the acceleration limit allows over
that length, and it may FALL by at most the same amount. Braking is bounded by
the same limit as accelerating.
"""

import math

import pytest

from planner.profile import Segment, plan, times

ACCEL = 5.0  # mm/s^2


def test_junction_speeds_respect_the_feed_caps():
    segments = [Segment(200, 30), Segment(200, 10), Segment(200, 30)]
    velocities = plan(segments, ACCEL)
    assert velocities[0] == 0.0
    assert velocities[-1] == 0.0
    for index, segment in enumerate(segments):
        assert velocities[index] <= segment.feed + 1e-9
        assert velocities[index + 1] <= segment.feed + 1e-9


def test_every_junction_is_reachable_under_the_acceleration_limit():
    """The invariant. Speed may rise by at most 2*a*L over a segment, and may
    fall by at most the same — a stop the tool cannot brake for is a fault, not
    a plan. Several profiles, because the constraint has to propagate: a short
    segment before a stop limits the junction before it, which limits the one
    before that."""
    profiles = [
        [Segment(500, 40), Segment(4, 40)],
        [Segment(300, 25), Segment(3, 25), Segment(2, 25)],
        [Segment(80, 35), Segment(6, 35), Segment(5, 35), Segment(4, 35)],
    ]
    for segments in profiles:
        velocities = plan(segments, ACCEL)
        assert velocities[-1] == pytest.approx(0.0, abs=1e-9)
        for segment, v_in, v_out in zip(segments, velocities, velocities[1:]):
            can_change = 2.0 * ACCEL * segment.length
            assert v_out ** 2 <= v_in ** 2 + can_change + 1e-6, (
                f"cannot accelerate {v_in:.2f}->{v_out:.2f} over {segment.length}mm"
            )
            assert v_in ** 2 <= v_out ** 2 + can_change + 1e-6, (
                f"cannot brake {v_in:.2f}->{v_out:.2f} over {segment.length}mm"
            )


def test_junction_speeds_are_as_fast_as_physics_allows():
    """Feasible must not mean timid. With 10mm left to stop in, the tool can
    cross the junction at exactly sqrt(2*a*10) — no faster, and a planner that
    crosses it any slower is leaving cycle time on the table."""
    segments = [Segment(90, 30), Segment(10, 30)]
    velocities = plan(segments, ACCEL)
    assert velocities[1] == pytest.approx(math.sqrt(2.0 * ACCEL * 10.0))


def test_the_planner_does_not_crawl_when_it_does_not_have_to():
    """A long easy path takes exactly the textbook time: accelerate to the cap,
    cruise, brake at the end. Guards against any 'fix' that slows everything."""
    segments = [Segment(1000, 50), Segment(1000, 50)]
    velocities = plan(segments, ACCEL)
    assert velocities[1] == pytest.approx(50.0)
    assert sum(times(segments, ACCEL, velocities)) == pytest.approx(50.0, rel=0.01)


def test_nonzero_start_and_end_speeds_are_honoured():
    segments = [Segment(50, 20)]
    velocities = plan(segments, ACCEL, start=5.0, end=8.0)
    assert velocities[0] == pytest.approx(5.0)
    assert velocities[-1] == pytest.approx(8.0)
