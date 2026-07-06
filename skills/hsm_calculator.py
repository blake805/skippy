import sys
import argparse

def calculate_spindle_speed(D, V_c):
    """
    Calculate spindle speed (S) in RPM.

    :param D: Tool diameter in mm
    :param V_c: Cutting speed in m/min
    :return: Spindle speed in RPM
    """
    S = (V_c * 1000) / (3.14159 * D)
    return S

def calculate_feed_rate(D, F, DOC, WOC, S):
    """
    Calculate feed rate (FR) in mm/min.

    :param D: Tool diameter in mm
    :param F: Flute count
    :param DOC: Depth of cut in mm
    :param WOC: Width of cut in mm
    :param S: Spindle speed in RPM
    :return: Feed rate in mm/min
    """
    FR = (F * S * D) / (2 * 3.14159)
    return FR

def apply_chip_thinning_compensation(FR, CTF):
    """
    Apply chip thinning compensation to feed rate.

    :param FR: Feed rate in mm/min
    :param CTF: Chip thinning factor
    :return: Compensated feed rate in mm/min
    """
    FR_comp = FR * CTF
    return FR_comp

def validate_calculation(S, FR, max_RPM, min_RPM, max_feed_rate, min_feed_rate):
    """
    Validate calculation against machine profile constraints and adjust results.

    :param S: Spindle speed in RPM
    :param FR: Feed rate in mm/min
    :param max_RPM: Maximum allowed RPM
    :param min_RPM: Minimum allowed RPM
    :param max_feed_rate: Maximum allowed feed rate in mm/min
    :param min_feed_rate: Minimum allowed feed rate in mm/min
    :return: Adjusted spindle speed and feed rate
    """
    if S > max_RPM:
        S = max_RPM
    elif S < min_RPM:
        S = min_RPM

    if FR > max_feed_rate:
        FR = max_feed_rate
    elif FR < min_feed_rate:
        FR = min_feed_rate

    return S, FR

def main():
    parser = argparse.ArgumentParser(description='HSM Calculator')
    parser.add_argument('--D', type=float, help='Tool diameter in mm', required=True)
    parser.add_argument('--F', type=int, help='Flute count', required=True)
    parser.add_argument('--DOC', type=float, help='Depth of cut in mm', required=True)
    parser.add_argument('--WOC', type=float, help='Width of cut in mm', required=True)
    parser.add_argument('--V_c', type=float, help='Cutting speed in m/min', required=True)
    parser.add_argument('--CTF', type=float, help='Chip thinning factor', required=True)
    parser.add_argument('--material_ID', type=int, help='Material ID', required=True)
    parser.add_argument('--max_RPM', type=int, help='Maximum allowed RPM', required=True)
    parser.add_argument('--min_RPM', type=int, help='Minimum allowed RPM', required=True)
    parser.add_argument('--max_feed_rate', type=int, help='Maximum allowed feed rate in mm/min', required=True)
    parser.add_argument('--min_feed_rate', type=int, help='Minimum allowed feed rate in mm/min', required=True)

    args = parser.parse_args()

    S = calculate_spindle_speed(args.D, args.V_c)
    FR = calculate_feed_rate(args.D, args.F, args.DOC, args.WOC, S)
    FR_comp = apply_chip_thinning_compensation(FR, args.CTF)
    adjusted_S, adjusted_FR = validate_calculation(S, FR_comp, args.max_RPM, args.min_RPM, args.max_feed_rate, args.min_feed_rate)

    print("Adjusted Spindle Speed:", adjusted_S)
    print("Adjusted Feed Rate:", adjusted_FR)

if __name__ == "__main__":
    main()