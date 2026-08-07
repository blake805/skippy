"""Unit conversions for shop work.

Every public function here takes and returns a float, carries a one-line
docstring, and is exported from the package. Keep it that way.
"""

MM_PER_INCH = 25.4


def mm_to_inch(mm: float) -> float:
    """Millimetres to inches."""
    return mm / MM_PER_INCH


def inch_to_mm(inch: float) -> float:
    """Inches to millimetres."""
    return inch * MM_PER_INCH
