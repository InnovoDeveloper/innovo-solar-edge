"""Dashboard image (720 x 720 PNG) for controllers that can only show pictures.

Rendered with Pillow every 60 s from the energy model: power flow (solar, grid,
house, battery), today's totals, price and solar performance - the Solar &
Energy dashboard without its settings panel. Exposed as an image entity
(authenticated) and, if enabled in the options, as a file under /local with an
unguessable name so a controller can load it with a plain URL.
"""

from __future__ import annotations

import datetime
import io
import logging
import math
import os
import secrets

from homeassistant.components.image import ImageEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

CONF_SNAPSHOT_FILE = "energy_snapshot_file"
SIZE = 720
INTERVAL = datetime.timedelta(seconds=60)
WWW_DIR = "innovo_solar_edge"

BG = (17, 20, 24)
CARD = (29, 34, 41)
LINE = (52, 60, 70)
TEXT = (232, 234, 237)
MUTED = (154, 160, 166)
SOLAR = (244, 180, 0)
GRID = (79, 140, 247)
HOUSE = (167, 139, 250)
BATTERY = (52, 168, 83)
BAD = (234, 67, 53)

BOLD_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]
REGULAR_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    for path in BOLD_FONTS if bold else REGULAR_FONTS:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _kw(watts) -> str:
    return "—" if watts is None else f"{abs(watts) / 1000:.1f} kW"


def _num(value, unit: str = "", digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}{unit}"


def _money(value, currency: str) -> str:
    if value is None:
        return "—"
    symbol = "$" if currency in ("USD", "CAD", "AUD", "NZD") else f"{currency} "
    return f"{symbol}{value:.2f}"


def _price(value, currency: str) -> str:
    if value is None:
        return "—"
    if currency in ("USD", "CAD", "AUD", "NZD"):
        return f"{value * 100:.1f}¢"
    return f"{value:.3f} {currency}"


def render(snapshot: dict) -> bytes:
    """Draw the dashboard. `snapshot` is a plain dict built in the event loop."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    f_title, f_big, f_mid = _font(30, True), _font(26, True), _font(20, True)
    f_txt, f_small = _font(18), _font(15)
    cur = snapshot["currency"]

    # header
    d.text((24, 18), "Solar & Energy", font=f_title, fill=TEXT)
    d.text((24, 56), snapshot["inverter"] or "—", font=f_small, fill=MUTED)
    stamp = f"Updated {snapshot['time']}"
    d.text((SIZE - 24 - d.textlength(stamp, font=f_small), 26), stamp, font=f_small, fill=MUTED)
    price = f"{_price(snapshot['price'], cur)}  {snapshot['period'] or ''}".strip()
    d.text((SIZE - 24 - d.textlength(price, font=f_mid), 48), price, font=f_mid, fill=SOLAR)

    # power flow: nodes around a hub, line colour/width shows active flows
    hub = (360, 285)
    nodes = {
        "solar": ((360, 150), SOLAR, "Solar", _kw(snapshot["solar_w"])),
        "grid": ((130, 285), GRID, "Grid", _kw(snapshot["grid_w"])),
        "house": ((590, 285), HOUSE, "Home", _kw(snapshot["house_w"])),
        "battery": ((360, 420), BATTERY, "Battery", _kw(snapshot["battery_w"])),
    }
    grid_w, batt_w, solar_w = snapshot["grid_w"] or 0, snapshot["battery_w"] or 0, snapshot["solar_w"] or 0
    flows = {
        "solar": solar_w > 50,
        "grid": abs(grid_w) > 50,
        "house": (snapshot["house_w"] or 0) > 50,
        "battery": abs(batt_w) > 100,
    }
    # direction: True = towards the hub
    inward = {"solar": True, "grid": grid_w > 0, "house": False, "battery": batt_w < 0}
    for key, ((x, y), color, _label, _value) in nodes.items():
        active = flows[key]
        d.line([(x, y), hub], fill=color if active else LINE, width=6 if active else 3)
        if active:  # arrow head halfway along the line
            mx, my = (x + hub[0]) / 2, (y + hub[1]) / 2
            ang = math.atan2(hub[1] - y, hub[0] - x) if inward[key] else math.atan2(y - hub[1], x - hub[0])
            tip = (mx + 12 * math.cos(ang), my + 12 * math.sin(ang))
            left = (mx + 12 * math.cos(ang + 2.5), my + 12 * math.sin(ang + 2.5))
            right = (mx + 12 * math.cos(ang - 2.5), my + 12 * math.sin(ang - 2.5))
            d.polygon([tip, left, right], fill=color)
    d.ellipse([hub[0] - 7, hub[1] - 7, hub[0] + 7, hub[1] + 7], fill=MUTED)

    for key, ((x, y), color, label, value) in nodes.items():
        r = 62
        d.ellipse([x - r, y - r, x + r, y + r], fill=CARD, outline=color, width=4)
        d.text((x - d.textlength(label, font=f_small) / 2, y - 36), label, font=f_small, fill=MUTED)
        d.text((x - d.textlength(value, font=f_mid) / 2, y - 14), value, font=f_mid, fill=TEXT)
        sub = ""
        if key == "grid" and flows["grid"]:
            sub = "buying" if grid_w > 0 else "selling"
        elif key == "battery":
            sub = _num(snapshot["battery_level"], "%", 0)
        elif key == "solar" and snapshot["solar_share"] is not None:
            sub = f"{snapshot['solar_share']:.0f}% of home"
        if sub:
            col = BAD if sub == "buying" else color
            d.text((x - d.textlength(sub, font=f_small) / 2, y + 14), sub, font=f_small, fill=col)

    # battery state under the hub
    if snapshot["battery_state"]:
        state = snapshot["battery_state"]
        d.text((470, 405), state, font=f_txt, fill=BATTERY)

    def tiles(y, items, h=92):
        n = len(items)
        gap, x0 = 10, 24
        w = (SIZE - 2 * x0 - gap * (n - 1)) / n
        for i, (label, value, color) in enumerate(items):
            x = x0 + i * (w + gap)
            d.rounded_rectangle([x, y, x + w, y + h], radius=12, fill=CARD)
            d.text((x + 12, y + 12), label, font=f_small, fill=MUTED)
            d.text((x + 12, y + 40), value, font=f_big, fill=color)

    d.text((24, 494), "Today", font=f_mid, fill=TEXT)
    tiles(522, [
        ("Solar kWh", _num(snapshot["solar_today"]), SOLAR),
        ("Home kWh", _num(snapshot["house_today"]), HOUSE),
        ("Bought kWh", _num(snapshot["import_today"]), GRID),
        ("Sold kWh", _num(snapshot["export_today"]), BATTERY),
        ("Cost", _money(snapshot["cost_today"], cur), TEXT),
    ])
    clean = snapshot["cleaning"] or "—"
    tiles(620, [
        ("Self-sufficient", _num(snapshot["self_sufficiency"], "%", 0), BATTERY),
        ("Panels vs spec", _num(snapshot["efficiency"], "%", 0), SOLAR),
        ("Health", snapshot["health"] or "—", TEXT),
        ("Clean panels", clean, BAD if clean == "Yes" else TEXT),
    ], h=70)

    if snapshot["next_change"]:
        d.text((24, 697), snapshot["next_change"], font=f_small, fill=MUTED)

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def build_snapshot(model) -> dict:
    v = model.values
    today = v.get("today") or {}
    nxt = v.get("next_price")
    cur = model.hass.config.currency
    next_text = None
    if nxt and nxt.get("at"):
        at = dt_util.as_local(nxt["at"]).strftime("%H:%M")
        next_text = f"Next price change {at}: {_price(nxt.get('price'), cur)} {nxt.get('period') or ''}".strip()
    return {
        "currency": cur,
        "time": dt_util.now().strftime("%H:%M"),
        "inverter": v.get("inverter_status"),
        "price": v.get("price"),
        "period": v.get("period"),
        "solar_w": v.get("solar_w"),
        "grid_w": v.get("grid_w"),
        "house_w": v.get("house_w"),
        "battery_w": v.get("battery_w"),
        "battery_level": v.get("battery_level"),
        "battery_state": v.get("battery_state"),
        "solar_share": v.get("solar_share_now"),
        "solar_today": today.get("solar"),
        "house_today": v.get("house_today"),
        "import_today": today.get("import"),
        "export_today": today.get("export"),
        "cost_today": v.get("cost_today"),
        "self_sufficiency": v.get("self_sufficiency_today"),
        "efficiency": v.get("efficiency_spec"),
        "health": v.get("health"),
        "cleaning": v.get("cleaning"),
        "next_change": next_text,
    }


class SnapshotPublisher:
    """Renders the image every minute; keeps the bytes and optionally a /local file."""

    def __init__(self, model):
        self.model = model
        self.hass: HomeAssistant = model.hass
        self.image: bytes | None = None
        self.updated: datetime.datetime | None = None
        self._unsub = None
        if not model.data.get("snapshot_secret"):
            model.data["snapshot_secret"] = secrets.token_urlsafe(12)
            model.save_soon()

    @property
    def file_enabled(self) -> bool:
        return bool(self.model.entry.options.get(CONF_SNAPSHOT_FILE, False))

    @property
    def url_path(self) -> str | None:
        if not self.file_enabled:
            return None
        return f"/local/{WWW_DIR}/dashboard-{self.model.data['snapshot_secret']}.png"

    @callback
    def start(self) -> None:
        self._unsub = async_track_time_interval(self.hass, self._tick, INTERVAL)
        self.hass.async_create_background_task(self._tick(), "innovo_solar_edge snapshot")

    @callback
    def stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    async def _tick(self, _now=None) -> None:
        try:
            data = build_snapshot(self.model)
            self.image = await self.hass.async_add_executor_job(render, data)
            self.updated = dt_util.utcnow()
            if self.file_enabled:
                await self.hass.async_add_executor_job(self._write_file, self.image)
            self.model.notify()
        except Exception:
            _LOGGER.exception("Dashboard image render failed")

    def _write_file(self, image: bytes) -> None:
        folder = self.hass.config.path("www", WWW_DIR)
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, f"dashboard-{self.model.data['snapshot_secret']}.png")
        tmp = target + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(image)
        os.replace(tmp, target)


def _base():
    from .energy import EnergyEntityMixin
    return EnergyEntityMixin


class DashboardImage(_base(), ImageEntity):
    _attr_content_type = "image/png"
    _attr_name = "Dashboard Image"
    _attr_icon = "mdi:monitor-dashboard"

    def __init__(self, model):
        ImageEntity.__init__(self, model.hass)
        _base().__init__(self, model, "dashboard_image", "energy_dashboard_image", "image")

    @property
    def image_last_updated(self):
        return self._model.snapshot.updated

    async def async_image(self) -> bytes | None:
        return self._model.snapshot.image


class DashboardImageUrl(_base(), SensorEntity):
    _attr_icon = "mdi:link-variant"
    _attr_name = "Dashboard Image URL"

    def __init__(self, model):
        super().__init__(model, "dashboard_image_url", "energy_dashboard_image_url", "sensor")

    @property
    def native_value(self):
        return self._model.snapshot.url_path or "disabled"

    @property
    def extra_state_attributes(self):
        return {"size": f"{SIZE}x{SIZE}", "refresh_seconds": int(INTERVAL.total_seconds())}
