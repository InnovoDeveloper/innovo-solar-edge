"""Dashboard screens (720 x 720 PNG) for controllers that can only show pictures.

Six pages drawn by screens.py - live, today, battery, money, solar, week. The
first four are redrawn every 60 s, solar and week every 10 minutes. Each page is
an image entity (authenticated); with the 'Publish the dashboard image' option
each page is also written under /local with an unguessable name so a controller
can load it with a plain URL.
"""

from __future__ import annotations

import datetime
import logging
import os
import secrets

from homeassistant.components.image import ImageEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .screens import PAGES, SIZE, render

_LOGGER = logging.getLogger(__name__)

CONF_SNAPSHOT_FILE = "energy_snapshot_file"
INTERVAL = datetime.timedelta(seconds=60)
SLOW_PAGES = {"solar": 10, "week": 10}  # redraw every N minutes
WWW_DIR = "innovo_solar_edge"
MAIN_PAGE = "live"


def screen_data(model) -> dict:
    """Everything the screens need, as plain values (built in the event loop)."""
    from .tariff import SRC_PLAN, plan_price

    hass = model.hass
    v = model.values
    now = dt_util.now()
    today = now.date().isoformat()
    cur = hass.config.currency
    settings = model.settings
    data = {
        "now": now,
        "currency": cur,
        "inverter": v.get("inverter_status"),
        "solar_w": v.get("solar_w"),
        "house_w": v.get("house_w"),
        "grid_w": v.get("grid_w"),
        "battery_w": v.get("battery_w"),
        "battery_level": v.get("battery_level"),
        "battery_state": v.get("battery_state"),
        "solar_share": v.get("solar_share_now"),
        "price": v.get("price"),
        "period": v.get("period"),
        "saved_today": v.get("saved_today"),
        "cost_today": v.get("cost_today"),
        "today": v.get("today") or {},
        "house_today": v.get("house_today"),
        "self_sufficiency": v.get("self_sufficiency_today"),
        "reserve": settings.get("reserve"),
        "capacity": settings.get("battery_kwh"),
        "efficiency": v.get("efficiency_spec"),
        "health": v.get("health"),
        "cleaning": v.get("cleaning"),
        "array_kw": v.get("array_kw"),
        "best_kwh": v.get("best_kwh"),
        "best_date": v.get("best_date"),
        "avg7": v.get("avg7"),
        "lifetime_avg": v.get("lifetime_avg"),
    }

    series = model.data.get("series") or {}
    data["series"] = list(series.get("pts") or []) if series.get("date") == today else []

    # battery time to full / to reserve
    bw, level, cap = v.get("battery_w"), v.get("battery_level"), settings.get("battery_kwh") or 0
    reserve = settings.get("reserve") or 0
    if bw and level is not None and cap:
        if bw > 150:
            data["time_to_full"] = (100 - level) / 100 * cap / (bw / 1000)
        elif bw < -150:
            data["time_to_empty"] = max(level - reserve, 0) / 100 * cap / (-bw / 1000)

    # next price change
    nxt = v.get("next_price")
    if nxt and nxt.get("at"):
        data["next"] = {"price": nxt.get("price"), "period": nxt.get("period"),
                        "at": dt_util.as_local(nxt["at"]).strftime("%H:%M")}

    # today's price timeline (15-minute slots) from the rate plan
    tariff = model.tariff
    if tariff.plan and tariff.state.get("source") == SRC_PLAN and not tariff.state.get("override"):
        midnight = dt_util.start_of_local_day()
        usage_day = (v.get("today") or {}).get("import")
        data["price_slots"] = [
            plan_price(tariff.plan, midnight + datetime.timedelta(minutes=15 * i), usage_day, None)[:2]
            for i in range(96)
        ]

    # sun
    try:
        from homeassistant.helpers.sun import get_astral_event_date

        rise = get_astral_event_date(hass, "sunrise", now.date())
        sset = get_astral_event_date(hass, "sunset", now.date())
        if rise and sset:
            rise, sset = dt_util.as_local(rise), dt_util.as_local(sset)
            data["sun"] = {
                "rise": rise.strftime("%H:%M"),
                "set": sset.strftime("%H:%M"),
                "progress": (now - rise).total_seconds() / (sset - rise).total_seconds(),
            }
    except Exception:  # no location configured
        pass

    # history: 30 days vs typical, last 7 days, week comparisons
    history = model.data.get("history") or []
    data["days30"] = [(d[0], d[1], model._typical_day(d[0])) for d in history[-30:]]
    days = [
        {"date": d[0], "solar": d[1], "house": d[2], "import": d[3],
         "cost": d[5] if len(d) > 5 else None, "saved": d[6] if len(d) > 6 else None}
        for d in history
    ]
    t = v.get("today") or {}
    days.append({"date": today, "solar": t.get("solar"), "house": v.get("house_today"), "import": t.get("import"),
                 "cost": v.get("cost_today"), "saved": v.get("saved_today"), "today": True})
    week = days[-7:]
    for day in week:
        day["label"] = datetime.date.fromisoformat(day["date"]).strftime("%a").upper()
        day["today"] = day.get("today", False)
        house, imp = day.get("house"), day.get("import")
        day["ss"] = (1 - imp / house) * 100 if house and imp is not None else None
    data["week"] = week
    data["week_totals"] = {k: sum((d.get(k) or 0) for d in week) for k in ("solar", "house", "import")}
    saved = [d["saved"] for d in days[-7:] if d.get("saved") is not None]
    costs = [d["cost"] for d in days[-7:] if d.get("cost") is not None]
    data["week_saved"] = sum(saved) if saved else None
    data["week_cost"] = sum(costs) if costs else None
    if len(days) >= 15:
        this = sum((d.get("solar") or 0) for d in days[-8:-1])
        prev = sum((d.get("solar") or 0) for d in days[-15:-8])
        data["week_change"] = (this / prev - 1) * 100 if prev else None
    return data


class SnapshotPublisher:
    """Renders the pages on a timer; keeps the PNG bytes and optional /local files."""

    def __init__(self, model):
        self.model = model
        self.hass: HomeAssistant = model.hass
        self.images: dict[str, bytes] = {}
        self.updated: dict[str, datetime.datetime] = {}
        self._unsub = None
        self._ticks = 0
        self._ready = False
        if not model.data.get("snapshot_secret"):
            model.data["snapshot_secret"] = secrets.token_urlsafe(12)
            model.save_soon()

    @property
    def file_enabled(self) -> bool:
        return bool(self.model.entry.options.get(CONF_SNAPSHOT_FILE, False))

    def file_name(self, page: str) -> str:
        secret = self.model.data["snapshot_secret"]
        return f"dashboard-{secret}.png" if page == MAIN_PAGE else f"dashboard-{secret}-{page}.png"

    def url_path(self, page: str = MAIN_PAGE) -> str | None:
        if not self.file_enabled:
            return None
        return f"/local/{WWW_DIR}/{self.file_name(page)}"

    @callback
    def start(self) -> None:
        self._unsub = async_track_time_interval(self.hass, self._tick, INTERVAL)
        self.hass.async_create_background_task(self._tick(force=True), "innovo_solar_edge screens")

    @callback
    def stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    async def _tick(self, _now=None, force: bool = False) -> None:
        self._ticks += 1
        try:
            data = screen_data(self.model)
        except Exception:
            _LOGGER.exception("Screen data failed")
            return
        ready = data.get("inverter") is not None and data.get("house_w") is not None
        if ready and not self._ready:
            self._ready, force = True, True  # redraw everything once real data has arrived
        pages = [p for p in PAGES if force or p not in self.images or self._ticks % SLOW_PAGES.get(p, 1) == 0]
        for page in pages:
            try:
                png = await self.hass.async_add_executor_job(render, page, data)
            except Exception:
                _LOGGER.exception("Rendering the %s screen failed", page)
                continue
            self.images[page] = png
            self.updated[page] = dt_util.utcnow()
            if self.file_enabled:
                await self.hass.async_add_executor_job(self._write_file, page, png)
        self.model.notify()

    def _write_file(self, page: str, image: bytes) -> None:
        folder = self.hass.config.path("www", WWW_DIR)
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, self.file_name(page))
        tmp = target + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(image)
        os.replace(tmp, target)


def _base():
    from .energy import EnergyEntityMixin
    return EnergyEntityMixin


class ScreenImage(_base(), ImageEntity):
    _attr_content_type = "image/png"

    def __init__(self, model, page: str):
        ImageEntity.__init__(self, model.hass)
        if page == MAIN_PAGE:  # keeps the original entity for existing installs
            _base().__init__(self, model, "dashboard_image", "energy_dashboard_image", "image")
            self._attr_name = "Dashboard Image"
        else:
            _base().__init__(self, model, f"screen_{page}", f"energy_screen_{page}", "image")
            self._attr_name = f"Screen {page.title()}"
        self._page = page
        self._attr_icon = "mdi:monitor-dashboard"

    @property
    def image_last_updated(self):
        return self._model.snapshot.updated.get(self._page)

    @property
    def extra_state_attributes(self):
        return {"page": self._page, "size": f"{SIZE}x{SIZE}"}

    async def async_image(self) -> bytes | None:
        return self._model.snapshot.images.get(self._page)


class DashboardImageUrl(_base(), SensorEntity):
    _attr_icon = "mdi:link-variant"
    _attr_name = "Dashboard Image URL"

    def __init__(self, model):
        super().__init__(model, "dashboard_image_url", "energy_dashboard_image_url", "sensor")

    @property
    def native_value(self):
        return self._model.snapshot.url_path() or "disabled"

    @property
    def extra_state_attributes(self):
        snap = self._model.snapshot
        return {
            "size": f"{SIZE}x{SIZE}",
            "refresh_seconds": int(INTERVAL.total_seconds()),
            "pages": {p: snap.url_path(p) for p in PAGES} if snap.file_enabled else None,
            "image_entities": {p: ("image.energy_dashboard_image" if p == MAIN_PAGE else f"image.energy_screen_{p}")
                               for p in PAGES},
        }


def screen_images(model) -> list[ImageEntity]:
    return [ScreenImage(model, page) for page in PAGES]
