"""High-tech 720 x 720 screens for controllers that can only show pictures.

Pages: live (power flow), today (24 h chart), battery, money, solar (health),
week. Each page is drawn at 2x and scaled down for smooth edges, with a blurred
"glow" layer screened on top for the neon look. Rendering is pure Pillow and runs
in the executor; `data` is a plain dict built in the event loop (screen_data).

Series rows are [minute_of_day, solar_w, house_w, grid_w, battery_w, battery_pct].
"""

from __future__ import annotations

import datetime
import io
import math
import os
import threading
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

SIZE = 720
SS = 2  # supersampling
C = SIZE * SS
_RAW = threading.local()  # set by render_image(): pages return the full-size image

PAGES = ["overview", "live", "today", "battery", "money", "solar", "week"]
TITLES = {
    "overview": "ENERGY OVERVIEW",
    "live": "LIVE ENERGY",
    "today": "TODAY",
    "battery": "BATTERY",
    "money": "ENERGY ECONOMICS",
    "solar": "SOLAR HEALTH",
    "week": "THIS WEEK",
}

BG_TOP = (6, 9, 18)
BG_BOTTOM = (10, 17, 32)
TEXT = (236, 241, 247)
MUTED = (128, 142, 162)
DIM = (70, 82, 100)
CYAN = (0, 229, 255)
SOLAR = (255, 196, 0)
SOLAR2 = (255, 120, 0)
HOME = (178, 140, 255)
GRID = (64, 156, 255)
BATT = (0, 232, 130)
BATT2 = (0, 200, 255)
RED = (255, 82, 102)
GREEN = BATT
MAGENTA = (255, 64, 200)
# set per theme (see themes.py); these are the "neon-dark" values
INK = (255, 255, 255)            # lines, tracks and grids drawn over the background
NODE = (14, 20, 34)              # fill of the flow-diagram circles
CARD_FILL = (255, 255, 255, 12)
CARD_LINE = (255, 255, 255, 30)
CARD_STYLE = "glass"
RADIUS = 1.0
TEXTURE = "dots"
WASHES = ((0, 20, 38), (22, 8, 40))
GLOW = 1.0
FONT_SCALE = 1.0

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
FALLBACK = {
    "bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "semi": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "medium": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
}
WEIGHT_FILE = {"bold": "Rajdhani-Bold.ttf", "semi": "Rajdhani-SemiBold.ttf", "medium": "Rajdhani-Medium.ttf"}

_THEME_LOCK = threading.RLock()  # renders swap the module colours, one theme at a time
_THEME = None


def apply_theme(key: str | None) -> None:
    """Swap in a theme-mode's colours, fonts and styles (see themes.py)."""
    global _THEME
    try:
        from .themes import resolve
    except ImportError:  # loaded on its own (tests)
        from themes import resolve
    globals().update(resolve(key))
    _THEME = key


def themed(fn):
    """Run a render with the theme named in data["_theme"] (default: neon-dark)."""
    def wrapper(page, data, *args, **kwargs):
        with _THEME_LOCK:
            previous = _THEME
            apply_theme(data.get("_theme"))
            try:
                return fn(page, data, *args, **kwargs)
            finally:
                apply_theme(previous)
    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper


def font(size: int, weight: str = "semi"):
    return _font(WEIGHT_FILE[weight], max(round(size * FONT_SCALE * SS), 2), weight)


@lru_cache(maxsize=256)
def _font(file: str, px: int, weight: str):
    path = os.path.join(FONT_DIR, file)
    if os.path.exists(path):
        return ImageFont.truetype(path, px)
    for alt in FALLBACK[weight]:
        if os.path.exists(alt):
            return ImageFont.truetype(alt, int(px * 0.85))
    return ImageFont.load_default(size=px)


def _resample(pts, step):
    """Points every `step` units along a polyline."""
    seg = [math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    total, out, i, acc, d = sum(seg), [], 0, 0.0, 0.0
    while d <= total and seg:
        while i < len(seg) - 1 and acc + seg[i] < d:
            acc += seg[i]
            i += 1
        t = (d - acc) / seg[i] if seg[i] else 0
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
        d += step
    return out


def draw_background(img, d, g, header, origin=(0, 0)):
    """The theme's background on a 2x-size image: gradient, colour washes and texture.
    g draws on the half-size glow layer; header (x0, y0, x1, y1) is kept free of texture;
    origin aligns the texture with a page placed at that point (wider/taller shapes)."""
    w, h = img.size
    for y in range(0, h, 4):
        d.rectangle([0, y, w, y + 4], fill=mix(BG_TOP, BG_BOTTOM, y / h))
    wash1, wash2 = WASHES
    gw, gh = w // 2, h // 2
    blobs = [((-80, -100, 240, 180), wash1), ((gw - 180, gh - 180, gw + 120, gh + 120), wash2)]
    if TEXTURE == "aurora":
        blobs += [((gw * 0.15, gh * 0.30, gw * 0.75, gh * 0.75), wash2), ((gw * 0.45, -60, gw * 1.05, gh * 0.4), wash1)]
    if GLOW:
        for box, colour in blobs:
            g.ellipse(box, fill=colour)
    else:  # light themes: soft tinted clouds instead of glow
        for box, colour in blobs:
            mask = Image.new("L", (gw // 2, gh // 2), 0)
            ImageDraw.Draw(mask).ellipse([v / 2 for v in box], fill=150)
            mask = mask.filter(ImageFilter.GaussianBlur(40)).resize((w, h), Image.BILINEAR)
            img.paste(colour, (0, 0), mask)
    ox, oy = origin
    hx0, hy0, hx1, hy1 = header

    def free(x, y):
        return not (hx0 <= x < hx1 and hy0 <= y < hy1)

    if TEXTURE == "dots":
        step = 24 * SS
        for x in range((ox + step) % step or step, w, step):
            for y in range((oy + 84 * SS) % step, h - 20 * SS, step):
                if free(x, y):
                    d.point([(x, y)], fill=INK + (26,))
    elif TEXTURE == "scanlines":
        for y in range(0, h, 3 * SS):
            d.line([(0, y), (w, y)], fill=(0, 0, 0, 70 if GLOW else 14), width=SS)
    elif TEXTURE == "grid":
        for minor, alpha in ((12 * SS, 14), (60 * SS, 30)):
            for x in range(ox % minor, w, minor):
                d.line([(x, 0), (x, h)], fill=INK + (alpha,), width=1 if alpha < 20 else SS)
            for y in range(oy % minor, h, minor):
                d.line([(0, y), (w, y)], fill=INK + (alpha,), width=1 if alpha < 20 else SS)
    elif TEXTURE == "grain":
        import random

        rnd = random.Random(7)
        for _ in range(w * h // 260):
            x, y = rnd.randrange(w), rnd.randrange(h)
            d.point([(x, y)], fill=((255, 240, 210) if rnd.random() < 0.5 else (0, 0, 0)) + (rnd.randrange(8, 26),))


def mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def period_color(period: str | None):
    p = (period or "").lower()
    if "override" in p:
        return MAGENTA
    if "super" in p:
        return BATT
    if "off" in p or "ulo" in p or "night" in p:
        return CYAN
    if "mid" in p or "shoulder" in p:
        return SOLAR
    if "on" in p or "peak" in p or "critical" in p:
        return RED
    return MUTED


def fmt_kw(watts) -> str:
    return "—" if watts is None else f"{abs(watts) / 1000:.1f}"


def fmt_money(value, currency: str) -> str:
    if value is None:
        return "—"
    symbol = "$" if currency in ("USD", "CAD", "AUD", "NZD") else ""
    return f"{'-' if value < 0 else ''}{symbol}{abs(value):.2f}"


def fmt_price(value, currency: str) -> str:
    if value is None:
        return "—"
    if currency in ("USD", "CAD", "AUD", "NZD"):
        return f"{value * 100:.1f}¢"
    return f"{value:.3f}"


def fmt_duration(hours) -> str | None:
    if hours is None or hours <= 0 or hours > 99:
        return None
    h, m = int(hours), int(round((hours % 1) * 60))
    if m == 60:
        h, m = h + 1, 0
    return f"{h}h {m:02d}m" if h else f"{m}m"


class Canvas:
    """2x canvas with a crisp layer and a half-resolution glow layer."""

    def __init__(self, page: str, data: dict, w: int = SIZE, h: int = SIZE):
        self.data = data
        self.W, self.H = w, h  # size in 720-space units (the square is 720 x 720)
        self.img = Image.new("RGB", (w * SS, h * SS), BG_TOP)
        self.d = ImageDraw.Draw(self.img, "RGBA")
        self.glow = Image.new("RGB", (w * SS // 2, h * SS // 2), (0, 0, 0))
        self.g = ImageDraw.Draw(self.glow, "RGBA")
        backdrop = getattr(_RAW, "bg", None)
        if backdrop is not None:  # drawn on a slice of a wider/taller background (fit())
            self.img.paste(backdrop)
        else:
            self._background()
        self._header(page)
        self._footer(page)

    # --- primitives (coordinates in 720 space) ---

    def s(self, v):
        return v * SS

    def text(self, xy, txt, size, color=TEXT, weight="semi", anchor="la", spacing=0):
        f = font(size, weight)
        x, y = xy
        if not spacing:
            self.d.text((x * SS, y * SS), str(txt), font=f, fill=color, anchor=anchor)
            return
        total = sum(self.d.textlength(ch, font=f) for ch in txt) + spacing * SS * (len(txt) - 1)
        start = x * SS - (total if anchor[0] == "r" else total / 2 if anchor[0] == "m" else 0)
        for ch in txt:
            self.d.text((start, y * SS), ch, font=f, fill=color, anchor="l" + anchor[1])
            start += self.d.textlength(ch, font=f) + spacing * SS

    def glow_text(self, xy, txt, size, color, weight="bold", anchor="mm"):
        f = font(size, weight)
        self.g.text((xy[0] * SS / 2, xy[1] * SS / 2), str(txt), font=font(max(size // 2, 6), weight),
                    fill=color, anchor=anchor)
        self.d.text((xy[0] * SS, xy[1] * SS), str(txt), font=f, fill=color, anchor=anchor)

    def line(self, pts, color, width=2, glow=0, alpha=255):
        self.d.line([(x * SS, y * SS) for x, y in pts], fill=color + (alpha,), width=int(width * SS))
        if glow:
            self.g.line([(x * SS / 2, y * SS / 2) for x, y in pts], fill=color, width=max(int(glow), 1))

    def circle(self, cx, cy, r, fill=None, outline=None, width=1, glow=0):
        box = [(cx - r) * SS, (cy - r) * SS, (cx + r) * SS, (cy + r) * SS]
        self.d.ellipse(box, fill=fill, outline=outline, width=int(width * SS))
        if glow:
            gb = [(cx - r) * SS / 2, (cy - r) * SS / 2, (cx + r) * SS / 2, (cy + r) * SS / 2]
            self.g.ellipse(gb, outline=outline or fill, width=int(glow))

    def card(self, x, y, w, h, radius=16, accent=None):
        box = [x * SS, y * SS, (x + w) * SS, (y + h) * SS]
        r = radius * RADIUS * SS
        if CARD_STYLE == "flat":
            self.d.rounded_rectangle(box, radius=r, fill=CARD_FILL)
        elif CARD_STYLE == "outline":
            self.d.rounded_rectangle(box, radius=r, fill=CARD_FILL, outline=CARD_LINE, width=int(1.4 * SS))
        elif CARD_STYLE == "plate":  # double rule and corner rivets
            self.d.rounded_rectangle(box, radius=r, fill=CARD_FILL, outline=CARD_LINE, width=int(1.6 * SS))
            inner = CARD_LINE[:3] + (CARD_LINE[3] // 2,)
            self.d.rounded_rectangle([b + (5 if i < 2 else -5) * SS for i, b in enumerate(box)], radius=max(r - 4 * SS, 0),
                                     outline=inner, width=SS)
            for cx, cy in ((x + 9, y + 9), (x + w - 9, y + 9), (x + 9, y + h - 9), (x + w - 9, y + h - 9)):
                self.circle(cx, cy, 2.6, fill=CYAN + (220,))
                self.circle(cx - 0.7, cy - 0.7, 0.9, fill=mix(CYAN, (255, 255, 255), 0.6) + (230,))
        elif CARD_STYLE == "console":  # side bar + top bar, rounded like a starship console
            self.d.rounded_rectangle([x * SS, y * SS, (x + 10) * SS, (y + h) * SS], radius=5 * SS, fill=CYAN + (255,))
            self.d.rounded_rectangle([x * SS, y * SS, (x + w * 0.4) * SS, (y + 7) * SS], radius=3.5 * SS, fill=CYAN + (255,))
            self.d.rounded_rectangle([(x + w * 0.4 + 6) * SS, y * SS, (x + w - 30) * SS, (y + 7) * SS], radius=3.5 * SS,
                                     fill=HOME + (255,))
            self.d.rounded_rectangle([(x + w - 24) * SS, y * SS, (x + w) * SS, (y + 7) * SS], radius=3.5 * SS,
                                     fill=MUTED + (255,))
        else:  # glass
            self.d.rounded_rectangle(box, radius=r, fill=CARD_FILL, outline=CARD_LINE, width=SS)
        if accent:
            self.d.rounded_rectangle([x * SS, (y + 10) * SS, (x + 3) * SS, (y + h - 10) * SS],
                                     radius=2 * SS, fill=accent + (255,))
            self.g.line([(x * SS / 2, (y + 10) * SS / 2), (x * SS / 2, (y + h - 10) * SS / 2)], fill=accent, width=3)

    def arc(self, cx, cy, r, start, end, width, c1, c2=None, glow=6, track=True):
        """Gradient arc from angle start to end (degrees, 0 = east, clockwise)."""
        box = [(cx - r) * SS, (cy - r) * SS, (cx + r) * SS, (cy + r) * SS]
        if track:
            self.d.arc(box, 0, 360, fill=INK + (18,), width=int(width * SS))
        if end <= start:
            return
        steps = max(int((end - start) / 2), 1)
        gbox = [v / 2 for v in box]
        for i in range(steps):
            a0 = start + (end - start) * i / steps
            a1 = start + (end - start) * (i + 1) / steps + 0.6
            col = mix(c1, c2 or c1, i / max(steps - 1, 1))
            self.d.arc(box, a0, a1, fill=col + (255,), width=int(width * SS))
            if glow:
                self.g.arc(gbox, a0, a1, fill=col, width=int(width * SS / 2 + glow))

    def bezier(self, p0, p1, p2, n=40):
        return [((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
                 (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1])
                for t in (i / n for i in range(n + 1))]

    def flow(self, pts, color, watts, reverse=False):
        """A connector. Active flows carry a repeating dark -> mid -> bright -> light step
        pattern in the direction of flow: a still picture that reads as moving (the
        peripheral drift illusion). data["_phase"] shifts the pattern on each refresh."""
        active = watts is not None and watts > 50
        if len(pts) == 2:
            (x0, y0), (x1, y1) = pts
            pts = [(x0 + (x1 - x0) * i / 40, y0 + (y1 - y0) * i / 40) for i in range(41)]
        self.line(pts, INK, 2, alpha=22)
        if not active:
            return
        path = list(reversed(pts)) if reverse else pts
        width = 2.6 + min(watts / 1500, 2.4)
        self.line(path, color, width + 2, glow=4 + width, alpha=35)  # soft bed under the pattern
        width += 1.6
        seg = 7.0
        steps = (mix(BG_TOP, color, 0.08), mix(BG_TOP, color, 0.55),
                 mix(color, (255, 255, 255), 0.85), mix(color, (255, 255, 255), 0.30))
        dense = _resample(path, 1.0)
        offset = (self.data.get("_phase") or 0) % 1 * seg * len(steps)
        for k in range(len(dense) - 1):
            col = steps[int((k + offset) // seg) % len(steps)]
            self.d.line([(dense[k][0] * SS, dense[k][1] * SS), (dense[k + 1][0] * SS, dense[k + 1][1] * SS)],
                        fill=col + (255,), width=int(width * SS))

    # --- icons (centered at cx, cy, size s) ---

    def icon_sun(self, cx, cy, s, color=SOLAR):
        self.circle(cx, cy, s * 0.32, fill=color + (255,))
        for i in range(8):
            a = math.radians(i * 45)
            self.line([(cx + math.cos(a) * s * 0.48, cy + math.sin(a) * s * 0.48),
                       (cx + math.cos(a) * s * 0.66, cy + math.sin(a) * s * 0.66)], color, 2.4)

    def icon_home(self, cx, cy, s, color=HOME):
        w = s * 0.62
        self.line([(cx - w, cy - s * 0.05), (cx, cy - s * 0.6), (cx + w, cy - s * 0.05)], color, 3)
        self.line([(cx - w * 0.72, cy - s * 0.2), (cx - w * 0.72, cy + s * 0.45),
                   (cx + w * 0.72, cy + s * 0.45), (cx + w * 0.72, cy - s * 0.2)], color, 3)
        self.d.rectangle([(cx - s * 0.12) * SS, (cy + s * 0.12) * SS, (cx + s * 0.12) * SS, (cy + s * 0.45) * SS],
                         fill=color + (255,))

    def icon_grid(self, cx, cy, s, color=GRID):
        top, base = cy - s * 0.6, cy + s * 0.5
        self.line([(cx - s * 0.4, base), (cx, top), (cx + s * 0.4, base)], color, 2.6)
        for k, w in ((0.25, 0.42), (0.55, 0.3)):
            y = top + (base - top) * k
            self.line([(cx - s * w, y), (cx + s * w, y)], color, 2.6)
        self.line([(cx - s * 0.22, cy + s * 0.05), (cx + s * 0.22, cy + s * 0.05)], color, 2)

    def icon_battery(self, cx, cy, s, level=None, color=BATT):
        w, h = s * 0.5, s * 0.85
        self.d.rounded_rectangle([(cx - w / 2) * SS, (cy - h / 2) * SS, (cx + w / 2) * SS, (cy + h / 2) * SS],
                                 radius=4 * SS, outline=color + (255,), width=int(2.6 * SS))
        self.d.rectangle([(cx - w * 0.2) * SS, (cy - h / 2 - 5) * SS, (cx + w * 0.2) * SS, (cy - h / 2) * SS],
                         fill=color + (255,))
        if level is not None:
            fill_h = (h - 8) * max(min(level, 100), 0) / 100
            self.d.rectangle([(cx - w / 2 + 4) * SS, (cy + h / 2 - 4 - fill_h) * SS,
                              (cx + w / 2 - 4) * SS, (cy + h / 2 - 4) * SS], fill=color + (200,))

    # --- frame ---

    def _background(self):
        draw_background(self.img, self.d, self.g, (0, 0, self.W * SS, 84 * SS))

    def _header(self, page):
        self.text((24, 18), "INNOVO", 13, CYAN, "bold", spacing=4)
        self.text((24, 34), TITLES[page], 30, TEXT, "bold", spacing=2)
        now = self.data["now"]
        right = self.W - 24
        self.text((right, 20), now.strftime("%H:%M"), 30, TEXT, "bold", anchor="ra")
        self.text((right, 54), now.strftime("%a %d %b").upper(), 13, MUTED, "semi", anchor="ra", spacing=1)
        if self.data.get("_bare"):  # template screens: time stamp only, no status or data
            return
        status = (self.data.get("inverter") or "Waiting").upper()
        col = BATT if status == "PRODUCING" else RED if status.startswith(("FAULT", "OFFLINE")) else MUTED
        clock_w = self.d.textlength(now.strftime("%H:%M"), font=font(30, "bold")) / SS
        f = font(13, "semi")
        status_w = (sum(self.d.textlength(ch, font=f) for ch in status) + SS * (len(status) - 1)) / SS
        sx = right - clock_w - 18 - status_w  # status sits left of the clock, whatever the font
        self.circle(sx - 10, 40, 4, fill=col + (255,), glow=0)
        self.g.ellipse([(sx - 18) * SS / 2, 32 * SS / 2, (sx - 2) * SS / 2, 48 * SS / 2], fill=col)
        self.text((sx, 40), status, 13, col, "semi", anchor="lm", spacing=1)
        for x in range(24, self.W - 24, 2):
            t = (x - 24) / (self.W - 48)
            self.d.point([(x * SS, 76 * SS)], fill=mix(CYAN, HOME, t) + (int(160 * (1 - abs(t - 0.5) * 1.6)),))

    def _footer(self, page):
        if self.data.get("_bare"):
            return
        n = len(PAGES)
        x0 = self.W / 2 - (n - 1) * 9
        y = self.H - 14
        for i, p in enumerate(PAGES):
            x = x0 + i * 18
            if p == page:
                self.d.rounded_rectangle([(x - 9) * SS, (y - 3) * SS, (x + 9) * SS, (y + 3) * SS], radius=3 * SS, fill=CYAN + (255,))
                self.g.line([((x - 9) * SS / 2, y * SS / 2), ((x + 9) * SS / 2, y * SS / 2)], fill=CYAN, width=4)
            else:
                self.circle(x, y, 3, fill=INK + (60,))

    def glow_layer(self) -> Image.Image:
        glow = self.glow.filter(ImageFilter.GaussianBlur(9)).resize(self.img.size, Image.BILINEAR)
        if GLOW != 1:
            glow = glow.point(lambda v: min(int(v * GLOW), 255))
        return glow

    def full(self) -> Image.Image:
        """The finished screen at the 2x working size (1440 x 1440)."""
        if not GLOW:  # light themes: no neon glow
            return self.img.copy()
        return ImageChops.screen(self.img, self.glow_layer())

    def png(self):
        if getattr(_RAW, "layers", False):  # transparent(): the crisp layer and the blurred glow
            return self.img, self.glow_layer()
        if getattr(_RAW, "on", False):  # render_image(): hand back the full-size picture
            return self.full()
        out = self.full().resize((self.W, self.H), Image.LANCZOS)
        buf = io.BytesIO()
        out.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


# ----------------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------------

def _flows(v):
    """Split the four power readings into the individual flows between nodes."""
    s, h, g, b = (v.get(k) or 0 for k in ("solar_w", "house_w", "grid_w", "battery_w"))
    export, imp = max(-g, 0), max(g, 0)
    chg, dis = max(b, 0), max(-b, 0)
    s2g = min(export, s)
    s2b = min(chg, max(s - s2g, 0))
    s2h = max(s - s2g - s2b, 0)
    g2b = max(chg - s2b, 0)
    b2g = max(export - s2g, 0)
    b2h = max(dis - b2g, 0)
    g2h = max(imp - g2b, 0)
    return {"s2h": s2h, "s2g": s2g, "s2b": s2b, "g2h": g2h, "b2h": b2h, "g2b": g2b, "b2g": b2g,
            "s": s, "h": h, "g": g, "b": b, "imp": imp, "export": export}


def _flow_diagram(cv, v, nodes, r=60, compact=False):
    """Solar / grid / home / battery nodes with live flows between them."""
    f = _flows(v)
    S, G, H, B = nodes["solar"], nodes["grid"], nodes["home"], nodes["battery"]
    cv.flow(cv.bezier(S, (H[0], S[1]), H), SOLAR, f["s2h"])
    cv.flow(cv.bezier(S, (G[0], S[1]), G), SOLAR, f["s2g"])
    cv.flow([S, B], SOLAR, f["s2b"])
    cv.flow([G, H], GRID, f["g2h"])
    cv.flow(cv.bezier(B, (H[0], B[1]), H), BATT, f["b2h"])
    cv.flow(cv.bezier(G, (G[0], B[1]), B), GRID, f["g2b"])
    cv.flow(cv.bezier(B, (G[0], B[1]), G), BATT, f["b2g"])

    vsize, isize, lsize = (22, 18, 11) if compact else (30, 26, 13)

    def node(key, color, label, value, sub=None, sub_color=None, icon=None):
        x, y = nodes[key]
        cv.circle(x, y, r + (6 if compact else 10), fill=BG_TOP + (255,))
        cv.circle(x, y, r, fill=NODE + (255,), outline=color + (255,), width=2 if compact else 2.5, glow=5 if compact else 6)
        icon(x, y - r * 0.5, isize)
        cv.text((x, y + r * 0.1), value, vsize, TEXT, "bold", anchor="mm")
        if not compact:
            cv.text((x + cv.d.textlength(value, font=font(vsize, "bold")) / SS / 2 + 3, y + 10), "kW", 12, MUTED, "semi", anchor="lm")
        if sub:
            cv.text((x, y + r * 0.56), sub, 10 if compact else 13, sub_color or color, "semi", anchor="mm", spacing=1)
        if label:
            gap = (22 if key == "battery" else 16) if not compact else (18 if key == "battery" else 13)
            cv.text((x, y + r + gap), label, lsize, MUTED, "semi", anchor="mm", spacing=2)

    level = v.get("battery_level")
    share = v.get("solar_share")
    unit = " kW" if compact else ""
    node("solar", SOLAR, "SOLAR", fmt_kw(f["s"]) + unit,
         None if compact or share is None else f"{share:.0f}% OF HOME", icon=cv.icon_sun)
    node("grid", GRID, "GRID", fmt_kw(f["g"]) + unit,
         "BUYING" if f["imp"] > 50 else "SELLING" if f["export"] > 50 else "IDLE",
         RED if f["imp"] > 50 else BATT if f["export"] > 50 else MUTED, icon=cv.icon_grid)
    node("home", HOME, "HOME", fmt_kw(f["h"]) + unit, icon=cv.icon_home)
    bx, by = nodes["battery"]
    if level is not None:
        cv.arc(bx, by, r + (8 if compact else 12), -90, -90 + 360 * level / 100, 4 if compact else 5, BATT, BATT2, glow=4)
    state = (v.get("battery_state") or "").upper()
    node("battery", BATT, f"BATTERY · {state}".rstrip(" ·") if not compact else "BATTERY",
         fmt_kw(f["b"]) + unit, f"{level:.0f}%" if level is not None else None,
         icon=lambda x, y, sz: cv.icon_battery(x, y, sz, level))


def _sun_arc(cv, v, cx, cy, rx, ry, labels=True):
    sun = v.get("sun") or {}
    pts = [(cx + rx * math.cos(math.radians(a)), cy - ry * math.sin(math.radians(a))) for a in range(180, -1, -3)]
    cv.line(pts, INK, 1.2, alpha=40)
    frac = sun.get("progress")
    if frac is not None and 0 <= frac <= 1:
        done = [p for i, p in enumerate(pts) if i / (len(pts) - 1) <= frac]
        if len(done) > 1:
            cv.line(done, SOLAR, 2, glow=5, alpha=200)
        a = math.radians(180 - 180 * frac)
        sx, sy = cx + rx * math.cos(a), cy - ry * math.sin(a)
        cv.circle(sx, sy, 8, fill=SOLAR + (255,))
        cv.g.ellipse([(sx - 20) * SS / 2, (sy - 20) * SS / 2, (sx + 20) * SS / 2, (sy + 20) * SS / 2], fill=SOLAR)
    if labels and sun.get("rise"):
        cv.text((cx - rx, cy + 14), f"SUNRISE {sun['rise']}", 12, MUTED, "semi", anchor="mt", spacing=1)
    if labels and sun.get("set"):
        cv.text((cx + rx, cy + 14), f"SUNSET {sun['set']}", 12, MUTED, "semi", anchor="mt", spacing=1)


def page_live(data: dict) -> bytes:
    cv = Canvas("live", data)
    v = data
    cur = v["currency"]
    _sun_arc(cv, v, 360, 205, 290, 112)
    _flow_diagram(cv, v, {"solar": (360, 248), "grid": (118, 392), "home": (602, 392), "battery": (360, 520)})

    pcol = period_color(v.get("period"))
    kpis = [
        ("SELF-POWERED", f"{v['solar_share']:.0f}%" if v.get("solar_share") is not None else "—", BATT),
        ("PRICE NOW", fmt_price(v.get("price"), cur), pcol),
        ("SAVED TODAY", fmt_money(v.get("saved_today"), cur), BATT),
    ]
    for i, (label, value, col) in enumerate(kpis):
        x = 24 + i * 228
        cv.card(x, 618, 216, 72, accent=col)
        cv.text((x + 18, 630), label, 12, MUTED, "semi", spacing=2)
        cv.text((x + 18, 646), value, 30, col, "bold")
    if v.get("period"):
        cv.text((24 + 228 + 216 - 14, 634), v["period"].upper(), 11, pcol, "semi", anchor="ra", spacing=1)
    return cv.png()


def _axes_hours(cv, x, y, w, h, size=12):
    for hr in (0, 6, 12, 18, 24):
        gx = x + w * hr / 24
        cv.line([(gx, y), (gx, y + h)], INK, 1, alpha=16)
        cv.text((gx, y + h + 7), f"{hr:02d}", size, MUTED, "semi", anchor="mt")


def _power_chart(cv, v, x, y, w, h, compact=False, solar_only=False):
    """Today's solar area, home and grid lines, battery %, price bands and a NOW marker."""
    pts = v.get("series") or []
    peak = max([max(p[1] or 0, p[2] or 0, abs(p[3] or 0)) for p in pts] + [1000 if solar_only else 2000]) * 1.15

    def X(minute):
        return x + w * minute / 1440

    def Y(watts):
        return y + h - h * max(min(watts / peak, 1), 0)

    _axes_hours(cv, x, y, w, h, 11 if compact else 12)
    for kw in range(1, int(peak / 1000) + 1):
        gy = Y(kw * 1000)
        cv.line([(x, gy), (x + w, gy)], INK, 1, alpha=12)
        if not compact:
            cv.text((x - 6, gy), f"{kw}", 11, DIM, "semi", anchor="rm")

    for i, (price, period) in enumerate(v.get("price_slots") or []):
        cv.d.rectangle([X(i * 15) * SS, (y + h + 1) * SS, X((i + 1) * 15) * SS, (y + h + 4) * SS],
                       fill=period_color(period) + (170,))

    if len(pts) > 1:
        poly = [(X(p[0]), Y(p[1] or 0)) for p in pts]
        area = Image.new("L", (C, C), 0)
        ImageDraw.Draw(area).polygon([(px * SS, py * SS) for px, py in poly + [(poly[-1][0], y + h), (poly[0][0], y + h)]], fill=255)
        grad = Image.new("RGB", (C, C), SOLAR)
        fade = Image.linear_gradient("L").resize((C, max(int(h * SS), 1))).point(lambda val: 255 - int(val * 0.8))
        mask = Image.new("L", (C, C), 0)
        mask.paste(fade, (0, int(y * SS)))
        cv.img.paste(grad, (0, 0), ImageChops.multiply(area, mask).point(lambda val: int(val * 0.55)))
        cv.line(poly, SOLAR, 2 if compact else 2.2, glow=4 if compact else 5)
        if not solar_only:
            cv.line([(X(p[0]), Y(max(p[3] or 0, 0))) for p in pts], GRID, 1.6, glow=3, alpha=230)
            cv.line([(X(p[0]), Y(p[2] or 0)) for p in pts], HOME, 1.8 if compact else 2.2, glow=4)
        soe = [(X(p[0]), y + h - h * p[5] / 100) for p in pts if p[5] is not None]
        if len(soe) > 1:
            cv.line(soe, BATT, 1.5, alpha=200)
    else:
        cv.text((x + w / 2, y + h / 2), "COLLECTING TODAY'S DATA…", 14 if compact else 16, MUTED, "semi", anchor="mm", spacing=2)

    now = v["now"]
    nx = X(now.hour * 60 + now.minute)
    cv.line([(nx, y - 4), (nx, y + h)], CYAN, 1.5, glow=4)
    cv.text((nx, y - 7), "NOW", 10 if compact else 11, CYAN, "bold", anchor="mb", spacing=1)
    return pts


def _legend(cv, x, y, items, step):
    for i, (label, col) in enumerate(items):
        lx = x + i * step
        cv.d.rounded_rectangle([lx * SS, (y - 3) * SS, (lx + 14) * SS, (y + 3) * SS], radius=3 * SS, fill=col + (255,))
        cv.text((lx + 20, y), label, 11, MUTED, "semi", anchor="lm", spacing=1)


def page_today(data: dict) -> bytes:
    cv = Canvas("today", data)
    v, cur = data, data["currency"]
    cv.card(24, 88, 672, 368)
    _legend(cv, 44, 102, (("SOLAR", SOLAR), ("HOME", HOME), ("GRID", GRID), ("BATTERY %", BATT)), 120)
    pts = _power_chart(cv, v, 44, 118, 640, 290)

    t = v.get("today") or {}
    kpis = [("PRODUCED", t.get("solar"), SOLAR), ("USED", v.get("house_today"), HOME),
            ("BOUGHT", t.get("import"), GRID), ("SOLD", t.get("export"), BATT)]
    for i, (label, value, col) in enumerate(kpis):
        kx = 24 + i * 170
        cv.card(kx, 470, 160, 96, accent=col)
        cv.text((kx + 18, 484), label, 12, MUTED, "semi", spacing=2)
        cv.text((kx + 18, 502), "—" if value is None else f"{value:.1f}", 36, col, "bold")
        cv.text((kx + 142, 552), "kWh", 12, MUTED, "semi", anchor="rs")

    ss = v.get("self_sufficiency")
    cv.card(24, 580, 220, 110)
    cv.arc(84, 635, 36, -90, -90 + 360 * (ss or 0) / 100, 8, BATT, BATT2, glow=5)
    cv.text((84, 635), "—" if ss is None else f"{ss:.0f}%", 20, TEXT, "bold", anchor="mm")
    cv.text((136, 616), "SELF-", 13, MUTED, "semi", spacing=2)
    cv.text((136, 634), "SUFFICIENT", 13, MUTED, "semi", spacing=2)
    peak_pt = max(pts, key=lambda p: p[1] or 0) if pts else None
    cv.card(256, 580, 440, 110)
    if peak_pt and (peak_pt[1] or 0) > 0:
        cv.text((276, 596), "PEAK SOLAR", 12, MUTED, "semi", spacing=2)
        cv.text((276, 614), f"{peak_pt[1] / 1000:.2f} kW", 30, SOLAR, "bold")
        cv.text((276, 652), f"AT {peak_pt[0] // 60:02d}:{peak_pt[0] % 60:02d}", 13, MUTED, "semi", spacing=1)
    cv.text((676, 596), "GRID COST", 12, MUTED, "semi", anchor="ra", spacing=2)
    cv.text((676, 614), fmt_money(v.get("cost_today"), cur), 30, TEXT, "bold", anchor="ra")
    cv.text((676, 652), f"SAVED {fmt_money(v.get('saved_today'), cur)}", 13, BATT, "semi", anchor="ra", spacing=1)
    return cv.png()


# Overview layouts per screen shape, in 720-space units: canvas (w, h) and card boxes (x, y, w, h)
OVERVIEW_LAYOUTS = {
    "1x1": {"size": (720, 720), "flow": (24, 88, 420, 300), "battery": (456, 88, 240, 300),
            "chart": (24, 400, 672, 176), "tiles": [(24 + i * 170, 588, 160, 102) for i in range(4)]},
    "16x9": {"size": (1280, 720), "flow": (24, 88, 580, 300), "battery": (616, 88, 264, 300),
             "chart": (24, 400, 1232, 290),
             "tiles": [(892, 88, 176, 144), (1080, 88, 176, 144), (892, 244, 176, 144), (1080, 244, 176, 144)]},
    "4x3": {"size": (960, 720), "flow": (24, 88, 560, 300), "battery": (596, 88, 340, 300),
            "chart": (24, 400, 912, 176), "tiles": [(24 + i * 231, 588, 219, 102) for i in range(4)]},
    "9x10": {"size": (720, 800), "flow": (24, 88, 420, 340), "battery": (456, 88, 240, 340),
             "chart": (24, 440, 672, 216), "tiles": [(24 + i * 170, 668, 160, 102) for i in range(4)]},
    "9x16": {"size": (720, 1280), "flow": (24, 88, 672, 420), "battery": (24, 520, 672, 280),
             "chart": (24, 812, 672, 220),
             "tiles": [(24, 1044, 330, 98), (366, 1044, 330, 98), (24, 1154, 330, 98), (366, 1154, 330, 98)]},
}


def page_overview(data: dict, variant: str | None = None, shape: str = "1x1") -> bytes:
    """Everything at a glance: flow, battery, today's chart and the key numbers.

    shape picks the layout (see OVERVIEW_LAYOUTS). Variants for controllers that
    draw their own content on top: "slot" - the battery card is an empty frame;
    "flow" - only the live flow; every other card is an empty frame, no clock.
    """
    lay = OVERVIEW_LAYOUTS[shape]
    cv = Canvas("overview", {**data, "_bare": variant == "flow"}, *lay["size"])
    v, cur = data, data["currency"]

    # live flow
    x, y, w, h = lay["flow"]
    cv.card(x, y, w, h)
    cv.text((x + 16, y + 12), "LIVE FLOW", 11, MUTED, "semi", spacing=2)
    k = min(max(min(w / 420, h / 300), 1), 1.35)
    cx = x + w / 2
    _sun_arc(cv, v, cx, y + h * 0.267, w * 0.405, 52 * h / 300, labels=False)
    _flow_diagram(cv, v, {"solar": (cx, y + h * 0.273), "grid": (x + 68 * k, y + h * 0.6),
                          "home": (x + w - 68 * k, y + h * 0.6), "battery": (cx, y + h * 0.807)},
                  r=36 * k, compact=True)

    # battery
    cv.card(*lay["battery"])
    if variant == "flow":  # empty frames where the other cards go
        cv.card(*lay["chart"])
        for tile in lay["tiles"]:
            cv.card(*tile)
        return cv.png()
    if variant != "slot":
        _overview_battery(cv, v, *lay["battery"])
    t = v.get("today") or {}

    # today chart
    x, y, w, h = lay["chart"]
    cv.card(x, y, w, h)
    cv.text((x + 16, y + 12), "TODAY", 11, MUTED, "semi", spacing=2)
    _legend(cv, x + 116, y + 12, (("SOLAR", SOLAR), ("HOME", HOME), ("GRID", GRID), ("BATT %", BATT)), 92)
    cv.text((x + w - 16, y + 12), f"{t.get('solar') or 0:.1f} kWh SOLAR  ·  {v.get('house_today') or 0:.1f} kWh HOME",
            11, MUTED, "semi", anchor="rm", spacing=1)
    _power_chart(cv, v, x + 16, y + 36, w - 32, h - 72, compact=True)

    # key numbers
    pcol = period_color(v.get("period"))
    eff = v.get("efficiency")
    health = v.get("health") or "—"
    clean = v.get("cleaning")
    ss = v.get("self_sufficiency")
    tiles = [
        ("SAVED TODAY", fmt_money(v.get("saved_today"), cur), f"GRID {fmt_money(v.get('cost_today'), cur)}", BATT),
        ("PRICE NOW", fmt_price(v.get("price"), cur), (v.get("period") or "").upper(), pcol),
        ("SELF-SUFFICIENT", "—" if ss is None else f"{ss:.0f}%", f"BOUGHT {t.get('import') or 0:.1f} kWh", BATT),
        ("PANELS VS SPEC", "—" if eff is None else f"{eff:.0f}%",
         f"{health.upper()}{'  ·  CLEAN' if clean == 'Yes' else ''}", RED if clean == "Yes" else SOLAR),
    ]
    for (label, value, sub, col), (x, y, w, h) in zip(tiles, lay["tiles"]):
        big = h >= 130
        cv.card(x, y, w, h, accent=col)
        cv.text((x + 16, y + 12), label, 11, MUTED, "semi", spacing=1)
        cv.text((x + 16, y + (40 if big else 30)), value, 40 if big else 32, col, "bold")
        cv.text((x + 16, y + h - 16), sub, 10, MUTED, "semi", anchor="ls", spacing=1)
    return cv.png()


def _overview_battery(cv, v, x, y, w, h):
    level = v.get("battery_level")
    cv.text((x + 16, y + 12), "BATTERY", 11, MUTED, "semi", spacing=2)
    k = min(max(min(w / 240, h / 300), 0.8), 1.3)
    bx, by, br = x + w / 2, y + h * 0.447, 76 * k
    cv.arc(bx, by, br, -90, -90 + 360 * (level or 0) / 100, 14, BATT2, BATT, glow=8)
    reserve = v.get("reserve")
    if reserve is not None:
        a = math.radians(-90 + 360 * reserve / 100)
        cv.line([(bx + math.cos(a) * (br - 10), by + math.sin(a) * (br - 10)),
                 (bx + math.cos(a) * (br + 10), by + math.sin(a) * (br + 10))], RED, 2.5, glow=2)
    cv.glow_text((bx, by - 4), "—" if level is None else f"{level:.0f}%", 40, TEXT, "bold")
    state = (v.get("battery_state") or "—").upper()
    scol = BATT if state == "CHARGING" else SOLAR if state == "DISCHARGING" else CYAN
    cv.text((bx, by + 28), state, 12, scol, "bold", anchor="mm", spacing=2)
    bw = v.get("battery_w")
    ttl = fmt_duration(v.get("time_to_full")) if (bw or 0) > 150 else fmt_duration(v.get("time_to_empty"))
    sub = (f"FULL IN {ttl}" if (bw or 0) > 150 else f"RESERVE IN {ttl}") if ttl else \
        ("" if bw is None else f"{abs(bw) / 1000:.1f} kW")
    cv.text((bx, y + h * 0.82), sub, 13, MUTED, "semi", anchor="mm", spacing=1)
    t = v.get("today") or {}
    cv.text((bx, y + h * 0.913), f"IN {t.get('charged') or 0:.1f}  ·  OUT {t.get('discharged') or 0:.1f} kWh", 11, DIM,
            "semi", anchor="mm", spacing=1)


def page_battery(data: dict) -> bytes:
    cv = Canvas("battery", data)
    v = data
    level = v.get("battery_level")
    cx, cy, r = 360, 300, 175
    # tick ring
    for i in range(60):
        a = math.radians(i * 6 - 90)
        r1, r2 = r + 26, r + (34 if i % 5 == 0 else 30)
        cv.line([(cx + math.cos(a) * r1, cy + math.sin(a) * r1), (cx + math.cos(a) * r2, cy + math.sin(a) * r2)],
                INK, 1.2, alpha=50 if i % 5 == 0 else 25)
    cv.arc(cx, cy, r, -90, -90 + 360 * (level or 0) / 100, 26, BATT2, BATT, glow=12)
    reserve = v.get("reserve")
    if reserve is not None:
        a = math.radians(-90 + 360 * reserve / 100)
        cv.line([(cx + math.cos(a) * (r - 18), cy + math.sin(a) * (r - 18)),
                 (cx + math.cos(a) * (r + 18), cy + math.sin(a) * (r + 18))], RED, 3, glow=3)
    cv.glow_text((cx, cy - 8), "—" if level is None else f"{level:.0f}", 120, TEXT, "bold")
    cv.text((cx + 70, cy + 18), "%", 30, MUTED, "bold", anchor="ls")
    state = (v.get("battery_state") or "—").upper()
    col = BATT if state == "CHARGING" else SOLAR if state == "DISCHARGING" else CYAN
    cv.text((cx, cy + 58), state, 18, col, "bold", anchor="mm", spacing=3)
    bw = v.get("battery_w")
    cv.text((cx, cy + 84), "" if bw is None else f"{abs(bw) / 1000:.2f} kW", 16, MUTED, "semi", anchor="mm")
    if reserve is not None:
        cv.text((cx, cy + r + 52), f"RESERVE {reserve:.0f}%   ·   CAPACITY {v.get('capacity') or 0:.1f} kWh", 12, MUTED, "semi",
                anchor="mm", spacing=1)

    # state-of-charge sparkline
    cv.card(24, 538, 672, 66)
    soe = [(p[0], p[5]) for p in (v.get("series") or []) if p[5] is not None]
    if len(soe) > 1:
        line = [(40 + 640 * m / 1440, 592 - 40 * s / 100) for m, s in soe]
        cv.line(line, BATT, 2, glow=4)
    cv.text((40, 546), "CHARGE TODAY", 11, MUTED, "semi", spacing=2)

    ttl = fmt_duration(v.get("time_to_full")) if (bw or 0) > 150 else fmt_duration(v.get("time_to_empty"))
    label = "TO FULL" if (bw or 0) > 150 else "TO RESERVE" if (bw or 0) < -150 else "STANDBY"
    t = v.get("today") or {}
    cards = [(label, ttl or "—", CYAN), ("CHARGED TODAY", "—" if t.get("charged") is None else f"{t['charged']:.1f} kWh", BATT),
             ("USED TODAY", "—" if t.get("discharged") is None else f"{t['discharged']:.1f} kWh", SOLAR)]
    for i, (lab, val, c) in enumerate(cards):
        x = 24 + i * 228
        cv.card(x, 616, 216, 74, accent=c)
        cv.text((x + 18, 628), lab, 12, MUTED, "semi", spacing=2)
        cv.text((x + 18, 646), val, 28, c, "bold")
    return cv.png()


def page_money(data: dict) -> bytes:
    cv = Canvas("money", data)
    v, cur = data, data["currency"]
    cv.text((360, 104), "SAVED TODAY BY SOLAR + BATTERY", 13, MUTED, "semi", anchor="mm", spacing=3)
    cv.glow_text((360, 168), fmt_money(v.get("saved_today"), cur), 92, BATT, "bold")

    # price timeline
    x, y, w, h = 44, 262, 632, 170
    cv.card(24, 236, 672, 240)
    slots = v.get("price_slots") or []
    prices = [p for p, _ in slots if p is not None]
    cv.text((44, 248), "PRICE TODAY" if prices else "GRID USE TODAY", 12, MUTED, "semi", spacing=2)
    if prices:
        top = max(prices) * 1.1
        now = v["now"]
        now_slot = (now.hour * 60 + now.minute) // 15
        bw = w / 96
        for i, (price, period) in enumerate(slots):
            if price is None:
                continue
            col = period_color(period)
            bh = h * price / top
            x0 = x + i * bw
            a = 255 if i == now_slot else 150
            cv.d.rectangle([(x0 + 0.6) * SS, (y + h - bh) * SS, (x0 + bw - 0.6) * SS, (y + h) * SS], fill=col + (a,))
            if i == now_slot:
                cv.g.rectangle([x0 * SS / 2, (y + h - bh) * SS / 2, (x0 + bw) * SS / 2, (y + h) * SS / 2], fill=col)
                cv.text((x0 + bw / 2, y + h - bh - 8), fmt_price(price, cur), 16, col, "bold", anchor="mb")
        for hr in (0, 6, 12, 18, 24):
            cv.text((x + w * hr / 24, y + h + 8), f"{hr:02d}", 12, MUTED, "semi", anchor="mt")
        # legend of periods present
        seen = []
        for _, period in slots:
            if period and period not in seen:
                seen.append(period)
        lx = 676
        for period in reversed(seen[:4]):
            tw = cv.d.textlength(period.upper(), font=font(11, "semi")) / SS + 20
            lx -= tw
            cv.d.rounded_rectangle([lx * SS, 244 * SS, (lx + 10) * SS, 254 * SS], radius=2 * SS, fill=period_color(period) + (255,))
            cv.text((lx + 14, 249), period.upper(), 11, MUTED, "semi", anchor="lm")
            lx -= 10
    else:
        # no rate plan (price from a sensor): grid energy bought per hour instead
        hourly = [0.0] * 24
        for p in v.get("series") or []:
            if p[3] is not None and p[3] > 0:
                hourly[p[0] // 60] += p[3] * 5 / 60 / 1000
        top = max(hourly + [0.5]) * 1.15
        cv.text((676, 249), "GRID kWh BOUGHT PER HOUR", 11, GRID, "semi", anchor="rm", spacing=1)
        bw = w / 24
        now_hr = v["now"].hour
        for hr, kwh in enumerate(hourly):
            if kwh <= 0:
                continue
            bh = h * kwh / top
            x0 = x + hr * bw
            col = GRID if hr != now_hr else CYAN
            cv.d.rounded_rectangle([(x0 + 3) * SS, (y + h - bh) * SS, (x0 + bw - 3) * SS, (y + h) * SS], radius=3 * SS,
                                   fill=col + (230,))
            if kwh == max(hourly):
                cv.text((x0 + bw / 2, y + h - bh - 6), f"{kwh:.1f}", 13, TEXT, "semi", anchor="mb")
        for hr in (0, 6, 12, 18, 24):
            cv.text((x + w * hr / 24, y + h + 8), f"{hr:02d}", 12, MUTED, "semi", anchor="mt")

    nxt = v.get("next") or {}
    pcol = period_color(v.get("period"))
    cards = [
        ("NOW", fmt_price(v.get("price"), cur), (v.get("period") or "").upper(), pcol),
        ("NEXT", fmt_price(nxt.get("price"), cur) if nxt else "—",
         f"{nxt['at']} {(nxt.get('period') or '').upper()}" if nxt else "", period_color(nxt.get("period"))),
        ("GRID COST TODAY", fmt_money(v.get("cost_today"), cur), "IMPORTS − EXPORT CREDIT", TEXT),
    ]
    for i, (lab, val, sub, c) in enumerate(cards):
        x0 = 24 + i * 228
        cv.card(x0, 488, 216, 96, accent=c)
        cv.text((x0 + 18, 500), lab, 12, MUTED, "semi", spacing=2)
        cv.text((x0 + 18, 518), val, 34, c, "bold")
        cv.text((x0 + 18, 566), sub, 11, MUTED, "semi", spacing=1)

    week_saved = v.get("week_saved")
    week_cost = v.get("week_cost")
    cv.card(24, 596, 672, 94)
    cv.text((44, 612), "LAST 7 DAYS", 12, MUTED, "semi", spacing=2)
    cv.text((44, 630), f"SAVED {fmt_money(week_saved, cur)}", 30, BATT, "bold")
    cv.text((676, 630), f"PAID {fmt_money(week_cost, cur)}", 30, TEXT, "bold", anchor="ra")
    if week_saved is not None and week_cost is not None and week_saved + week_cost > 0:
        share = week_saved / (week_saved + week_cost)
        cv.d.rounded_rectangle([44 * SS, 672 * SS, 676 * SS, 678 * SS], radius=3 * SS, fill=INK + (25,))
        cv.d.rounded_rectangle([44 * SS, 672 * SS, (44 + 632 * share) * SS, 678 * SS], radius=3 * SS, fill=BATT + (255,))
        cv.g.line([(44 * SS / 2, 675 * SS / 2), ((44 + 632 * share) * SS / 2, 675 * SS / 2)], fill=BATT, width=4)
    return cv.png()


def page_solar(data: dict) -> bytes:
    cv = Canvas("solar", data)
    v = data
    eff = v.get("efficiency")
    # semicircle gauge
    cx, cy, r = 190, 250, 130
    span = min(max((eff or 0) / 130, 0), 1)
    cv.arc(cx, cy, r, 180, 360, 20, SOLAR2, SOLAR, glow=0, track=False)
    box = [(cx - r) * SS, (cy - r) * SS, (cx + r) * SS, (cy + r) * SS]
    cv.d.arc(box, 180, 360, fill=INK + (22,), width=20 * SS)
    cv.arc(cx, cy, r, 180, 180 + 180 * span, 20, SOLAR2, SOLAR, glow=10, track=False)
    cv.glow_text((cx, cy - 22), "—" if eff is None else f"{eff:.0f}%", 64, TEXT, "bold")
    cv.text((cx, cy + 22), "OUTPUT VS SPEC", 12, MUTED, "semi", anchor="mm", spacing=2)

    health = v.get("health") or "—"
    hcol = BATT if health == "Improving" else RED if health == "Declining" else CYAN
    clean = v.get("cleaning") or "—"
    ccol = RED if clean == "Yes" else BATT if clean == "No" else MUTED
    chips = [("HEALTH", health.upper(), hcol), ("CLEAN PANELS", clean.upper(), ccol),
             ("ARRAY", f"{v.get('array_kw') or 0:.2f} kW", SOLAR)]
    for i, (lab, val, c) in enumerate(chips):
        y = 104 + i * 70
        cv.card(372, y, 324, 60, accent=c)
        cv.text((392, y + 30), lab, 12, MUTED, "semi", anchor="lm", spacing=2)
        cv.text((680, y + 30), val, 24, c, "bold", anchor="rm")

    # 30 days vs typical
    x, y, w, h = 44, 352, 632, 200
    cv.card(24, 326, 672, 262)
    cv.text((44, 338), "LAST 30 DAYS  ·  kWh", 12, MUTED, "semi", spacing=2)
    days = (v.get("days30") or [])[-30:]
    if days:
        top = max([d[1] for d in days] + [d[2] or 0 for d in days] + [1]) * 1.15
        bw = w / 30
        for i, (date, kwh, typical) in enumerate(days):
            x0 = x + (30 - len(days) + i) * bw
            bh = h * kwh / top
            col = mix(SOLAR2, SOLAR, kwh / top)
            cv.d.rounded_rectangle([(x0 + 2) * SS, (y + h - bh) * SS, (x0 + bw - 2) * SS, (y + h) * SS], radius=2 * SS, fill=col + (230,))
        typ = [(x + (30 - len(days) + i) * bw + bw / 2, y + h - h * (d[2] or 0) / top) for i, d in enumerate(days) if d[2]]
        if len(typ) > 1:
            cv.line(typ, CYAN, 1.8, glow=4)
            cv.line([(588, 344), (604, 344)], CYAN, 2)
            cv.text((610, 344), "TYPICAL", 11, CYAN, "semi", anchor="lm", spacing=1)
        cv.text((x, y + h + 10), days[0][0][5:], 11, MUTED, "semi", anchor="lt")
        cv.text((x + w, y + h + 10), days[-1][0][5:], 11, MUTED, "semi", anchor="rt")
    else:
        cv.text((360, 450), "COLLECTING HISTORY…", 16, MUTED, "semi", anchor="mm", spacing=2)

    cur_stats = [("BEST DAY", f"{v['best_kwh']:.1f}" if v.get("best_kwh") else "—", (v.get("best_date") or "")[5:]),
                 ("7-DAY AVG", f"{v['avg7']:.1f}" if v.get("avg7") else "—", "kWh / day"),
                 ("LIFETIME AVG", f"{v['lifetime_avg']:.1f}" if v.get("lifetime_avg") else "—", "kWh / day")]
    for i, (lab, val, sub) in enumerate(cur_stats):
        x0 = 24 + i * 228
        cv.card(x0, 600, 216, 90)
        cv.text((x0 + 18, 612), lab, 12, MUTED, "semi", spacing=2)
        cv.text((x0 + 18, 630), val, 32, SOLAR, "bold")
        cv.text((x0 + 198, 670), sub, 12, MUTED, "semi", anchor="rs")
    return cv.png()


def page_week(data: dict) -> bytes:
    cv = Canvas("week", data)
    v = data
    week = (v.get("week") or [])[-7:]
    x, y, w, h = 44, 130, 632, 330
    cv.card(24, 88, 672, 420)
    for i, (label, col) in enumerate((("SOLAR", SOLAR), ("HOME", HOME), ("BOUGHT", GRID))):
        lx = 44 + i * 110
        cv.d.rounded_rectangle([lx * SS, 99 * SS, (lx + 14) * SS, 105 * SS], radius=3 * SS, fill=col + (255,))
        cv.text((lx + 20, 102), label, 12, MUTED, "semi", anchor="lm", spacing=1)
    if week:
        top = max([max(d["solar"] or 0, d["house"] or 0, d["import"] or 0) for d in week] + [1]) * 1.15
        gw = w / 7
        for i, day in enumerate(week):
            gx = x + i * gw
            for j, (key, col) in enumerate((("solar", SOLAR), ("house", HOME), ("import", GRID))):
                val = day[key] or 0
                bh = h * val / top
                bx = gx + 12 + j * (gw - 24) / 3
                cv.d.rounded_rectangle([bx * SS, (y + h - bh) * SS, (bx + (gw - 24) / 3 - 4) * SS, (y + h) * SS],
                                       radius=3 * SS, fill=col + (235 if not day["today"] else 140,))
            dname = day["label"]
            cv.text((gx + gw / 2, y + h + 10), dname, 13, TEXT if day["today"] else MUTED, "bold" if day["today"] else "semi",
                    anchor="mt", spacing=1)
            if day.get("ss") is not None:
                cv.text((gx + gw / 2, y - 6), f"{day['ss']:.0f}%", 12, BATT, "semi", anchor="mb")
        cv.text((676, 102), "% = SELF-SUFFICIENT", 11, BATT, "semi", anchor="rm", spacing=1)
    else:
        cv.text((360, 300), "COLLECTING HISTORY…", 16, MUTED, "semi", anchor="mm", spacing=2)

    tot = v.get("week_totals") or {}
    cards = [("SOLAR", tot.get("solar"), SOLAR), ("HOME", tot.get("house"), HOME), ("BOUGHT", tot.get("import"), GRID)]
    for i, (lab, val, c) in enumerate(cards):
        x0 = 24 + i * 228
        cv.card(x0, 520, 216, 82, accent=c)
        cv.text((x0 + 18, 532), f"WEEK {lab}", 12, MUTED, "semi", spacing=2)
        cv.text((x0 + 18, 550), "—" if val is None else f"{val:.0f}", 34, c, "bold")
        cv.text((x0 + 198, 590), "kWh", 12, MUTED, "semi", anchor="rs")
    change = v.get("week_change")
    cv.card(24, 614, 672, 76)
    cv.text((44, 628), "SOLAR VS PREVIOUS 7 DAYS", 12, MUTED, "semi", spacing=2)
    if change is None:
        cv.text((44, 646), "NEED TWO WEEKS OF HISTORY", 22, MUTED, "bold")
    else:
        col = BATT if change >= 0 else RED
        tri = [(44, 670), (64, 670), (54, 652)] if change >= 0 else [(44, 652), (64, 652), (54, 670)]
        cv.d.polygon([(px * SS, py * SS) for px, py in tri], fill=col + (255,))
        cv.text((74, 644), f"{abs(change):.0f}%", 32, col, "bold")
    return cv.png()


RENDERERS = {
    "overview": page_overview,
    "live": page_live,
    "today": page_today,
    "battery": page_battery,
    "money": page_money,
    "solar": page_solar,
    "week": page_week,
}



# ----------------------------------------------------------------------------
# Solar-only variants (no SolarEdge grid meter) and the no-battery page
# ----------------------------------------------------------------------------

METER_NOTE = "ADD A SOLAREDGE METER TO SEE HOME & GRID"


def _solar_gauge(cv, v, cx, cy, r, width, big):
    """Output vs inverter capacity: a 270-degree gauge with the live kW inside."""
    cap = v.get("inverter_kw") or 0
    solar = (v.get("solar_w") or 0) / 1000
    frac = min(solar / cap, 1) if cap else 0
    box = [(cx - r) * SS, (cy - r) * SS, (cx + r) * SS, (cy + r) * SS]
    cv.d.arc(box, 135, 405, fill=INK + (22,), width=int(width * SS))
    cv.arc(cx, cy, r, 135, 135 + 270 * frac, width, SOLAR2, SOLAR, glow=width * 0.6, track=False)
    cv.icon_sun(cx, cy - r * 0.42, r * 0.32)
    cv.glow_text((cx, cy + r * 0.02), f"{solar:.1f}", big, TEXT, "bold")
    cv.text((cx, cy + r * 0.34), "kW SOLAR NOW", max(int(big * 0.16), 10), MUTED, "semi", anchor="mm", spacing=2)
    if cap:
        cv.text((cx, cy + r * 0.62), f"{frac * 100:.0f}% OF {cap:g} kW", max(int(big * 0.15), 10), SOLAR, "semi",
                anchor="mm", spacing=1)


def _note(cv, y=596):
    cv.text((360, y), METER_NOTE, 11, DIM, "semi", anchor="mm", spacing=2)


def page_live_solar(data: dict) -> bytes:
    cv = Canvas("live", data)
    v, cur = data, data["currency"]
    _sun_arc(cv, v, 360, 205, 290, 112)
    _solar_gauge(cv, v, 360, 372, 150, 22, 76)
    t = v.get("today") or {}
    pcol = period_color(v.get("period"))
    vs = v.get("today_vs_typical")
    kpis = [
        ("PRODUCED TODAY", "—" if t.get("solar") is None else f"{t['solar']:.1f} kWh", SOLAR),
        ("PRICE NOW", fmt_price(v.get("price"), cur), pcol),
        ("SOLAR VALUE", fmt_money(v.get("saved_today"), cur), GREEN),
    ]
    for i, (label, value, col) in enumerate(kpis):
        x = 24 + i * 228
        cv.card(x, 618, 216, 72, accent=col)
        cv.text((x + 18, 630), label, 12, MUTED, "semi", spacing=2)
        cv.text((x + 18, 646), value, 30, col, "bold")
    if vs is not None:
        cv.text((24 + 216 - 14, 682), f"{vs:.0f}% OF TYPICAL", 11, SOLAR, "semi", anchor="rs", spacing=1)
    _note(cv)
    return cv.png()


def _solar_chart(cv, v, x, y, w, h, compact=False):
    """Solar area (+ battery % if any) with typical marker and NOW line."""
    data = dict(v)
    data["series"] = [[p[0], p[1], None, None, p[4], p[5]] for p in (v.get("series") or [])]
    return _power_chart(cv, data, x, y, w, h, compact=compact, solar_only=True)


def page_overview_solar(data: dict, variant: str | None = None, shape: str = "1x1") -> bytes:
    """Overview for solar-only sites (no meter, no battery), with the same layouts per
    shape and the same variants as page_overview: "slot" - the Today card is an empty
    frame; "flow" - only Solar now; every other card is an empty frame, no clock."""
    lay = OVERVIEW_LAYOUTS[shape]
    cv = Canvas("overview", {**data, "_bare": variant == "flow"}, *lay["size"])
    v, cur = data, data["currency"]
    t = v.get("today") or {}

    # solar now (where the live flow is on a full site)
    x, y, w, h = lay["flow"]
    cv.card(x, y, w, h)
    cv.text((x + 16, y + 12), "SOLAR NOW", 11, MUTED, "semi", spacing=2)
    k = min(max(min(w / 420, h / 300), 1), 1.35)
    cx = x + w / 2
    _sun_arc(cv, v, cx, y + h * 0.34, min(w * 0.417, 175 * k * 1.4), 70 * h / 300, labels=False)
    _solar_gauge(cv, v, cx, y + h * 0.6, 92 * k, 14, round(46 * k))

    # today (where the battery is on a full site)
    cv.card(*lay["battery"])
    if variant == "flow":
        cv.card(*lay["chart"])
        for tile in lay["tiles"]:
            cv.card(*tile)
        return cv.png()
    if variant != "slot":
        x, y, w, h = lay["battery"]
        k = min(max(min(w / 240, h / 300), 0.8), 1.3)
        bx = x + w / 2
        cv.text((x + 16, y + 12), "TODAY", 11, MUTED, "semi", spacing=2)
        produced = t.get("solar")
        cv.glow_text((bx, y + h * 0.28), "—" if produced is None else f"{produced:.1f}", round(54 * k), SOLAR, "bold")
        cv.text((bx, y + h * 0.413), "kWh PRODUCED", 12, MUTED, "semi", anchor="mm", spacing=2)
        typical, vs = v.get("typical_today"), v.get("today_vs_typical")
        ry = y + h * 0.707
        cv.arc(bx, ry, 46 * k, -90, -90 + 360 * min((vs or 0) / 100, 1), 9, SOLAR2, SOLAR, glow=5)
        cv.text((bx, ry), "—" if vs is None else f"{vs:.0f}%", 20, TEXT, "bold", anchor="mm")
        cv.text((bx, y + h * 0.913), f"OF TYPICAL {typical:.1f} kWh" if typical else "OF TYPICAL", 11, MUTED,
                "semi", anchor="mm", spacing=1)

    # today's chart
    x, y, w, h = lay["chart"]
    cv.card(x, y, w, h)
    cv.text((x + 16, y + 12), "SOLAR TODAY", 11, MUTED, "semi", spacing=2)
    _solar_chart(cv, v, x + 16, y + 36, w - 32, h - 72, compact=True)

    # key numbers
    pcol = period_color(v.get("period"))
    eff = v.get("efficiency")
    clean = v.get("cleaning")
    tiles = [
        ("SOLAR VALUE", fmt_money(v.get("saved_today"), cur), "TODAY", GREEN),
        ("PRICE NOW", fmt_price(v.get("price"), cur), (v.get("period") or "").upper(), pcol),
        ("7-DAY AVG", f"{v['avg7']:.1f}" if v.get("avg7") else "—", "kWh / DAY", SOLAR),
        ("PANELS VS SPEC", "—" if eff is None else f"{eff:.0f}%",
         f"{(v.get('health') or '—').upper()}{'  ·  CLEAN' if clean == 'Yes' else ''}", RED if clean == "Yes" else SOLAR),
    ]
    for (label, value, sub, col), (x, y, w, h) in zip(tiles, lay["tiles"]):
        big = h >= 130
        cv.card(x, y, w, h, accent=col)
        cv.text((x + 16, y + 12), label, 11, MUTED, "semi", spacing=1)
        cv.text((x + 16, y + (40 if big else 30)), value, 40 if big else 32, col, "bold")
        cv.text((x + 16, y + h - 16), sub, 10, MUTED, "semi", anchor="ls", spacing=1)
    return cv.png()


def page_today_solar(data: dict) -> bytes:
    cv = Canvas("today", data)
    v, cur = data, data["currency"]
    cv.card(24, 88, 672, 368)
    _legend(cv, 44, 102, (("SOLAR", SOLAR),) + ((("BATTERY %", BATT),) if v.get("has_battery") else ()), 120)
    pts = _solar_chart(cv, v, 44, 118, 640, 290)
    t = v.get("today") or {}
    peak = max(pts, key=lambda p: p[1] or 0) if pts else None
    kpis = [("PRODUCED", None if t.get("solar") is None else f"{t['solar']:.1f}", "kWh", SOLAR),
            ("TYPICAL", None if v.get("typical_today") is None else f"{v['typical_today']:.1f}", "kWh", CYAN),
            ("VS TYPICAL", None if v.get("today_vs_typical") is None else f"{v['today_vs_typical']:.0f}", "%", GREEN),
            ("PEAK", None if not peak or not peak[1] else f"{peak[1] / 1000:.2f}", "kW", SOLAR2)]
    for i, (label, value, unit, col) in enumerate(kpis):
        kx = 24 + i * 170
        cv.card(kx, 470, 160, 96, accent=col)
        cv.text((kx + 18, 484), label, 12, MUTED, "semi", spacing=2)
        cv.text((kx + 18, 502), value or "—", 36, col, "bold")
        cv.text((kx + 142, 552), unit, 12, MUTED, "semi", anchor="rs")
    cv.card(24, 580, 672, 110)
    cv.text((44, 596), "SOLAR VALUE TODAY", 12, MUTED, "semi", spacing=2)
    cv.text((44, 614), fmt_money(v.get("saved_today"), cur), 34, GREEN, "bold")
    cv.text((676, 596), "PRICE NOW", 12, MUTED, "semi", anchor="ra", spacing=2)
    cv.text((676, 614), fmt_price(v.get("price"), cur), 34, period_color(v.get("period")), "bold", anchor="ra")
    if peak and peak[1]:
        cv.text((44, 664), f"PEAK AT {peak[0] // 60:02d}:{peak[0] % 60:02d}", 12, MUTED, "semi", spacing=1)
    cv.text((676, 664), METER_NOTE, 10, DIM, "semi", anchor="ra", spacing=1)
    return cv.png()


def page_battery_none(data: dict) -> bytes:
    cv = Canvas("battery", data)
    cx, cy = 360, 300
    cv.arc(cx, cy, 150, 0, 360, 18, (40, 52, 70), (40, 52, 70), glow=0)
    cv.icon_battery(cx, cy - 4, 120, None, color=MUTED)
    cv.text((cx, cy + 210), "NO BATTERY ON THIS SYSTEM", 24, TEXT, "bold", anchor="mm", spacing=3)
    cv.text((cx, cy + 250), "A home battery stores midday solar for the evening peak", 16, MUTED, "semi", anchor="mm")
    cv.text((cx, cy + 276), "and keeps the lights on during outages.", 16, MUTED, "semi", anchor="mm")
    return cv.png()


def page_money_solar(data: dict) -> bytes:
    cv = Canvas("money", data)
    v, cur = data, data["currency"]
    cv.text((360, 104), "SOLAR VALUE TODAY", 13, MUTED, "semi", anchor="mm", spacing=3)
    cv.glow_text((360, 168), fmt_money(v.get("saved_today"), cur), 92, GREEN, "bold")
    x, y, w, h = 44, 262, 632, 170
    cv.card(24, 236, 672, 240)
    slots = v.get("price_slots") or []
    prices = [p for p, _ in slots if p is not None]
    if prices:
        cv.text((44, 248), "PRICE TODAY", 12, MUTED, "semi", spacing=2)
        top = max(prices) * 1.1
        now = v["now"]
        now_slot = (now.hour * 60 + now.minute) // 15
        bw = w / 96
        for i, (price, period) in enumerate(slots):
            if price is None:
                continue
            col = period_color(period)
            bh = h * price / top
            x0 = x + i * bw
            cv.d.rectangle([(x0 + 0.6) * SS, (y + h - bh) * SS, (x0 + bw - 0.6) * SS, (y + h) * SS],
                           fill=col + (255 if i == now_slot else 150,))
            if i == now_slot:
                cv.g.rectangle([x0 * SS / 2, (y + h - bh) * SS / 2, (x0 + bw) * SS / 2, (y + h) * SS / 2], fill=col)
                cv.text((x0 + bw / 2, y + h - bh - 8), fmt_price(price, cur), 16, col, "bold", anchor="mb")
    else:
        cv.text((44, 248), "SOLAR PER HOUR", 12, MUTED, "semi", spacing=2)
        hourly = [0.0] * 24
        for p in v.get("series") or []:
            if p[1]:
                hourly[p[0] // 60] += p[1] * 5 / 60 / 1000
        top = max(hourly + [0.5]) * 1.15
        bw = w / 24
        for hr, kwh in enumerate(hourly):
            if kwh > 0:
                bh = h * kwh / top
                cv.d.rounded_rectangle([(x + hr * bw + 3) * SS, (y + h - bh) * SS, (x + hr * bw + bw - 3) * SS, (y + h) * SS],
                                       radius=3 * SS, fill=SOLAR + (230,))
    for hr in (0, 6, 12, 18, 24):
        cv.text((x + w * hr / 24, y + h + 8), f"{hr:02d}", 12, MUTED, "semi", anchor="mt")

    nxt = v.get("next") or {}
    t = v.get("today") or {}
    pcol = period_color(v.get("period"))
    cards = [
        ("NOW", fmt_price(v.get("price"), cur), (v.get("period") or "").upper(), pcol),
        ("NEXT", fmt_price(nxt.get("price"), cur) if nxt else "—",
         f"{nxt['at']} {(nxt.get('period') or '').upper()}" if nxt else "", period_color(nxt.get("period"))),
        ("PRODUCED TODAY", "—" if t.get("solar") is None else f"{t['solar']:.1f}", "kWh", SOLAR),
    ]
    for i, (lab, val, sub, c) in enumerate(cards):
        x0 = 24 + i * 228
        cv.card(x0, 488, 216, 96, accent=c)
        cv.text((x0 + 18, 500), lab, 12, MUTED, "semi", spacing=2)
        cv.text((x0 + 18, 518), val, 34, c, "bold")
        cv.text((x0 + 18, 566), sub, 11, MUTED, "semi", spacing=1)
    cv.card(24, 596, 672, 94)
    cv.text((44, 612), "LAST 7 DAYS", 12, MUTED, "semi", spacing=2)
    cv.text((44, 630), f"SOLAR VALUE {fmt_money(v.get('week_saved'), cur)}", 30, GREEN, "bold")
    cv.text((676, 612), METER_NOTE, 10, DIM, "semi", anchor="ra", spacing=1)
    return cv.png()


def page_week_solar(data: dict) -> bytes:
    cv = Canvas("week", data)
    v = data
    week = (v.get("week") or [])[-7:]
    x, y, w, h = 44, 130, 632, 330
    cv.card(24, 88, 672, 420)
    _legend(cv, 44, 102, (("SOLAR kWh", SOLAR),), 120)
    vals = [d.get("solar") or 0 for d in week]
    if week:
        top = max(vals + [1]) * 1.15
        gw = w / 7
        for i, day in enumerate(week):
            val = day.get("solar") or 0
            bh = h * val / top
            bx = x + i * gw + gw * 0.2
            cv.d.rounded_rectangle([bx * SS, (y + h - bh) * SS, (bx + gw * 0.6) * SS, (y + h) * SS], radius=4 * SS,
                                   fill=mix(SOLAR2, SOLAR, val / top) + (235 if not day.get("today") else 140,))
            cv.text((bx + gw * 0.3, y + h - bh - 6), f"{val:.1f}", 13, TEXT, "semi", anchor="mb")
            cv.text((bx + gw * 0.3, y + h + 10), day["label"], 13, TEXT if day.get("today") else MUTED,
                    "bold" if day.get("today") else "semi", anchor="mt", spacing=1)
    full = [d.get("solar") or 0 for d in week if not d.get("today")]
    best = max(full) if full else None
    cards = [("WEEK SOLAR", sum(vals), "kWh", SOLAR), ("DAILY AVERAGE", sum(full) / len(full) if full else None, "kWh", CYAN),
             ("BEST DAY", best, "kWh", GREEN)]
    for i, (lab, val, unit, c) in enumerate(cards):
        x0 = 24 + i * 228
        cv.card(x0, 520, 216, 82, accent=c)
        cv.text((x0 + 18, 532), lab, 12, MUTED, "semi", spacing=2)
        cv.text((x0 + 18, 550), "—" if val is None else f"{val:.0f}" if val >= 100 else f"{val:.1f}", 34, c, "bold")
        cv.text((x0 + 198, 590), unit, 12, MUTED, "semi", anchor="rs")
    change = v.get("week_change")
    cv.card(24, 614, 672, 76)
    cv.text((44, 628), "SOLAR VS PREVIOUS 7 DAYS", 12, MUTED, "semi", spacing=2)
    if change is None:
        cv.text((44, 646), "NEED TWO WEEKS OF HISTORY", 22, MUTED, "bold")
    else:
        col = GREEN if change >= 0 else RED
        tri = [(44, 670), (64, 670), (54, 652)] if change >= 0 else [(44, 652), (64, 652), (54, 670)]
        cv.d.polygon([(px * SS, py * SS) for px, py in tri], fill=col + (255,))
        cv.text((74, 644), f"{abs(change):.0f}%", 32, col, "bold")
    return cv.png()


PLACEHOLDER_TEXT = ("Please come back again later,", "we are still gathering the data", "for this page...")


def page_placeholder(page: str, data: dict) -> bytes:
    """Shown until a page has something to show (after install/restart, no history yet)."""
    data = {**data, "now": data.get("now") or datetime.datetime.now()}
    cv = Canvas(page, data)
    cx, cy = 360, 330
    for i in range(12):  # spinner: fading arc segments
        a0 = i * 30 - 90
        col = mix(BG_BOTTOM, CYAN, (i + 1) / 12)
        cv.arc(cx, cy, 64, a0, a0 + 22, 8, col, col, glow=4 if i > 8 else 0, track=False)
    cv.circle(cx, cy, 36, outline=INK + (40,), width=1.5)
    for i, line in enumerate(PLACEHOLDER_TEXT):
        cv.text((cx, cy + 120 + i * 34), line, 24, TEXT if i == 0 else MUTED, "semi", anchor="mm")
    cv.text((cx, 610), "THIS PAGE REFRESHES AUTOMATICALLY", 12, DIM, "semi", anchor="mm", spacing=3)
    return cv.png()


def is_ready(page: str, data: dict) -> bool:
    """Whether a page has enough data to be worth drawing."""
    live = data.get("inverter") is not None and (
        data.get("house_w") is not None if data.get("has_grid", True) else data.get("solar_w") is not None
    )
    if page in ("overview", "live", "battery", "money"):
        return live
    if page == "today":
        return live and len(data.get("series") or []) >= 2
    if page in ("solar", "week"):
        return live and len(data.get("days30") or []) >= 1
    return live


SOLAR_ONLY = {
    "overview": page_overview_solar,
    "live": page_live_solar,
    "today": page_today_solar,
    "money": page_money_solar,
    "week": page_week_solar,
}


@themed
def render(page: str, data: dict) -> bytes:
    if page == "battery" and data.get("has_battery") is False and data.get("inverter") is not None:
        return page_battery_none(data)
    if not is_ready(page, data):
        return page_placeholder(page, data)
    if data.get("has_grid") is False and page in SOLAR_ONLY:
        return SOLAR_ONLY[page](data)
    return RENDERERS[page](data)


# ----------------------------------------------------------------------------
# Published files: every page in several shapes and sizes, as JPG
# ----------------------------------------------------------------------------

# (width, height) per shape, hi-res and low-res
FORMATS = {
    "1x1": {"hires": (1440, 1440), "lowres": (720, 720)},
    "16x9": {"hires": (1920, 1080), "lowres": (960, 540)},
    "4x3": {"hires": (1600, 1200), "lowres": (800, 600)},
    "9x10": {"hires": (1080, 1200), "lowres": (540, 600)},
    "9x16": {"hires": (1080, 1920), "lowres": (540, 960)},
}
# extra overview layouts for controllers that draw their own content on top
VARIANTS = {"overview-slot": "slot", "overview-flow": "flow"}
JPEG_QUALITY = 88


@themed
def render_image(page: str, data: dict, backdrop: Image.Image | None = None,
                 shape: str = "1x1", layers: bool = False):
    """A page (or an overview variant) as a full-size picture (2x working size).
    The overview family has its own layout per shape; other pages are square
    (optionally drawn on a given background slice, see fit()). With layers=True
    returns (crisp layer, blurred glow layer) instead, for transparent()."""
    _RAW.on, _RAW.bg, _RAW.layers = True, backdrop, layers
    try:
        if has_layout(page, data):
            draw = page_overview_solar if data.get("has_grid") is False else page_overview
            return draw(data, VARIANTS.get(page), shape)
        if page in VARIANTS:
            page = "overview"
        return render(page, data)
    finally:
        _RAW.on, _RAW.bg, _RAW.layers = False, None, False


def has_layout(page: str, data: dict) -> bool:
    """Pages drawn natively at every shape (the rest are centred on a wider background)."""
    return (page == "overview" or page in VARIANTS) and is_ready("overview", data)


def _backdrop(width: int, height: int, x0: int, y0: int) -> Image.Image:
    """The theme's background at any shape, in the 2x working size, aligned with a
    square page placed at (x0, y0)."""
    img = Image.new("RGB", (width, height), BG_TOP)
    d = ImageDraw.Draw(img, "RGBA")
    glow = Image.new("RGB", (width // 2, height // 2), (0, 0, 0))
    draw_background(img, d, ImageDraw.Draw(glow, "RGBA"), (x0, y0, x0 + C, y0 + 84 * SS), (x0, y0))
    if not GLOW:
        return img
    glow = glow.filter(ImageFilter.GaussianBlur(9)).resize((width, height), Image.BILINEAR)
    if GLOW != 1:
        glow = glow.point(lambda v: min(int(v * GLOW), 255))
    return ImageChops.screen(img, glow)


@themed
def fit(page: str, data: dict, width: int, height: int, shape: str = "1x1") -> Image.Image:
    """A page at any shape, never stretched: pages with their own layout fill the
    whole picture; others are drawn at full height (or width) in the middle of the
    background continued to that shape, without seams."""
    if has_layout(page, data):
        return render_image(page, data, shape=shape).resize((width, height), Image.LANCZOS)
    if width == height:
        return render_image(page, data).resize((width, height), Image.LANCZOS)
    scale = C / min(width, height)
    bw, bh = round(width * scale), round(height * scale)
    x0, y0 = (bw - C) // 2, (bh - C) // 2
    back = _backdrop(bw, bh, x0, y0)
    square = render_image(page, data, back.crop((x0, y0, x0 + C, y0 + C)))
    back.paste(square, (x0, y0))
    return back.resize((width, height), Image.LANCZOS)


@themed
def transparent(page: str, data: dict, width: int, height: int, shape: str = "1x1") -> Image.Image:
    """A page with a transparent background (cards translucent, glows kept in colour),
    as RGBA at width x height. Drawn twice - on black and on white - and the
    difference gives the exact transparency of every pixel."""
    import numpy as np

    native = has_layout(page, data)
    if native:
        w, h = OVERVIEW_LAYOUTS[shape]["size"]
        size = (w * SS, h * SS)
    else:
        size = (C, C)
    (black, glow), (white, _) = (
        render_image(page, data, Image.new("RGB", size, colour), shape if native else "1x1", layers=True)
        for colour in ((0, 0, 0), (255, 255, 255))
    )
    B = np.asarray(black, dtype=np.float32) / 255
    W = np.asarray(white, dtype=np.float32) / 255
    G = np.asarray(glow, dtype=np.float32) / 255
    alpha = np.clip(1 - (W - B).max(axis=2), 0, 1)
    colour = 1 - (1 - B) * (1 - G)  # premultiplied content, with the glow screen-blended on
    alpha = np.maximum(alpha, colour.max(axis=2))
    colour = colour / np.maximum(alpha, 1e-4)[..., None]
    rgba = np.dstack([np.clip(colour, 0, 1), alpha]) * 255
    img = Image.fromarray(rgba.round().astype(np.uint8), "RGBA")
    if native or width == height:
        return img.resize((width, height), Image.LANCZOS)
    side = min(width, height)  # square page: centred on a clear picture of the shape
    out = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    out.paste(img.resize((side, side), Image.LANCZOS), ((width - side) // 2, (height - side) // 2))
    return out


def png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=6)
    return buf.getvalue()


def jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    return buf.getvalue()


@themed
def export(page: str, data: dict, shapes=None, kinds=("jpg",)) -> dict[str, bytes]:
    """Every shape (or the given ones) and size of one page, as JPG and/or transparent
    PNG: {"overview-16x9-hires.jpg": bytes, "overview-16x9-hires.png": bytes, ...}."""
    files = {}
    for shape, sizes in FORMATS.items():
        if shapes is not None and shape not in shapes:
            continue
        for kind in kinds:
            if kind == "png":
                big = transparent(page, data, *sizes["hires"], shape=shape)
            else:
                big = fit(page, data, *sizes["hires"], shape=shape)
            for res, (w, h) in sizes.items():
                img = big if (w, h) == big.size else big.resize((w, h), Image.LANCZOS)
                files[f"{page}-{shape}-{res}.{kind}"] = png(img) if kind == "png" else jpeg(img)
    return files
