from gauge import CALIBRATION_OFFSET, load_readings, summarize


def test_suspect_rows_are_dropped():
    assert len(load_readings()) == 2978


def test_summarize_takes_the_offset_off():
    mean, _ = summarize()
    raw = sum(load_readings()) / len(load_readings())
    assert abs(raw - mean - CALIBRATION_OFFSET) < 1e-9


def test_the_calibrated_mean_is_below_nominal():
    # Bounds, not the exact figures: an eval task grades an agent on producing the
    # summarize() numbers, and a test that states them hands the answer to anything
    # that greps the repo instead of running the code.
    mean, stdev = summarize()
    assert 0.995 < mean < 1.0
    assert 0.0 < stdev < 0.005
