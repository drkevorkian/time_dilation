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

def safe_decimal_convert(value, error_msg="Invalid decimal conversion"):
    """Safely convert values to Decimal with proper error handling."""
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

def format_output(earth_time, traveler_time, gamma, velocity_str, unit="m/s"):
    """Format calculation results with proper spacing and alignment."""
    try:
        output = [
            "\n=== Time Dilation Calculation Results ===",
            f"Velocity: {velocity_str}% of c ({unit})",
            f"Gamma factor: {format_large_or_small_number(gamma)}",
            "\nTime Breakdown:",
            "-" * 40,
            "Earth time:",
            f"  {format_time(earth_time)}",
            f"  ({format_large_or_small_number(earth_time)} seconds)",
            "\nTraveler time:",
            f"  {format_time(traveler_time)}",
            f"  ({format_large_or_small_number(traveler_time)} seconds)",
            "-" * 40
        ]
        return "\n".join(output)
    except Exception as e:
        logging.error(f"Output formatting error: {str(e)}")
        return "Error formatting output"

class TimeDilationCalculator(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("Time Dilation Calculator")
        self.geometry("800x600")
        self.configure(bg='#f0f0f0')

        # Create main frame
        main_frame = ttk.Frame(self, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        # Input fields
        ttk.Label(main_frame, text="Velocity (% of c):").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.velocity_var = tk.StringVar()
        self.velocity_entry = ttk.Entry(main_frame, textvariable=self.velocity_var, width=40)
        self.velocity_entry.grid(row=0, column=1, sticky=tk.W, pady=5)
        ttk.Label(main_frame, text="e.g., 99.999999999999").grid(row=0, column=2, sticky=tk.W, pady=5)

        ttk.Label(main_frame, text="Time on Earth (years):").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.time_var = tk.StringVar()
        self.time_entry = ttk.Entry(main_frame, textvariable=self.time_var, width=40)
        self.time_entry.grid(row=1, column=1, sticky=tk.W, pady=5)

        # Calculate button
        self.calc_button = ttk.Button(main_frame, text="Calculate", command=self.calculate)
        self.calc_button.grid(row=2, column=0, columnspan=3, pady=20)

        # Results display
        ttk.Label(main_frame, text="Results:").grid(row=3, column=0, sticky=tk.W)
        self.results_text = scrolledtext.ScrolledText(main_frame, width=80, height=20)
        self.results_text.grid(row=4, column=0, columnspan=3, pady=5)

        # Status bar
        self.status_var = tk.StringVar()
        self.status_bar = ttk.Label(self, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.grid(row=1, column=0, sticky=(tk.W, tk.E))

    def calculate(self):
        """Perform time dilation calculations and display results."""
        self.results_text.delete(1.0, tk.END)
        self.status_var.set("Calculating...")
        
        try:
            velocity = safe_decimal_convert(self.velocity_var.get())
            earth_time = safe_decimal_convert(self.time_var.get())
            
            if velocity is None or earth_time is None:
                raise ValueError("Invalid input values")
                
            if not (0 < velocity < 100):
                raise ValueError("Velocity must be between 0 and 100 percent of c")
                
            if earth_time <= 0:
                raise ValueError("Time must be positive")

            results = []
            for c_current in c_units:
                gamma = time_dilation_factor(velocity, c_current)
                if gamma is not None:
                    earth_time_seconds = earth_time * Decimal("31557600")
                    traveler_time_seconds = time_dilation(earth_time_seconds, velocity, c_current)
                    
                    results.append(format_output(
                        earth_time_seconds,
                        traveler_time_seconds,
                        gamma,
                        str(velocity),
                        f"{format_large_or_small_number(c_current)} m/s"
                    ))
                    break

            if results:
                self.results_text.insert(tk.END, "\n".join(results))
                self.status_var.set("Calculation complete")
            else:
                self.results_text.insert(tk.END, "No valid results obtained")
                self.status_var.set("Calculation failed")

        except Exception as e:
            self.results_text.insert(tk.END, f"Error: {str(e)}")
            self.status_var.set("Error in calculation")
            logging.error(f"Calculation error: {str(e)}")

if __name__ == "__main__":
    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        filename='time_dilation.log'
    )
    
    # Create and run the application
    app = TimeDilationCalculator()
    app.mainloop()
