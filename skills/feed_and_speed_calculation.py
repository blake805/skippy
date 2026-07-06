"""
Replicates the feed and speed calculation from the 'HSMAdvisor-DLL-Test' repository.

Imports necessary libraries and modules, defines functions to calculate feed and speed,
implements the calculation logic, and tests the script with sample inputs.

Author: [Your Name]
Date: [Today's Date]
"""

import math

def calculate_feed_and_speed(tool_diameter, spindle_speed, feed_per_tooth, tool_material):
    """
    Calculates feed rate and cutting speed based on tool diameter, spindle speed, 
    feed per tooth, and tool material.

    Args:
        tool_diameter (float): The diameter of the tool in mm.
        spindle_speed (float): The speed of the spindle in rpm.
        feed_per_tooth (float): The feed per tooth in mm.
        tool_material (str): The material of the tool.

    Returns:
        tuple: A tuple containing the feed rate in mm/min and the cutting speed in m/min.
    """
    # Calculate feed rate in mm/min
    feed_rate = spindle_speed * feed_per_tooth * tool_diameter
    
    # Calculate cutting speed in m/min
    cutting_speed = (spindle_speed * math.pi * tool_diameter) / 1000
    
    return feed_rate, cutting_speed

def main():
    # Test the calculation with sample inputs
    tool_diameter = 10  # mm
    spindle_speed = 1000  # rpm
    feed_per_tooth = 0.1  # mm
    tool_material = "Carbide"

    feed_rate, cutting_speed = calculate_feed_and_speed(tool_diameter, spindle_speed, feed_per_tooth, tool_material)

    print(f"Feed Rate: {feed_rate} mm/min")
    print(f"Cutting Speed: {cutting_speed} m/min")

if __name__ == "__main__":
    main()