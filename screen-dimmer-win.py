import ctypes
import json
import math
import os
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import tkinter as tk
from tkinter import messagebox, ttk

from astral import Observer
from astral.sun import elevation, sun
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
    "auto_curve": None,      # [[x, brightness%], ...] on the normalized day axis
    "auto_curve_floor": 0,   # minimum brightness % for the custom curve
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
    """Seed curve on the normalized axis: x = -1 solar midnight, 0 sunrise,
    1 solar noon. Night level with a dawn ramp through sunrise reaching full
    brightness early in the morning."""
    n = float(night_pct)
    return [[-1.0, n], [-0.2, n], [0.0, 60.0], [0.2, 100.0], [1.0, 100.0]]

def eval_curve(points, x):
    """Brightness (0-100) at normalized position x on the day curve.

    x = -1 at solar midnight, 0 at sunrise, 1 at solar noon. The evening is
    the mirror of the morning (same curve, other half-day). Values extend
    flat beyond the first/last point.
    """
    if not points:
        return 100.0
    pts = sorted(points, key=lambda p: p[0])
    if len(pts) == 1:
        return pts[0][1]
    x = max(-1.0, min(1.0, x))
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, b0), (x1, b1) in zip(pts, pts[1:]):
        if x0 <= x < x1:
            f = (x - x0) / (x1 - x0)
            return b0 + (b1 - b0) * f
    return pts[-1][1]

def eval_curve_24h(points, hour):
    """Legacy: brightness (0-100) at `hour` (0-24) on a periodic 24h curve.
    Only used to sample saved curves during one-time migration."""
    if not points:
        return 100.0
    pts = sorted(points, key=lambda p: p[0])
    if len(pts) == 1:
        return pts[0][1]
    hour = hour % 24.0
    first, last = pts[0], pts[-1]
    if hour < first[0] or hour >= last[0]:
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

def legacy_is_24h(points):
    """True if a saved curve still uses the old 24h wall-clock axis."""
    return any(float(p[0]) > 12.0 + 1e-6 for p in points)

def convert_to_curve(points24, sun):
    """Fold a legacy 24h curve into the normalized [-1,1] day curve.

    The day is now anchored on sun events and mirrored: each normalized
    position x maps to a morning wall-clock time and its evening mirror, and
    the converted brightness is the average of the two legacy readings.
    Sampled at 0.02 steps, decimated to points whose value changed by >= 4%
    from the last kept point (plus endpoints), so adjacent points stay far
    enough apart to drag easily.
    """
    def h_of(dt):
        return dt.hour + dt.minute / 60.0

    def b_at(x):
        if x >= 0.0:
            t_m = sun['sunrise'] + timedelta(hours=x * (sun['noon'] - sun['sunrise']).total_seconds() / 3600.0)
            t_e = sun['sunset'] - timedelta(hours=x * (sun['sunset'] - sun['noon']).total_seconds() / 3600.0)
        else:
            xm = -x
            t_m = sun['sunrise'] - timedelta(hours=xm * (sun['sunrise'] - sun['mid_before']).total_seconds() / 3600.0)
            t_e = sun['sunset'] + timedelta(hours=xm * (sun['mid_after'] - sun['sunset']).total_seconds() / 3600.0)
        return (eval_curve_24h(points24, h_of(t_m)) + eval_curve_24h(points24, h_of(t_e))) / 2.0

    out = []
    last_b = None
    for i in range(-50, 51):  # x in [-1, 1], 0.02 steps
        xx = round(i * 0.02, 2)
        b = round(b_at(xx))
        if last_b is None or abs(b - last_b) >= 4.0:
            out.append([xx, b])
            last_b = b
    if not out or out[0][0] != -1.0:
        out.insert(0, [-1.0, round(b_at(-1.0))])
    if out[-1][0] != 1.0:
        out.append([1.0, round(b_at(1.0))])
    return out

def curve_x(now, sun):
    """Normalized position of `now` on the day curve (mirrored for evening).

    Morning half: x = (t - sunrise) / (noon - sunrise).
    Night (before sunrise): x = (t - sunrise) / (sunrise - midnight_before).
    The afternoon/evening mirrors those two onto the same axis.
    """
    if now < sun['sunrise']:
        return (now - sun['sunrise']) / (sun['sunrise'] - sun['mid_before'])
    if now < sun['noon']:
        return (now - sun['sunrise']) / (sun['noon'] - sun['sunrise'])
    if now < sun['sunset']:
        return (sun['sunset'] - now) / (sun['sunset'] - sun['noon'])
    return -(now - sun['sunset']) / (sun['mid_after'] - sun['sunset'])

def curve_sector(now, sun):
    """Human-readable position of `now` relative to the sun events."""
    def h(dt):
        return abs(dt.total_seconds()) / 3600.0
    if now < sun['sunrise']:
        return f"{h(now - sun['sunrise']):.1f}h before sunrise"
    if now < sun['noon']:
        return f"{h(now - sun['sunrise']):.1f}h after sunrise"
    if now < sun['sunset']:
        return f"{h(sun['sunset'] - now):.1f}h before sunset"
    return f"{h(now - sun['sunset']):.1f}h after sunset"

def clear_sky_irradiance(elev_deg, day_of_year):
    """Clear-sky irradiance (W/m2) on a horizontal surface.

    Standard direct+diffuse model (Masters 2004, the same methodology NMSU
    CR674 uses): direct normal A*exp(-B/sin(elev)) projected onto the
    horizontal plus a diffuse fraction. Returns 0 when the sun is at or
    below the horizon.
    """
    if elev_deg <= 0.0:
        return 0.0
    n = day_of_year
    a = 1160 + 75 * math.sin(math.radians(360.0 / 365 * (n - 275)))
    b = 0.174 + 0.035 * math.sin(math.radians(360.0 / 365 * (n - 100)))
    c = 0.095 + 0.04 * math.sin(math.radians(360.0 / 365 * (n - 100)))
    sinb = math.sin(math.radians(elev_deg))
    idn = a * math.exp(-b / sinb)
    return idn * sinb + idn * c

def generate_elevation_curve(sun, lat, lon, tz, floor):
    """Build a curve on the normalized axis from today's clear-sky irradiance.

    Each x maps to a wall-clock time on the morning axis (or its night
    extension); solar elevation comes from astral, irradiance from the
    clear-sky model. Brightness = floor + (100 - floor) * I / I_peak, so the
    curve hits `floor` at night/sunrise and 100% at solar noon. The result
    depends on today's sun path (steeper profile in summer, flatter in
    winter) and is decimated for easy editing.
    """
    def h_of(dt):
        return dt.hour + dt.minute / 60.0

    obs = Observer(lat, lon)
    tz = tz or datetime.now().astimezone().tzinfo
    today = datetime.now(tz).date()
    yday = today.timetuple().tm_yday

    def time_at(x):
        if x >= 0.0:
            return sun['sunrise'] + timedelta(hours=x * (sun['noon'] - sun['sunrise']).total_seconds() / 3600.0)
        return sun['sunrise'] + timedelta(hours=x * (sun['sunrise'] - sun['mid_before']).total_seconds() / 3600.0)

    def irrad_at(x):
        t = time_at(x).replace(tzinfo=tz)
        return clear_sky_irradiance(elevation(obs, t), yday)

    irrad = [irrad_at(i * 0.02) for i in range(-50, 51)]
    peak = max(irrad)
    if peak <= 0.0:
        return default_curve(floor)

    out = []
    last = None
    for i, irr in enumerate(irrad):
        xx = round((i - 50) * 0.02, 2)
        b = round(floor + (100.0 - floor) * irr / peak)
        if last is None or abs(b - last) >= 4.0:
            out.append([xx, b])
            last = b
    if not out or out[0][0] != -1.0:
        out.insert(0, [-1.0, round(floor)])
    if out[-1][0] != 1.0:
        out.append([1.0, round(floor + (100.0 - floor) * irrad[-1] / peak)])
    return out

class CurveEditor(tk.Toplevel):
    """Edit the day curve on a normalized, sun-anchored axis.

    x = -1 at solar midnight, 0 at sunrise, 1 at solar noon. Because morning
    and evening half-days differ in length (location/season), each side is
    normalized to its own duration and the evening is the mirror of the
    morning. Drag points, click empty space to add, right-click to delete.
    """

    CW, CH = 620, 300
    PAD_L, PAD_R, PAD_T, PAD_B = 50, 14, 12, 40
    MIN_POINTS = 1
    MAX_POINTS = 64
    X_MIN, X_MAX = -1.0, 1.0

    def __init__(self, parent, points, night_pct, on_done, get_sun, get_gen=None, floor=0.0):
        super().__init__(parent)
        self.title("Auto Brightness Curve \u2014 solar midnight \u2192 sunrise \u2192 solar noon")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.night_pct = night_pct
        self.floor = float(floor)
        self.points = [[float(x), max(self.floor, float(b))] for x, b in points
                       if self.X_MIN <= float(x) <= self.X_MAX] or default_curve(night_pct)
        self.on_done = on_done
        self.get_sun = get_sun
        self.get_gen = get_gen
        self.selected = None
        self._clock_job = None

        self.canvas = tk.Canvas(self, width=self.CW, height=self.CH, bg="white",
                                highlightthickness=1, highlightbackground="#999")
        self.canvas.pack(padx=10, pady=(10, 2))

        self.status = tk.Label(self, text="", fg="#333")
        self.status.pack(anchor="w", padx=12)

        tk.Label(self, text="Drag points to move \u00b7 click empty space to add \u00b7 right-click a point to delete",
                 fg="gray").pack(anchor="w", padx=12)
        tk.Label(self, text="The evening mirrors this curve from noon back to midnight.",
                 fg="gray").pack(anchor="w", padx=12)

        floor_row = tk.Frame(self)
        floor_row.pack(fill="x", padx=12, pady=(6, 0))
        tk.Label(floor_row, text="Min brightness floor (%)").pack(side="left")
        self.floor_slider = tk.Scale(floor_row, from_=0, to=50, orient="horizontal",
                                     command=lambda e: self.on_floor_change(), length=220)
        self.floor_slider.set(int(self.floor))
        self.floor_slider.pack(side="left", padx=8)

        btns = tk.Frame(self)
        btns.pack(pady=8)
        self.gen_btn = tk.Button(btns, text="Generate from sun elevation", command=self.generate)
        self.gen_btn.pack(side="left", padx=6)
        tk.Button(btns, text="Reset to Default", command=self.reset_points).pack(side="left", padx=6)
        tk.Button(btns, text="Cancel", command=self.destroy).pack(side="left", padx=6)
        tk.Button(btns, text="Done", command=self.done).pack(side="left", padx=6)
        if self.get_gen is None:
            self.gen_btn.config(state="disabled")

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

    # --- geometry (x in [-1, 1]) ---
    def x_at(self, x):
        return self.PAD_L + (x - self.X_MIN) / (self.X_MAX - self.X_MIN) * (self.CW - self.PAD_L - self.PAD_R)

    def x_from(self, px):
        return max(self.X_MIN, min(self.X_MAX,
                                   self.X_MIN + (px - self.PAD_L) / (self.CW - self.PAD_L - self.PAD_R)
                                   * (self.X_MAX - self.X_MIN)))

    def y_at(self, bright):
        return self.PAD_T + (1.0 - bright / 100.0) * (self.CH - self.PAD_T - self.PAD_B)

    def bright_at(self, y):
        return max(0.0, min(100.0, (1.0 - (y - self.PAD_T) / (self.CH - self.PAD_T - self.PAD_B)) * 100.0))

    def draw(self):
        c = self.canvas
        c.delete("all")

        # vertical grid: -1, -0.5, 0, 0.5, 1
        for tick in (-1.0, -0.5, 0.0, 0.5, 1.0):
            x = self.x_at(tick)
            c.create_line(x, self.PAD_T, x, self.CH - self.PAD_B, fill="#e8e8e8")
            if tick in (-0.5, 0.5):
                c.create_text(x, self.CH - self.PAD_B + 11, text=str(tick), fill="#888", font=("Segoe UI", 8))
        c.create_text(self.x_at(-1.0), self.CH - self.PAD_B + 24, text="solar midnight",
                      fill="#666", font=("Segoe UI", 8))
        c.create_text(self.x_at(0.0), self.CH - self.PAD_B + 24, text="sunrise",
                      fill="#666", font=("Segoe UI", 8))
        c.create_text(self.x_at(1.0), self.CH - self.PAD_B + 24, text="solar noon",
                      fill="#666", font=("Segoe UI", 8))
        for b in range(0, 101, 25):
            y = self.y_at(b)
            c.create_line(self.PAD_L, y, self.CW - self.PAD_R, y, fill="#e8e8e8")
            c.create_text(self.PAD_L - 6, y, text=str(b), anchor="e", fill="#888", font=("Segoe UI", 8))

        # floor band: nothing below the min-brightness floor
        if self.floor > 0.0:
            yf = self.y_at(self.floor)
            c.create_rectangle(self.PAD_L, yf, self.CW - self.PAD_R, self.CH - self.PAD_B,
                               fill="#ffe3e3", stipple="gray50", outline="")
            c.create_line(self.PAD_L, yf, self.CW - self.PAD_R, yf, fill="#d88", dash=(2, 2))
            c.create_text(self.PAD_L + 4, max(self.PAD_T + 2, yf - 8),
                          text=f"floor {round(self.floor)}%", anchor="w", fill="#c77", font=("Segoe UI", 8))

        pts = sorted(self.points, key=lambda p: p[0])
        if pts:
            poly = [(self.x_at(x), self.y_at(b)) for x, b in pts]
            c.create_line(poly, fill="#1a6fd0", width=2)

        for x, b in pts:
            c.create_oval(self.x_at(x) - 6, self.y_at(b) - 6,
                          self.x_at(x) + 6, self.y_at(b) + 6,
                          fill="#ff5555", outline="#222", width=1)

        # current-time marker
        now = datetime.now()
        try:
            sun = self.get_sun()
            x = curve_x(now, sun)
            sector = curve_sector(now, sun)
        except Exception:
            x, sector = 0.0, "position unavailable"
        bx = eval_curve(pts, x) if pts else 100.0
        px = self.x_at(x)
        c.create_line(px, self.PAD_T, px, self.CH - self.PAD_B, fill="#d00", dash=(3, 3))
        c.create_oval(px - 4, self.y_at(bx) - 4, px + 4, self.y_at(bx) + 4, fill="#d00", outline="")
        tx = min(max(px, self.PAD_L + 14), self.CW - self.PAD_R - 14)
        c.create_text(tx, self.PAD_T + 2, text="now", anchor="n", fill="#d00", font=("Segoe UI", 8))
        self.status.config(text=f"Now: {fmt_time(now)} \u00b7 {sector} \u2192 brightness {round(bx)}%")

    # --- mouse handling ---
    def _hit(self, event):
        """Index of the point within 12px of the click, or None."""
        best, best_d = None, 12.0
        for i, (x, b) in enumerate(self.points):
            dx = self.x_at(x) - event.x
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
        x = round(self.x_from(event.x) * 100) / 100.0
        b = max(self.floor, round(self.bright_at(event.y)))
        for j, (ex, _) in enumerate(self.points):
            if abs(ex - x) < 0.005:
                self.selected = j  # already a point here; select it instead
                return
        self.points.append([x, b])
        self.selected = len(self.points) - 1
        self.draw()

    def on_drag(self, event):
        if self.selected is None:
            return
        i = self.selected
        x = round(self.x_from(event.x) * 100) / 100.0
        b = max(self.floor, min(100.0, round(self.bright_at(event.y))))
        pts = sorted(self.points, key=lambda p: p[0])
        j = pts.index(self.points[i])
        lo = (pts[j - 1][0] + 0.005) if j > 0 else self.X_MIN
        hi = (pts[j + 1][0] - 0.005) if j < len(pts) - 1 else self.X_MAX
        self.points[i] = [max(lo, min(hi, x)), b]
        self.draw()

    def on_release(self, event):
        self.selected = None

    def on_right_click(self, event):
        i = self._hit(event)
        if i is not None and len(self.points) > self.MIN_POINTS:
            del self.points[i]
            self.draw()

    def on_floor_change(self):
        self.floor = float(self.floor_slider.get())
        self.points = [[x, max(self.floor, b)] for x, b in self.points]
        self.draw()

    def generate(self):
        """Replace the curve with today's clear-sky irradiance profile."""
        ctx = self.get_gen() if self.get_gen else None
        if not ctx:
            messagebox.showwarning(
                "Location required",
                "Set a zip code in the main window first (you can open this editor again after).",
                parent=self)
            return
        lat, lon, tz = ctx
        try:
            self.points = generate_elevation_curve(self.get_sun(), lat, lon, tz, self.floor)
        except Exception as exc:
            messagebox.showerror("Generation failed", str(exc), parent=self)
            return
        self.draw()

    def reset_points(self):
        self.points = [[x, max(self.floor, b)] for x, b in default_curve(self.night_pct)]
        self.draw()

    def done(self):
        self.on_done(sorted(self.points, key=lambda p: p[0]), self.floor)
        self.destroy()

# --- 4. GUI APPLICATION ---
class SunsetApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Custom Sunset Screen")
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
        self.refresh_location_summary()
        if self.lat is not None and self.lon is not None and self.loc_name:
            self.calculate_sunset()

        # Apply saved auto-brightness state and start the periodic timer
        self.apply_auto_state()
        self.after(60_000, self.auto_tick)

        self.start_watchdog()

    def create_widgets(self):
        # --- Live status header ---
        self.header_label = tk.Label(self, text="", font=("Segoe UI", 10, "bold"))
        self.header_label.pack(padx=12, pady=(10, 0), anchor="w")

        # --- Color mode state (the selector itself lives in Advanced) ---
        self.mode_var = tk.StringVar(value="temp")
        self._advanced_dlg = None

        # --- Temperature/Brightness Mode Frame ---
        self.temp_frame = tk.LabelFrame(self, text="Temperature & Brightness", padx=10, pady=10)

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
        # Packed/unpacked via toggle_modes

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

        # --- Auto Brightness ---
        self.auto_frame = tk.LabelFrame(self, text="Auto Brightness", padx=10, pady=8)
        self.auto_frame.pack(padx=10, pady=5, fill="x")

        self.auto_var = tk.BooleanVar(value=bool(self.settings.get("auto_enabled", False)))
        tk.Checkbutton(self.auto_frame, text="Auto-adjust by time of day",
                       variable=self.auto_var, command=self.toggle_auto).pack(anchor="w")

        # Sub-controls: only visible while auto is enabled
        self.auto_details = tk.Frame(self.auto_frame)

        self.auto_mode_var = tk.StringVar(value=self.settings.get("auto_mode", "sun"))
        mode_row = tk.Frame(self.auto_details)
        mode_row.pack(anchor="w", pady=(4, 0))
        tk.Radiobutton(mode_row, text="Follow sunrise/sunset", variable=self.auto_mode_var,
                       value="sun", command=self.on_auto_mode_change).pack(side="left")
        tk.Radiobutton(mode_row, text="Custom curve", variable=self.auto_mode_var,
                       value="curve", command=self.on_auto_mode_change).pack(side="left", padx=(10, 0))

        # Night brightness applies only to the sunrise/sunset schedule; the
        # custom curve owns its floor in the curve editor.
        self.night_row = tk.Frame(self.auto_details)
        tk.Label(self.night_row, text="Night Brightness (%)").pack(side="left")
        self.night_slider = tk.Scale(self.night_row, from_=5, to=100, orient="horizontal",
                                     command=lambda e: self.on_night_change(), length=170)
        self.night_slider.set(int(self.settings.get("night_brightness", 30)))
        self.night_slider.pack(side="left", padx=8)

        self.curve_btn = tk.Button(self.auto_details, text="Edit Curve\u2026", command=self.open_curve_editor)

        self.sun_times_label = tk.Label(self.auto_details, text="", fg="gray")
        self.sun_times_label.pack(anchor="w", pady=(4, 0))

        self.auto_status = tk.Label(self.auto_details, text="", fg="black")
        self.auto_status.pack(anchor="w")

        # --- Location (one-time setup, collapsed by default) ---
        loc_frame = tk.Frame(self, padx=12)
        loc_frame.pack(pady=(6, 0), fill="x")
        self.loc_summary = tk.Label(loc_frame, text="", anchor="w")
        self.loc_summary.pack(side="left")
        self.loc_edit_btn = tk.Button(loc_frame, text="Change\u2026", command=self.toggle_location_editor)
        self.loc_edit_btn.pack(side="right")

        self.loc_editor = tk.Frame(self, padx=12)
        row = tk.Frame(self.loc_editor)
        row.pack(fill="x", pady=(4, 0))
        tk.Label(row, text="Zip Code:").pack(side="left")
        self.zip_entry = tk.Entry(row, width=8)
        saved_zip = self.settings.get("zip_code", "")
        if saved_zip:
            self.zip_entry.insert(0, saved_zip)
        self.zip_entry.pack(side="left", padx=5)
        tk.Button(row, text="Set Location", command=self.fetch_location).pack(side="left")
        self.loc_feedback = tk.Label(self.loc_editor, text="", fg="red", anchor="w")
        self.loc_feedback.pack(fill="x")

        # --- Advanced ---
        self.advanced_btn = tk.Button(self, text="Advanced\u2026", command=self.open_advanced)
        self.advanced_btn.pack(pady=(10, 8))

        self.toggle_modes()         # show the slider frame for the current mode
        self.toggle_auto_details()  # show/hide auto sub-controls
        self.on_auto_mode_change()  # show night slider vs curve button

    def toggle_modes(self):
        if self.mode_var.get() == "temp":
            self.rgb_frame.pack_forget()
            self.temp_frame.pack(padx=10, pady=5, fill="x", before=self._auto_anchor())
        else:
            self.temp_frame.pack_forget()
            self.rgb_frame.pack(padx=10, pady=5, fill="x", before=self._auto_anchor())
        self.update_screen()

    def _auto_anchor(self):
        """The auto frame sits below the slider frames; pack before it."""
        return self.auto_frame

    def toggle_auto_details(self):
        if self.auto_var.get():
            self.auto_details.pack(fill="x", pady=(2, 0))
        else:
            self.auto_details.pack_forget()

    def refresh_header(self):
        """One-line live status: color mode, brightness, auto state."""
        if self.mode_var.get() == "temp":
            color = f"{self.temp_slider.get()}K"
            manual_bright = self.bright_slider.get()
        else:
            color = f"R{self.r_slider.get()}% G{self.g_slider.get()}% B{self.b_slider.get()}%"
            manual_bright = 100  # RGB mode applies no extra dimming manually
        if self.auto_var.get():
            bright = int(round(self.auto_brightness * 100))
            state = "Auto on"
        else:
            bright = manual_bright
            state = "Auto off"
        self.header_label.config(text=f"{color} \u00b7 {bright}% \u00b7 {state}")

    def refresh_location_summary(self):
        if self.loc_name:
            zip_code = self.settings.get("zip_code", "")
            suffix = f" ({zip_code})" if zip_code else ""
            self.loc_summary.config(text=f"Location: {self.loc_name}{suffix}", fg="black")
            self.loc_edit_btn.config(text="Change\u2026")
        else:
            self.loc_summary.config(text="Location: not set", fg="gray")
            self.loc_edit_btn.config(text="Set\u2026")

    def toggle_location_editor(self):
        if self.loc_editor.winfo_ismapped():
            self.loc_editor.pack_forget()
        else:
            self.loc_editor.pack(fill="x", before=self.advanced_btn)

    def open_advanced(self):
        if self._advanced_dlg is not None and self._advanced_dlg.winfo_exists():
            self._advanced_dlg.lift()
            return
        dlg = tk.Toplevel(self)
        self._advanced_dlg = dlg
        dlg.title("Advanced")
        dlg.resizable(False, False)
        dlg.transient(self)

        mode_frame = tk.LabelFrame(dlg, text="Color Mode", padx=10, pady=8)
        mode_frame.pack(padx=10, pady=(10, 5), fill="x")
        tk.Radiobutton(mode_frame, text="Temperature", variable=self.mode_var,
                       value="temp", command=self.toggle_modes).pack(anchor="w")
        tk.Radiobutton(mode_frame, text="RGB channels", variable=self.mode_var,
                       value="rgb", command=self.toggle_modes).pack(anchor="w")

        tk.Button(dlg, text="Reset Screen to Normal", bg="lightgray",
                  command=self.reset_screen).pack(padx=10, pady=(5, 10), fill="x")

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
            self.loc_feedback.config(text="Invalid Zip Code")
            return

        result = get_location_from_zip(zip_code)
        if result:
            self.lat, self.lon, name = result
            self.loc_name = name
            try:
                tz_str = TimezoneFinder().timezone_at(lat=self.lat, lng=self.lon)
                self.tz = ZoneInfo(tz_str) if tz_str else None
            except Exception:
                self.tz = None
            self.loc_feedback.config(text="")
            self.save_settings()  # also stores the zip code
            self.refresh_location_summary()
            self.loc_editor.pack_forget()
            self.calculate_sunset()
            if self.auto_var.get():
                self.run_auto_now()  # location may change the auto schedule
        else:
            self.loc_feedback.config(text="Zip Code not found")

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
            self.sun_times_label.config(
                text=f"Sunrise {sunrise_time} \u00b7 Sunset {sunset_time}", fg="gray")
        except Exception:
            self.sun_times_label.config(
                text="Sun times unavailable for this date/location", fg="red")

    # --- Auto Brightness ---
    def toggle_auto(self):
        self.toggle_auto_details()
        self.save_settings()
        self.apply_auto_state()

    def get_sun_times(self):
        """Today's sun anchors as naive-local datetimes, or a fixed-schedule
        fallback (sunrise 7:00, sunset 19:00, noon 13:00) when no location."""
        if self.lat is not None and self.lon is not None:
            tz = self.tz or datetime.now().astimezone().tzinfo
            try:
                s = sun(Observer(self.lat, self.lon), date=datetime.now(tz).date(), tzinfo=tz)
                noon = s['noon'].replace(tzinfo=None)
                return {
                    'mid_before': noon - timedelta(hours=12),
                    'sunrise': s['sunrise'].replace(tzinfo=None),
                    'noon': noon,
                    'sunset': s['sunset'].replace(tzinfo=None),
                    'mid_after': noon + timedelta(hours=12),
                }
            except Exception:
                pass
        now = datetime.now()
        sunrise = now.replace(hour=7, minute=0, second=0, microsecond=0)
        sunset = now.replace(hour=19, minute=0, second=0, microsecond=0)
        noon = now.replace(hour=13, minute=0, second=0, microsecond=0)
        return {
            'mid_before': noon - timedelta(hours=12),
            'sunrise': sunrise,
            'noon': noon,
            'sunset': sunset,
            'mid_after': noon + timedelta(hours=12),
        }

    def on_auto_mode_change(self):
        """Switch between sun-synced and custom-curve schedules."""
        if self.auto_mode_var.get() == "curve":
            curve = self.settings.get("auto_curve")
            if not curve:
                self.settings["auto_curve"] = default_curve(self.night_slider.get())
            elif legacy_is_24h(curve):
                # one-time migration: fold a saved 24h wall-clock curve into
                # the normalized sun-anchored curve (morning/evening averaged)
                self.settings["auto_curve"] = convert_to_curve(curve, self.get_sun_times())
            # The curve owns its floor in the editor; hide the sun-mode slider
            self.night_row.pack_forget()
            self.curve_btn.pack(anchor="w", pady=(4, 0), before=self.sun_times_label)
        else:
            self.curve_btn.pack_forget()
            self.night_row.pack(fill="x", pady=(4, 0), before=self.sun_times_label)
        self.save_settings()
        if self.auto_var.get():
            self.run_auto_now()

    def _gen_context(self):
        """(lat, lon, tz) for the curve generator, or None without a location."""
        if self.lat is None or self.lon is None:
            return None
        return (self.lat, self.lon, self.tz)

    def open_curve_editor(self):
        pts = self.settings.get("auto_curve") or default_curve(self.night_slider.get())
        floor = float(self.settings.get("auto_curve_floor", 0))
        CurveEditor(self, pts, self.night_slider.get(), self.on_curve_edited,
                    self.get_sun_times, self._gen_context, floor)

    def on_curve_edited(self, points, floor):
        self.settings["auto_curve"] = points
        self.settings["auto_curve_floor"] = floor
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
            self.auto_status.config(text="")
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
            floor = float(self.settings.get("auto_curve_floor", 0))
            now = datetime.now(self.tz or datetime.now().astimezone().tzinfo).replace(tzinfo=None)
            sun = self.get_sun_times()
            try:
                b = eval_curve(pts, curve_x(now, sun)) / 100.0
                sector = curve_sector(now, sun)
            except Exception:
                b, sector = 1.0, "position unavailable"
            b = max(0.05, floor / 100.0, min(1.0, b))
            return b, f"{int(round(b * 100))}% \u00b7 {sector}"

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
            b, phase = night, f"night \u00b7 sunrise {fmt_time(sunrise)}"
        elif now < sunrise + trans:
            b, phase = blend(now, sunrise, sunrise + trans, night, day), "sunrise fade-in"
        elif now < sunset - trans:
            b, phase = day, f"day \u00b7 sunset {fmt_time(sunset)}"
        elif now < sunset:
            b, phase = blend(now, sunset - trans, sunset, day, night), "sunset fade-out"
        else:
            b, phase = night, "night \u00b7 sunrise tomorrow"

        status = f"{int(round(b * 100))}% \u00b7 {phase}{source}"
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

        self.refresh_header()

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
