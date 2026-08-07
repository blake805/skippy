from feeds import chip_load, feed_rate, rpm_for


def test_rpm_for_a_half_inch_cutter():
    assert abs(rpm_for(300.0, 0.5) - 2291.83) < 0.1


def test_chip_load_divides_by_the_teeth():
    # 40 IPM, 4000 RPM, 4 flutes: each tooth takes 0.0025".
    assert abs(chip_load(40.0, 4000.0, 4) - 0.0025) < 1e-9


def test_feed_rate_is_the_inverse_of_chip_load():
    assert abs(feed_rate(chip_load(40.0, 4000.0, 4), 4000.0, 4) - 40.0) < 1e-9
