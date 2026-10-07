"""Dashboard screens (720 x 720 PNG) for controllers that can only show pictures.

Seven pages drawn by screens.py - overview, live, today, battery, money, solar,
week. Most are redrawn every 60 s, solar and week every 10 minutes; a page with
no data yet shows a "gathering data" placeholder. Each page is
an image entity (authenticated); with the 'Publish the dashboard image' option
each page is also written under /local with an unguessable name so a controller
can load it with a plain URL. With a publish folder set, every page and the
overview variants (overview-slot, overview-flow) are also written there as JPG
with fixed names, <page>-<shape>-<res>.jpg for each shape in screens.FORMATS
(1x1, 16x9, 4x3, 9x10, 9x16) in hires and lowres, plus <page>.jpg (square,
low-res) - for a folder another web server on the box already serves over HTTP.
With the PNG option, the same names are also written as transparent .png.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import secrets
import time

from homeassistant.components.image import ImageEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .screens import FORMATS, PAGES, SIZE, VARIANTS, export, render

_LOGGER = logging.getLogger(__name__)

CONF_SNAPSHOT_FILE = "energy_snapshot_file"
CONF_PUBLISH_DIR = "energy_publish_dir"
CONF_PUBLISH_PNG = "energy_publish_png"  # also transparent PNGs
SITE_PAGE = "index.html"  # viewer page written next to the screens
SITE_INFO = "screens.json"
SHAPES_EVERY = 2  # publish the non-square shapes every N minutes (a full set is ~17 s of CPU on an 8-core ARM box)
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
        "has_grid": model.has_grid,
        "has_battery": model.has_battery,
        "inverter_kw": model.inverter_kw,
        "typical_today": v.get("typical_today"),
        "today_vs_typical": v.get("today_vs_typical"),
        # the flow lines' pattern moves a quarter step each minute, so every refresh differs
        "_phase": (now.hour * 60 + now.minute) % 4 / 4,
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

    @property
    def publish_dir(self) -> str | None:
        return self.model.entry.options.get(CONF_PUBLISH_DIR) or None

    @property
    def publish_kinds(self) -> tuple[str, ...]:
        return ("jpg", "png") if self.model.entry.options.get(CONF_PUBLISH_PNG) else ("jpg",)

    def publish_path(self, page: str) -> str | None:
        folder = self.publish_dir
        return os.path.join(folder, f"{page}.jpg") if folder else None

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
        if self.publish_dir and not getattr(self, "_publishing", False):
            # every shape of the pages drawn this tick (+ the overview variants);
            # the non-square shapes less often, as they cost several renders each.
            # Skipped while the previous round is still running (PNGs take a while).
            shapes = None if force or self._ticks % SHAPES_EVERY == 0 else ["1x1"]
            names = pages + (list(VARIANTS) if "overview" in pages else [])
            self._publishing = True
            try:
                await self.hass.async_add_executor_job(self._publish, names, data, shapes)
            finally:
                self._publishing = False

    def _write_file(self, page: str, image: bytes) -> None:
        folder = self.hass.config.path("www", WWW_DIR)
        os.makedirs(folder, exist_ok=True)
        _write_atomic(os.path.join(folder, self.file_name(page)), image)

    def _publish(self, names: list[str], data: dict, shapes) -> None:
        """Write <name>-<shape>-<res>.jpg for each page, plus <name>.jpg (square, low-res)."""
        folder = self.publish_dir
        started = time.monotonic()
        try:
            os.makedirs(folder, exist_ok=True)
            for name in names:
                try:
                    files = export(name, data, shapes, self.publish_kinds)
                except Exception:
                    _LOGGER.exception("Publishing the %s screen failed", name)
                    continue
                for file_name, image in files.items():
                    _write_atomic(os.path.join(folder, file_name), image)
                for kind in self.publish_kinds:
                    square = files.get(f"{name}-1x1-lowres.{kind}")
                    if square:
                        _write_atomic(os.path.join(folder, f"{name}.{kind}"), square)
            self._write_site(folder, data)
            self._publish_error = None
        except OSError as err:
            if getattr(self, "_publish_error", None) != str(err):  # log each new problem once
                self._publish_error = str(err)
                _LOGGER.warning("Can't write the screens to %s: %s", folder, err)
        _LOGGER.debug("Published %d screens (%s) in %.1f s", len(names), shapes or "all shapes",
                      time.monotonic() - started)


    def _write_site(self, folder: str, data: dict) -> None:
        """The viewer page (index.html, once per start) and screens.json (each publish)."""
        pages = [p for p in PAGES + list(VARIANTS) if not (p == "battery" and data.get("has_battery") is False)]
        info = {
            "updated": dt_util.utcnow().isoformat(),
            "pages": pages,
            "shapes": {shape: {res: f"{w}x{h}" for res, (w, h) in sizes.items()} for shape, sizes in FORMATS.items()},
            "solar_only": data.get("has_grid") is False,
            "formats": list(self.publish_kinds),
        }
        _write_atomic(os.path.join(folder, SITE_INFO), json.dumps(info).encode())
        if not getattr(self, "_site_written", False):
            with open(os.path.join(os.path.dirname(__file__), "web", SITE_PAGE), "rb") as handle:
                _write_atomic(os.path.join(folder, SITE_PAGE), handle.read())
            self._site_written = True


def published_names() -> list[str]:
    """Every file the publisher can write, for clean-up when the integration is removed."""
    names = [SITE_PAGE, SITE_INFO]
    for page in PAGES + list(VARIANTS):
        for kind in ("jpg", "png"):
            names.append(f"{page}.{kind}")
            names += [f"{page}-{shape}-{res}.{kind}" for shape, sizes in FORMATS.items() for res in sizes]
    return names


def remove_published(folder: str) -> None:
    """Delete the published screens and viewer page (not the folder's other files)."""
    for name in published_names():
        for path in (os.path.join(folder, name), os.path.join(folder, name) + ".tmp"):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
    try:
        os.rmdir(folder)  # only if nothing else is in it
    except OSError:
        pass


def _write_atomic(target: str, data: bytes) -> None:
    """Write via a temp file so a web server never serves a half-written image."""
    tmp = target + ".tmp"
    with open(tmp, "wb") as handle:
        handle.write(data)
    os.chmod(tmp, 0o644)
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
        image = self._model.snapshot.images.get(self._page)
        if image is None:  # not drawn yet: serve the "gathering data" placeholder
            image = await self.hass.async_add_executor_job(
                render, self._page, {"now": dt_util.now(), "currency": self.hass.config.currency}
            )
        return image


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
            "publish_dir": snap.publish_dir,
            "published_files": "<page>-<shape>-<res>.jpg" if snap.publish_dir else None,
            "published_formats": list(snap.publish_kinds) if snap.publish_dir else None,
            "published_pages": PAGES + list(VARIANTS) if snap.publish_dir else None,
            "published_shapes": {shape: {res: f"{w}x{h}" for res, (w, h) in sizes.items()}
                                 for shape, sizes in FORMATS.items()} if snap.publish_dir else None,
            "image_entities": {p: ("image.energy_dashboard_image" if p == MAIN_PAGE else f"image.energy_screen_{p}")
                               for p in PAGES},
        }


def screen_images(model) -> list[ImageEntity]:
    return [ScreenImage(model, page) for page in PAGES]
