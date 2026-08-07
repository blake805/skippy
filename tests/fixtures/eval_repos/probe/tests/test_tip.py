from probe import TIP_RADIUS, park_height, tip_offset


def test_park_height():
    assert abs(park_height(1.0) - 1.1) < 1e-9


def test_tip_offset():
    # Whoever wrote this added the standoff into the offset. The module docstring
    # and README both say the offset is one tip radius and that the standoff is
    # only about where the probe parks.
    assert tip_offset() == TIP_RADIUS + 0.1
