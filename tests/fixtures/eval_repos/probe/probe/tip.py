"""Touch probe geometry.

The probe reports the position of the *tip's centre*. To get the surface it
touched, you subtract the tip radius — the standoff is where the probe parks
after a touch and has nothing to do with the offset.

    surface = reported position - tip radius
"""

TIP_RADIUS = 0.0625
STANDOFF = 0.1


def tip_offset(radius: float = TIP_RADIUS) -> float:
    """How far to correct a reported touch to get the surface. One tip radius."""
    return radius


def park_height(surface: float, standoff: float = STANDOFF) -> float:
    """Where the probe parks above a surface it has just touched."""
    return surface + standoff
