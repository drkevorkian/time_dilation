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
        ttk.Label(main_frame, text="e.g., 99.999999999999").grid(row=0, column=2, sticky=tk.W, pady=5)

        ttk.Label(main_frame, text="Time on Earth (years):").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.time_var = tk.StringVar()
        self.time_entry = ttk.Entry(main_frame, textvariable=self.time_var, width=40)
        self.time_entry.grid(row=1, column=1, sticky=tk.W, pady=5)

        # Add unit selection
        ttk.Label(main_frame, text="Speed of Light Unit:").grid(row=2, column=0, sticky=tk.W, pady=5)
        self.unit_var = tk.StringVar(value="m/s")
        self.unit_combo = ttk.Combobox(main_frame, textvariable=self.unit_var, 
                                      values=["m/s", "cm/s", "mm/s", "μm/s", "nm/s"],
                                      state="readonly")
        self.unit_combo.grid(row=2, column=1, sticky=tk.W, pady=5)
        
        # Calculate button
        self.calc_button = ttk.Button(main_frame, text="Calculate", command=self.calculate)
        self.calc_button.grid(row=3, column=0, columnspan=3, pady=20)

        # Results display
        ttk.Label(main_frame, text="Results:").grid(row=4, column=0, sticky=tk.W)
        self.results_text = scrolledtext.ScrolledText(main_frame, width=80, height=20)
        self.results_text.grid(row=5, column=0, columnspan=3, pady=5, sticky=(tk.W, tk.E, tk.N, tk.S))

        # Add copy button
        self.copy_button = ttk.Button(main_frame, text="Copy Results", command=self.copy_results)
        self.copy_button.grid(row=6, column=0, columnspan=3, pady=5)
        
        # Progress bar
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(main_frame, mode='determinate', 
                                          variable=self.progress_var)
        self.progress_bar.grid(row=7, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=5)

        # Status bar
        self.status_var = tk.StringVar()
        self.status_bar = ttk.Label(self, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.grid(row=1, column=0, sticky=(tk.W, tk.E))

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
            
    def calculate(self):
        """Perform time dilation calculations with enhanced validation and security."""
        self.results_text.delete(1.0, tk.END)
        self.status_var.set("Calculating...")
        self.progress_var.set(0)
        
        try:
            # Input validation with specific error messages
            velocity = safe_decimal_convert(self.velocity_var.get())
            if velocity is None:
                raise ValueError("Please enter a valid number for velocity")
            
            earth_time = safe_decimal_convert(self.time_var.get())
            if earth_time is None:
                raise ValueError("Please enter a valid number for time")
            
            if not (0 < velocity < 100):
                raise ValueError("Velocity must be between 0 and 100 percent of c (exclusive)")
            
            if velocity >= 99.9999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999:
                raise ValueError("Velocity too close to speed of light - calculation would be unreliable")
                
            if earth_time <= 0:
                raise ValueError("Time must be positive")
                
            # Get selected unit and corresponding c value
            unit_index = ["m/s", "cm/s", "mm/s", "μm/s", "nm/s"].index(self.unit_var.get())
            c_current = c_units[unit_index]
            
            self.progress_var.set(50)  # Update progress
            
            # Perform calculation
            gamma = time_dilation_factor(velocity, c_current)
            if gamma is not None:
                earth_time_seconds = earth_time * Decimal("31557600")
                traveler_time_seconds = time_dilation(earth_time_seconds, velocity, c_current)
                
                result = format_output(
                    earth_time_seconds,
                    traveler_time_seconds,
                    gamma,
                    str(velocity),
                    f"{format_large_or_small_number(c_current)} {self.unit_var.get()}"
                )
                
                self.results_text.insert(tk.END, result)
                self.status_var.set("Calculation complete")
                
                # Log calculation details for debugging
                logging.info(f"Calculation completed - Velocity: {velocity}%, Time: {earth_time} years, "
                           f"Gamma: {gamma}, Unit: {self.unit_var.get()}")
            else:
                raise ValueError("Invalid calculation result")
                
            self.progress_var.set(100)  # Complete progress
            
        except Exception as e:
            self.results_text.insert(tk.END, f"Error: {str(e)}")
            self.status_var.set("Error in calculation")
            logging.error(f"Calculation error: {str(e)}")
            self.progress_var.set(0)

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
