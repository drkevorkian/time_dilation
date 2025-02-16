import math
import logging
import tkinter as tk
from tkinter import ttk, scrolledtext
from decimal import Decimal, getcontext, InvalidOperation, DivisionByZero, Overflow

getcontext().prec = 200  # Set precision to handle extreme values

# Speed of light in different units
C_MS = Decimal("299792458")  # m/s
C_CMS = C_MS * 100         # cm/s
C_MMS = C_MS * 1000        # mm/s
C_UM = C_MS * 1000000       # micrometers/s
C_NM = C_MS * Decimal("1e9")          # nanometers/s

c_units = [C_MS, C_CMS, C_MMS, C_UM, C_NM]

SI_PREFIXES = [
    (Decimal("1e30"), "Nonillion"),
    (Decimal("1e27"), "Octillion"),
    (Decimal("1e24"), "Septillion"),
    (Decimal("1e21"), "Sextillion"),
    (Decimal("1e18"), "Quintillion"),
    (Decimal("1e15"), "Quadrillion"),
    (Decimal("1e12"), "Trillion"),
    (Decimal("1e9"), "Billion"),
    (Decimal("1e6"), "Million"),
    (Decimal("1e3"), "Thousand"),
    (Decimal("1e0"), ""),
    (Decimal("1e-3"), "Milli"),
    (Decimal("1e-6"), "Micro"),
    (Decimal("1e-9"), "Nano"),
    (Decimal("1e-12"), "Pico"),
    (Decimal("1e-15"), "Femto"),
    (Decimal("1e-18"), "Atto"),
    (Decimal("1e-21"), "Zepto"),
    (Decimal("1e-24"), "Yocto"),
    (Decimal("1e-27"), "Ronto"),
    (Decimal("1e-30"), "Quecto")
]

# Distance conversion constants (as Decimal)
KM_PER_LIGHTYEAR = Decimal("9.461e12")  # kilometers in a light year
MILES_PER_LIGHTYEAR = Decimal("5.879e12")  # miles in a light year

def safe_decimal_convert(value, error_msg="Invalid decimal conversion"):
    """
    Safely convert values to Decimal with proper error handling.
    
    Args:
        value: The value to convert to Decimal
        error_msg (str): Custom error message for logging
        
    Returns:
        Decimal: The converted value
        None: If conversion fails
        
    Example:
        >>> safe_decimal_convert("123.456")
        Decimal('123.456')
        >>> safe_decimal_convert("invalid")
        None
    """
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as e:
        logging.error(f"{error_msg}: {str(e)}")
        return None

def format_large_or_small_number(value):
    """Convert large or small numbers into human-readable SI prefixes."""
    value = Decimal(str(value))  # Ensure compatibility with Decimal operations
    for factor, prefix in SI_PREFIXES:
        if abs(value) >= factor:
            # Format number and remove trailing zeros after decimal point
            formatted = f"{value / factor:.50f}".rstrip('0').rstrip('.')
            return f"{formatted} {prefix}".strip()
    # For very small numbers, also remove trailing zeros
    return f"{value:.100f}".rstrip('0').rstrip('.')  # rstrip('.') removes decimal point if no decimals

def format_time(seconds):
    """Convert seconds to years, months, days, hours, minutes, seconds while preserving precision."""
    try:
        seconds = Decimal(str(seconds))
        years = seconds // Decimal("31557600")  # 365.25 days in a year
        seconds %= Decimal("31557600")
        months = seconds // Decimal("2629800")  # Approx. 30.44 days per month
        seconds %= Decimal("2629800")
        days = seconds // Decimal("86400")
        seconds %= Decimal("86400")
        hours = seconds // Decimal("3600")
        seconds %= Decimal("3600")
        minutes = seconds // Decimal("60")
        seconds %= Decimal("60")
        time_parts = []
        if years > 0:
            time_parts.append(f"{years} years")
        if months > 0:
            time_parts.append(f"{months} months")
        if days > 0:
            time_parts.append(f"{days} days")
        if hours > 0:
            time_parts.append(f"{hours} hours")
        if minutes > 0:
            time_parts.append(f"{minutes} minutes")
        if seconds != 0:
            time_parts.append(f"{format_large_or_small_number(seconds)} seconds")
        return ", ".join(time_parts) if time_parts else "0 seconds"
    except:
        return "Error calculating time"

def time_dilation_factor(velocity_percentage, c):
    try:
        velocity_percentage = Decimal(str(velocity_percentage))
        velocity = (velocity_percentage / 100) * c
        gamma = 1 / math.sqrt(1 - (velocity**2 / c**2))
        return gamma
    except (ValueError, ArithmeticError):
        return None

def time_dilation(time_earth, velocity_percentage, c):
    gamma = Decimal(str(time_dilation_factor(velocity_percentage, c)))
    if gamma is None:
        return None
    return Decimal(str(time_earth)) / gamma

def convert_to_lightyears(value, from_unit):
    """
    Convert a distance value to light years.
    
    Args:
        value (Decimal): The distance value to convert
        from_unit (str): The unit to convert from ('ly', 'km', 'mi')
        
    Returns:
        Decimal: The distance in light years
    """
    try:
        value = Decimal(str(value))
        if from_unit == 'ly':
            return value
        elif from_unit == 'km':
            return value / KM_PER_LIGHTYEAR
        elif from_unit == 'mi':
            return value / MILES_PER_LIGHTYEAR
        else:
            raise ValueError(f"Unsupported unit: {from_unit}")
    except (InvalidOperation, DivisionByZero) as e:
        logging.error(f"Conversion error: {str(e)}")
        return None

def convert_from_lightyears(lightyears, to_unit):
    """
    Convert from light years to specified unit.
    
    Args:
        lightyears (Decimal): The distance in light years
        to_unit (str): The unit to convert to ('ly', 'km', 'mi')
        
    Returns:
        Decimal: The converted distance value
    """
    try:
        lightyears = Decimal(str(lightyears))
        if to_unit == 'ly':
            return lightyears
        elif to_unit == 'km':
            return lightyears * KM_PER_LIGHTYEAR
        elif to_unit == 'mi':
            return lightyears * MILES_PER_LIGHTYEAR
        else:
            raise ValueError(f"Unsupported unit: {to_unit}")
    except (InvalidOperation, DivisionByZero) as e:
        logging.error(f"Conversion error: {str(e)}")
        return None

def convert_distance_to_travel_time(distance, velocity_percentage, from_unit='ly'):
    """
    Convert a distance to the time it would take to travel at given velocity.
    
    Args:
        distance (Decimal): The distance to travel
        velocity_percentage (Decimal): Velocity as percentage of c
        from_unit (str): The unit of distance ('ly', 'km', 'mi')
        
    Returns:
        Decimal: The time in years it would take to travel this distance at given velocity
    """
    try:
        # First convert distance to light years if needed
        if from_unit == 'ly':
            light_years = Decimal(str(distance))
        elif from_unit == 'km':
            light_years = Decimal(str(distance)) / KM_PER_LIGHTYEAR
        elif from_unit == 'mi':
            light_years = Decimal(str(distance)) / MILES_PER_LIGHTYEAR
        else:
            raise ValueError(f"Unsupported unit: {from_unit}")
            
        # Calculate travel time: distance / velocity
        velocity_fraction = velocity_percentage / 100
        travel_time_years = light_years / velocity_fraction
        
        return travel_time_years * Decimal("31557600")  # Convert years to seconds
        
    except (InvalidOperation, DivisionByZero) as e:
        logging.error(f"Travel time calculation error: {str(e)}")
        return None

def format_output(earth_time, traveler_time, gamma, velocity_str, unit="m/s", measurement_type="Time"):
    """Format calculation results with measurement type."""
    try:
        output = [
            "\n=== Time Dilation Calculation Results ===",
            f"Velocity: {velocity_str}% of c ({unit})",
            f"Gamma factor: {format_large_or_small_number(gamma)}",
            "\nMeasurement Details:",
            "-" * 40,
        ]
        
        # Always show results as time
        output.extend([
            "Earth time:",
            f"  {format_time(earth_time)}",
            f"  ({format_large_or_small_number(earth_time)} seconds)",
            "\nTraveler time:",
            f"  {format_time(traveler_time)}",
            f"  ({format_large_or_small_number(traveler_time)} seconds)",
        ])
        
        output.append("-" * 40)
        return "\n".join(output)
    except Exception as e:
        logging.error(f"Output formatting error: {str(e)}")
        return "Error formatting output"

class TimeDilationCalculator(tk.Tk):
    """
    Main application window for the Time Dilation Calculator.
    
    This class handles the GUI interface and calculation logic for computing
    relativistic time dilation effects. It includes input validation, progress
    tracking, and result formatting.
    """
    
    def __init__(self):
        """Initialize the calculator window and set up the GUI components."""
        super().__init__()
        
        # Add security measures
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.resizable(True, True)  # Allow resizing but with constraints
        self.minsize(800, 600)  # Set minimum window size
        
        self.title("Time Dilation Calculator")
        self.geometry("800x600")
        self.configure(bg='#f0f0f0')

        # Configure grid weights for main window
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Create main frame
        main_frame = ttk.Frame(self, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        # Configure grid weights for main_frame
        main_frame.grid_columnconfigure(1, weight=1)  # Make column 1 (entry fields) expandable
        main_frame.grid_rowconfigure(5, weight=1)     # Make row 5 (results text) expandable

        # Input fields
        ttk.Label(main_frame, text="Velocity (% of c):").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.velocity_var = tk.StringVar()
        self.velocity_entry = ttk.Entry(main_frame, textvariable=self.velocity_var, width=40)
        self.velocity_entry.grid(row=0, column=1, sticky=tk.W, pady=5)
        self.velocity_entry.insert(0, "99.999999999999")
        self.velocity_entry.bind("<FocusIn>", self.clear_placeholder)
        self.velocity_entry.bind("<FocusOut>", self.restore_placeholder)

        # Add measurement type selection
        ttk.Label(main_frame, text="Measurement Type:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.measurement_var = tk.StringVar(value="Time")
        self.measurement_combo = ttk.Combobox(main_frame, textvariable=self.measurement_var,
                                            values=["Time", "Distance"],
                                            state="readonly")
        self.measurement_combo.grid(row=1, column=1, sticky=tk.W, pady=5)
        self.measurement_combo.bind('<<ComboboxSelected>>', self.update_measurement_label)

        # Input field for time/distance with stored reference
        self.measurement_label = ttk.Label(main_frame, text="Time on Earth (years):")
        self.measurement_label.grid(row=2, column=0, sticky=tk.W, pady=5)
        self.time_var = tk.StringVar()
        self.time_entry = ttk.Entry(main_frame, textvariable=self.time_var, width=40)
        self.time_entry.grid(row=2, column=1, sticky=tk.W, pady=5)

        # Add distance unit selection (initially hidden)
        self.distance_unit_label = ttk.Label(main_frame, text="Distance Unit:")
        self.distance_unit_var = tk.StringVar(value="Light Years (ly)")  # Set default with full text
        self.distance_unit_combo = ttk.Combobox(main_frame, textvariable=self.distance_unit_var,
                                               values=["Light Years (ly)", "Kilometers (km)", "Miles (mi)"],
                                               state="readonly", width=20)
        
        # Hide distance unit widgets initially
        self.distance_unit_label.grid(row=2, column=2, sticky=tk.W, pady=5)
        self.distance_unit_combo.grid(row=2, column=3, sticky=tk.W, pady=5)
        self.distance_unit_label.grid_remove()
        self.distance_unit_combo.grid_remove()

        # Add unit selection
        ttk.Label(main_frame, text="Speed of Light Unit:").grid(row=3, column=0, sticky=tk.W, pady=5)
        self.unit_var = tk.StringVar(value="m/s")
        self.unit_combo = ttk.Combobox(main_frame, textvariable=self.unit_var, 
                                      values=["m/s", "cm/s", "mm/s", "μm/s", "nm/s"],
                                      state="readonly")
        self.unit_combo.grid(row=3, column=1, sticky=tk.W, pady=5)
        
        # Calculate button
        self.calc_button = ttk.Button(main_frame, text="Calculate", command=self.calculate)
        self.calc_button.grid(row=4, column=0, columnspan=3, pady=20)

        # Results display
        ttk.Label(main_frame, text="Results:").grid(row=5, column=0, sticky=tk.W)
        self.results_text = scrolledtext.ScrolledText(main_frame, width=80, height=20)
        self.results_text.grid(row=6, column=0, columnspan=3, pady=5, sticky=(tk.W, tk.E, tk.N, tk.S))

        # Add copy button
        self.copy_button = ttk.Button(main_frame, text="Copy Results", command=self.copy_results)
        self.copy_button.grid(row=7, column=0, columnspan=3, pady=5)
        
        # Progress bar
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(main_frame, mode='determinate', 
                                          variable=self.progress_var)
        self.progress_bar.grid(row=8, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=5)

        # Status bar
        self.status_var = tk.StringVar()
        self.status_bar = ttk.Label(self, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.grid(row=1, column=0, sticky=(tk.W, tk.E))

        # Add tooltips to input fields
        self.create_tooltip(self.velocity_entry, 
            "Enter velocity as a percentage of light speed (0-100 exclusive)"
            "e.g., 99.999999999999, this will accuratly calculate a "
             " max of 199 digits past the decimal point"
            )
        self.create_tooltip(self.measurement_combo, 
            "Select the measurement type (Time/Distance)")
        self.create_tooltip(self.copy_button, 
            "Copy the calculation results to clipboard")
        
        # Update progress bar steps
        self.PROGRESS_STEPS = {
            'START': 0,
            'INPUT_VALIDATED': 20,
            'UNIT_SELECTED': 30,
            'GAMMA_CALCULATED': 60,
            'TIME_CALCULATED': 80,
            'COMPLETE': 100
        }

    def create_tooltip(self, widget, text):
        """
        Create a tooltip for a given widget.
        
        Args:
            widget: The tkinter widget to add the tooltip to
            text (str): The tooltip text to display
        """
        def show_tooltip(event):
            tooltip = tk.Toplevel()
            tooltip.wm_overrideredirect(True)
            tooltip.wm_geometry(f"+{event.x_root+10}+{event.y_root+10}")
            
            label = ttk.Label(tooltip, text=text, background="#ffffe0", 
                            relief='solid', borderwidth=1)
            label.pack()
            
            def hide_tooltip():
                tooltip.destroy()
            
            widget.tooltip = tooltip
            widget.bind('<Leave>', lambda e: hide_tooltip())
            
        widget.bind('<Enter>', show_tooltip)
    
    def copy_results(self):
        """Copy results to clipboard with proper error handling."""
        try:
            self.clipboard_clear()
            self.clipboard_append(self.results_text.get(1.0, tk.END))
            self.status_var.set("Results copied to clipboard")
        except Exception as e:
            logging.error(f"Clipboard error: {str(e)}")
            self.status_var.set("Failed to copy results")
    
    def on_closing(self):
        """Handle window closing safely."""
        try:
            self.clipboard_clear()  # Clear clipboard for security
            logging.info("Application closing")
            self.destroy()
        except Exception as e:
            logging.error(f"Error during cleanup: {str(e)}")
            self.destroy()
            
    def update_measurement_label(self, event=None):
        """Update the label and show/hide distance units based on measurement type selection."""
        measurement_type = self.measurement_var.get()
        
        if measurement_type == "Time":
            self.measurement_label.configure(text="Time on Earth (years):")
            self.create_tooltip(self.time_entry, 
                "Enter the amount of time that passes on Earth (in years)")
            self.distance_unit_label.grid_remove()
            self.distance_unit_combo.grid_remove()
        else:  # Distance
            self.measurement_label.configure(text="Distance to Travel:")
            self.create_tooltip(self.time_entry, 
                "Enter the distance to travel (units selectable on the right)")
            self.distance_unit_label.grid()
            self.distance_unit_combo.grid()

    def calculate(self):
        """
        Perform time dilation calculations with correct distance-to-time conversion.
        
        This method:
        1. Validates user input
        2. Performs the time dilation calculation
        3. Updates the progress bar
        4. Displays formatted results
        
        Raises:
            ValueError: For invalid input values
            ArithmeticError: For calculation errors
        """
        self.results_text.delete(1.0, tk.END)
        self.status_var.set("Calculating...")
        self.progress_var.set(self.PROGRESS_STEPS['START'])
        
        try:
            # Input validation
            velocity = safe_decimal_convert(self.velocity_var.get())
            if velocity is None:
                raise ValueError("Please enter a valid number for velocity")
            
            # Convert input based on measurement type
            measurement_type = self.measurement_var.get()
            input_value = safe_decimal_convert(self.time_var.get())
            
            if input_value is None:
                raise ValueError(f"Please enter a valid number for {measurement_type.lower()}")
            
            if input_value <= 0:
                raise ValueError(f"{measurement_type} must be positive")

            # Calculate earth time based on measurement type
            if measurement_type == "Distance":
                # Extract unit code from the full text
                unit_text = self.distance_unit_var.get()
                if "Light Years" in unit_text:
                    unit = 'ly'
                elif "Kilometers" in unit_text:
                    unit = 'km'
                elif "Miles" in unit_text:
                    unit = 'mi'
                else:
                    raise ValueError(f"Unsupported unit: {unit_text}")
                    
                # Calculate how long it takes to travel the distance at given velocity
                earth_time = convert_distance_to_travel_time(input_value, velocity, unit)
                if earth_time is None:
                    raise ValueError("Error calculating travel time")
            else:
                earth_time = input_value * Decimal("31557600")  # Convert years to seconds
            
            self.progress_var.set(self.PROGRESS_STEPS['INPUT_VALIDATED'])
            
            # Velocity range validation
            if not (0 < velocity < 100):
                raise ValueError("Velocity must be between 0 and 100 percent of c (exclusive)")
            
            if velocity >= 99.9999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999:
                raise ValueError(
                    "Velocity is too close to the speed of light. "
                    "Calculations become unreliable above 99.99999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999% of c due to "
                    "extreme relativistic effects."
                )
            
            # Unit selection and calculation
            unit_index = ["m/s", "cm/s", "mm/s", "μm/s", "nm/s"].index(self.unit_var.get())
            c_current = c_units[unit_index]
            
            self.progress_var.set(self.PROGRESS_STEPS['UNIT_SELECTED'])
            
            # Calculate gamma factor
            gamma = time_dilation_factor(velocity, c_current)
            if gamma is None:
                raise ValueError("Invalid calculation result")
            
            self.progress_var.set(self.PROGRESS_STEPS['GAMMA_CALCULATED'])
            
            # Calculate time dilation
            traveler_time = time_dilation(earth_time, velocity, c_current)
            
            self.progress_var.set(self.PROGRESS_STEPS['TIME_CALCULATED'])
            
            # Format and display results
            result = format_output(
                earth_time,
                traveler_time,
                gamma,
                str(velocity),
                f"{format_large_or_small_number(c_current)} {self.unit_var.get()}"
            )
            
            self.results_text.insert(tk.END, result)
            self.status_var.set("Calculation complete")
            
            # Log calculation details
            logging.info(
                f"Calculation completed - "
                f"Velocity: {velocity}%, "
                f"Time: {earth_time} {measurement_type}, "
                f"Gamma: {gamma}, "
                f"Unit: {self.unit_var.get()}"
            )
            
            self.progress_var.set(self.PROGRESS_STEPS['COMPLETE'])
            
        except Exception as e:
            self.results_text.insert(tk.END, f"Error: {str(e)}")
            self.status_var.set("Error in calculation")
            logging.error(f"Calculation error: {str(e)}")
            self.progress_var.set(self.PROGRESS_STEPS['START'])

    def clear_placeholder(self, event):
        """Clear placeholder text when entry gets focus."""
        if self.velocity_entry.get() == "99.999999999999":
            self.velocity_entry.delete(0, tk.END)

    def restore_placeholder(self, event):
        """Restore placeholder text if entry is empty and loses focus."""
        if not self.velocity_entry.get():
            self.velocity_entry.insert(0, "99.999999999999")

if __name__ == "__main__":
    # Set up logging with timestamp
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        filename='time_dilation.log'
    )
    
    logging.info("Application starting")
    app = TimeDilationCalculator()
    app.mainloop()
    logging.info("Application closing")
