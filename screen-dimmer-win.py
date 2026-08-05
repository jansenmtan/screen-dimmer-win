import ctypes
import json
import math
import os
import urllib.request
from datetime import datetime, timedelta
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

# Settings are persisted next to this script
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

DEFAULT_SETTINGS = {
    "zip_code": "",
    "location": None,        # {"lat": float, "lon": float, "name": str, "tz": str}
    "auto_enabled": False,
    "night_brightness": 30,
    "auto_mode": "sun",     # "sun" = follow sunrise/sunset, "curve" = custom 24h curve
    "auto_curve": None,      # [[hour, brightness%], ...] for custom curve mode
}

TRANSITION_MINUTES = 30  # fade length around sunrise/sunset

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

def fmt_time(dt):
    """Formats a datetime like '7:15 AM' (Windows-safe, no %-I flag)."""
    return dt.strftime('%I:%M %p').lstrip('0')

def blend(now, start, end, from_val, to_val):
    """Linear interpolation of a value as `now` moves from start to end."""
    total = (end - start).total_seconds()
    if total <= 0:
        return to_val
    f = max(0.0, min(1.0, (now - start).total_seconds() / total))
    return from_val + (to_val - from_val) * f

def default_curve(night_pct):
    """Seed curve: night level with 30-min fades around 7:00 AM / 7:00 PM."""
    n = float(night_pct)
    return [[0.0, n], [6.5, n], [7.0, 100.0], [18.5, 100.0], [19.0, n], [24.0, n]]

def eval_curve(points, hour):
    """Brightness (0-100) at `hour` (0-24) on a piecewise-linear 24h curve.

    The curve is periodic: the segment after the last point wraps around to
    the first point (e.g. 22:00 -> 06:00 with no point at midnight)."""
    if not points:
        return 100.0
    pts = sorted(points, key=lambda p: p[0])
    if len(pts) == 1:
        return pts[0][1]
    hour = hour % 24.0
    first, last = pts[0], pts[-1]
    if hour < first[0] or hour >= last[0]:
        # wrap-around segment: last point -> first point (next day)
        span = first[0] + 24.0 - last[0]
        if hour >= last[0]:
            t = (hour - last[0]) / span
        else:
            t = (hour + 24.0 - last[0]) / span
        return last[1] + (first[1] - last[1]) * t
    for (h0, b0), (h1, b1) in zip(pts, pts[1:]):
        if h0 <= hour < h1:
            f = (hour - h0) / (h1 - h0)
            return b0 + (b1 - b0) * f
    return pts[-1][1]

class CurveEditor(tk.Toplevel):
    """Edit the 24-hour brightness curve.

    Drag points to move them, click empty space to add a point, right-click a
    point to delete it. Brightness is interpolated linearly between points.
    """

    CW, CH = 620, 300
    PAD_L, PAD_R, PAD_T, PAD_B = 50, 14, 12, 34
    MIN_POINTS = 1
    MAX_POINTS = 48

    def __init__(self, parent, points, night_pct, on_done):
        super().__init__(parent)
        self.title("Auto Brightness Curve \u2014 24h schedule")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.night_pct = night_pct
        self.points = [[float(h), float(b)] for h, b in points] or default_curve(night_pct)
        self.on_done = on_done
        self.selected = None
        self._clock_job = None

        self.canvas = tk.Canvas(self, width=self.CW, height=self.CH, bg="white",
                                highlightthickness=1, highlightbackground="#999")
        self.canvas.pack(padx=10, pady=(10, 2))

        self.status = tk.Label(self, text="", fg="#333")
        self.status.pack(anchor="w", padx=12)

        tk.Label(self, text="Drag points to move \u00b7 click empty space to add \u00b7 right-click a point to delete",
                 fg="gray").pack(anchor="w", padx=12)

        btns = tk.Frame(self)
        btns.pack(pady=8)
        tk.Button(btns, text="Reset to Default", command=self.reset_points).pack(side="left", padx=6)
        tk.Button(btns, text="Cancel", command=self.destroy).pack(side="left", padx=6)
        tk.Button(btns, text="Done", command=self.done).pack(side="left", padx=6)

        self.canvas.bind("<Button-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Button-3>", self.on_right_click)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        self.draw()
        self._clock_job = self.after(60_000, self._clock_tick)

    def _clock_tick(self):
        self.draw()
        self._clock_job = self.after(60_000, self._clock_tick)

    def destroy(self):
        if self._clock_job is not None:
            try:
                self.after_cancel(self._clock_job)
            except Exception:
                pass
            self._clock_job = None
        super().destroy()

    # --- geometry ---
    def x_at(self, hour):
        return self.PAD_L + hour / 24.0 * (self.CW - self.PAD_L - self.PAD_R)

    def hour_at(self, x):
        return max(0.0, min(24.0, (x - self.PAD_L) / (self.CW - self.PAD_L - self.PAD_R) * 24.0))

    def y_at(self, bright):
        return self.PAD_T + (1.0 - bright / 100.0) * (self.CH - self.PAD_T - self.PAD_B)

    def bright_at(self, y):
        return max(0.0, min(100.0, (1.0 - (y - self.PAD_T) / (self.CH - self.PAD_T - self.PAD_B)) * 100.0))

    def draw(self):
        c = self.canvas
        c.delete("all")

        # grid: hours every 2h, brightness every 25%
        for h in range(0, 25, 2):
            x = self.x_at(h)
            c.create_line(x, self.PAD_T, x, self.CH - self.PAD_B, fill="#e8e8e8")
            c.create_text(x, self.CH - self.PAD_B + 9, text=str(h), fill="#888", font=("Segoe UI", 8))
        for b in range(0, 101, 25):
            y = self.y_at(b)
            c.create_line(self.PAD_L, y, self.CW - self.PAD_R, y, fill="#e8e8e8")
            c.create_text(self.PAD_L - 6, y, text=str(b), anchor="e", fill="#888", font=("Segoe UI", 8))

        # polyline, wrapping from the last point to the first point (next day)
        pts = sorted(self.points, key=lambda p: p[0])
        if pts:
            poly = [(self.x_at(h), self.y_at(b)) for h, b in pts]
            poly.append((self.x_at(pts[0][0] + 24.0), self.y_at(pts[0][1])))
            c.create_line(poly, fill="#1a6fd0", width=2)

        for h, b in pts:
            c.create_oval(self.x_at(h) - 5, self.y_at(b) - 5,
                          self.x_at(h) + 5, self.y_at(b) + 5,
                          fill="#ff5555", outline="#222", width=1)

        # current-time marker
        now = datetime.now()
        hour = now.hour + now.minute / 60.0
        bx = eval_curve(pts, hour) if pts else 100.0
        x = self.x_at(hour)
        c.create_line(x, self.PAD_T, x, self.CH - self.PAD_B, fill="#d00", dash=(3, 3))
        c.create_oval(x - 4, self.y_at(bx) - 4, x + 4, self.y_at(bx) + 4, fill="#d00", outline="")
        self.status.config(text=f"Now: {fmt_time(now)} \u2192 brightness {round(bx)}%")

    # --- mouse handling ---
    def _hit(self, event):
        """Index of the point within 8px of the click, or None."""
        best, best_d = None, 8.0
        for i, (h, b) in enumerate(self.points):
            dx = self.x_at(h) - event.x
            dy = self.y_at(b) - event.y
            d = (dx * dx + dy * dy) ** 0.5
            if d <= best_d:
                best, best_d = i, d
        return best

    def on_press(self, event):
        i = self._hit(event)
        if i is not None:
            self.selected = i
            return
        if len(self.points) >= self.MAX_POINTS:
            return
        x0, x1 = self.PAD_L, self.CW - self.PAD_R
        y0, y1 = self.PAD_T, self.CH - self.PAD_B
        if not (x0 <= event.x <= x1 and y0 <= event.y <= y1):
            return
        h = max(0.0, min(24.0, round(self.hour_at(event.x) * 12) / 12.0))  # 5-min snap
        b = round(self.bright_at(event.y))
        for j, (eh, _) in enumerate(self.points):
            if abs(eh - h) < 1 / 60:
                self.selected = j  # already a point here; select it instead
                return
        self.points.append([h, b])
        self.selected = len(self.points) - 1
        self.draw()

    def on_drag(self, event):
        if self.selected is None:
            return
        i = self.selected
        h = max(0.0, min(24.0, round(self.hour_at(event.x) * 12) / 12.0))
        b = max(0.0, min(100.0, round(self.bright_at(event.y))))
        pts = sorted(self.points, key=lambda p: p[0])
        j = pts.index(self.points[i])
        lo = (pts[j - 1][0] + 1 / 60) if j > 0 else 0.0
        hi = (pts[j + 1][0] - 1 / 60) if j < len(pts) - 1 else 24.0
        self.points[i] = [max(lo, min(hi, h)), b]
        self.draw()

    def on_release(self, event):
        self.selected = None

    def on_right_click(self, event):
        i = self._hit(event)
        if i is not None and len(self.points) > self.MIN_POINTS:
            del self.points[i]
            self.draw()

    def reset_points(self):
        self.points = default_curve(self.night_pct)
        self.draw()

    def done(self):
        self.on_done(sorted(self.points, key=lambda p: p[0]))
        self.destroy()

# --- 4. GUI APPLICATION ---
class SunsetApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Custom Sunset Screen")
        self.geometry("400x720")
        self.resizable(False, False)

        # Prevent screen from staying tinted if the app is closed
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Load saved preferences (zip code, location, auto-brightness)
        self.settings = self.load_settings()
        loc = self.settings.get("location") or {}
        self.lat = loc.get("lat")
        self.lon = loc.get("lon")
        self.loc_name = loc.get("name")
        self.tz = None
        if loc.get("tz"):
            try:
                self.tz = ZoneInfo(loc["tz"])
            except Exception:
                self.tz = None
        self.auto_brightness = 1.0

        self._watchdog_job = None
        self.last_mode = get_display_mode()
        log_event(f"App started. Display mode: {fmt_mode(self.last_mode)}")

        self.create_widgets()

        # Restore saved location, if any
        if self.lat is not None and self.lon is not None and self.loc_name:
            self.loc_label.config(text=f"Location: {self.loc_name}", fg="black")
            self.calculate_sunset()

        # Apply saved auto-brightness state and start the periodic timer
        self.apply_auto_state()
        self.after(60_000, self.auto_tick)

        self.start_watchdog()

    def create_widgets(self):
        # --- Location Section ---
        loc_frame = tk.LabelFrame(self, text="Location", padx=10, pady=10)
        loc_frame.pack(padx=10, pady=10, fill="x")

        tk.Label(loc_frame, text="Zip Code:").grid(row=0, column=0, sticky="w")
        self.zip_entry = tk.Entry(loc_frame, width=10)
        self.zip_entry.grid(row=0, column=1, padx=5)
        saved_zip = self.settings.get("zip_code", "")
        if saved_zip:
            self.zip_entry.insert(0, saved_zip)

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

        # --- Auto Brightness Frame ---
        auto_frame = tk.LabelFrame(self, text="Auto Brightness", padx=10, pady=10)
        auto_frame.pack(padx=10, pady=5, fill="x")

        self.auto_var = tk.BooleanVar(value=bool(self.settings.get("auto_enabled", False)))
        tk.Checkbutton(auto_frame, text="Auto-adjust by time of day",
                       variable=self.auto_var, command=self.toggle_auto).pack(anchor="w")

        self.auto_mode_var = tk.StringVar(value=self.settings.get("auto_mode", "sun"))
        mode_row = tk.Frame(auto_frame)
        mode_row.pack(anchor="w", pady=(5, 0))
        tk.Radiobutton(mode_row, text="Follow sunrise/sunset", variable=self.auto_mode_var,
                       value="sun", command=self.on_auto_mode_change).pack(side="left")
        tk.Radiobutton(mode_row, text="Custom curve", variable=self.auto_mode_var,
                       value="curve", command=self.on_auto_mode_change).pack(side="left", padx=(10, 0))

        self.curve_btn = tk.Button(auto_frame, text="Edit Curve\u2026", command=self.open_curve_editor)
        self.curve_btn.pack(anchor="w", pady=(5, 0))

        tk.Label(auto_frame, text="Night Brightness (%)").pack(anchor="w", pady=(5, 0))
        self.night_slider = tk.Scale(auto_frame, from_=5, to=100, orient="horizontal",
                                     command=lambda e: self.on_night_change())
        self.night_slider.set(int(self.settings.get("night_brightness", 30)))
        self.night_slider.pack(fill="x")

        self.auto_status = tk.Label(auto_frame, text="Off", fg="gray")
        self.auto_status.pack(anchor="w", pady=(5, 0))

        self.on_auto_mode_change()

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

    def load_settings(self):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError
        except Exception:
            data = {}
        merged = dict(DEFAULT_SETTINGS)
        merged.update(data)
        return merged

    def save_settings(self):
        loc = None
        if self.lat is not None and self.lon is not None:
            loc = {
                "lat": self.lat,
                "lon": self.lon,
                "name": self.loc_name,
                "tz": self.tz.key if self.tz else None,
            }
        self.settings.update({
            "zip_code": self.zip_entry.get().strip(),
            "location": loc,
            "auto_enabled": bool(self.auto_var.get()),
            "night_brightness": self.night_slider.get(),
            "auto_mode": self.auto_mode_var.get(),
            "auto_curve": self.settings.get("auto_curve"),
        })
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=2)
        except Exception:
            pass  # never crash the app over a settings write

    def fetch_location(self):
        zip_code = self.zip_entry.get().strip()
        if not zip_code.isdigit() or len(zip_code) != 5:
            self.loc_label.config(text="Invalid Zip Code", fg="red")
            return

        result = get_location_from_zip(zip_code)
        if result:
            self.lat, self.lon, name = result
            self.loc_name = name
            self.loc_label.config(text=f"Location: {name}", fg="black")
            try:
                tz_str = TimezoneFinder().timezone_at(lat=self.lat, lng=self.lon)
                self.tz = ZoneInfo(tz_str) if tz_str else None
            except Exception:
                self.tz = None
            self.save_settings()
            self.calculate_sunset()
            if self.auto_var.get():
                self.run_auto_now()  # location may change the auto schedule
        else:
            self.loc_label.config(text="Zip Code not found", fg="red")

    def calculate_sunset(self):
        if self.lat is None or self.lon is None:
            return

        tz = self.tz or datetime.now().astimezone().tzinfo
        now_local = datetime.now(tz)

        # Calculate sun events using Astral
        try:
            s = sun(Observer(self.lat, self.lon), date=now_local.date(), tzinfo=tz)
            sunrise_time = fmt_time(s['sunrise'])
            sunset_time = fmt_time(s['sunset'])
            self.sunset_label.config(
                text=f"Sunrise: {sunrise_time}   Sunset: {sunset_time}", fg="black")
        except Exception:
            self.sunset_label.config(
                text="Sun times unavailable for this date/location", fg="red")

    # --- Auto Brightness ---
    def toggle_auto(self):
        self.save_settings()
        self.apply_auto_state()

    def on_auto_mode_change(self):
        """Switch between sun-synced and custom-curve schedules."""
        if self.auto_mode_var.get() == "curve":
            if not self.settings.get("auto_curve"):
                self.settings["auto_curve"] = default_curve(self.night_slider.get())
            self.night_slider.config(state="disabled")
            self.curve_btn.config(state="normal")
        else:
            self.night_slider.config(state="normal")
            self.curve_btn.config(state="disabled")
        self.save_settings()
        if self.auto_var.get():
            self.run_auto_now()

    def open_curve_editor(self):
        pts = self.settings.get("auto_curve") or default_curve(self.night_slider.get())
        CurveEditor(self, pts, self.night_slider.get(), self.on_curve_edited)

    def on_curve_edited(self, points):
        self.settings["auto_curve"] = points
        self.save_settings()
        if self.auto_var.get():
            self.run_auto_now()

    def apply_auto_state(self):
        """Enable/disable manual brightness control based on the auto checkbox."""
        if self.auto_var.get():
            self.bright_slider.config(state="disabled")
            self.run_auto_now()
        else:
            self.bright_slider.config(state="normal")
            self.auto_brightness = 1.0
            self.auto_status.config(text="Off", fg="gray")
            self.update_screen()

    def on_night_change(self):
        self.save_settings()
        if self.auto_var.get():
            self.run_auto_now()

    def auto_tick(self):
        """Periodic timer: refresh auto brightness, then reschedule."""
        if self.auto_var.get():
            self.run_auto_now()
        self.after(60_000, self.auto_tick)

    def run_auto_now(self):
        brightness, status = self.compute_auto_brightness()
        self.auto_brightness = brightness
        self.auto_status.config(text=status, fg="black")
        self.update_screen()

    def compute_auto_brightness(self):
        """Returns (brightness 0.05-1.0, status text) based on the time of day."""
        if self.auto_mode_var.get() == "curve":
            pts = self.settings.get("auto_curve") or default_curve(self.night_slider.get())
            now = datetime.now()
            hour = now.hour + now.minute / 60.0
            try:
                b = eval_curve(pts, hour) / 100.0
            except Exception:
                b = 1.0
            b = max(0.05, min(1.0, b))
            return b, f"Custom curve | Brightness {int(round(b * 100))}%"

        day = 1.0
        night = self.night_slider.get() / 100.0
        trans = timedelta(minutes=TRANSITION_MINUTES)
        source = ""

        sunrise = sunset = None
        if self.lat is not None and self.lon is not None:
            tz = self.tz or datetime.now().astimezone().tzinfo
            now = datetime.now(tz)
            try:
                s = sun(Observer(self.lat, self.lon), date=now.date(), tzinfo=tz)
                sunrise, sunset = s['sunrise'], s['sunset']
            except Exception:
                source = " (fixed schedule)"
        else:
            tz = datetime.now().astimezone().tzinfo
            now = datetime.now(tz)
            source = " (fixed schedule)"

        if sunrise is None:
            # Fallback: fixed 7:00-19:00 day with 1-hour fades, local time
            sunrise = now.replace(hour=7, minute=0, second=0, microsecond=0)
            sunset = now.replace(hour=19, minute=0, second=0, microsecond=0)
            trans = timedelta(minutes=60)

        if now < sunrise:
            b, phase = night, f"Night - sunrise {fmt_time(sunrise)}"
        elif now < sunrise + trans:
            b, phase = blend(now, sunrise, sunrise + trans, night, day), "Sunrise fade-in"
        elif now < sunset - trans:
            b, phase = day, f"Day - sunset {fmt_time(sunset)}"
        elif now < sunset:
            b, phase = blend(now, sunset - trans, sunset, day, night), "Sunset fade-out"
        else:
            b, phase = night, "Night - sunrise tomorrow"

        status = f"{phase} | Brightness {int(round(b * 100))}%{source}"
        return max(0.05, min(1.0, b)), status

    def update_screen(self):
        mode = self.mode_var.get()

        if mode == "temp":
            # Convert Kelvin to RGB
            r_mult, g_mult, b_mult = kelvin_to_rgb(self.temp_slider.get())
            # Auto mode overrides the brightness slider (5-100 -> 0.05 - 1.0)
            if self.auto_var.get():
                brightness = self.auto_brightness
            else:
                brightness = self.bright_slider.get() / 100.0

            apply_gamma(r_mult, g_mult, b_mult, brightness)
        else:
            # Get RGB values directly
            r_mult = self.r_slider.get() / 100.0
            g_mult = self.g_slider.get() / 100.0
            b_mult = self.b_slider.get() / 100.0
            # In RGB mode, auto brightness applies as a multiplier if enabled
            brightness = self.auto_brightness if self.auto_var.get() else 1.0
            apply_gamma(r_mult, g_mult, b_mult, brightness)

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
        # Persist preferences, then reset screen to normal before closing
        self.save_settings()
        apply_gamma(1.0, 1.0, 1.0, 1.0)
        ctypes.windll.user32.ReleaseDC(0, hdc)
        self.destroy()

if __name__ == "__main__":
    app = SunsetApp()
    app.mainloop()
