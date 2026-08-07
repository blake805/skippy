"""Refuses a commanded speed the motor cannot hold."""

from .motor import max_rpm


def check(requested: float, derate: float = 1.0) -> float:
    """The speed that will actually be commanded, clamped to what the motor can do."""
    ceiling = max_rpm(derate)
    return ceiling if requested > ceiling else requested


def is_safe(requested: float, derate: float = 1.0) -> bool:
    """True when the requested speed is within the motor's limit."""
    return requested <= max_rpm(derate)
