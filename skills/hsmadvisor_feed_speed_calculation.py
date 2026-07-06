import math
import argparse

def calculate_chip_load(doc, woc, flute_count):
    """
    Calculate the chip load based on the depth of cut, width of cut, and flute count.

    Args:
        doc (float): Depth of cut in mm.
        woc (float): Width of cut in mm.
        flute_count (int): Number of flutes.

    Returns:
        float: Chip load.
    """
    return (doc * woc) / flute_count

def calculate_spindle_speed(cutting_speed, tool_diameter):
    """
    Calculate the spindle speed based on the cutting speed and tool diameter.

    Args:
        cutting_speed (float): Cutting speed in m/min.
        tool_diameter (float): Tool diameter in mm.

    Returns:
        float: Spindle speed.
    """
    return (1000 * cutting_speed) / (math.pi * tool_diameter)

def calculate_feed_rate(chip_load, spindle_speed, flute_count):
    """
    Calculate the feed rate based on the chip load, spindle speed, and flute count.

    Args:
        chip_load (float): Chip load.
        spindle_speed (float): Spindle speed.
        flute_count (int): Number of flutes.

    Returns:
        float: Feed rate.
    """
    return chip_load * spindle_speed * flute_count

def apply_chip_thinning_compensation(feed_rate, chip_thinning_factor):
    """
    Apply the chip thinning compensation to the feed rate.

    Args:
        feed_rate (float): Feed rate.
        chip_thinning_factor (float): Chip thinning factor.

    Returns:
        float: Feed rate with chip thinning compensation.
    """
    return feed_rate * chip_thinning_factor

def validate_calculation(spindle_speed, feed_rate, max_rpm, min_rpm, max_feed_rate, min_feed_rate):
    """
    Validate the calculation by checking the machine profile constraints.

    Args:
        spindle_speed (float): Spindle speed.
        feed_rate (float): Feed rate.
        max_rpm (float): Maximum RPM.
        min_rpm (float): Minimum RPM.
        max_feed_rate (float): Maximum feed rate.
        min_feed_rate (float): Minimum feed rate.

    Returns:
        bool: Whether the calculation is valid.
    """
    if spindle_speed > max_rpm or spindle_speed < min_rpm:
        return False
    if feed_rate > max_feed_rate or feed_rate < min_feed_rate:
        return False
    return True

def adjust_calculation(spindle_speed, feed_rate, max_rpm, min_rpm, max_feed_rate, min_feed_rate):
    """
    Adjust the calculation if necessary to ensure that the spindle speed and feed rate are within the allowed limits.

    Args:
        spindle_speed (float): Spindle speed.
        feed_rate (float): Feed rate.
        max_rpm (float): Maximum RPM.
        min_rpm (float): Minimum RPM.
        max_feed_rate (float): Maximum feed rate.
        min_feed_rate (float): Minimum feed rate.

    Returns:
        tuple: Adjusted spindle speed and feed rate.
    """
    if spindle_speed > max_rpm:
        spindle_speed = max_rpm
    elif spindle_speed < min_rpm:
        spindle_speed = min_rpm
    if feed_rate > max_feed_rate:
        feed_rate = max_feed_rate
    elif feed_rate < min_feed_rate:
        feed_rate = min_feed_rate
    return spindle_speed, feed_rate

def main():
    parser = argparse.ArgumentParser(description='HSMAdvisor Feed and Speed Calculation Script')
    parser.add_argument('--doc', type=float, help='Depth of cut in mm', required=True)
    parser.add_argument('--woc', type=float, help='Width of cut in mm', required=True)
    parser.add_argument('--flute_count', type=int, help='Number of flutes', required=True)
    parser.add_argument('--cutting_speed', type=float, help='Cutting speed in m/min', required=True)
    parser.add_argument('--tool_diameter', type=float, help='Tool diameter in mm', required=True)
    parser.add_argument('--chip_thinning_factor', type=float, help='Chip thinning factor', required=True)
    parser.add_argument('--max_rpm', type=float, help='Maximum RPM', required=True)
    parser.add_argument('--min_rpm', type=float, help='Minimum RPM', required=True)
    parser.add_argument('--max_feed_rate', type=float, help='Maximum feed rate', required=True)
    parser.add_argument('--min_feed_rate', type=float, help='Minimum feed rate', required=True)
    args = parser.parse_args()

    chip_load = calculate_chip_load(args.doc, args.woc, args.flute_count)
    spindle_speed = calculate_spindle_speed(args.cutting_speed, args.tool_diameter)
    feed_rate = calculate_feed_rate(chip_load, spindle_speed, args.flute_count)
    feed_rate_comp = apply_chip_thinning_compensation(feed_rate, args.chip_thinning_factor)

    if not validate_calculation(spindle_speed, feed_rate_comp, args.max_rpm, args.min_rpm, args.max_feed_rate, args.min_feed_rate):
        spindle_speed, feed_rate_comp = adjust_calculation(spindle_speed, feed_rate_comp, args.max_rpm, args.min_rpm, args.max_feed_rate, args.min_feed_rate)

    print("Spindle Speed:", spindle_speed)
    print("Feed Rate:", feed_rate_comp)

if __name__ == '__main__':
    main()