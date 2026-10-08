"""Illustrated energy-flow scenes for the dashboard screens (one per graphics style).

Each scene draws the solar / home / grid / battery flow inside the area the classic
node diagram would use, labels the four values, and carries the drift pattern
(screens.Canvas.flow) along its beams, wires, ribbons or traces so the still picture
reads as moving. Coordinates are in 720-space units, like the rest of screens.py.
"""

from __future__ import annotations

import math
import random

from PIL import Image, ImageDraw


def _screens():
    try:
        from . import screens
    except ImportError:  # loaded on its own (tests)
        import screens
    return screens


class Ctx:
    """Everything a scene needs: canvas, colours, flows and the area to draw in."""

    def __init__(self, cv, v, nodes, r, compact):
        self.S = S = _screens()
        self.cv, self.v, self.nodes, self.r, self.compact = cv, v, nodes, r, compact
        self.f = S._flows(v)
        sx, sy = nodes["solar"]
        gx, _ = nodes["grid"]
        hx, _ = nodes["home"]
        _, by = nodes["battery"]
        self.x, self.x1 = gx - r * 1.15, hx + r * 1.15
        self.y, self.y1 = sy - r * 1.45, by + r * 1.25
        self.w, self.h = self.x1 - self.x, self.y1 - self.y
        self.u = min(self.w / 622, self.h / 434)  # 1.0 on the Live screen
        self.tags = []
        self.level = v.get("battery_level")
        self.has_battery = v.get("has_battery") is not False

    # --- drawing helpers -------------------------------------------------------------------
    def px(self, pts):
        SS = self.S.SS
        return [(px * SS, py * SS) for px, py in pts]

    def poly(self, pts, fill=None, outline=None, width=1.0):
        self.cv.d.polygon(self.px(pts), fill=fill, outline=outline, width=max(int(width * self.S.SS), 1) if outline else 1)

    def flow(self, pts, color, watts, reverse=False):
        self.cv.flow(pts, color, watts, reverse)

    def kw(self, watts):
        return f"{self.S.fmt_kw(watts)} kW"

    def tag(self, x, y, label, value, sub=None, sub_color=None, anchor="m"):
        self.tags.append((x, y, label, value, sub, sub_color, anchor))

    def standard_tags(self, at):
        """at: {"solar"|"home"|"grid"|"battery": (x, y, anchor)}"""
        S, f = self.S, self.f
        grid_sub = "BUYING" if f["imp"] > 50 else "SELLING" if f["export"] > 50 else "IDLE"
        grid_col = S.RED if f["imp"] > 50 else S.BATT if f["export"] > 50 else S.MUTED
        lvl = self.level
        items = {
            "solar": ("SOLAR", self.kw(f["s"]), None, None),
            "home": ("HOME", self.kw(f["h"]), None, None),
            "grid": ("GRID", self.kw(f["g"]), grid_sub, grid_col),
            "battery": ("BATTERY", self.kw(f["b"]), None if lvl is None else f"{lvl:.0f}%", S.BATT),
        }
        for key, (x, y, anchor) in at.items():
            if key == "battery" and not self.has_battery:
                continue
            self.tag(x, y, *items[key], anchor=anchor)

    def draw_tags(self):
        S, cv, u = self.S, self.cv, self.u
        big = max(int(24 * u), 14)
        small = max(int(11 * u), 9)
        for x, y, label, value, sub, sub_color, anchor in self.tags:
            a = {"m": "m", "l": "l", "r": "r"}[anchor]
            cv.text((x, y - big * 0.78), label, small, S.MUTED, "semi", anchor=a + "m", spacing=2)
            cv.text((x, y + 1), value, big, S.TEXT, "bold", anchor=a + "m")
            if sub:
                cv.text((x, y + big * 0.82), sub, small, sub_color or S.MUTED, "semi", anchor=a + "m", spacing=1)

    def sun(self, cx, cy, rad, rays=12, style="glow"):
        S, cv = self.S, self.cv
        if style == "line":
            cv.circle(cx, cy, rad, outline=S.SOLAR + (255,), width=1.6)
            for i in range(rays):
                a = math.radians(i * 360 / rays)
                cv.line([(cx + math.cos(a) * rad * 1.3, cy + math.sin(a) * rad * 1.3),
                         (cx + math.cos(a) * rad * 1.65, cy + math.sin(a) * rad * 1.65)], S.SOLAR, 1.4)
            return
        cv.g.ellipse([(cx - rad * 2.2) * S.SS / 2, (cy - rad * 2.2) * S.SS / 2, (cx + rad * 2.2) * S.SS / 2,
                      (cy + rad * 2.2) * S.SS / 2], fill=S.mix((0, 0, 0), S.SOLAR, 0.55))
        for i in range(rays):
            a = math.radians(i * 360 / rays + 7)
            cv.line([(cx + math.cos(a) * rad * 1.25, cy + math.sin(a) * rad * 1.25),
                     (cx + math.cos(a) * rad * 1.7, cy + math.sin(a) * rad * 1.7)], S.SOLAR, 2.2 * self.u + 0.8, alpha=220)
        cv.circle(cx, cy, rad, fill=S.SOLAR + (255,), glow=6)
        cv.circle(cx - rad * 0.3, cy - rad * 0.3, rad * 0.35, fill=S.mix(S.SOLAR, (255, 255, 255), 0.55) + (200,))

    def catenary(self, a, b, sag):
        return [(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t + sag * 4 * t * (1 - t)) for t in (i / 30 for i in range(31))]

    def grid_flow(self, path):
        """A wire between home (path start) and the grid (path end)."""
        f = self.f
        if f["imp"] > 50:
            self.flow(path, self.S.GRID, f["imp"], reverse=True)
        else:
            self.flow(path, self.S.GRID, f["export"])

    def battery_flow(self, path):
        """A cable between the battery (path start) and home (path end)."""
        f = self.f
        if f["b2h"] + f["b2g"] > 50:
            self.flow(path, self.S.BATT, f["b2h"] + f["b2g"])
        else:
            self.flow(path, self.S.BATT, f["s2b"] + f["g2b"], reverse=True)

    def pylon(self, bx, by, height, color=None, alpha=170):
        S, u = self.S, self.u
        color = color or S.INK
        top = by - height
        lw = 1.3 * u + 0.5
        legs = [((bx - 16 * u, by), (bx - 3 * u, top)), ((bx + 16 * u, by), (bx + 3 * u, top))]
        for p0, p1 in legs:
            self.cv.line([p0, p1], color, lw, alpha=alpha)
        n = 6
        for i in range(n):
            t0, t1 = i / n, (i + 1) / n
            l0 = (legs[0][0][0] + (legs[0][1][0] - legs[0][0][0]) * t0, by - height * t0)
            r1 = (legs[1][0][0] + (legs[1][1][0] - legs[1][0][0]) * t1, by - height * t1)
            r0 = (legs[1][0][0] + (legs[1][1][0] - legs[1][0][0]) * t0, by - height * t0)
            l1 = (legs[0][0][0] + (legs[0][1][0] - legs[0][0][0]) * t1, by - height * t1)
            self.cv.line([l0, r1], color, lw * 0.7, alpha=alpha - 50)
            self.cv.line([r0, l1], color, lw * 0.7, alpha=alpha - 50)
        for yy, half in ((top + 10 * u, 26 * u), (top + 26 * u, 20 * u)):
            self.cv.line([(bx - half, yy), (bx + half, yy)], color, lw, alpha=alpha)
        return (bx - 26 * u, top + 10 * u), (bx + 26 * u, top + 10 * u)

    def battery_box(self, x0, y0, bw, bh):
        S, cv = self.S, self.cv
        cv.d.rounded_rectangle(self.px([(x0, y0), (x0 + bw, y0 + bh)]), radius=3 * S.SS, fill=S.NODE + (255,),
                               outline=S.BATT + (255,), width=max(int(1.6 * S.SS), 1))
        cv.d.rectangle(self.px([(x0 + bw * 0.32, y0 - 4 * self.u), (x0 + bw * 0.68, y0)]), fill=S.BATT + (255,))
        bars = 4
        lit = 0 if self.level is None else math.ceil(bars * self.level / 100)
        for i in range(bars):
            by1 = y0 + bh - 5 * self.u - i * (bh - 8 * self.u) / bars
            by0 = by1 - (bh - 8 * self.u) / bars + 2.5 * self.u
            cv.d.rectangle(self.px([(x0 + 5 * self.u, by0), (x0 + bw - 5 * self.u, by1)]),
                           fill=(S.BATT + (230,)) if i < lit else (S.INK + (25,)))


# ----------------------------------------------------------------------------------------------
# Scenes
# ----------------------------------------------------------------------------------------------

def landscape(c: Ctx):
    """Sun over rolling hills, a house with rooftop panels, trees, pylons and a battery."""
    S, cv, u, f = c.S, c.cv, c.u, c.f
    x, y, w, h = c.x, c.y, c.w, c.h
    horizon = y + h * 0.6
    sx, sy, sr = x + w * 0.13, y + h * 0.2, 30 * u
    c.sun(sx, sy, sr)
    # hills, back to front
    for base, amp, freq, phase, shade in ((0.0, 0.06, 2.1, 0.6, 0.18), (0.08, 0.05, 3.0, 2.0, 0.28), (0.2, 0.04, 2.4, 4.1, 0.4)):
        pts = []
        for i in range(0, 61):
            t = i / 60
            pts.append((x + w * t, horizon + h * base - h * amp * math.sin(freq * t * math.pi + phase)))
        pts += [(x + w, y + h), (x, y + h)]
        c.poly(pts, fill=S.mix(S.BG_TOP, S.BATT, shade) + (255,))
    # trees
    for tx, scale in ((x + w * 0.07, 1.0), (x + w * 0.2, 0.8), (x + w * 0.66, 0.7)):
        ty = horizon + h * 0.08
        cv.line([(tx, ty), (tx, ty - 26 * u * scale)], S.mix(S.BG_TOP, S.HOME, 0.55), 3 * u)
        cv.circle(tx, ty - 34 * u * scale, 15 * u * scale, fill=S.mix(S.BG_TOP, S.BATT, 0.62) + (255,))
    # house
    hx, gy = x + w * 0.44, horizon + h * 0.16
    hw, hh, rh = 140 * u, 74 * u, 58 * u
    c.poly([(hx - hw / 2, gy - hh), (hx + hw / 2, gy - hh), (hx + hw / 2, gy), (hx - hw / 2, gy)],
           fill=S.mix(S.BG_TOP, S.HOME, 0.3) + (255,), outline=S.HOME + (255,), width=1.4)
    peak, eave_l, eave_r = (hx, gy - hh - rh), (hx - hw / 2 - 12 * u, gy - hh), (hx + hw / 2 + 12 * u, gy - hh)
    c.poly([eave_l, peak, eave_r], fill=S.mix(S.BG_TOP, S.SOLAR2, 0.25) + (255,), outline=S.mix(S.SOLAR2, S.BG_TOP, 0.2) + (255,),
           width=1.2)
    # panels on the right roof slope
    def on_roof(t, inset):
        px, py = peak[0] + (eave_r[0] - peak[0]) * t, peak[1] + (eave_r[1] - peak[1]) * t
        return px - 9 * u * inset, py + 12 * u * inset
    for k in range(3):
        t0, t1 = 0.12 + k * 0.27, 0.12 + k * 0.27 + 0.24
        quad = [on_roof(t0, 0.3), on_roof(t1, 0.3), on_roof(t1, 1.6), on_roof(t0, 1.6)]
        c.poly(quad, fill=S.mix(S.BG_TOP, S.GRID, 0.55) + (255,), outline=S.SOLAR + (255,), width=1.0)
    panel = on_roof(0.5, 1.0)
    # door and a lit window
    cv.d.rectangle(c.px([(hx - 10 * u, gy - 34 * u), (hx + 10 * u, gy)]), fill=S.mix(S.BG_TOP, S.HOME, 0.6) + (255,))
    cv.d.rectangle(c.px([(hx - hw / 2 + 16 * u, gy - hh + 18 * u), (hx - hw / 2 + 46 * u, gy - hh + 42 * u)]),
                   fill=S.mix(S.SOLAR, S.BG_TOP, 0.25) + (255,))
    # battery cabinet
    bx0, bw, bh = hx - hw / 2 - 44 * u, 26 * u, 44 * u
    if c.has_battery:
        c.battery_box(bx0, gy - bh, bw, bh)
    # pylons and wires
    p1x, p2x = x + w * 0.8, x + w * 1.0 - 8 * u
    l1, r1 = c.pylon(p1x, horizon + h * 0.12, 112 * u)
    l2, r2 = c.pylon(p2x, horizon + h * 0.05, 84 * u, alpha=120)
    house_pt = (hx + hw / 2, gy - hh + 12 * u)
    wire = c.catenary(house_pt, l1, 16 * u)
    cv.line(wire, S.INK, 1, alpha=90)
    cv.line(c.catenary(r1, l2, 12 * u), S.INK, 1, alpha=70)
    # flows
    edge = (sx + sr * 1.2, sy + sr * 0.5)
    c.flow([edge, panel], S.SOLAR, f["s"])
    c.flow([panel, (hx, gy - hh * 0.45)], S.SOLAR, f["s2h"])
    c.grid_flow(wire)
    if c.has_battery:
        c.battery_flow([(bx0 + bw, gy - bh * 0.6), (hx - hw / 2, gy - bh * 0.6)])
    c.standard_tags({"solar": (sx, sy + sr + 46 * u, "m"), "home": (hx, gy - hh - rh - 34 * u, "m"),
                     "grid": (p1x, horizon + h * 0.12 - 112 * u - 34 * u, "m"),
                     "battery": (bx0 + bw / 2 - 10 * u, gy + 28 * u, "m")})


def house(c: Ctx):
    """An isometric house drawing: roof panels, battery cabinet, lattice pylon and wire."""
    S, cv, u, f = c.S, c.cv, c.u, c.f
    x, y, w, h = c.x, c.y, c.w, c.h
    U = 1.55 * u
    L, D, H, R = 120, 78, 56, 38
    cx, cy = x + w * 0.42, y + h * 0.58
    ox = cx - (L / 2 - D / 2) * 0.866 * U
    oy = cy - ((L / 2 + D / 2) * 0.5 - H / 2) * U

    def P(i, j, k):
        return (ox + (i - j) * 0.866 * U, oy + (i + j) * 0.5 * U - k * U)

    line = S.INK + (210,)
    face = S.INK + (10,)
    lw = 1.5
    c.poly([P(0, 0, H), P(L, 0, H), P(L, D / 2, H + R), P(0, D / 2, H + R)], fill=face, outline=line, width=lw)
    c.poly([P(L, 0, 0), P(L, D, 0), P(L, D, H), P(L, 0, H)], fill=S.INK + (16,), outline=line, width=lw)
    c.poly([P(L, 0, H), P(L, D, H), P(L, D / 2, H + R)], fill=S.INK + (16,), outline=line, width=lw)
    c.poly([P(0, D, 0), P(L, D, 0), P(L, D, H), P(0, D, H)], fill=face, outline=line, width=lw)
    c.poly([P(0, D, H), P(L, D, H), P(L, D / 2, H + R), P(0, D / 2, H + R)], fill=S.INK + (8,), outline=line, width=lw)
    # roof panels: 4 x 2 on the front roof plane
    def roof(i, s):  # s: 0 at the eave, 1 at the ridge
        return P(i, D - D / 2 * s, H + R * s)
    for a in range(4):
        for b in range(2):
            i0, i1 = 8 + a * 27, 8 + a * 27 + 23
            s0, s1 = 0.12 + b * 0.42, 0.12 + b * 0.42 + 0.36
            c.poly([roof(i0, s0), roof(i1, s0), roof(i1, s1), roof(i0, s1)], fill=S.SOLAR + (55,), outline=S.SOLAR + (230,),
                   width=1.0)
    panel = roof(L / 2, 0.5)
    # door and windows on the front face
    c.poly([P(L * 0.62, D, 0), P(L * 0.76, D, 0), P(L * 0.76, D, H * 0.62), P(L * 0.62, D, H * 0.62)], outline=line, width=1.2)
    for i0 in (10, 38):
        c.poly([P(i0, D, H * 0.42), P(i0 + 20, D, H * 0.42), P(i0 + 20, D, H * 0.8), P(i0, D, H * 0.8)],
               fill=S.SOLAR + (40,), outline=line, width=1.0)
    # battery cabinet beside the right wall
    if c.has_battery:
        bi0, bi1, bj0, bj1, bk = L + 10, L + 26, D * 0.5, D * 0.86, 36
        c.poly([P(bi1, bj0, 0), P(bi1, bj1, 0), P(bi1, bj1, bk), P(bi1, bj0, bk)], fill=S.BATT + (40,), outline=S.BATT + (255,), width=1.3)
        c.poly([P(bi0, bj1, 0), P(bi1, bj1, 0), P(bi1, bj1, bk), P(bi0, bj1, bk)], fill=S.BATT + (60,), outline=S.BATT + (255,), width=1.3)
        c.poly([P(bi0, bj0, bk), P(bi1, bj0, bk), P(bi1, bj1, bk), P(bi0, bj1, bk)], fill=S.BATT + (30,), outline=S.BATT + (255,), width=1.3)
        lvl = 0 if c.level is None else c.level / 100
        c.poly([P(bi1, bj0 + 4, 4), P(bi1, bj0 + 4 + (bj1 - bj0 - 8) * lvl, 4), P(bi1, bj0 + 4 + (bj1 - bj0 - 8) * lvl, bk - 6),
                P(bi1, bj0 + 4, bk - 6)], fill=S.BATT + (200,))
    # dimension line under the front face
    d0, d1 = P(0, D + 16, 0), P(L, D + 16, 0)
    cv.line([d0, d1], S.INK, 1, alpha=110)
    for p in (d0, d1):
        cv.line([(p[0], p[1] - 6 * u), (p[0], p[1] + 6 * u)], S.INK, 1, alpha=110)
    # pylon and wire
    pb = P(L + 70, -24, 0)
    l1, r1 = c.pylon(pb[0], pb[1], 120 * u)
    wire = c.catenary(P(L, 0, H), l1, 14 * u)
    cv.line(wire, S.INK, 1, alpha=110)
    # sun (line art)
    sx, sy, sr = x + w * 0.12, y + h * 0.16, 24 * u
    c.sun(sx, sy, sr, style="line")
    c.flow([(sx + sr * 1.3, sy + sr * 0.6), panel], S.SOLAR, f["s"])
    c.flow([panel, P(L * 0.3, D, H * 0.3)], S.SOLAR, f["s2h"])
    c.grid_flow(wire)
    if c.has_battery:
        c.battery_flow([P(L + 10, D * 0.68, 18), P(L, D * 0.68, 18)])
    front = P(L * 0.45, D + 16, 0)
    c.standard_tags({"solar": (sx, sy + sr + 50 * u, "m"), "home": (front[0], front[1] + 34 * u, "m"),
                     "grid": (pb[0], pb[1] - 120 * u - 30 * u, "m"),
                     "battery": (P(L + 18, D, 0)[0] + 34 * u, P(L + 18, D, 0)[1] + 22 * u, "m")})


def _cubic(p0, p1, p2, p3, n=36):
    out = []
    for i in range(n + 1):
        t = i / n
        a, b, cc, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3
        out.append((a * p0[0] + b * p1[0] + cc * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + cc * p2[1] + d * p3[1]))
    return out


def sankey(c: Ctx):
    """Ribbons as wide as the power they carry, from sources (left) to uses (right)."""
    S, cv, u, f = c.S, c.cv, c.u, c.f
    x, y, w, h = c.x, c.y, c.w, c.h
    links = [("solar", "home", f["s2h"], S.SOLAR), ("solar", "battery", f["s2b"], S.SOLAR), ("solar", "grid", f["s2g"], S.SOLAR),
             ("grid", "home", f["g2h"], S.GRID), ("grid", "battery", f["g2b"], S.GRID),
             ("battery", "home", f["b2h"], S.BATT), ("battery", "grid", f["b2g"], S.BATT)]
    links = [l for l in links if l[2] > 50]
    src_tot = {k: sum(l[2] for l in links if l[0] == k) for k in ("solar", "grid", "battery")}
    dst_tot = {k: sum(l[2] for l in links if l[1] == k) for k in ("home", "battery", "grid")}
    colors = {"solar": S.SOLAR, "grid": S.GRID, "battery": S.BATT, "home": S.HOME}
    total = max(sum(src_tot.values()), sum(dst_tot.values()), 1)
    gap = 18 * u
    srcs = [k for k in ("solar", "grid", "battery") if src_tot[k] > 0] or ["solar"]
    dsts = [k for k in ("home", "battery", "grid") if dst_tot[k] > 0] or ["home"]
    usable = h * 0.82 - gap * (max(len(srcs), len(dsts)) - 1)
    scale = usable / total
    bar = 12 * u
    lx, rx = x + w * 0.3, x + w * 0.7 - bar

    def stack(keys, totals):
        heights = {k: max(totals[k] * scale, 4 * u) for k in keys}
        y0 = y + h * 0.5 - (sum(heights.values()) + gap * (len(keys) - 1)) / 2
        out = {}
        for k in keys:
            out[k] = [y0, y0 + heights[k], y0]  # top, bottom, next free offset
            y0 += heights[k] + gap
        return out

    left, right = stack(srcs, src_tot), stack(dsts, dst_tot)
    for k, (t, b, _) in left.items():
        cv.d.rounded_rectangle(c.px([(lx, t), (lx + bar, b)]), radius=3 * S.SS, fill=colors[k] + (255,))
    for k, (t, b, _) in right.items():
        cv.d.rounded_rectangle(c.px([(rx, t), (rx + bar, b)]), radius=3 * S.SS, fill=colors[k] + (255,))
    mx = (lx + bar + rx) / 2
    for src, dst, watts, colour in links:
        hgt = watts * scale
        s0 = left[src][2]
        left[src][2] += hgt
        d0 = right[dst][2]
        right[dst][2] += hgt
        top = _cubic((lx + bar, s0), (mx, s0), (mx, d0), (rx, d0))
        bottom = _cubic((rx, d0 + hgt), (mx, d0 + hgt), (mx, s0 + hgt), (lx + bar, s0 + hgt))
        c.poly(top + bottom, fill=colour + (95,))
        centre = _cubic((lx + bar, s0 + hgt / 2), (mx, s0 + hgt / 2), (mx, d0 + hgt / 2), (rx, d0 + hgt / 2))
        c.flow(centre, colour, watts)
    if not links:
        cv.text((x + w / 2, y + h * 0.5), "NO ENERGY FLOWING", max(int(14 * u), 10), S.MUTED, "semi", anchor="mm", spacing=2)
    at = {k: (lx - 14 * u, (t + b) / 2, "r") for k, (t, b, _) in left.items()}
    for k, (t, b, _) in right.items():
        at.setdefault(k if k != "grid" or "grid" not in at else "grid", (rx + bar + 14 * u, (t + b) / 2, "l"))
    if "home" not in at:
        at["home"] = (rx + bar + 14 * u, y + h / 2, "l")
    c.standard_tags(at)


def orbit(c: Ctx):
    """An orrery: home as a ringed planet; sun, grid and battery on orbits beaming inward."""
    S, cv, u, f = c.S, c.cv, c.u, c.f
    x, y, w, h = c.x, c.y, c.w, c.h
    cx, cy = x + w * 0.5, y + h * 0.54
    for rx_, ry_ in ((w * 0.42, h * 0.38), (w * 0.27, h * 0.25)):
        box = [(cx - rx_) * S.SS, (cy - ry_) * S.SS, (cx + rx_) * S.SS, (cy + ry_) * S.SS]
        for a in range(0, 360, 6):
            cv.d.arc(box, a, a + 3, fill=S.CYAN + (90,), width=max(int(1.3 * S.SS), 1))

    def on(rx_, ry_, deg):
        a = math.radians(deg)
        return cx + math.cos(a) * rx_, cy + math.sin(a) * ry_

    sun = on(w * 0.42, h * 0.38, 215)
    grid = on(w * 0.42, h * 0.38, 335)
    batt = on(w * 0.27, h * 0.25, 125)
    R = 34 * u

    def beam(a, b, bow=0.22):
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        dx, dy = b[0] - a[0], b[1] - a[1]
        ctrl = (mx - dy * bow, my + dx * bow)
        return c.cv.bezier(a, ctrl, b)

    home = (cx, cy)
    c.flow(beam(sun, home), S.SOLAR, f["s2h"])
    c.flow(beam(sun, grid, -0.18), S.SOLAR, f["s2g"])
    if c.has_battery:
        c.flow(beam(sun, batt), S.SOLAR, f["s2b"])
        c.flow(beam(batt, home), S.BATT, f["b2h"])
        c.flow(beam(grid, batt), S.GRID, f["g2b"])
    c.flow(beam(grid, home, -0.2), S.GRID, f["g2h"])
    # home planet with a ring (back half, planet, front half)
    ring = [(cx - R * 2.0) * S.SS, (cy - R * 0.5) * S.SS, (cx + R * 2.0) * S.SS, (cy + R * 0.5) * S.SS]
    cv.d.arc(ring, 180, 360, fill=S.CYAN + (200,), width=max(int(2.4 * u * S.SS), 1))
    for i in range(10, 0, -1):
        cv.circle(cx - R * 0.25 * (1 - i / 10), cy - R * 0.25 * (1 - i / 10), R * i / 10,
                  fill=S.mix(S.mix(S.BG_TOP, S.HOME, 0.35), S.mix(S.HOME, (255, 255, 255), 0.3), 1 - i / 10) + (255,))
    cv.d.arc(ring, 0, 180, fill=S.CYAN + (230,), width=max(int(2.4 * u * S.SS), 1))
    cv.icon_home(cx, cy - R * 0.05, R * 0.8, color=S.TEXT)
    c.sun(*sun, 26 * u)
    cv.circle(*grid, 22 * u, fill=S.NODE + (255,), outline=S.GRID + (255,), width=2, glow=5)
    cv.icon_grid(grid[0], grid[1], 22 * u)
    if c.has_battery:
        cv.circle(*batt, 20 * u, fill=S.NODE + (255,), outline=S.BATT + (255,), width=2, glow=5)
        cv.icon_battery(batt[0], batt[1], 22 * u, c.level)
        if c.level is not None:
            cv.arc(batt[0], batt[1], 26 * u, -90, -90 + 360 * c.level / 100, 3, S.BATT, S.BATT2, glow=3, plain=True)
    c.standard_tags({"solar": (sun[0], sun[1] + 64 * u, "m"), "grid": (grid[0], grid[1] + 50 * u, "m"),
                     "battery": (batt[0] - 60 * u, batt[1] + 18 * u, "r"), "home": (cx, cy + R + 44 * u, "m")})


def prism(c: Ctx):
    """The sun's beam enters a prism and leaves as coloured rays to home, battery and grid
    (only rays that reach something are drawn)."""
    S, cv, u, f = c.S, c.cv, c.u, c.f
    x, y, w, h = c.x, c.y, c.w, c.h
    sun = (x + w * 0.11, y + h * 0.2)
    c.sun(*sun, 26 * u)
    pc = (x + w * 0.42, y + h * 0.52)
    side = min(w, h) * 0.42
    top = (pc[0], pc[1] - side * 0.577)
    bl = (pc[0] - side / 2, pc[1] + side * 0.289)
    br = (pc[0] + side / 2, pc[1] + side * 0.289)
    entry = ((top[0] + bl[0]) / 2, (top[1] + bl[1]) / 2)
    exit_ = ((top[0] + br[0]) / 2, (top[1] + br[1]) / 2)
    total = max(f["s"], 1)

    def beam(a, b, colour, watts, alpha=60):
        width = 2 + 12 * u * min(watts / total, 1) if watts > 50 else 1.2
        cv.line([a, b], colour, width + 4 * u, alpha=alpha, glow=8 if watts > 50 else 0)
        c.flow([a, b], colour, watts)

    beam((sun[0] + 26 * u, sun[1] + 14 * u), entry, S.mix(S.SOLAR, (255, 255, 255), 0.55), f["s"], alpha=90)
    homep = (x + w * 0.84, y + h * 0.2)
    battp = (x + w * 0.88, y + h * 0.56)
    gridp = (x + w * 0.66, y + h * 0.88)
    beam(exit_, homep, S.HOME, f["s2h"])
    if c.has_battery:
        beam(exit_, battp, S.BATT, f["s2b"])
        beam(battp, homep, S.BATT, f["b2h"], alpha=40)
    beam(exit_, gridp, S.GRID, f["s2g"])
    beam(gridp, homep, S.GRID, f["g2h"], alpha=40)
    # the prism itself (glass)
    c.poly([top, br, bl], fill=S.INK + (22,), outline=S.INK + (190,), width=1.8)
    cv.line([entry, exit_], S.mix(S.SOLAR, (255, 255, 255), 0.6), 2 * u, alpha=120)
    cv.line([(top[0] - side * 0.05, top[1] + side * 0.16), (top[0] - side * 0.16, top[1] + side * 0.38)], (255, 255, 255), 1.4, alpha=90)
    for p, colour, icon in ((homep, S.HOME, cv.icon_home), (gridp, S.GRID, cv.icon_grid)):
        cv.circle(*p, 20 * u, fill=S.NODE + (255,), outline=colour + (255,), width=2, glow=5)
        icon(p[0], p[1], 20 * u)
    if c.has_battery:
        cv.circle(*battp, 20 * u, fill=S.NODE + (255,), outline=S.BATT + (255,), width=2, glow=5)
        cv.icon_battery(battp[0], battp[1], 20 * u, c.level)
    c.standard_tags({"solar": (sun[0], sun[1] + 62 * u, "m"), "home": (homep[0] - 30 * u, homep[1], "r"),
                     "battery": (battp[0], battp[1] + 52 * u, "m"), "grid": (gridp[0] - 30 * u, gridp[1], "r")})


def _bolt(a, b, rnd, depth=5, spread=0.2):
    pts = [a, b]
    disp = math.dist(a, b) * spread
    for _ in range(depth):
        out = [pts[0]]
        for p, q in zip(pts, pts[1:]):
            mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
            dx, dy = q[0] - p[0], q[1] - p[1]
            n = math.hypot(dx, dy) or 1
            off = rnd.uniform(-disp, disp)
            out += [(mx - dy / n * off, my + dx / n * off), q]
        pts, disp = out, disp * 0.55
    return pts


def plasma(c: Ctx):
    """Electrode spheres with lightning arcs; the bolts are redrawn on every refresh."""
    S, cv, u, f = c.S, c.cv, c.u, c.f
    nodes, r = c.nodes, c.r
    seed = int((c.v.get("_phase") or 0) * 1000) + int(c.v["now"].timestamp() // 60)
    pairs = [("solar", "home", f["s2h"], S.SOLAR), ("solar", "grid", f["s2g"], S.SOLAR), ("solar", "battery", f["s2b"], S.SOLAR),
             ("grid", "home", f["g2h"], S.GRID), ("battery", "home", f["b2h"], S.BATT),
             ("grid", "battery", f["g2b"], S.GRID), ("battery", "grid", f["b2g"], S.BATT)]
    for k, (a, b, watts, colour) in enumerate(pairs):
        if (a == "battery" or b == "battery") and not c.has_battery:
            continue
        pa, pb = nodes[a], nodes[b]
        dx, dy = pb[0] - pa[0], pb[1] - pa[1]
        n = math.hypot(dx, dy) or 1
        p0 = (pa[0] + dx / n * r * 0.9, pa[1] + dy / n * r * 0.9)
        p1 = (pb[0] - dx / n * r * 0.9, pb[1] - dy / n * r * 0.9)
        if watts <= 50:
            cv.line([p0, p1], S.INK, 1, alpha=18)
            continue
        rnd = random.Random(seed * 31 + k)
        width = 1.2 + min(watts / 1800, 2.2)
        for strand in range(2 if watts > 1500 else 1):
            pts = _bolt(p0, p1, rnd)
            cv.line(pts, colour, width + 2.5, glow=10 + width * 3, alpha=90)
            cv.line(pts, S.mix(colour, (255, 255, 255), 0.75), width * 0.7, alpha=255)
            mid = pts[len(pts) // 2 + rnd.randrange(-6, 6)]
            ang = math.atan2(dy, dx) + rnd.choice((-1, 1)) * rnd.uniform(0.5, 1.1)
            fork = _bolt(mid, (mid[0] + math.cos(ang) * n * 0.22, mid[1] + math.sin(ang) * n * 0.22), rnd, depth=3)
            cv.line(fork, colour, width * 0.6, glow=6, alpha=160)
    for key, colour, icon in (("solar", S.SOLAR, cv.icon_sun), ("grid", S.GRID, cv.icon_grid), ("home", S.HOME, cv.icon_home),
                              ("battery", S.BATT, None)):
        if key == "battery" and not c.has_battery:
            continue
        x, y = nodes[key]
        for i in range(12, 0, -1):  # metal sphere with a coloured rim
            t = 1 - i / 12
            cv.circle(x - r * 0.22 * t, y - r * 0.28 * t, r * 0.8 * i / 12,
                      fill=S.mix(S.mix(S.NODE, colour, 0.25), S.mix(S.INK, colour, 0.25), t * 0.6) + (255,))
        cv.circle(x, y, r * 0.8, outline=colour + (255,), width=2.2, glow=8)
        if icon:
            icon(x, y - r * 0.32, r * 0.42)
        else:
            cv.icon_battery(x, y - r * 0.32, r * 0.42, c.level)
    c.standard_tags({k: (nodes[k][0], nodes[k][1] + r * 0.18, "m") for k in ("solar", "grid", "home", "battery")})
    c.tags = [(tx, ty, "", value, sub, subc, a) for tx, ty, _, value, sub, subc, a in c.tags]  # value inside the sphere
    for key, label in (("solar", "SOLAR"), ("grid", "GRID"), ("home", "HOME"), ("battery", "BATTERY")):
        if key == "battery" and not c.has_battery:
            continue
        x, y = nodes[key]
        cv.text((x, y + r * 0.8 + 14 * c.u), label, max(int(11 * c.u), 9), S.MUTED, "semi", anchor="mm", spacing=2)


def metro(c: Ctx):
    """A transit map: solar, grid and battery lines running into a home interchange."""
    S, cv, u, f = c.S, c.cv, c.u, c.f
    nodes = c.nodes
    sx, sy = nodes["solar"]
    gx, gy = nodes["grid"]
    hx, hy = nodes["home"]
    bx, by = nodes["battery"]
    off = 9 * u
    lw = 8 * u + 1

    def route(points):
        return [(px, py) for px, py in points]

    lines = []
    d = (hy - off) - sy
    lines.append(("solar", "home", route([(sx, sy), (sx + d, hy - off), (hx, hy - off)]), S.SOLAR, f["s2h"]))
    lines.append(("grid", "home", route([(gx, gy), (hx, gy)]), S.GRID, f["g2h"] + f["s2g"]))
    if c.has_battery:
        d2 = by - (hy + off)
        lines.append(("battery", "home", route([(bx, by), (bx + d2, hy + off), (hx, hy + off)]), S.BATT, f["b2h"] + f["s2b"]))
        d3 = by - gy
        lines.append(("grid", "battery", route([(gx, gy), (gx + d3 * 0.6, gy + d3 * 0.6), (bx - 30 * u, by), (bx, by)]),
                      S.GRID, f["g2b"] + f["b2g"]))
    for _, _, pts, colour, watts in lines:
        active = watts > 50
        base = colour if active else S.mix(S.BG_TOP, colour, 0.3)
        cv.d.line(c.px(pts), fill=base + (255,), width=int(lw * S.SS), joint="curve")
        for p in (pts[0], pts[-1]):
            cv.circle(*p, lw / 2, fill=base + (255,))
    # flow pattern on the active lines, in the direction energy is going
    c.flow(lines[0][2], S.SOLAR, f["s2h"])
    c.flow(lines[1][2], S.GRID, f["g2h"], reverse=False) if f["g2h"] > 50 else c.flow(lines[1][2], S.GRID, f["s2g"], reverse=True)
    if c.has_battery:
        if f["b2h"] > 50:
            c.flow(lines[2][2], S.BATT, f["b2h"])
        else:
            c.flow(lines[2][2], S.BATT, f["s2b"], reverse=True)
    # stations and the interchange
    for key in ("solar", "grid", "battery"):
        if key == "battery" and not c.has_battery:
            continue
        x, y = nodes[key]
        cv.circle(x, y, 11 * u + 2, fill=(255, 255, 255, 255), outline=S.mix(S.BG_TOP, S.INK, 0.15) + (255,), width=3 * u + 1)
    cv.d.rounded_rectangle(c.px([(hx - 13 * u, hy - off - 13 * u), (hx + 13 * u, hy + off + 13 * u)]), radius=13 * u * S.SS,
                           fill=(255, 255, 255, 255), outline=S.mix(S.BG_TOP, S.INK, 0.15) + (255,), width=int((3 * u + 1) * S.SS))
    # line badges
    for (key, letter, colour) in (("solar", "S", S.SOLAR), ("grid", "G", S.GRID), ("battery", "B", S.BATT)):
        if key == "battery" and not c.has_battery:
            continue
        x, y = nodes[key]
        bx0 = x - 34 * u if key != "solar" else x - 13 * u
        by0 = y - 13 * u if key != "solar" else y - 40 * u
        cv.d.rounded_rectangle(c.px([(bx0, by0), (bx0 + 22 * u, by0 + 22 * u)]), radius=5 * u * S.SS, fill=colour + (255,))
        cv.text((bx0 + 11 * u, by0 + 11 * u), letter, max(int(14 * u), 9), S.BG_TOP, "bold", anchor="mm")
    c.standard_tags({"solar": (sx + 22 * u, sy - 26 * u, "l"), "grid": (gx, gy + 46 * u, "m"),
                     "home": (hx, hy + off + 50 * u, "m"), "battery": (bx + 24 * u, by + 30 * u, "l")})


def circuit(c: Ctx):
    """A circuit board: a chip per source, copper traces with vias, current along them."""
    S, cv, u, f = c.S, c.cv, c.u, c.f
    x, y, w, h = c.x, c.y, c.w, c.h
    nodes, r = c.nodes, c.r
    cv.d.rounded_rectangle(c.px([(x, y), (x + w, y + h)]), radius=10 * u * S.SS, fill=S.mix(S.BG_TOP, S.BATT, 0.1) + (255,),
                           outline=S.mix(S.BG_TOP, S.BATT, 0.35) + (255,), width=max(int(1.4 * S.SS), 1))
    rnd = random.Random(5)
    for _ in range(26):  # background traces
        px0, py0 = x + rnd.uniform(0.04, 0.96) * w, y + rnd.uniform(0.06, 0.94) * h
        length = rnd.uniform(20, 60) * u
        dirx, diry = rnd.choice(((1, 0), (0, 1), (0.7, 0.7), (0.7, -0.7)))
        p1 = (px0 + dirx * length, py0 + diry * length)
        cv.line([(px0, py0), p1], S.INK, 1.2, alpha=22)
        cv.circle(*p1, 2.2 * u + 0.6, outline=S.INK + (40,), width=1)
    copper = S.mix(S.SOLAR2, S.BG_TOP, 0.35)
    corners = []

    def trace(a, b, horizontal_first=True):
        (ax, ay), (bx, by) = a, b
        ch = 14 * u
        if horizontal_first:
            cx_, cy_ = bx, ay
            p = [(ax, ay), (cx_ - ch * (1 if bx > ax else -1), ay), (bx, ay + ch * (1 if by > ay else -1)), (bx, by)]
        else:
            cx_, cy_ = ax, by
            p = [(ax, ay), (ax, by - ch * (1 if by > ay else -1)), (ax + ch * (1 if bx > ax else -1), by), (bx, by)]
        corners.extend([p[1], p[2]])
        cv.line(p, copper, 4.2 * u + 1, alpha=255)
        return p

    S_, G, H, B = nodes["solar"], nodes["grid"], nodes["home"], nodes["battery"]
    t_sh = trace(S_, H)
    t_sg = trace(S_, G)
    t_gh = trace((G[0], G[1] + 8 * u), (H[0], H[1] + 8 * u))
    t_sb = [(S_[0], S_[1]), (B[0], B[1])]
    if c.has_battery:
        cv.line(t_sb, copper, 4.2 * u + 1, alpha=255)
        t_bh = trace(B, H, horizontal_first=True)
    for p in corners:
        cv.circle(*p, 4 * u + 1, fill=S.mix(S.BG_TOP, S.BATT, 0.1) + (255,), outline=copper + (255,), width=1.6 * u + 0.6)
    c.flow(t_sh, S.SOLAR, f["s2h"])
    c.flow(t_sg, S.SOLAR, f["s2g"])
    c.flow(t_gh, S.GRID, f["g2h"])
    if c.has_battery:
        c.flow(t_sb, S.SOLAR, f["s2b"])
        c.flow(t_bh, S.BATT, f["b2h"])
    for key, label in (("solar", "SOLAR"), ("grid", "GRID"), ("home", "HOME"), ("battery", "BATT")):
        if key == "battery" and not c.has_battery:
            continue
        x0, y0 = nodes[key]
        cw, chh = r * 2.3, r * 1.3
        for i in range(8):  # pins
            pxp = x0 - cw / 2 + cw * (i + 0.5) / 8
            for sgn in (-1, 1):
                ya, yb = sorted((y0 + sgn * chh / 2, y0 + sgn * (chh / 2 + 6 * u)))
                cv.d.rectangle(c.px([(pxp - 2.4 * u, ya), (pxp + 2.4 * u, yb)]), fill=S.mix(S.CYAN, S.SOLAR, 0.5) + (230,))
        cv.d.rounded_rectangle(c.px([(x0 - cw / 2, y0 - chh / 2), (x0 + cw / 2, y0 + chh / 2)]), radius=3 * u * S.SS,
                               fill=S.NODE + (255,), outline=S.INK + (70,), width=max(int(1.2 * S.SS), 1))
        cv.circle(x0 - cw / 2 + 8 * u, y0 - chh / 2 + 8 * u, 2.8 * u, fill=S.INK + (60,))
        cv.text((x0 - cw / 2 + 16 * u, y0 - chh / 2 + 10 * u), label, max(int(9 * u), 8), S.MUTED, "semi", anchor="lm", spacing=1)
    c.standard_tags({k: (nodes[k][0], nodes[k][1] + 6 * u, "m") for k in ("solar", "grid", "home", "battery")})
    c.tags = [(tx, ty, "", value, sub, subc, a) for tx, ty, _, value, sub, subc, a in c.tags]  # label is on the chip


SCENES = {"landscape": landscape, "house": house, "sankey": sankey, "orbit": orbit, "prism": prism, "plasma": plasma,
          "metro": metro, "circuit": circuit}


def draw(cv, v, nodes, r, compact) -> bool:
    """Draw the current style's scene; False if the style uses the classic node diagram."""
    S = _screens()
    scene = SCENES.get(S.SCENE)
    if scene is None:
        return False
    c = Ctx(cv, v, nodes, r, compact)
    scene(c)
    if S.PIXEL_ART:  # redraw the illustration as chunky pixel art (labels stay sharp)
        box = [int(v_ * S.SS) for v_ in (c.x, c.y, c.x1, c.y1)]
        region = cv.img.crop(box)
        block = max(int(5 * c.u * S.SS), 4)
        small = region.resize((max(region.width // block, 1), max(region.height // block, 1)), Image.BOX)
        cv.img.paste(small.resize(region.size, Image.NEAREST), box[:2])
    c.draw_tags()
    return True
