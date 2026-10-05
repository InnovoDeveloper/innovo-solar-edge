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
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

SIZE = 720
SS = 2  # supersampling
C = SIZE * SS

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
MAGENTA = (255, 64, 200)

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
FALLBACK = {
    "bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "semi": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "medium": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
}
WEIGHT_FILE = {"bold": "Rajdhani-Bold.ttf", "semi": "Rajdhani-SemiBold.ttf", "medium": "Rajdhani-Medium.ttf"}


@lru_cache(maxsize=64)
def font(size: int, weight: str = "semi"):
    path = os.path.join(FONT_DIR, WEIGHT_FILE[weight])
    if os.path.exists(path):
        return ImageFont.truetype(path, size * SS)
    for alt in FALLBACK[weight]:
        if os.path.exists(alt):
            return ImageFont.truetype(alt, int(size * SS * 0.85))
    return ImageFont.load_default(size=size * SS)


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

    def __init__(self, page: str, data: dict):
        self.data = data
        self.img = Image.new("RGB", (C, C), BG_TOP)
        self.d = ImageDraw.Draw(self.img, "RGBA")
        self.glow = Image.new("RGB", (C // 2, C // 2), (0, 0, 0))
        self.g = ImageDraw.Draw(self.glow, "RGBA")
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
        self.d.rounded_rectangle([x * SS, y * SS, (x + w) * SS, (y + h) * SS], radius=radius * SS,
                                 fill=(255, 255, 255, 12), outline=(255, 255, 255, 30), width=SS)
        if accent:
            self.d.rounded_rectangle([x * SS, (y + 10) * SS, (x + 3) * SS, (y + h - 10) * SS],
                                     radius=2 * SS, fill=accent + (255,))
            self.g.line([(x * SS / 2, (y + 10) * SS / 2), (x * SS / 2, (y + h - 10) * SS / 2)], fill=accent, width=3)

    def arc(self, cx, cy, r, start, end, width, c1, c2=None, glow=6, track=True):
        """Gradient arc from angle start to end (degrees, 0 = east, clockwise)."""
        box = [(cx - r) * SS, (cy - r) * SS, (cx + r) * SS, (cy + r) * SS]
        if track:
            self.d.arc(box, 0, 360, fill=(255, 255, 255, 18), width=int(width * SS))
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
        """A connector; active flows get a glowing core and 'particles' showing direction."""
        active = watts is not None and watts > 50
        if len(pts) == 2:
            (x0, y0), (x1, y1) = pts
            pts = [(x0 + (x1 - x0) * i / 40, y0 + (y1 - y0) * i / 40) for i in range(41)]
        self.line(pts, (255, 255, 255), 2, alpha=22)
        if not active:
            return
        width = 2 + min(watts / 1500, 3)
        self.line(pts, color, width, glow=6 + width * 2, alpha=200)
        path = list(reversed(pts)) if reverse else pts
        n = len(path)
        for k in range(1, 6):  # particles with fading trail
            idx = int(n * k / 6)
            for trail in range(4):
                j = max(idx - trail * 2, 0)
                x, y = path[j]
                r = 4.5 - trail
                self.circle(x, y, r, fill=mix(color, (255, 255, 255), 0.5 if trail == 0 else 0) + (255 - trail * 60,))
            x, y = path[idx]
            self.g.ellipse([(x - 6) * SS / 2, (y - 6) * SS / 2, (x + 6) * SS / 2, (y + 6) * SS / 2], fill=color)

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
        for y in range(0, C, 4):
            self.d.rectangle([0, y, C, y + 4], fill=mix(BG_TOP, BG_BOTTOM, y / C))
        # soft colour washes
        self.g.ellipse([-80, -100, 240, 180], fill=(0, 20, 38))
        self.g.ellipse([540, 540, 840, 840], fill=(22, 8, 40))
        # dot grid
        for x in range(24, SIZE, 24):
            for y in range(84, SIZE - 20, 24):
                self.d.point([(x * SS, y * SS)], fill=(255, 255, 255, 26))

    def _header(self, page):
        self.text((24, 18), "INNOVO", 13, CYAN, "bold", spacing=4)
        self.text((24, 34), TITLES[page], 30, TEXT, "bold", spacing=2)
        now = self.data["now"]
        self.text((SIZE - 24, 20), now.strftime("%H:%M"), 30, TEXT, "bold", anchor="ra")
        self.text((SIZE - 24, 54), now.strftime("%a %d %b").upper(), 13, MUTED, "semi", anchor="ra", spacing=1)
        status = self.data.get("inverter") or "Waiting"
        col = BATT if status == "Producing" else RED if status.startswith(("Fault", "Offline")) else MUTED
        self.circle(SIZE - 170, 40, 4, fill=col + (255,), glow=0)
        self.g.ellipse([(SIZE - 178) * SS / 2, 32 * SS / 2, (SIZE - 162) * SS / 2, 48 * SS / 2], fill=col)
        self.text((SIZE - 160, 40), status.upper(), 13, col, "semi", anchor="lm", spacing=1)
        for x in range(24, SIZE - 24, 2):
            t = (x - 24) / (SIZE - 48)
            self.d.point([(x * SS, 76 * SS)], fill=mix(CYAN, HOME, t) + (int(160 * (1 - abs(t - 0.5) * 1.6)),))

    def _footer(self, page):
        n = len(PAGES)
        x0 = SIZE / 2 - (n - 1) * 9
        for i, p in enumerate(PAGES):
            x = x0 + i * 18
            if p == page:
                self.d.rounded_rectangle([(x - 9) * SS, 703 * SS, (x + 9) * SS, 709 * SS], radius=3 * SS, fill=CYAN + (255,))
                self.g.line([((x - 9) * SS / 2, 706 * SS / 2), ((x + 9) * SS / 2, 706 * SS / 2)], fill=CYAN, width=4)
            else:
                self.circle(x, 706, 3, fill=(255, 255, 255, 60))

    def png(self) -> bytes:
        glow = self.glow.filter(ImageFilter.GaussianBlur(9)).resize((C, C), Image.BILINEAR)
        out = ImageChops.screen(self.img, glow)
        out = out.resize((SIZE, SIZE), Image.LANCZOS)
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
        cv.circle(x, y, r, fill=(14, 20, 34, 255), outline=color + (255,), width=2 if compact else 2.5, glow=5 if compact else 6)
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
    cv.line(pts, (255, 255, 255), 1.2, alpha=40)
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
        cv.line([(gx, y), (gx, y + h)], (255, 255, 255), 1, alpha=16)
        cv.text((gx, y + h + 7), f"{hr:02d}", size, MUTED, "semi", anchor="mt")


def _power_chart(cv, v, x, y, w, h, compact=False):
    """Today's solar area, home and grid lines, battery %, price bands and a NOW marker."""
    pts = v.get("series") or []
    peak = max([max(p[1] or 0, p[2] or 0, abs(p[3] or 0)) for p in pts] + [2000]) * 1.15

    def X(minute):
        return x + w * minute / 1440

    def Y(watts):
        return y + h - h * max(min(watts / peak, 1), 0)

    _axes_hours(cv, x, y, w, h, 11 if compact else 12)
    for kw in range(1, int(peak / 1000) + 1):
        gy = Y(kw * 1000)
        cv.line([(x, gy), (x + w, gy)], (255, 255, 255), 1, alpha=12)
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


def page_overview(data: dict) -> bytes:
    """Everything at a glance: flow, battery, today's chart and the key numbers."""
    cv = Canvas("overview", data)
    v, cur = data, data["currency"]

    # flow (left)
    cv.card(24, 88, 420, 300)
    cv.text((40, 100), "LIVE FLOW", 11, MUTED, "semi", spacing=2)
    _sun_arc(cv, v, 234, 168, 170, 52, labels=False)
    _flow_diagram(cv, v, {"solar": (234, 170), "grid": (92, 268), "home": (376, 268), "battery": (234, 330)},
                  r=36, compact=True)

    # battery (right)
    level = v.get("battery_level")
    cv.card(456, 88, 240, 300)
    cv.text((472, 100), "BATTERY", 11, MUTED, "semi", spacing=2)
    bx, by, br = 576, 222, 76
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
    cv.text((bx, 334), sub, 13, MUTED, "semi", anchor="mm", spacing=1)
    t = v.get("today") or {}
    cv.text((bx, 362), f"IN {t.get('charged') or 0:.1f}  ·  OUT {t.get('discharged') or 0:.1f} kWh", 11, DIM, "semi",
            anchor="mm", spacing=1)

    # today chart (middle)
    cv.card(24, 400, 672, 176)
    cv.text((40, 412), "TODAY", 11, MUTED, "semi", spacing=2)
    _legend(cv, 140, 412, (("SOLAR", SOLAR), ("HOME", HOME), ("GRID", GRID), ("BATT %", BATT)), 92)
    cv.text((680, 412), f"{t.get('solar') or 0:.1f} kWh SOLAR  ·  {v.get('house_today') or 0:.1f} kWh HOME", 11, MUTED,
            "semi", anchor="rm", spacing=1)
    _power_chart(cv, v, 40, 436, 640, 104, compact=True)

    # key numbers (bottom)
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
    for i, (label, value, sub, col) in enumerate(tiles):
        x = 24 + i * 170
        cv.card(x, 588, 160, 102, accent=col)
        cv.text((x + 16, 600), label, 11, MUTED, "semi", spacing=1)
        cv.text((x + 16, 618), value, 32, col, "bold")
        cv.text((x + 16, 674), sub, 10, MUTED, "semi", anchor="ls", spacing=1)
    return cv.png()


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
                (255, 255, 255), 1.2, alpha=50 if i % 5 == 0 else 25)
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
        cv.d.rounded_rectangle([44 * SS, 672 * SS, 676 * SS, 678 * SS], radius=3 * SS, fill=(255, 255, 255, 25))
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
    cv.d.arc(box, 180, 360, fill=(255, 255, 255, 22), width=20 * SS)
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
    cv.circle(cx, cy, 36, outline=(255, 255, 255, 40), width=1.5)
    for i, line in enumerate(PLACEHOLDER_TEXT):
        cv.text((cx, cy + 120 + i * 34), line, 24, TEXT if i == 0 else MUTED, "semi", anchor="mm")
    cv.text((cx, 610), "THIS PAGE REFRESHES AUTOMATICALLY", 12, DIM, "semi", anchor="mm", spacing=3)
    return cv.png()


def is_ready(page: str, data: dict) -> bool:
    """Whether a page has enough data to be worth drawing."""
    live = data.get("inverter") is not None and data.get("house_w") is not None
    if page in ("overview", "live", "battery", "money"):
        return live
    if page == "today":
        return live and len(data.get("series") or []) >= 2
    if page in ("solar", "week"):
        return live and len(data.get("days30") or []) >= 1
    return live


def render(page: str, data: dict) -> bytes:
    if not is_ready(page, data):
        return page_placeholder(page, data)
    return RENDERERS[page](data)
