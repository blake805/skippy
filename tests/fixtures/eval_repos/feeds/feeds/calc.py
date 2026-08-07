"""Feeds and speeds.

Chip load is the thickness of material each tooth removes per revolution:

    chip load = feed rate / (RPM * number of teeth)

with feed in inches per minute and the result in inches per tooth.
"""


def rpm_for(surface_speed: float, diameter: float) -> float:
    """Spindle RPM for a surface speed in feet per minute and a cutter diameter."""
    return (surface_speed * 12.0) / (3.14159 * diameter)


def chip_load(feed_rate: float, rpm: float, teeth: int) -> float:
    """Inches per tooth, from a feed rate in inches per minute."""
    return feed_rate / rpm


def feed_rate(chip: float, rpm: float, teeth: int) -> float:
    """Inches per minute needed to hold a given chip load."""
    return chip * rpm * teeth
