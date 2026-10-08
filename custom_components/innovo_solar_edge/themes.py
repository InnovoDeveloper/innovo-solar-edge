"""Display themes for the dashboard screens: 10 looks, each with a dark and a light mode.

A theme sets the fonts, card style, background texture and glow; each mode sets the
colours. screens.apply_theme() swaps these in for one render. "neon-dark" is the
original look.

Card styles: glass (translucent, thin line), flat (solid, no line), outline (line only),
plate (double line + corner rivets), console (coloured side bar and top bar).
Textures: dots, none, scanlines, grid, grain, aurora.
"""

# Fonts (SIL Open Font License, see fonts/OFL-*.txt): bold / semi / medium files
_FONTS = {
    "rajdhani": ("Rajdhani-Bold.ttf", "Rajdhani-SemiBold.ttf", "Rajdhani-Medium.ttf"),
    "vt323": ("VT323-Regular.ttf", "VT323-Regular.ttf", "VT323-Regular.ttf"),
    "inter": ("Inter-Bold.ttf", "Inter-SemiBold.ttf", "Inter-Medium.ttf"),
    "playfair": ("PlayfairDisplay-Bold.ttf", "PlayfairDisplay-SemiBold.ttf", "PlayfairDisplay-Medium.ttf"),
    "montserrat": ("Montserrat-Bold.ttf", "Montserrat-SemiBold.ttf", "Montserrat-Medium.ttf"),
    "cinzel": ("Cinzel-Bold.ttf", "Cinzel-SemiBold.ttf", "Cinzel-Medium.ttf"),
    "quicksand": ("Quicksand-Bold.ttf", "Quicksand-SemiBold.ttf", "Quicksand-Medium.ttf"),
    "nunito": ("Nunito-Bold.ttf", "Nunito-SemiBold.ttf", "Nunito-Medium.ttf"),
    "antonio": ("Antonio-Bold.ttf", "Antonio-SemiBold.ttf", "Antonio-Medium.ttf"),
    "plexmono": ("IBMPlexMono-Bold.ttf", "IBMPlexMono-SemiBold.ttf", "IBMPlexMono-Medium.ttf"),
}

WHITE, BLACK = (255, 255, 255), (0, 0, 0)


def _mode(bg_top, bg_bottom, text, muted, dim, accent, solar, solar2, home, grid, batt, batt2, red,
          card_fill, card_line, washes, ink=WHITE, glow=1.0):
    return {"BG_TOP": bg_top, "BG_BOTTOM": bg_bottom, "TEXT": text, "MUTED": muted, "DIM": dim,
            "CYAN": accent, "SOLAR": solar, "SOLAR2": solar2, "HOME": home, "GRID": grid,
            "BATT": batt, "BATT2": batt2, "GREEN": batt, "RED": red, "INK": ink,
            "CARD_FILL": card_fill, "CARD_LINE": card_line, "WASHES": washes, "GLOW": glow}


THEMES = {
    "neon": {
        "label": "Neon", "blurb": "Deep navy, neon glows - the original Innovo look",
        "font": "rajdhani", "scale": 1.0, "card": "glass", "radius": 1.0, "texture": "dots",
        "dark": _mode((6, 9, 18), (10, 17, 32), (236, 241, 247), (128, 142, 162), (70, 82, 100), (0, 229, 255),
                      (255, 196, 0), (255, 120, 0), (178, 140, 255), (64, 156, 255), (0, 232, 130), (0, 200, 255),
                      (255, 82, 102), (255, 255, 255, 12), (255, 255, 255, 30), ((0, 20, 38), (22, 8, 40))) | {"NODE": (14, 20, 34)},
        "light": _mode((240, 244, 250), (226, 233, 244), (16, 24, 40), (88, 102, 126), (150, 160, 178), (0, 145, 200),
                       (226, 150, 0), (226, 92, 0), (118, 86, 226), (28, 108, 220), (0, 164, 98), (0, 140, 200),
                       (214, 40, 70), (255, 255, 255, 190), (16, 24, 40, 26), ((180, 225, 245), (225, 210, 245)),
                       ink=BLACK, glow=0),
    },
    "retro": {
        "label": "Retro Arcade", "blurb": "80s synthwave, CRT scanlines, pixel type",
        "font": "vt323", "scale": 1.32, "card": "outline", "radius": 0.4, "texture": "scanlines",
        "dark": _mode((20, 4, 36), (44, 8, 58), (255, 236, 255), (196, 128, 226), (112, 60, 140), (0, 255, 240),
                      (255, 220, 0), (255, 96, 0), (255, 70, 210), (70, 150, 255), (60, 255, 150), (0, 255, 240),
                      (255, 60, 100), (255, 70, 210, 14), (255, 70, 210, 110), ((60, 0, 70), (0, 40, 70)), glow=1.3),
        "light": _mode((255, 240, 252), (255, 218, 242), (62, 10, 84), (148, 66, 160), (200, 150, 210), (200, 0, 160),
                       (214, 150, 0), (230, 70, 0), (200, 0, 150), (40, 90, 220), (0, 160, 90), (0, 150, 170),
                       (220, 30, 80), (255, 255, 255, 150), (200, 0, 160, 90), ((255, 200, 240), (200, 230, 255)),
                       ink=BLACK, glow=0),
    },
    "modern": {
        "label": "Modern", "blurb": "Flat, airy and clean",
        "font": "inter", "scale": 0.9, "card": "flat", "radius": 1.25, "texture": "none",
        "dark": _mode((22, 24, 28), (28, 30, 36), (240, 242, 245), (142, 148, 158), (84, 90, 100), (99, 140, 255),
                      (255, 184, 0), (255, 128, 30), (167, 139, 250), (96, 165, 250), (52, 211, 153), (56, 189, 248),
                      (248, 113, 113), (255, 255, 255, 12), (0, 0, 0, 0), ((26, 30, 40), (30, 26, 40)), glow=0.25),
        "light": _mode((247, 248, 250), (240, 242, 246), (17, 24, 39), (107, 114, 128), (190, 196, 206), (59, 110, 245),
                       (234, 160, 0), (234, 100, 20), (124, 92, 240), (37, 120, 235), (16, 170, 120), (14, 150, 220),
                       (220, 60, 60), (255, 255, 255, 245), (0, 0, 0, 0), ((228, 236, 252), (240, 232, 252)),
                       ink=BLACK, glow=0),
    },
    "classic": {
        "label": "Classic", "blurb": "Elegant serif, navy and gold",
        "font": "playfair", "scale": 0.94, "card": "plate", "radius": 0.35, "texture": "none",
        "dark": _mode((14, 22, 40), (20, 30, 54), (245, 238, 222), (186, 170, 140), (110, 104, 92), (212, 175, 55),
                      (232, 184, 70), (205, 130, 50), (186, 160, 214), (130, 168, 214), (126, 194, 146), (130, 190, 200),
                      (204, 88, 82), (255, 255, 255, 8), (212, 175, 55, 80), ((26, 36, 60), (40, 34, 30)), glow=0.2),
        "light": _mode((250, 245, 232), (240, 232, 212), (30, 36, 58), (112, 100, 80), (190, 178, 150), (156, 116, 26),
                       (190, 136, 20), (176, 92, 30), (112, 82, 150), (40, 84, 150), (52, 128, 82), (40, 120, 140),
                       (176, 50, 50), (255, 255, 255, 120), (156, 116, 26, 120), ((250, 236, 200), (232, 226, 240)),
                       ink=BLACK, glow=0),
    },
    "alhazen": {
        "label": "Alhazen", "blurb": "Monochrome and precise, one red accent",
        "font": "montserrat", "scale": 0.88, "card": "flat", "radius": 0.7, "texture": "none",
        "dark": _mode((8, 8, 9), (14, 14, 16), (242, 242, 242), (150, 150, 150), (78, 78, 82), (232, 33, 39),
                      (236, 236, 236), (150, 150, 150), (186, 186, 190), (120, 120, 126), (76, 217, 100), (150, 220, 255),
                      (232, 33, 39), (255, 255, 255, 9), (0, 0, 0, 0), ((22, 22, 24), (26, 14, 14)), glow=0.3),
        "light": _mode((246, 246, 246), (236, 236, 236), (20, 20, 20), (112, 112, 112), (190, 190, 190), (210, 24, 30),
                       (40, 40, 40), (120, 120, 120), (84, 84, 88), (150, 150, 156), (30, 168, 76), (40, 130, 200),
                       (210, 24, 30), (255, 255, 255, 235), (0, 0, 0, 0), ((236, 236, 240), (244, 230, 230)),
                       ink=BLACK, glow=0),
    },
    "steampunk": {
        "label": "Steampunk", "blurb": "Brass, copper and rivets",
        "font": "cinzel", "scale": 0.86, "card": "plate", "radius": 0.25, "texture": "grain",
        "dark": _mode((30, 21, 14), (44, 30, 19), (242, 222, 184), (196, 156, 104), (112, 86, 60), (205, 127, 50),
                      (232, 182, 82), (204, 112, 42), (176, 126, 94), (112, 156, 144), (122, 176, 120), (110, 160, 160),
                      (196, 72, 50), (0, 0, 0, 50), (181, 140, 70, 170), ((60, 36, 14), (40, 30, 10)), glow=0.6),
        "light": _mode((242, 228, 198), (226, 206, 168), (62, 40, 20), (122, 90, 54), (180, 150, 110), (150, 84, 30),
                       (176, 120, 20), (170, 80, 20), (130, 80, 56), (52, 110, 100), (60, 122, 64), (50, 110, 120),
                       (160, 50, 30), (255, 244, 214, 110), (120, 80, 30, 160), ((250, 226, 180), (220, 200, 160)),
                       ink=BLACK, glow=0),
    },
    "ethereal": {
        "label": "Ethereal", "blurb": "Soft pastels and frosted glass",
        "font": "quicksand", "scale": 0.98, "card": "glass", "radius": 1.75, "texture": "aurora",
        "dark": _mode((26, 18, 46), (16, 26, 50), (246, 240, 255), (184, 174, 214), (110, 100, 150), (206, 176, 255),
                      (255, 212, 156), (255, 168, 176), (214, 176, 255), (156, 204, 255), (156, 240, 212), (170, 220, 255),
                      (255, 140, 172), (255, 255, 255, 22), (255, 255, 255, 46), ((34, 14, 46), (10, 30, 48)), glow=1.0),
        "light": _mode((250, 244, 255), (236, 244, 255), (62, 50, 92), (130, 120, 162), (196, 188, 220), (150, 110, 230),
                       (230, 160, 80), (230, 120, 140), (160, 110, 230), (80, 140, 230), (50, 180, 150), (90, 160, 230),
                       (220, 80, 120), (255, 255, 255, 150), (150, 110, 230, 40), ((255, 210, 240), (200, 230, 255)),
                       ink=BLACK, glow=0),
    },
    "eco": {
        "label": "Eco", "blurb": "Leaf greens and natural tones",
        "font": "nunito", "scale": 0.92, "card": "glass", "radius": 1.4, "texture": "dots",
        "dark": _mode((10, 26, 18), (14, 38, 26), (236, 248, 238), (142, 182, 152), (78, 110, 88), (124, 222, 124),
                      (250, 212, 84), (240, 152, 44), (196, 164, 116), (112, 172, 212), (92, 222, 142), (120, 210, 200),
                      (232, 104, 84), (255, 255, 255, 11), (160, 255, 180, 34), ((20, 50, 26), (30, 46, 14)), glow=0.6),
        "light": _mode((244, 250, 240), (228, 240, 224), (24, 52, 34), (96, 128, 104), (170, 196, 176), (40, 150, 70),
                       (216, 160, 20), (210, 110, 20), (140, 100, 50), (40, 120, 180), (30, 160, 80), (30, 150, 140),
                       (200, 70, 50), (255, 255, 255, 170), (40, 150, 70, 46), ((220, 245, 210), (240, 245, 200)),
                       ink=BLACK, glow=0),
    },
    "blueprint": {
        "label": "Blueprint", "blurb": "Engineering drawing: blue paper, white line-work",
        "font": "plexmono", "scale": 0.84, "card": "outline", "radius": 0.15, "texture": "grid",
        "dark": _mode((14, 46, 98), (10, 38, 84), (236, 245, 255), (168, 196, 234), (110, 150, 206), (255, 255, 255),
                      (255, 232, 150), (255, 200, 96), (206, 224, 255), (150, 212, 255), (164, 242, 204), (180, 230, 255),
                      (255, 150, 150), (255, 255, 255, 6), (255, 255, 255, 120), ((30, 70, 130), (20, 60, 120)), glow=0),
        "light": _mode((242, 246, 252), (232, 238, 248), (18, 52, 112), (70, 104, 160), (160, 184, 220), (18, 60, 140),
                       (190, 130, 0), (190, 90, 0), (60, 80, 170), (20, 100, 200), (0, 140, 90), (20, 120, 180),
                       (200, 40, 50), (255, 255, 255, 120), (18, 60, 140, 110), ((220, 232, 250), (230, 238, 252)),
                       ink=(18, 52, 112), glow=0),
    },
}

# Graphics styles: how things are drawn, independent of the colours; any style works with
# any palette (mix and match). "scene" is the illustration of the energy flow:
#   nodes      circles/shapes joined by lines (node, route, decor below)
#   landscape  sun, hills, house with rooftop panels, trees, pylons, battery cabinet
#   house      isometric house drawing with roof panels, battery and a lattice pylon
#   sankey     ribbons as wide as the power they carry
#   orbit      an orrery: home as a ringed planet, sun / grid / battery on orbits
#   prism      the sun's beam split by a prism into rays to home, battery and grid
#   plasma     electrode spheres with lightning arcs (redrawn every refresh)
#   metro      a transit map: solar, grid and battery lines into a home interchange
#   circuit    a circuit board: chips, copper traces and vias
# Other keys: node circle|double|square|pill|orb|gear, route curve|ortho|wave|pipe|trace,
# gauge ring|segments|dial, icons line|solid|pixel|schematic, chart area|bars|steps|line,
# decor none|gears|leaves|stars|sparkles|dims, pixel (draw the scene as pixel art).
# Styles without their own palette name the fonts / cards / texture they use.
_NODES = {"node": "circle", "route": "curve", "decor": "none"}
STYLES = {
    "neon":      {"label": "Original (Neon)", "scene": "nodes", **_NODES, "gauge": "ring", "icons": "line", "chart": "area"},
    "alhazen":   {"label": "Alhazen", "scene": "prism", "gauge": "ring", "icons": "line", "chart": "line"},
    "modern":    {"label": "Modern", "scene": "sankey", "gauge": "ring", "icons": "solid", "chart": "bars"},
    "classic":   {"label": "Classic", "scene": "orbit", "gauge": "dial", "icons": "line", "chart": "line"},
    "eco":       {"label": "Eco", "scene": "landscape", "gauge": "ring", "icons": "solid", "chart": "bars"},
    "retro":     {"label": "Retro Arcade", "scene": "landscape", "pixel": True, "gauge": "segments", "icons": "pixel",
                  "chart": "steps", "decor": "stars"},
    "blueprint": {"label": "Blueprint", "scene": "house", "gauge": "dial", "icons": "schematic", "chart": "line"},
    "plasma":    {"label": "Plasma", "scene": "plasma", "gauge": "ring", "icons": "solid", "chart": "area",
                  "palette": "neon", "font": "rajdhani", "scale": 1.0, "card": "glass", "radius": 1.0, "texture": "none"},
    "metro":     {"label": "Metro", "scene": "metro", "gauge": "segments", "icons": "solid", "chart": "bars",
                  "palette": "modern", "font": "antonio", "scale": 1.04, "card": "flat", "radius": 1.2, "texture": "none"},
    "circuit":   {"label": "Circuit", "scene": "circuit", "gauge": "segments", "icons": "schematic", "chart": "steps",
                  "palette": "eco", "font": "plexmono", "scale": 0.84, "card": "outline", "radius": 0.3, "texture": "grid"},
    "steampunk": {"label": "Steampunk", "scene": "nodes", "node": "gear", "route": "pipe", "decor": "gears", "gauge": "dial",
                  "icons": "line", "chart": "area"},
    "ethereal":  {"label": "Ethereal", "scene": "nodes", "node": "orb", "route": "wave", "decor": "sparkles", "gauge": "ring",
                  "icons": "solid", "chart": "area"},
}

MODES = ("dark", "light", "black", "white")  # black / white: the dark / light colours on pure #000 / #FFF
DEFAULT = "neon-dark"


def default_palette(style: str) -> str:
    """The colours a style comes with."""
    return STYLES.get(style, {}).get("palette", style if style in THEMES else "neon")


def theme_keys() -> list[str]:
    """Every theme-mode key, e.g. ["neon-dark", "neon-light", ...]."""
    return [f"{name}-{mode}" for name in THEMES for mode in MODES]


def resolve(key: str | None, style: str | None = None) -> dict:
    """Everything screens.py needs for a palette-mode key (e.g. "blueprint-dark") drawn in a
    graphics style (default: the palette's own). Unknown names fall back to neon-dark."""
    name, _, mode = (key or DEFAULT).partition("-")
    theme = THEMES.get(name) or THEMES["neon"]
    look = STYLES.get(style or name) or STYLES.get(name) or STYLES["neon"]
    # fonts, cards and texture follow the style (its own entry, or the palette of the same name)
    graphics = look if "font" in look else (THEMES.get(style or name) or theme)
    pure = {"black": ("dark", BLACK), "white": ("light", WHITE)}.get(mode)
    values = dict(theme[pure[0]] if pure else (theme.get(mode) or theme["dark"]))
    if pure:  # solid background: no gradient, colour washes or texture
        values.update(BG_TOP=pure[1], BG_BOTTOM=pure[1], WASHES=(pure[1], pure[1]))
        values.pop("NODE", None)
    if "NODE" not in values:  # flow-diagram circles: near the background, a touch lighter/whiter
        light = mode == "light"
        base, toward, amount = values["BG_TOP"], (WHITE if light else values["TEXT"]), (0.7 if light else 0.05)
        values["NODE"] = tuple(int(base[i] + (toward[i] - base[i]) * amount) for i in range(3))
    bold, semi, medium = _FONTS[graphics["font"]]
    values.update(WEIGHT_FILE={"bold": bold, "semi": semi, "medium": medium}, FONT_SCALE=graphics["scale"],
                  CARD_STYLE=graphics["card"], RADIUS=graphics["radius"],
                  TEXTURE="none" if pure else graphics["texture"],
                  SCENE=look.get("scene", "nodes"), PIXEL_ART=bool(look.get("pixel")),
                  NODE_SHAPE=look.get("node", "circle"), ROUTE=look.get("route", "curve"), GAUGE=look["gauge"],
                  ICONS=look["icons"], CHART=look["chart"], DECOR=look.get("decor", "none"))
    return values
