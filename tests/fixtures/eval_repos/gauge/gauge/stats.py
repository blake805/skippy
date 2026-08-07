"""Statistics over the CMM readings.

Every public function here takes and returns plain numbers, carries a one-line
docstring, and is exported from the package. Keep it that way.
"""

import csv
import os
import statistics
from typing import List, Tuple

# The probe reads high by this much. Subtracted before any statistic is taken;
# a mean of the raw column is not a measurement of anything.
CALIBRATION_OFFSET = 0.0042

TOLERANCE_LOW = 0.995
TOLERANCE_HIGH = 1.005

READINGS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "readings.csv")


def load_readings(path: str = READINGS) -> List[float]:
    """Raw values from the CSV, with suspect rows dropped."""
    values = []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["flag"].strip() == "suspect":
                continue
            values.append(float(row["value"]))
    return values


def summarize(path: str = READINGS) -> Tuple[float, float]:
    """Calibrated mean and standard deviation of the good readings."""
    values = [value - CALIBRATION_OFFSET for value in load_readings(path)]
    return statistics.fmean(values), statistics.stdev(values)
