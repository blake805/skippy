from spindle import check, describe, is_safe, max_rpm


def test_max_rpm_derates():
    assert max_rpm() == 24000.0
    assert max_rpm(0.5) == 12000.0


def test_guard_clamps_to_the_ceiling():
    assert check(30000.0) == 24000.0
    assert check(10000.0) == 10000.0


def test_is_safe():
    assert is_safe(10000.0)
    assert not is_safe(30000.0)


def test_report_names_the_ceiling():
    assert describe() == "Spindle tops out at 24000 RPM."
