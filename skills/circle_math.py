import argparse
import math

def calculate_circle_area(radius):
    """
    Calculate the area of a circle given the radius.

    Args:
        radius (float): The radius of the circle.

    Returns:
        float: The area of the circle.
    """
    if radius < 0:
        raise ValueError("Radius cannot be negative")
    return math.pi * (radius ** 2)

def main():
    parser = argparse.ArgumentParser(description="Calculate the area of a circle")
    parser.add_argument("-r", "--radius", type=float, required=True, help="The radius of the circle")
    args = parser.parse_args()
    try:
        area = calculate_circle_area(args.radius)
        print(f"The area of the circle with radius {args.radius} is {area}")
    except ValueError as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()