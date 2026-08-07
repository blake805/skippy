"""Human-readable summaries of the spindle's configuration."""

from .motor import max_rpm


def describe(derate: float = 1.0) -> str:
    """One line naming the motor's ceiling."""
    return f"Spindle tops out at {max_rpm(derate):.0f} RPM."
