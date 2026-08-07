from units import inch_to_mm, mm_to_inch


def test_mm_to_inch():
    assert mm_to_inch(25.4) == 1.0


def test_inch_to_mm():
    assert inch_to_mm(1.0) == 25.4


def test_round_trip():
    assert abs(mm_to_inch(inch_to_mm(3.5)) - 3.5) < 1e-9
