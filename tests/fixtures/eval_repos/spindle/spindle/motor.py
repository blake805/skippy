"""The spindle motor's limits."""

BASE_RPM = 24000.0


def max_rpm(derate: float = 1.0) -> float:
    """The highest RPM the motor will hold, after any derating factor."""
    return BASE_RPM * derate
