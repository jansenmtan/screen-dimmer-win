import ctypes
import json
import math
import os
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import tkinter as tk
from tkinter import ttk

from astral import Observer
from astral.sun import sun
from timezonefinder import TimezoneFinder

# --- 1. WINDOWS API CONFIGURATION ---
class RGB(ctypes.Structure):
    _fields_ = [
        ('red', ctypes.c_ushort * 256),
        ('green', ctypes.c_ushort * 256),
        ('blue', ctypes.c_ushort * 256),
    ]

class DEVMODE(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32),
        ("dmSpecVersion", ctypes.c_ushort),
        ("dmDriverVersion", ctypes.c_ushort),
        ("dmSize", ctypes.c_ushort),
        ("dmDriverExtra", ctypes.c_ushort),
        ("dmFields", ctypes.c_ulong),
        ("dmOrientation", ctypes.c_short),
        ("dmPaperSize", ctypes.c_short),
        ("dmPaperLength", ctypes.c_short),
        ("dmPaperWidth", ctypes.c_short),
        ("dmScale", ctypes.c_short),
        ("dmCopies", ctypes.c_short),
        ("dmDefaultSource", ctypes.c_short),
        ("dmPrintQuality", ctypes.c_short),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", ctypes.c_ushort),
        ("dmBitsPerPel", ctypes.c_ulong),
        ("dmPelsWidth", ctypes.c_ulong),
        ("dmPelsHeight", ctypes.c_ulong),
        ("dmDisplayFlags", ctypes.c_ulong),
        ("dmDisplayFrequency", ctypes.c_ulong),
        ("dmICMMethod", ctypes.c_ulong),
        ("dmICMIntent", ctypes.c_ulong),
        ("dmMediaType", ctypes.c_ulong),
        ("dmDitherType", ctypes.c_ulong),
        ("dmReserved1", ctypes.c_ulong),
        ("dmReserved2", ctypes.c_ulong),
        ("dmPanningWidth", ctypes.c_ulong),
        ("dmPanningHeight", ctypes.c_ulong),
    ]

ENUM_CURRENT_SETTINGS = -1

hdc = ctypes.windll.user32.GetDC(0)

# The last ramp WE wrote. The hardware LUT is global, volatile state: games,
# the OS, and display mode changes can overwrite it at any time with no
# notification. The watchdog (section 2b) detects that and re-applies this.
current_ramp = None

WATCHDOG_INTERVAL_MS = 2000
# Tolerance in 16-bit LUT units to absorb driver quantization on read-back.
# A genuine reset or foreign ramp differs by far more when dimming is active.
RAMP_TOLERANCE = 1024

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gamma_watchdog.log")

def log_event(message):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    try:
        print(line)
    except Exception:
        pass  # no console when launched via pythonw

def apply_gamma(r_mult, g_mult, b_mult, brightness=1.0):
    """
    Applies the gamma ramp to Windows.
    Multipliers should be between 0.0 and 1.0.
    Brightness should be between 0.05 (5%) and 1.0 (100%).
    """
    global current_ramp
    ramp = RGB()
    for i in range(256):
        # Base linear value from 0 to 65535
        base = int(i * 65535 / 255)

        # Apply brightness multiplier first, then color channel multiplier
        ramp.red[i] = max(0, min(65535, int(base * brightness * r_mult)))
        ramp.green[i] = max(0, min(65535, int(base * brightness * g_mult)))
        ramp.blue[i] = max(0, min(65535, int(base * brightness * b_mult)))

    current_ramp = ramp
    ctypes.windll.gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp))

# --- 2. WATCHDOG DIAGNOSTIC HELPERS ---
def ramp_max_diff(a, b):
    """Largest absolute difference between two ramps, across all channels."""
    worst = 0
    for channel in ('red', 'green', 'blue'):
        ca = getattr(a, channel)
        cb = getattr(b, channel)
        for i in range(256):
            d = abs(ca[i] - cb[i])
            if d > worst:
                worst = d
    return worst

def is_linear_ramp(ramp):
    """True if ramp is the default 1:1 (identity) ramp, within tolerance."""
    for i in range(256):
        base = int(i * 65535 / 255)
        if (abs(ramp.red[i] - base) > RAMP_TOLERANCE
                or abs(ramp.green[i] - base) > RAMP_TOLERANCE
                or abs(ramp.blue[i] - base) > RAMP_TOLERANCE):
            return False
    return True

def ramp_samples(ramp):
    """Sample values for identifying a foreign ramp.
    Linear reference: i=32 -> 8224, i=128 -> 32896, i=255 -> 65535.
    Mid values ABOVE that reference = brightening curve (washed-out look)."""
    idx = (32, 128, 255)
    r = [ramp.red[i] for i in idx]
    g = [ramp.green[i] for i in idx]
    b = [ramp.blue[i] for i in idx]
    return f"R{r} G{g} B{b} @ i={list(idx)}"

def get_display_mode():
    """Current (width, height, refresh Hz) of the primary display, or None."""
    dm = DEVMODE()
    dm.dmSize = ctypes.sizeof(DEVMODE)
    if ctypes.windll.user32.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
        return (dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency)
    return None

def fmt_mode(mode):
    if mode is None:
        return "unknown"
    w, h, freq = mode
    return f"{w}x{h} @{freq}Hz"

# --- 3. HELPER FUNCTIONS ---
def kelvin_to_rgb(kelvin):
    """
    Converts a color temperature in Kelvin to RGB multipliers (0.0 - 1.0).
    Uses the Tanner Helland algorithm.
    """
    temp = kelvin / 100.0

    # Calculate Red
    if temp <= 66:
        r = 255
    else:
        r = 329.698727446 * math.pow(temp - 60, -0.1332047592)

    # Calculate Green
    if temp <= 66:
        g = 99.4708025861 * math.log(temp) - 161.1195681661
    else:
        g = 288.122169225283 * math.pow(temp - 60, -0.0755148492)

    # Calculate Blue
    if temp >= 66:
        b = 255
    elif temp <= 19:
        b = 0
    else:
        b = 138.5177312231 * math.log(temp - 10) - 305.0447927307

    # Normalize so that 6500K (daylight) maps to exactly (1.0, 1.0, 1.0),
    # i.e. an unchanged screen at max temp + 100% brightness.
    # Reference values are the raw formula outputs at temp=65 (6500K).
    r_norm = max(0.0, min(1.0, r / 255.0))
    g_norm = max(0.0, min(1.0, g / 254.11008387561782))
    b_norm = max(0.0, min(1.0, b / 250.0419083427406))

    return r_norm, g_norm, b_norm

def get_location_from_zip(zip_code):
    """Fetches Lat/Lon and Place Name from a US Zip Code using Zippopotam."""
    try:
        url = f"https://api.zippopotam.us/us/{zip_code}"
        with urllib.request.urlopen(url) as response:
            data = json.loads(response.read().decode())
        place = data['places'][0]
        lat = float(place['latitude'])
        lon = float(place['longitude'])
        name = f"{place['place name']}, {place['state abbreviation']}"
        return lat, lon, name
    except Exception:
        return None

# --- 4. GUI APPLICATION ---
class SunsetApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Custom Sunset Screen")
        self.geometry("400x500")
        self.resizable(False, False)

        # Prevent screen from staying tinted if the app is closed
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.lat = None
        self.lon = None

        self._watchdog_job = None
        self.last_mode = get_display_mode()
        log_event(f"App started. Display mode: {fmt_mode(self.last_mode)}")

        self.create_widgets()
        self.start_watchdog()

    def create_widgets(self):
        # --- Location Section ---
        loc_frame = tk.LabelFrame(self, text="Location", padx=10, pady=10)
        loc_frame.pack(padx=10, pady=10, fill="x")

        tk.Label(loc_frame, text="Zip Code:").grid(row=0, column=0, sticky="w")
        self.zip_entry = tk.Entry(loc_frame, width=10)
        self.zip_entry.grid(row=0, column=1, padx=5)

        tk.Button(loc_frame, text="Set Location", command=self.fetch_location).grid(row=0, column=2, padx=5)

        self.loc_label = tk.Label(loc_frame, text="Not set", fg="gray")
        self.loc_label.grid(row=1, column=0, columnspan=3, pady=(5,0), sticky="w")

        self.sunset_label = tk.Label(loc_frame, text="Sunset: N/A", fg="gray")
        self.sunset_label.grid(row=2, column=0, columnspan=3, sticky="w")

        # --- Mode Selection ---
        self.mode_var = tk.StringVar(value="temp")

        mode_frame = tk.Frame(self)
        mode_frame.pack(pady=5)
        tk.Radiobutton(mode_frame, text="Temperature Mode", variable=self.mode_var, value="temp", command=self.toggle_modes).pack(side="left", padx=10)
        tk.Radiobutton(mode_frame, text="RGB Mode", variable=self.mode_var, value="rgb", command=self.toggle_modes).pack(side="left", padx=10)

        # --- Temperature/Brightness Mode Frame ---
        self.temp_frame = tk.LabelFrame(self, text="Temperature & Brightness", padx=10, pady=10)
        self.temp_frame.pack(padx=10, pady=5, fill="both", expand=True)

        tk.Label(self.temp_frame, text="Color Temp (Kelvin)").pack()
        self.temp_slider = tk.Scale(self.temp_frame, from_=1000, to=6500, orient="horizontal", command=lambda e: self.update_screen())
        self.temp_slider.set(6500)
        self.temp_slider.pack(fill="x")

        tk.Label(self.temp_frame, text="Brightness (%)").pack(pady=(10,0))
        self.bright_slider = tk.Scale(self.temp_frame, from_=5, to=100, orient="horizontal", command=lambda e: self.update_screen())
        self.bright_slider.set(100)
        self.bright_slider.pack(fill="x")

        # --- RGB Mode Frame ---
        self.rgb_frame = tk.LabelFrame(self, text="RGB Intensity", padx=10, pady=10)
        # Packed/Unpacked via toggle_modes

        tk.Label(self.rgb_frame, text="Red (%)").pack()
        self.r_slider = tk.Scale(self.rgb_frame, from_=0, to=100, orient="horizontal", fg="red", command=lambda e: self.update_screen())
        self.r_slider.set(100)
        self.r_slider.pack(fill="x")

        tk.Label(self.rgb_frame, text="Green (%)").pack(pady=(10,0))
        self.g_slider = tk.Scale(self.rgb_frame, from_=0, to=100, orient="horizontal", fg="green", command=lambda e: self.update_screen())
        self.g_slider.set(100)
        self.g_slider.pack(fill="x")

        tk.Label(self.rgb_frame, text="Blue (%)").pack(pady=(10,0))
        self.b_slider = tk.Scale(self.rgb_frame, from_=0, to=100, orient="horizontal", fg="blue", command=lambda e: self.update_screen())
        self.b_slider.set(100)
        self.b_slider.pack(fill="x")

        # --- Reset Button ---
        tk.Button(self, text="Reset to Normal (Daylight)", bg="lightgray", command=self.reset_screen).pack(pady=10)

        self.toggle_modes() # Initialize correct frame visibility

    def toggle_modes(self):
        if self.mode_var.get() == "temp":
            self.rgb_frame.pack_forget()
            self.temp_frame.pack(padx=10, pady=5, fill="both", expand=True)
        else:
            self.temp_frame.pack_forget()
            self.rgb_frame.pack(padx=10, pady=5, fill="both", expand=True)
        self.update_screen()

    def fetch_location(self):
        zip_code = self.zip_entry.get()
        if not zip_code.isdigit() or len(zip_code) != 5:
            self.loc_label.config(text="Invalid Zip Code", fg="red")
            return

        result = get_location_from_zip(zip_code)
        if result:
            self.lat, self.lon, name = result
            self.loc_label.config(text=f"Location: {name}", fg="black")
            self.calculate_sunset()
        else:
            self.loc_label.config(text="Zip Code not found", fg="red")

    def calculate_sunset(self):
        if not self.lat or not self.lon:
            return

        # Find the timezone for these coordinates
        tf = TimezoneFinder()
        tz_str = tf.timezone_at(lat=self.lat, lng=self.lon)
        tz = ZoneInfo(tz_str)

        # Get current local time at that location
        now_local = datetime.now(tz)

        # Calculate sun events using Astral
        s = sun(Observer(self.lat, self.lon), date=now_local.date(), tzinfo=tz)
        sunset_time = s['sunset'].strftime('%I:%M %p')

        self.sunset_label.config(text=f"Local Sunset Today: {sunset_time}", fg="black")

    def update_screen(self):
        mode = self.mode_var.get()

        if mode == "temp":
            # Convert Kelvin to RGB
            r_mult, g_mult, b_mult = kelvin_to_rgb(self.temp_slider.get())
            # Get brightness (slider is 5-100, convert to 0.05 - 1.0)
            brightness = self.bright_slider.get() / 100.0

            apply_gamma(r_mult, g_mult, b_mult, brightness)
        else:
            # Get RGB values directly
            r_mult = self.r_slider.get() / 100.0
            g_mult = self.g_slider.get() / 100.0
            b_mult = self.b_slider.get() / 100.0
            # In RGB mode, brightness is implicit to the sliders
            apply_gamma(r_mult, g_mult, b_mult, brightness=1.0)

    # --- Gamma watchdog: detect & repair external resets/overwrites ---
    def start_watchdog(self):
        self._watchdog_job = self.after(WATCHDOG_INTERVAL_MS, self.gamma_watchdog)

    def gamma_watchdog(self):
        """Compare the live hardware ramp against the last one we wrote.

        Doubles as an experiment to identify what wipes the ramp in games:
        - foreign ramp LINEAR + display mode just changed -> driver reset the
          LUT on the mode switch
        - foreign ramp is a CUSTOM curve -> the game (or another app) wrote
          its own ramp over ours
        - NO drift detected but the screen still looks washed out -> our ramp
          is installed but being bypassed (HDR / exclusive flip path), and
          re-applying cannot fix that
        """
        if current_ramp is not None:
            actual = RGB()
            if ctypes.windll.gdi32.GetDeviceGammaRamp(hdc, ctypes.byref(actual)):
                diff = ramp_max_diff(current_ramp, actual)
                mode_now = get_display_mode()
                mode_changed = mode_now != self.last_mode
                self.last_mode = mode_now

                if diff > RAMP_TOLERANCE:
                    kind = ("LINEAR/default (reset)"
                            if is_linear_ramp(actual)
                            else "CUSTOM curve (another app/game wrote it)")
                    log_event(
                        f"Ramp overwritten! max diff={diff}. Foreign ramp is {kind}. "
                        f"Samples: {ramp_samples(actual)}. "
                        f"Display mode {fmt_mode(mode_now)}"
                        f"{' [CHANGED in last 2s]' if mode_changed else ' [unchanged]'}. "
                        f"Re-applied our ramp."
                    )
                    ctypes.windll.gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(current_ramp))
                elif mode_changed:
                    log_event(
                        f"Display mode changed to {fmt_mode(mode_now)} "
                        f"but our ramp is intact (diff={diff})."
                    )
        self._watchdog_job = self.after(WATCHDOG_INTERVAL_MS, self.gamma_watchdog)

    def reset_screen(self):
        self.mode_var.set("temp")
        self.toggle_modes()
        self.temp_slider.set(6500)
        self.bright_slider.set(100)
        self.r_slider.set(100)
        self.g_slider.set(100)
        self.b_slider.set(100)
        apply_gamma(1.0, 1.0, 1.0, 1.0)

    def on_close(self):
        if self._watchdog_job is not None:
            try:
                self.after_cancel(self._watchdog_job)
            except Exception:
                pass
            self._watchdog_job = None
        log_event("App closing; ramp reset to normal.")
        # Critical: Reset screen to normal before closing
        apply_gamma(1.0, 1.0, 1.0, 1.0)
        ctypes.windll.user32.ReleaseDC(0, hdc)
        self.destroy()

if __name__ == "__main__":
    app = SunsetApp()
    app.mainloop()
