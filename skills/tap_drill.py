import sys
import re

def parse_metric_thread(spec):
    """
    Parse metric thread specification like M6x1.0 or M8x1.25.
    Returns major diameter and pitch.
    """
    match = re.match(r'M(\d+\.?\d*)x(\d+\.?\d*)', spec)
    if not match:
        raise ValueError(f"Invalid metric thread specification: {spec}")
    
    major_diameter = float(match.group(1))
    pitch = float(match.group(2))
    
    return major_diameter, pitch

def calculate_tap_drill(major_diameter, pitch):
    """
    Calculate tap drill size = major diameter - pitch.
    """
    tap_drill = major_diameter - pitch
    return tap_drill

def main():
    if len(sys.argv) < 2:
        print("Usage: python tap_drill.py <thread_specification>")
        sys.exit(1)
    
    spec = sys.argv[1]
    
    try:
        major_diameter, pitch = parse_metric_thread(spec)
        tap_drill = calculate_tap_drill(major_diameter, pitch)
        print(f"Thread: {spec}")
        print(f"Major Diameter: {major_diameter}mm")
        print(f"Pitch: {pitch}mm")
        print(f"Tap Drill Size: {tap_drill}mm")
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()