import sys

def calculate_radius(chord_length, arc_height):
    """
    Calculate the radius of a circle given the chord length and arc height.

    Args:
        chord_length (float): The length of the chord.
        arc_height (float): The height of the arc.

    Returns:
        float: The calculated radius.
    """
    radius = (chord_length**2 + 4*arc_height**2) / (8*arc_height)
    return radius

if __name__ == "__main__":
    # Check if the correct number of command-line arguments are provided
    if len(sys.argv)!= 3:
        print("Usage: python radius_calculator.py <chord_length> <arc_height>")
        sys.exit(1)

    try:
        # Attempt to convert the command-line arguments to floats
        chord_length = float(sys.argv[1])
        arc_height = float(sys.argv[2])

        # Check for division by zero
        if arc_height == 0:
            print("Error: Arc height cannot be zero.")
            sys.exit(1)

        # Calculate and print the radius
        radius = calculate_radius(chord_length, arc_height)
        print(f"The radius is: {radius}")
    except ValueError:
        # Handle invalid input (non-numeric values)
        print("Error: Invalid input. Please provide valid numbers for chord length and arc height.")