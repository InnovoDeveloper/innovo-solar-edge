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

import asyncio
import datetime
import json
import logging
import os
import re
import secrets
import shutil
import time

from homeassistant.components.image import ImageEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .control import CONTROL_PORT
from .screens import FORMATS, PAGES, SIZE, VARIANTS, export, is_ready, render

_LOGGER = logging.getLogger(__name__)

CONF_SNAPSHOT_FILE = "energy_snapshot_file"
CONF_PUBLISH_DIR = "energy_publish_dir"
CONF_PUBLISH_PNG = "energy_publish_png"  # also transparent PNGs
SITE_PAGE = "index.html"  # viewer page written next to the screens
SITE_INFO = "screens.json"
PREVIEWS = "previews"     # one thumbnail per graphics style, for the design picker
PAGE_VERSION = "1.14.2"    # must match PAGE_VERSION in web/index.html (old cached pages reload)
MAX_LOOKS = 4
PREVIEW_EVERY = 6 * 3600  # seconds between style thumbnail refreshes             # generated looks published live (each costs a full render round)
LOOK_RE = re.compile(r"^([a-z]+)--([a-z]+)-([a-z]+)$")
CLEANABLE = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".tmp", ".html", ".json", ".tgz")
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
        self._lock = asyncio.Lock()       # one publish round at a time (ticks, generate, clean)
        self._urgent: list[str] = []      # new looks waiting for their first pictures (served between screens)
        self._serving = False
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

    # --- looks: a graphics style + colour palette + mode, published in their own folder ---
    @property
    def looks(self) -> list[str]:
        return self.model.data.setdefault("looks", [])

    @staticmethod
    def look_key(style: str, palette: str, mode: str) -> str:
        from .themes import MODES, STYLES, THEMES

        if style not in STYLES or palette not in THEMES or mode not in MODES:
            raise ValueError(f"Unknown look: style {style!r}, colours {palette!r}, mode {mode!r}")
        return f"{style}--{palette}-{mode}"

    @staticmethod
    def look_data(data: dict, look: str) -> dict:
        style, palette, mode = LOOK_RE.match(look).groups()
        return {**data, "_style": style, "_theme": f"{palette}-{mode}"}

    async def async_generate(self, style: str, palette: str, mode: str, wait: bool = True) -> str:
        """Add a look and publish it right away (in the background if wait=False);
        returns its folder name."""
        from homeassistant.exceptions import HomeAssistantError

        if not self.publish_dir:
            raise HomeAssistantError("Set a publish folder in the integration settings first")
        try:
            look = self.look_key(style, palette, mode)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        if look not in self.looks:
            if len(self.looks) >= MAX_LOOKS:
                raise HomeAssistantError(f"Up to {MAX_LOOKS} looks; remove one first")
            self.looks.append(look)
            self.model.save_soon()
        if look not in self._urgent:
            self._urgent.append(look)
        if wait:
            await self._publish_new_look(look)
        else:
            self.hass.async_create_background_task(self._publish_new_look(look), f"innovo_solar_edge look {look}")
        return look

    async def _publish_new_look(self, look: str) -> None:
        """A new look jumps the queue: if a publish round is running, it draws the look
        between two screens; otherwise it is drawn right now."""
        data = screen_data(self.model)
        await self.hass.async_add_executor_job(self._write_site, self.publish_dir, data)  # lists it as "generating" now
        if not self._lock.locked():
            async with self._lock:
                await self.hass.async_add_executor_job(self._serve_urgent, data)

    def _serve_urgent(self, data: dict) -> None:
        """First pictures for new looks: every view in the square shape (JPG) in a few
        seconds, then the other shapes; called between screens of a running round."""
        if self._serving or not self._urgent:
            return
        self._serving = True
        try:
            while self._urgent:
                look = self._urgent.pop(0)
                if look not in self.looks:
                    continue
                names = PAGES + list(VARIANTS)
                self._publish_looks(names, data, ["1x1"], [look], kinds=("jpg",))
                self._write_site(self.publish_dir, data)  # ready to view
                others = [shape for shape in FORMATS if shape != "1x1"]
                self._publish_looks(names, data, others, [look], kinds=("jpg",))
                self._write_site(self.publish_dir, data)
        finally:
            self._serving = False

    async def async_remove(self, look: str) -> None:
        if look in self.looks:
            self.looks.remove(look)  # stops publishing it straight away
            self.model.save_soon()
        if look in self._urgent:
            self._urgent.remove(look)
        if self.publish_dir and LOOK_RE.match(look):
            # drop it from screens.json now; delete the folder once the current round is done
            await self.hass.async_add_executor_job(self._write_site, self.publish_dir, screen_data(self.model))
            async with self._lock:
                await self.hass.async_add_executor_job(shutil.rmtree, os.path.join(self.publish_dir, look), True)
                await self.hass.async_add_executor_job(self._write_site, self.publish_dir, screen_data(self.model))

    async def async_clean(self) -> int:
        """Delete every image or folder in the publish folder that isn't in use."""
        if not self.publish_dir:
            return 0
        async with self._lock:
            removed = await self.hass.async_add_executor_job(self._clean)
            await self.hass.async_add_executor_job(self._write_site, self.publish_dir, screen_data(self.model))
        _LOGGER.info("Cleaned %d unused screen files/folders from %s", removed, self.publish_dir)
        return removed

    def _cleanable(self) -> list[dict]:
        """What a clean-up would delete: folders and loose files no look uses
        (only images, pages, json and archives - anything else is left alone)."""
        folder = self.publish_dir
        keep = {SITE_PAGE, SITE_INFO, PREVIEWS, *self.looks, *published_names()}
        out = []
        for entry in sorted(os.listdir(folder)):
            if entry in keep:
                continue
            path = os.path.join(folder, entry)
            if os.path.isdir(path) and not os.path.islink(path):
                files = [os.path.join(r, f) for r, _, fs in os.walk(path) for f in fs]
                if all(f.lower().endswith(CLEANABLE) for f in files):
                    out.append({"name": entry + "/", "files": len(files), "bytes": sum(os.path.getsize(f) for f in files)})
            elif entry.lower().endswith(CLEANABLE):
                out.append({"name": entry, "files": 1, "bytes": os.path.getsize(path)})
        return out

    def _clean(self) -> int:
        removed = 0
        for item in self._cleanable():
            path = os.path.join(self.publish_dir, item["name"].rstrip("/"))
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
            removed += 1
        return removed

    def _look_info(self, look: str) -> dict:
        style, palette, mode = LOOK_RE.match(look).groups()
        last = (PAGES + list(VARIANTS))[-1]  # written last in each round
        shapes = [shape for shape in FORMATS
                  if os.path.exists(os.path.join(self.publish_dir, look, f"{last}-{shape}-lowres.jpg"))]
        return {"key": look, "style": style, "palette": palette, "mode": mode, "shapes": shapes,
                "ready": "1x1" in shapes and look not in self._urgent, **self._folder_stats(look)}

    def _folder_stats(self, sub: str) -> dict:
        files = [os.path.join(r, f) for r, _, fs in os.walk(os.path.join(self.publish_dir, sub)) for f in fs]
        return {"files": len(files), "bytes": sum(os.path.getsize(f) for f in files if os.path.exists(f))}

    async def async_command(self, action: str, payload: dict) -> dict:
        """Requests from the viewer page (control.py)."""
        if action == "generate":
            look = await self.async_generate(payload.get("style", ""), payload.get("palette", ""), payload.get("mode", ""),
                                             wait=False)
            return {"look": look}
        # the page doesn't wait: remove / clean run as soon as the current publish round ends
        if action == "remove":
            look = payload.get("look", "")
            self.hass.async_create_background_task(self.async_remove(look), f"innovo_solar_edge remove {look}")
            return {"removing": look}
        if action == "clean":
            self.hass.async_create_background_task(self.async_clean(), "innovo_solar_edge clean screens")
            return {"cleaning": True}
        return {"error": f"unknown action {action!r}"}

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
        if self.publish_dir and not self._lock.locked():
            # every shape of the pages drawn this tick (+ the overview variants), for the
            # original look and every generated one; the non-square shapes less often, as
            # they cost several renders each. Skipped while the previous round still runs.
            shapes = None if force or self._ticks % SHAPES_EVERY == 0 else ["1x1"]
            names = pages + (list(VARIANTS) if "overview" in pages else [])
            async with self._lock:
                await self.hass.async_add_executor_job(self._publish, names, data, shapes)
                if self.looks:
                    await self.hass.async_add_executor_job(self._publish_looks, names, data, shapes, list(self.looks))

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
                self._serve_urgent(data)  # a new look waiting? draw it first
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


    def _publish_looks(self, names: list[str], data: dict, shapes, looks: list[str], kinds=None) -> None:
        """Each generated look in its own folder: <publish dir>/<look>/<page>-<shape>-<res>.jpg"""
        kinds = kinds or self.publish_kinds
        for look in looks:
            if not LOOK_RE.match(look):
                continue
            sub = os.path.join(self.publish_dir, look)
            try:
                os.makedirs(sub, exist_ok=True)
                styled = self.look_data(data, look)
                for name in names:
                    self._serve_urgent(data)  # a new look waiting? draw it first
                    try:
                        files = export(name, styled, shapes, kinds)
                    except Exception:
                        _LOGGER.exception("Publishing the %s screen in look %s failed", name, look)
                        continue
                    for file_name, image in files.items():
                        _write_atomic(os.path.join(sub, file_name), image)
                    for kind in kinds:
                        if square := files.get(f"{name}-1x1-lowres.{kind}"):
                            _write_atomic(os.path.join(sub, f"{name}.{kind}"), square)
            except OSError as err:
                _LOGGER.warning("Can't write look %s: %s", look, err)

    def _write_previews(self, folder: str, data: dict) -> None:
        """One small thumbnail per graphics style (its own colours, dark), once."""
        from .themes import STYLES, default_palette
        from PIL import Image
        import io

        sub = os.path.join(folder, PREVIEWS)
        os.makedirs(sub, exist_ok=True)
        for style in STYLES:
            target = os.path.join(sub, f"{style}.jpg")
            try:
                png = render("live", {**data, "_style": style, "_theme": f"{default_palette(style)}-dark"})
                img = Image.open(io.BytesIO(png)).convert("RGB").resize((360, 360), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=82)
                _write_atomic(target, buf.getvalue())
            except Exception:
                _LOGGER.exception("Style preview %s failed", style)

    def _write_site(self, folder: str, data: dict) -> None:
        """The viewer page (index.html, once per start) and screens.json (each publish)."""
        from .themes import BLURBS, MODE_LABELS, MODES, STYLES, THEMES

        def hexes(mode_values):
            return {k.lower(): "#%02x%02x%02x" % mode_values[k] for k in ("BG_TOP", "TEXT", "CYAN", "SOLAR", "HOME", "GRID", "BATT")}

        pages = [p for p in PAGES + list(VARIANTS) if not (p == "battery" and data.get("has_battery") is False)]
        api = getattr(self.hass.config, "api", None)
        info = {
            "updated": dt_util.utcnow().isoformat(),
            "pages": pages,
            "shapes": {shape: {res: f"{w}x{h}" for res, (w, h) in sizes.items()} for shape, sizes in FORMATS.items()},
            "solar_only": data.get("has_grid") is False,
            "formats": list(self.publish_kinds),
            "looks": [self._look_info(look) for look in self.looks if LOOK_RE.match(look)],
            "cleanable": self._cleanable(),
            "page_version": PAGE_VERSION,
            "max_looks": MAX_LOOKS,
            "styles": [{"key": k, "label": v["label"], "blurb": BLURBS.get(k, ""), "preview": f"{PREVIEWS}/{k}.jpg"}
                       for k, v in STYLES.items()],
            "palettes": [{"key": k, "label": v["label"], "dark": hexes(v["dark"]), "light": hexes(v["light"])}
                         for k, v in THEMES.items()],
            "modes": [[m, MODE_LABELS[m]] for m in MODES],
            "control": {"port": CONTROL_PORT},
        }
        _write_atomic(os.path.join(folder, SITE_INFO), json.dumps(info).encode())
        due = time.monotonic() - getattr(self, "_previews_at", -1e9) > PREVIEW_EVERY
        if due and is_ready("live", data):  # never thumbnails of the "gathering data" placeholder
            self._previews_at = time.monotonic()
            self._write_previews(folder, data)
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
    for entry in os.listdir(folder) if os.path.isdir(folder) else []:  # looks and style previews
        if entry == PREVIEWS or LOOK_RE.match(entry):
            shutil.rmtree(os.path.join(folder, entry), ignore_errors=True)
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
