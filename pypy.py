import logging
import tkinter as tk
import webbrowser
from decimal import (
    Decimal,
    DivisionByZero,
    InvalidOperation,
    getcontext,
    localcontext,
)
from pathlib import Path
from tkinter import scrolledtext, ttk


DECIMAL_PRECISION = 200
GUARD_DIGITS = 12
MAX_INPUT_CHARACTERS = 10_000

getcontext().prec = DECIMAL_PRECISION


# Speed of light in different units
C_MS = Decimal("299792458")
C_CMS = C_MS * 100
C_MMS = C_MS * 1000
C_UM = C_MS * 1_000_000
C_NM = C_MS * Decimal("1e9")

C_UNITS = [C_MS, C_CMS, C_MMS, C_UM, C_NM]
C_UNIT_NAMES = ["m/s", "cm/s", "mm/s", "μm/s", "nm/s"]


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
    (Decimal("1e-30"), "Quecto"),
]


# Exact definitions:
# - One Julian year is exactly 365.25 days.
# - One international mile is exactly 1,609.344 metres.
# - The SI value of c is exact.
SECONDS_PER_JULIAN_YEAR = Decimal("31557600")
SECONDS_PER_MONTH = SECONDS_PER_JULIAN_YEAR / Decimal("12")
SECONDS_PER_DAY = Decimal("86400")
SECONDS_PER_HOUR = Decimal("3600")
SECONDS_PER_MINUTE = Decimal("60")

METERS_PER_KILOMETER = Decimal("1000")
METERS_PER_MILE = Decimal("1609.344")
METERS_PER_LIGHTYEAR = C_MS * SECONDS_PER_JULIAN_YEAR
KM_PER_LIGHTYEAR = METERS_PER_LIGHTYEAR / METERS_PER_KILOMETER
MILES_PER_LIGHTYEAR = METERS_PER_LIGHTYEAR / METERS_PER_MILE


def as_decimal(value):
    """
    Convert a value to Decimal without importing a binary float's
    representation into the calculation.
    """
    if isinstance(value, Decimal):
        return value

    return Decimal(str(value))


def safe_decimal_convert(value, error_msg="Invalid decimal conversion"):
    """
    Safely convert a user-supplied value to a finite Decimal.

    Returns None when conversion fails.
    """
    try:
        text = str(value).strip()

        if not text:
            raise ValueError("input is empty")

        if len(text) > MAX_INPUT_CHARACTERS:
            raise ValueError("input is unreasonably long")

        result = Decimal(text)

        if not result.is_finite():
            raise ValueError("NaN and infinity are not valid inputs")

        return result

    except (InvalidOperation, ValueError, TypeError) as error:
        logging.error("%s: %s", error_msg, error)
        return None


def format_significant(value, significant_digits=DECIMAL_PRECISION):
    """
    Format a finite Decimal without converting it to binary floating point.
    """
    value = as_decimal(value)

    if not value.is_finite():
        return str(value)

    if value.is_zero():
        return "0"

    integer_digits = value.copy_abs().adjusted() + 1
    decimal_places = max(significant_digits - integer_digits, 0)

    formatted = f"{value:.{decimal_places}f}"

    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")

    return formatted


def format_scientific(value):
    """
    Format a Decimal in scientific notation while retaining the configured
    decimal precision.
    """
    value = as_decimal(value)

    if value.is_zero():
        return "0"

    text = f"{value:.{DECIMAL_PRECISION - 1}E}"
    mantissa, exponent = text.split("E")

    mantissa = mantissa.rstrip("0").rstrip(".")

    return f"{mantissa}e{int(exponent):+d}"


def format_large_or_small_number(value):
    """
    Convert large or small Decimal values into human-readable SI prefixes.

    Numbers below the smallest available SI prefix are shown in scientific
    notation so they are never accidentally displayed as zero.
    """
    value = as_decimal(value)

    if not value.is_finite():
        return str(value)

    if value.is_zero():
        return "0"

    for factor, prefix in SI_PREFIXES:
        if abs(value) >= factor:
            formatted = format_significant(value / factor)
            return f"{formatted} {prefix}".strip()

    return format_scientific(value)


def format_time(seconds):
    """
    Convert seconds into years, months, days, hours, minutes, and seconds while
    preserving Decimal precision.
    """
    try:
        remaining = as_decimal(seconds)

        if not remaining.is_finite() or remaining < 0:
            raise ValueError("time must be a finite, non-negative value")

        years = remaining // SECONDS_PER_JULIAN_YEAR
        remaining %= SECONDS_PER_JULIAN_YEAR

        months = remaining // SECONDS_PER_MONTH
        remaining %= SECONDS_PER_MONTH

        days = remaining // SECONDS_PER_DAY
        remaining %= SECONDS_PER_DAY

        hours = remaining // SECONDS_PER_HOUR
        remaining %= SECONDS_PER_HOUR

        minutes = remaining // SECONDS_PER_MINUTE
        remaining %= SECONDS_PER_MINUTE

        time_parts = []

        if years:
            time_parts.append(f"{years} years")

        if months:
            time_parts.append(f"{months} months")

        if days:
            time_parts.append(f"{days} days")

        if hours:
            time_parts.append(f"{hours} hours")

        if minutes:
            time_parts.append(f"{minutes} minutes")

        if remaining:
            formatted_seconds = format_large_or_small_number(remaining)
            time_parts.append(f"{formatted_seconds} seconds")

        return ", ".join(time_parts) if time_parts else "0 seconds"

    except (InvalidOperation, ValueError, ArithmeticError):
        return "Error calculating time"


def time_dilation_factor(velocity_percentage, c=None):
    """
    Return the Lorentz factor using stable, all-Decimal arithmetic.

    For velocity p expressed as a percentage of c:

        gamma = 100 / sqrt((100 - p) * (100 + p))

    This is algebraically identical to:

        gamma = 1 / sqrt(1 - (p / 100)^2)

    The factored form avoids subtracting two nearly equal rounded values as
    velocity approaches 100 percent of c.

    The optional c argument is retained for compatibility with the original
    function. Gamma is dimensionless and does not depend on the selected unit
    used to represent c.
    """
    try:
        percentage = as_decimal(velocity_percentage)
        hundred = Decimal("100")

        if not percentage.is_finite():
            return None

        if not (Decimal("0") < percentage < hundred):
            return None

        with localcontext() as context:
            context.prec = DECIMAL_PRECISION + GUARD_DIGITS

            radicand = (
                (hundred - percentage)
                * (hundred + percentage)
            )

            gamma = hundred / radicand.sqrt()

        # Apply the application's documented 200-digit working precision.
        return +gamma

    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def time_dilation(time_earth, velocity_percentage, c=None):
    """
    Calculate the proper time experienced by the traveler.
    """
    gamma = time_dilation_factor(velocity_percentage, c)

    if gamma is None:
        return None

    try:
        earth_time = as_decimal(time_earth)

        if not earth_time.is_finite() or earth_time < 0:
            return None

        with localcontext() as context:
            context.prec = DECIMAL_PRECISION + GUARD_DIGITS
            traveler_time = earth_time / gamma

        return +traveler_time

    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def convert_to_lightyears(value, from_unit):
    """
    Convert a distance to light-years.

    Supported units:
        ly
        km
        mi
    """
    try:
        distance = as_decimal(value)

        if not distance.is_finite():
            raise ValueError("distance must be finite")

        if from_unit == "ly":
            return distance

        if from_unit == "km":
            return distance / KM_PER_LIGHTYEAR

        if from_unit == "mi":
            return distance / MILES_PER_LIGHTYEAR

        raise ValueError(f"Unsupported unit: {from_unit}")

    except (InvalidOperation, DivisionByZero, ValueError) as error:
        logging.error("Conversion error: %s", error)
        return None


def convert_from_lightyears(lightyears, to_unit):
    """
    Convert light-years into another distance unit.

    Supported units:
        ly
        km
        mi
    """
    try:
        distance = as_decimal(lightyears)

        if not distance.is_finite():
            raise ValueError("distance must be finite")

        if to_unit == "ly":
            return distance

        if to_unit == "km":
            return distance * KM_PER_LIGHTYEAR

        if to_unit == "mi":
            return distance * MILES_PER_LIGHTYEAR

        raise ValueError(f"Unsupported unit: {to_unit}")

    except (InvalidOperation, DivisionByZero, ValueError) as error:
        logging.error("Conversion error: %s", error)
        return None


def convert_distance_to_travel_time(
    distance,
    velocity_percentage,
    from_unit="ly",
):
    """
    Convert a travel distance into Earth-frame travel time in seconds.

    Because one light-year is the distance light travels during one Julian
    year, travel time is:

        light-years * seconds-per-Julian-year / velocity-fraction
    """
    try:
        light_years = convert_to_lightyears(distance, from_unit)
        percentage = as_decimal(velocity_percentage)

        if light_years is None:
            raise ValueError("distance conversion failed")

        if not light_years.is_finite() or light_years <= 0:
            raise ValueError("distance must be positive")

        if not percentage.is_finite():
            raise ValueError("velocity must be finite")

        if not (
            Decimal("0")
            < percentage
            < Decimal("100")
        ):
            raise ValueError(
                "velocity must be between 0 and 100 percent of c"
            )

        with localcontext() as context:
            context.prec = DECIMAL_PRECISION + GUARD_DIGITS

            travel_time_seconds = (
                light_years
                * SECONDS_PER_JULIAN_YEAR
                * Decimal("100")
                / percentage
            )

        return +travel_time_seconds

    except (
        InvalidOperation,
        DivisionByZero,
        ValueError,
        ArithmeticError,
    ) as error:
        logging.error("Travel time calculation error: %s", error)
        return None


def format_output(
    earth_time,
    traveler_time,
    gamma,
    velocity_str,
    unit="m/s",
):
    """
    Format calculation results for the GUI.
    """
    try:
        output = [
            "",
            "=== Time Dilation Calculation Results ===",
            f"Velocity: {velocity_str}% of c",
            f"Speed of light: {unit}",
            f"Gamma factor: {format_large_or_small_number(gamma)}",
            "",
            "Measurement Details:",
            "-" * 40,
            "Earth time:",
            f"  {format_time(earth_time)}",
            (
                "  ("
                f"{format_large_or_small_number(earth_time)}"
                " seconds)"
            ),
            "",
            "Traveler time:",
            f"  {format_time(traveler_time)}",
            (
                "  ("
                f"{format_large_or_small_number(traveler_time)}"
                " seconds)"
            ),
            "-" * 40,
        ]

        return "\n".join(output)

    except (InvalidOperation, ValueError, ArithmeticError) as error:
        logging.error("Output formatting error: %s", error)
        return "Error formatting output"


class TimeDilationCalculator(tk.Tk):
    """
    Tkinter time-dilation calculator.
    """

    def __init__(self):
        super().__init__()

        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.resizable(True, True)
        self.minsize(800, 600)

        self.title("Time Dilation Calculator")
        self.geometry("800x600")
        self.configure(bg="#f8f8ff")

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.notebook = ttk.Notebook(self)
        self.notebook.grid(
            row=0,
            column=0,
            sticky=(tk.W, tk.E, tk.N, tk.S),
        )

        self.create_calculator_tab()
        self.create_references_tab()
        self.create_source_tab()
        self.create_status_bar()

        self.notebook.bind(
            "<<NotebookTabChanged>>",
            self.on_tab_changed,
        )

        self.create_tooltip(
            self.velocity_entry,
            (
                "Enter velocity as a percentage of light speed "
                "(greater than 0 and less than 100), e.g. "
                "99.999999999999. Calculations use 200 "
                "significant decimal digits internally."
            ),
        )

        self.create_tooltip(
            self.measurement_combo,
            "Select the measurement type: Time or Distance.",
        )

        self.create_tooltip(
            self.time_entry,
            "Enter the amount of time that passes on Earth.",
        )

        self.create_tooltip(
            self.copy_button,
            "Copy the calculation results to the clipboard.",
        )

        self.PROGRESS_STEPS = {
            "START": 0,
            "INPUT_VALIDATED": 20,
            "UNIT_SELECTED": 30,
            "GAMMA_CALCULATED": 60,
            "TIME_CALCULATED": 80,
            "COMPLETE": 100,
        }

    def create_calculator_tab(self):
        self.calculator_tab = ttk.Frame(
            self.notebook,
            padding="10",
        )

        self.notebook.add(
            self.calculator_tab,
            text="Calculator",
        )

        self.calculator_tab.grid_columnconfigure(0, weight=1)
        self.calculator_tab.grid_rowconfigure(0, weight=1)

        main_frame = ttk.Frame(
            self.calculator_tab,
            padding="10",
        )

        main_frame.grid(
            row=0,
            column=0,
            sticky=(tk.W, tk.E, tk.N, tk.S),
        )

        main_frame.grid_columnconfigure(1, weight=1)
        main_frame.grid_rowconfigure(6, weight=1)

        ttk.Label(
            main_frame,
            text="Velocity (% of c):",
        ).grid(
            row=0,
            column=0,
            sticky=tk.W,
            pady=5,
        )

        self.velocity_var = tk.StringVar(
            value="99.999999999999"
        )

        self.velocity_entry = ttk.Entry(
            main_frame,
            textvariable=self.velocity_var,
            width=40,
        )

        self.velocity_entry.grid(
            row=0,
            column=1,
            sticky=tk.W,
            pady=5,
        )

        self.velocity_entry.bind(
            "<FocusIn>",
            self.clear_placeholder,
        )

        self.velocity_entry.bind(
            "<FocusOut>",
            self.restore_placeholder,
        )

        ttk.Label(
            main_frame,
            text="Measurement Type:",
        ).grid(
            row=1,
            column=0,
            sticky=tk.W,
            pady=5,
        )

        self.measurement_var = tk.StringVar(
            value="Time"
        )

        self.measurement_combo = ttk.Combobox(
            main_frame,
            textvariable=self.measurement_var,
            values=["Time", "Distance"],
            state="readonly",
        )

        self.measurement_combo.grid(
            row=1,
            column=1,
            sticky=tk.W,
            pady=5,
        )

        self.measurement_combo.bind(
            "<<ComboboxSelected>>",
            self.update_measurement_label,
        )

        self.measurement_label = ttk.Label(
            main_frame,
            text="Time on Earth (years):",
        )

        self.measurement_label.grid(
            row=2,
            column=0,
            sticky=tk.W,
            pady=5,
        )

        self.time_var = tk.StringVar()

        self.time_entry = ttk.Entry(
            main_frame,
            textvariable=self.time_var,
            width=40,
        )

        self.time_entry.grid(
            row=2,
            column=1,
            sticky=tk.W,
            pady=5,
        )

        self.distance_unit_label = ttk.Label(
            main_frame,
            text="Distance Unit:",
        )

        self.distance_unit_var = tk.StringVar(
            value="Light Years (ly)"
        )

        self.distance_unit_combo = ttk.Combobox(
            main_frame,
            textvariable=self.distance_unit_var,
            values=[
                "Light Years (ly)",
                "Kilometers (km)",
                "Miles (mi)",
            ],
            state="readonly",
            width=20,
        )

        self.distance_unit_label.grid(
            row=2,
            column=2,
            sticky=tk.W,
            padx=(10, 0),
            pady=5,
        )

        self.distance_unit_combo.grid(
            row=2,
            column=3,
            sticky=tk.W,
            pady=5,
        )

        self.distance_unit_label.grid_remove()
        self.distance_unit_combo.grid_remove()

        ttk.Label(
            main_frame,
            text="Speed of Light Unit:",
        ).grid(
            row=3,
            column=0,
            sticky=tk.W,
            pady=5,
        )

        self.unit_var = tk.StringVar(value="m/s")

        self.unit_combo = ttk.Combobox(
            main_frame,
            textvariable=self.unit_var,
            values=C_UNIT_NAMES,
            state="readonly",
        )

        self.unit_combo.grid(
            row=3,
            column=1,
            sticky=tk.W,
            pady=5,
        )

        self.calc_button = ttk.Button(
            main_frame,
            text="Calculate",
            command=self.calculate,
        )

        self.calc_button.grid(
            row=4,
            column=0,
            columnspan=4,
            pady=20,
        )

        ttk.Label(
            main_frame,
            text="Results:",
        ).grid(
            row=5,
            column=0,
            sticky=tk.W,
        )

        self.results_text = scrolledtext.ScrolledText(
            main_frame,
            width=80,
            height=20,
        )

        self.results_text.grid(
            row=6,
            column=0,
            columnspan=4,
            pady=5,
            sticky=(tk.W, tk.E, tk.N, tk.S),
        )

        self.copy_button = ttk.Button(
            main_frame,
            text="Copy Results",
            command=self.copy_results,
        )

        self.copy_button.grid(
            row=7,
            column=0,
            columnspan=4,
            pady=5,
        )

        self.progress_var = tk.DoubleVar()

        self.progress_bar = ttk.Progressbar(
            main_frame,
            mode="determinate",
            variable=self.progress_var,
        )

        self.progress_bar.grid(
            row=8,
            column=0,
            columnspan=4,
            sticky=(tk.W, tk.E),
            pady=5,
        )

    def create_references_tab(self):
        self.references_tab = ttk.Frame(
            self.notebook,
            padding="10",
        )

        self.notebook.add(
            self.references_tab,
            text="References",
        )

        self.references_tab.grid_columnconfigure(
            0,
            weight=1,
        )

        self.references_tab.grid_rowconfigure(
            0,
            weight=1,
        )

        references_frame = ttk.Frame(
            self.references_tab
        )

        references_frame.grid(
            row=0,
            column=0,
            sticky="nsew",
        )

        references_frame.grid_columnconfigure(
            0,
            weight=1,
        )

        references_frame.grid_rowconfigure(
            0,
            weight=1,
        )

        self.references_canvas = tk.Canvas(
            references_frame,
            bg="white",
        )

        scrollbar = ttk.Scrollbar(
            references_frame,
            orient="vertical",
            command=self.references_canvas.yview,
        )

        self.scrollable_frame = ttk.Frame(
            self.references_canvas
        )

        self.scrollable_frame.grid_columnconfigure(
            0,
            weight=1,
        )

        self.scrollable_frame.grid_columnconfigure(
            1,
            weight=1,
        )

        self.references_canvas.configure(
            yscrollcommand=scrollbar.set
        )

        self.references_canvas.grid(
            row=0,
            column=0,
            sticky="nsew",
        )

        scrollbar.grid(
            row=0,
            column=1,
            sticky="ns",
        )

        self.canvas_frame = (
            self.references_canvas.create_window(
                (0, 0),
                window=self.scrollable_frame,
                anchor="nw",
            )
        )

        self.scrollable_frame.bind(
            "<Configure>",
            self.configure_scroll_region,
        )

        self.references_canvas.bind(
            "<Configure>",
            self.configure_canvas_window,
        )

        references = [
            (
                "On the Electrodynamics of Moving Bodies (1905)",
                (
                    "The paper that introduced special "
                    "relativity to the world"
                ),
                (
                    "https://www.fourmilab.ch/etexts/"
                    "einstein/specrel/www/"
                ),
                "Foundational Works",
            ),
            (
                "A Brief History of Time - Stephen Hawking",
                (
                    "A landmark exploration of space, time, "
                    "and the universe"
                ),
                (
                    "https://drive.google.com/file/d/"
                    "0B1PBdu03t91DYUdVc0c4Y2Vzb0k/view"
                    "?pli=1&resourcekey="
                    "0-evEIWkVG3IO0k73tz2dlSw"
                ),
                "Classic Books",
            ),
            (
                "MIT OpenCourseWare: Special Relativity",
                (
                    "Complete university course materials "
                    "with video lectures"
                ),
                (
                    "https://ocw.mit.edu/courses/physics/"
                    "8-20-introduction-to-special-relativity-"
                    "january-iap-2021/"
                ),
                "Educational Resources",
            ),
            (
                "Stanford Encyclopedia: Spacetime Theories",
                (
                    "Comprehensive academic overview of "
                    "time-dilation concepts"
                ),
                (
                    "https://plato.stanford.edu/entries/"
                    "spacetime-theories/"
                ),
                "Educational Resources",
            ),
            (
                "APS Physics: Time Dilation Evidence",
                (
                    "Famous atomic-clock experiment that "
                    "confirmed time dilation"
                ),
                "https://physics.aps.org/story/v15/st4",
                "Experimental Evidence",
            ),
            (
                "GPS and Relativity",
                (
                    "How GPS satellites account for "
                    "relativistic time dilation"
                ),
                (
                    "https://www.astronomy.ohio-state.edu/"
                    "pogge.1/Ast162/Unit5/gps.html"
                ),
                "Modern Applications",
            ),
            (
                "PhET Interactive Simulations",
                (
                    "University of Colorado's relativity "
                    "simulations"
                ),
                (
                    "https://phet.colorado.edu/en/simulations/"
                    "filter?subjects=physics&type=html,prototype"
                ),
                "Interactive Tools",
            ),
            (
                "HyperPhysics: Time Dilation",
                (
                    "Comprehensive physics reference with "
                    "calculations"
                ),
                (
                    "http://hyperphysics.phy-astr.gsu.edu/"
                    "hbase/Relativ/tdil.html"
                ),
                "Additional Resources",
            ),
            (
                "Leonard Susskind: Special Relativity",
                (
                    "Stanford University's complete lecture "
                    "series on special relativity"
                ),
                (
                    "https://theoreticalminimum.com/courses/"
                    "special-relativity"
                ),
                "Video Lectures",
            ),
            (
                "Relativistic Calculator",
                (
                    "Online tool for computing relativistic "
                    "effects"
                ),
                (
                    "https://www.omnicalculator.com/physics/"
                    "time-dilation"
                ),
                "Software Tools",
            ),
            (
                "Einstein Papers Project",
                (
                    "Digital archive of Einstein's special "
                    "relativity papers"
                ),
                (
                    "https://einsteinpapers.press.princeton.edu/"
                ),
                "Historical Context",
            ),
            (
                "Lorentz Transformations",
                (
                    "Detailed mathematical foundation of "
                    "special relativity"
                ),
                (
                    "https://mathworld.wolfram.com/"
                    "LorentzTransformation.html"
                ),
                "Advanced Topics",
            ),
        ]

        for index, reference in enumerate(references):
            title, description, url, category = reference
            row = index // 2
            column = index % 2

            self.add_paper_link(
                title,
                description,
                url,
                category,
                row,
                column,
            )

    def create_source_tab(self):
        self.source_tab = ttk.Frame(
            self.notebook,
            padding="10",
        )

        self.notebook.add(
            self.source_tab,
            text="Source Code",
        )

        self.source_tab.grid_columnconfigure(0, weight=1)
        self.source_tab.grid_rowconfigure(0, weight=1)

        self.source_text = scrolledtext.ScrolledText(
            self.source_tab,
            width=80,
            height=30,
            font=("Courier", 10),
        )

        self.source_text.grid(
            row=0,
            column=0,
            sticky=(tk.W, tk.E, tk.N, tk.S),
        )

    def create_status_bar(self):
        self.status_var = tk.StringVar()

        self.status_bar = ttk.Label(
            self,
            textvariable=self.status_var,
            relief=tk.SUNKEN,
            anchor=tk.W,
        )

        self.status_bar.grid(
            row=1,
            column=0,
            sticky=(tk.W, tk.E),
        )

    def configure_scroll_region(self, event=None):
        self.references_canvas.configure(
            scrollregion=self.references_canvas.bbox("all")
        )

    def configure_canvas_window(self, event):
        self.references_canvas.itemconfigure(
            self.canvas_frame,
            width=event.width,
        )

    def add_paper_link(
        self,
        title,
        description,
        url,
        category,
        row,
        column,
    ):
        style = ttk.Style()
        style.configure(
            "Card.TFrame",
            background="#ace5ee",
        )

        section_frame = ttk.Frame(
            self.scrollable_frame,
            style="Card.TFrame",
        )

        section_frame.grid(
            row=row,
            column=column,
            sticky="nsew",
            padx=5,
            pady=2,
        )

        section_frame.grid_columnconfigure(0, weight=1)

        category_label = ttk.Label(
            section_frame,
            text=f"📄 {category}",
            font=("Segoe UI", 11, "bold"),
            background="#ace5ee",
            anchor="w",
            wraplength=300,
        )

        category_label.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=5,
            pady=(5, 0),
        )

        title_button = tk.Button(
            section_frame,
            text=title,
            font=("Segoe UI", 10, "underline"),
            foreground="#0000ff",
            background="#ace5ee",
            activebackground="#ace5ee",
            cursor="hand2",
            borderwidth=0,
            relief="flat",
            anchor="w",
            justify="left",
            wraplength=300,
            command=lambda link=url: webbrowser.open_new(link),
        )

        title_button.grid(
            row=1,
            column=0,
            sticky="ew",
            padx=20,
        )

        description_label = ttk.Label(
            section_frame,
            text=description,
            font=("Segoe UI", 10, "italic"),
            background="#ace5ee",
            foreground="#000000",
            anchor="w",
            wraplength=300,
            padding=(0, 2),
        )

        description_label.grid(
            row=2,
            column=0,
            sticky="ew",
            padx=20,
            pady=(0, 5),
        )

    def create_tooltip(self, widget, text):
        """
        Attach a simple tooltip to a widget.
        """

        def show_tooltip(event):
            existing_tooltip = getattr(
                widget,
                "_tooltip_window",
                None,
            )

            if (
                existing_tooltip is not None
                and existing_tooltip.winfo_exists()
            ):
                return

            tooltip = tk.Toplevel(widget)
            tooltip.wm_overrideredirect(True)
            tooltip.wm_geometry(
                f"+{event.x_root + 10}+{event.y_root + 10}"
            )

            label = ttk.Label(
                tooltip,
                text=text,
                background="#ffffe0",
                relief="solid",
                borderwidth=1,
                padding=4,
            )

            label.pack()
            widget._tooltip_window = tooltip

        def hide_tooltip(event=None):
            tooltip = getattr(
                widget,
                "_tooltip_window",
                None,
            )

            if (
                tooltip is not None
                and tooltip.winfo_exists()
            ):
                tooltip.destroy()

            widget._tooltip_window = None

        widget.bind("<Enter>", show_tooltip)
        widget.bind("<Leave>", hide_tooltip)
        widget.bind("<ButtonPress>", hide_tooltip)

    def copy_results(self):
        """
        Copy results to the clipboard.
        """
        try:
            contents = self.results_text.get(
                "1.0",
                tk.END,
            ).rstrip()

            if not contents:
                self.status_var.set(
                    "There are no results to copy"
                )
                return

            self.clipboard_clear()
            self.clipboard_append(contents)
            self.update()

            self.status_var.set(
                "Results copied to clipboard"
            )

        except tk.TclError as error:
            logging.error("Clipboard error: %s", error)
            self.status_var.set("Failed to copy results")

    def on_closing(self):
        """
        Close the application.
        """
        logging.info("Application closing")
        self.destroy()

    def update_measurement_label(self, event=None):
        """
        Update the measurement label and distance-unit controls.
        """
        measurement_type = self.measurement_var.get()

        if measurement_type == "Time":
            self.measurement_label.configure(
                text="Time on Earth (years):"
            )

            self.distance_unit_label.grid_remove()
            self.distance_unit_combo.grid_remove()

        else:
            self.measurement_label.configure(
                text="Distance to Travel:"
            )

            self.distance_unit_label.grid()
            self.distance_unit_combo.grid()

    def calculate(self):
        """
        Validate inputs, calculate time dilation, and display the result.
        """
        self.results_text.delete("1.0", tk.END)
        self.status_var.set("Calculating...")
        self.progress_var.set(
            self.PROGRESS_STEPS["START"]
        )

        try:
            velocity = safe_decimal_convert(
                self.velocity_var.get(),
                "Invalid velocity",
            )

            if velocity is None:
                raise ValueError(
                    "Please enter a valid number for velocity"
                )

            if not (
                Decimal("0")
                < velocity
                < Decimal("100")
            ):
                raise ValueError(
                    "Velocity must be greater than 0 and "
                    "less than 100 percent of c"
                )

            measurement_type = self.measurement_var.get()

            input_value = safe_decimal_convert(
                self.time_var.get(),
                f"Invalid {measurement_type.lower()}",
            )

            if input_value is None:
                raise ValueError(
                    "Please enter a valid number for "
                    f"{measurement_type.lower()}"
                )

            if input_value <= 0:
                raise ValueError(
                    f"{measurement_type} must be positive"
                )

            self.progress_var.set(
                self.PROGRESS_STEPS["INPUT_VALIDATED"]
            )

            if measurement_type == "Distance":
                unit_text = self.distance_unit_var.get()

                unit_mapping = {
                    "Light Years (ly)": "ly",
                    "Kilometers (km)": "km",
                    "Miles (mi)": "mi",
                }

                unit = unit_mapping.get(unit_text)

                if unit is None:
                    raise ValueError(
                        f"Unsupported distance unit: {unit_text}"
                    )

                earth_time = (
                    convert_distance_to_travel_time(
                        input_value,
                        velocity,
                        unit,
                    )
                )

                if earth_time is None:
                    raise ValueError(
                        "Unable to calculate travel time"
                    )

            elif measurement_type == "Time":
                with localcontext() as context:
                    context.prec = (
                        DECIMAL_PRECISION + GUARD_DIGITS
                    )

                    earth_time = (
                        input_value
                        * SECONDS_PER_JULIAN_YEAR
                    )

                earth_time = +earth_time

            else:
                raise ValueError(
                    "Unsupported measurement type"
                )

            try:
                unit_index = C_UNIT_NAMES.index(
                    self.unit_var.get()
                )
            except ValueError as error:
                raise ValueError(
                    "Unsupported speed-of-light unit"
                ) from error

            c_current = C_UNITS[unit_index]

            self.progress_var.set(
                self.PROGRESS_STEPS["UNIT_SELECTED"]
            )

            gamma = time_dilation_factor(
                velocity,
                c_current,
            )

            if gamma is None:
                raise ValueError(
                    "Unable to calculate the Lorentz factor"
                )

            self.progress_var.set(
                self.PROGRESS_STEPS["GAMMA_CALCULATED"]
            )

            with localcontext() as context:
                context.prec = (
                    DECIMAL_PRECISION + GUARD_DIGITS
                )

                traveler_time = earth_time / gamma

            traveler_time = +traveler_time

            self.progress_var.set(
                self.PROGRESS_STEPS["TIME_CALCULATED"]
            )

            formatted_c = (
                f"{format_large_or_small_number(c_current)} "
                f"{self.unit_var.get()}"
            )

            result = format_output(
                earth_time=earth_time,
                traveler_time=traveler_time,
                gamma=gamma,
                velocity_str=str(velocity),
                unit=formatted_c,
            )

            self.results_text.insert(tk.END, result)
            self.status_var.set("Calculation complete")

            logging.info(
                "Calculation completed - "
                "Velocity: %s%%, "
                "Earth time: %s seconds, "
                "Traveler time: %s seconds, "
                "Gamma: %s, "
                "Unit: %s",
                velocity,
                earth_time,
                traveler_time,
                gamma,
                self.unit_var.get(),
            )

            self.progress_var.set(
                self.PROGRESS_STEPS["COMPLETE"]
            )

        except (
            InvalidOperation,
            DivisionByZero,
            ValueError,
            ArithmeticError,
        ) as error:
            self.results_text.insert(
                tk.END,
                f"Error: {error}",
            )

            self.status_var.set("Error in calculation")

            logging.error(
                "Calculation error: %s",
                error,
            )

            self.progress_var.set(
                self.PROGRESS_STEPS["START"]
            )

    def clear_placeholder(self, event=None):
        """
        Clear the example velocity when the field gains focus.
        """
        if (
            self.velocity_entry.get()
            == "99.999999999999"
        ):
            self.velocity_entry.delete(0, tk.END)

    def restore_placeholder(self, event=None):
        """
        Restore the example velocity when the field is left empty.
        """
        if not self.velocity_entry.get().strip():
            self.velocity_var.set(
                "99.999999999999"
            )

    def fetch_source_code(self):
        """
        Display the exact source file currently being executed.
        """
        if self.notebook.select() != str(self.source_tab):
            return

        self.source_text.configure(state="normal")
        self.source_text.delete("1.0", tk.END)

        try:
            source = Path(__file__).read_text(
                encoding="utf-8"
            )

            self.source_text.insert(
                tk.END,
                source,
            )

        except (OSError, UnicodeError) as error:
            self.source_text.insert(
                tk.END,
                (
                    "Unable to display the local source code: "
                    f"{error}"
                ),
            )

        self.source_text.configure(state="disabled")

    def on_tab_changed(self, event=None):
        """
        Refresh the source-code tab when selected.
        """
        self.fetch_source_code()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s - %(levelname)s - %(message)s"
        ),
        filename="time_dilation.log",
    )

    logging.info("Application starting")

    app = TimeDilationCalculator()
    app.mainloop()

    logging.info("Application closed")


if __name__ == "__main__":
    main()
