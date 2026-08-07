from gauge import CALIBRATION_OFFSET, load_readings, summarize


def test_suspect_rows_are_dropped():
    assert len(load_readings()) == 2978


def test_summarize_takes_the_offset_off():
    mean, _ = summarize()
    raw = sum(load_readings()) / len(load_readings())
    assert abs(raw - mean - CALIBRATION_OFFSET) < 1e-9


def test_the_calibrated_mean_is_below_nominal():
    mean, stdev = summarize()
    assert round(mean, 4) == 0.9988
    assert round(stdev, 4) == 0.0037
