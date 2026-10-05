"""Innovo energy model: live power flows, daily totals and solar performance.

Everything here is derived from this integration's own (already scaled)
inverter, meter and battery entities after each coordinator update, so it
never adds Modbus traffic. Results are exposed as entities on a "SolarEdge
Energy" device so that home-control platforms can read the whole picture
from this one integration.

Sign conventions: grid power + = importing, battery power + = charging.
Battery and solar power are estimates: with a DER-reported battery only the
state of energy is available, so battery power = SoE rate x usable capacity.
"""

from __future__ import annotations

import asyncio
import calendar
import datetime
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from homeassistant.components.date import DateEntity
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_PRICE_ENTITY = "energy_price_entity"
CONF_TYPICAL_LOOKUP = "energy_typical_lookup"

STORE_VERSION = 1
SAVE_DELAY = 300
SOE_WINDOW_S = 120  # battery power is the SoE slope over this window
BATTERY_DEADBAND_W = 100
HISTORY_DAYS = 365
ARRAY_ISSUE = "energy_array_settings"

# Typical monthly output (kWh per kW of panels) comes from NREL PVWatts v8 for
# HA's home location rounded to ~1 km, plus the tilt/azimuth settings. It can be
# turned off in the options; the typical/spec metrics are then unavailable.
PVWATTS_URL = "https://developer.nlr.gov/api/pvwatts/v8.json"
PVWATTS_RETRY_S = 6 * 3600

# Placeholders until the installer enters the real array; a repair issue asks
# for them. Battery capacity defaults to the battery's nameplate.
DEFAULT_SETTINGS = {
    "panel_count": 10,
    "panel_watts": 400,
    "tilt": 20,
    "azimuth": 180,
    "battery_kwh": None,
    "commissioned": None,
    "confirmed": False,
}
FALLBACK_BATTERY_KWH = 10.0


def signal_update(entry_id: str) -> str:
    return f"{DOMAIN}_energy_update_{entry_id}"


class EnergyModel:
    """Derives energy flows and performance metrics for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, hub, coordinator):
        self.hass = hass
        self.entry = entry
        self.hub = hub
        self.coordinator = coordinator
        self._store = Store(hass, STORE_VERSION, f"{DOMAIN}.energy.{entry.entry_id}")
        self.data: dict[str, Any] = {}
        self._soe_window: deque[tuple[float, float]] = deque()
        self._unsubs: list[Callable[[], None]] = []
        self._sources: dict[str, str] = {}
        self.inverter = hub.inverters[0] if hub.inverters else None
        self.values: dict[str, Any] = {}

    # ----- identity -----

    @property
    def uid_base(self) -> str:
        return f"{self.inverter.uid_base}_energy"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.uid_base)},
            name="SolarEdge Energy",
            manufacturer="Innovo",
            model="Energy and performance model",
        )

    @property
    def settings(self) -> dict[str, Any]:
        return self.data["settings"]

    @property
    def array_kw(self) -> float:
        return self.settings["panel_count"] * self.settings["panel_watts"] / 1000

    # ----- lifecycle -----

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        settings = {**DEFAULT_SETTINGS, **stored.get("settings", {})}
        if "panel_watts" in stored.get("settings", {}) and "confirmed" not in stored["settings"]:
            settings["confirmed"] = True  # stores created before the flag existed
        if settings["battery_kwh"] is None:
            settings["battery_kwh"] = self._nameplate_kwh() or FALLBACK_BATTERY_KWH
        self.data = {
            "settings": settings,
            "acc": {"solar": 0.0, "charged": 0.0, "discharged": 0.0, **stored.get("acc", {})},
            "last": stored.get("last", {}),
            "pending": stored.get("pending", 0.0),
            "today": stored.get("today"),
            "last_today": stored.get("last_today"),
            "history": stored.get("history", []),
            "typical": stored.get("typical") or {"per_kw": None, "source": None, "params": None},
            "month": stored.get("month"),
            "tariff": stored.get("tariff"),
            "snapshot_secret": stored.get("snapshot_secret"),
        }
        from .tariff import SRC_PLAN, SRC_SENSOR, TariffManager

        default_source = SRC_SENSOR if self.entry.options.get(CONF_PRICE_ENTITY) else SRC_PLAN
        self.tariff = TariffManager(self, default_source)

        from .snapshot import SnapshotPublisher

        self.snapshot = SnapshotPublisher(self)

    @property
    def signal(self) -> str:
        return signal_update(self.entry.entry_id)

    @callback
    def notify(self) -> None:
        async_dispatcher_send(self.hass, self.signal)

    @callback
    def save_soon(self) -> None:
        self._store.async_delay_save(lambda: self.data, 5)

    @callback
    def recompute(self) -> None:
        """Re-run the update with the latest readings (after a settings change)."""
        self._update(publish=False)

    def _nameplate_kwh(self) -> float | None:
        for battery in [*self.hub.batteries, *self.hub.der_batteries]:
            try:
                rated = float(battery.battery_info.B_RatedEnergy)
            except (AttributeError, TypeError, ValueError):
                continue
            if 0 < rated < 1_000_000:
                return round(rated / 1000, 2)
        return None

    @callback
    def async_start(self) -> None:
        self._unsubs.append(self.coordinator.async_add_listener(self._on_coordinator_update))
        self._update_array_issue()
        self.snapshot.start()
        self.hass.async_create_background_task(
            self._async_refresh_typical(), f"{DOMAIN} pvwatts lookup"
        )

    @callback
    def _update_array_issue(self) -> None:
        issue_id = f"{ARRAY_ISSUE}_{self.entry.entry_id}"
        if self.settings.get("confirmed"):
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        else:
            ir.async_create_issue(
                self.hass, DOMAIN, issue_id, is_fixable=False,
                severity=ir.IssueSeverity.WARNING, translation_key=ARRAY_ISSUE,
            )

    async def async_stop(self) -> None:
        self.snapshot.stop()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self._store.async_save(self.data)

    @callback
    def _save_later(self) -> None:
        self._store.async_delay_save(lambda: self.data, SAVE_DELAY)

    # ----- settings (number/date entities) -----

    @callback
    def async_set_setting(self, key: str, value: Any) -> None:
        self.settings[key] = value
        if key in ("panel_count", "panel_watts"):
            self.settings["confirmed"] = True
            self._update_array_issue()
        self._store.async_delay_save(lambda: self.data, 5)
        if key in ("tilt", "azimuth"):
            self.hass.async_create_background_task(
                self._async_refresh_typical(), f"{DOMAIN} pvwatts lookup"
            )
        self._compute_metrics()
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))

    # ----- source entities -----

    def _source(self, platform: str, unique_id: str) -> str | None:
        key = f"{platform}:{unique_id}"
        if key not in self._sources:
            entity_id = er.async_get(self.hass).async_get_entity_id(platform, DOMAIN, unique_id)
            if entity_id is None:
                return None
            self._sources[key] = entity_id
        return self._sources[key]

    def _state(self, entity_id: str | None):
        if entity_id is None:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        return state

    def _num(self, entity_id: str | None) -> float | None:
        state = self._state(entity_id)
        try:
            return float(state.state) if state else None
        except ValueError:
            return None

    def _read(self) -> dict[str, Any]:
        inv = self.inverter.uid_base
        grid = next((m for m in self.hub.meters if "Import" in str(m.option)), None)
        batteries = list(self.hub.batteries) + list(getattr(self.hub, "der_batteries", []))
        battery = batteries[0] if batteries else None

        status = self._state(self._source("sensor", f"{inv}_status"))
        vendor = self._state(self._source("sensor", f"{inv}_status_vendor4"))
        grid_on = self._state(self._source("binary_sensor", f"{inv}_grid_status_on_off"))
        price_entity = self.entry.options.get(CONF_PRICE_ENTITY)
        price = self._state(price_entity) if price_entity else None
        try:
            price_value = float(price.state) if price else None
        except ValueError:
            price_value = None

        return {
            "inv_w": self._num(self._source("sensor", f"{inv}_ac_power")),
            "ac_energy": self._num(self._source("sensor", f"{inv}_ac_energy_kwh")),
            "status": status.state if status else None,
            "vendor": vendor.state if vendor else None,
            "grid_on": None if grid_on is None else grid_on.state == "on",
            "meter_w": self._num(self._source("sensor", f"{grid.uid_base}_ac_power")) if grid else None,
            "import": self._num(self._source("sensor", f"{grid.uid_base}_imported_kwh")) if grid else None,
            "export": self._num(self._source("sensor", f"{grid.uid_base}_exported_kwh")) if grid else None,
            "soe": self._num(self._source("sensor", f"{battery.uid_base}_battery_soe")) if battery else None,
            "price": price_value,
            "period": price.attributes.get("period") if price else None,
        }

    # ----- update cycle -----

    @callback
    def _on_coordinator_update(self) -> None:
        # Source entities write their states in their own coordinator
        # callbacks; run once they all have.
        self.hass.loop.call_soon(self._update)

    @callback
    def _update(self, publish: bool = True) -> None:
        try:
            raw = self._read()
            self._compute_live(raw)
            self._accumulate(raw)
            self._compute_today(raw)
            self._compute_metrics()
        except Exception:  # never take the integration down over a derived value
            _LOGGER.exception("Energy model update failed")
            return
        self._save_later()
        if publish:
            async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))

    def _sun_up(self, inv_w: float | None) -> bool:
        sun = self.hass.states.get("sun.sun")
        if sun is None:
            return bool(inv_w and inv_w > 0)
        return sun.state == "above_horizon"

    def _compute_live(self, raw: dict[str, Any]) -> None:
        v = self.values
        cap = self.settings["battery_kwh"]
        soe = raw["soe"]

        if soe is not None:
            now = time.monotonic()
            self._soe_window.append((now, soe))
            while self._soe_window and now - self._soe_window[0][0] > SOE_WINDOW_S:
                self._soe_window.popleft()
            t0, s0 = self._soe_window[0]
            if now - t0 >= 30:
                watts = (soe - s0) / 100 * cap * 1000 / ((now - t0) / 3600)
                v["battery_w"] = 0 if abs(watts) < BATTERY_DEADBAND_W else round(watts)
        battery_w = v.get("battery_w")

        grid_w = None if raw["meter_w"] is None else -raw["meter_w"]
        inv_w = raw["inv_w"]
        v["grid_w"] = None if grid_w is None else round(grid_w)
        v["house_w"] = None if grid_w is None or inv_w is None else round(max(grid_w + inv_w, 0))
        if inv_w is None:
            v["solar_w"] = None
        elif not self._sun_up(inv_w):
            v["solar_w"] = 0
        else:
            v["solar_w"] = round(max(inv_w + (battery_w or 0), 0))
        v["battery_level"] = None if soe is None else round(soe)

        if battery_w is None or soe is None:
            v["battery_state"] = None
        elif battery_w > 150:
            v["battery_state"] = "Charging"
        elif battery_w < -150:
            v["battery_state"] = "Discharging"
        else:
            v["battery_state"] = "Full" if soe >= 99 else "Idle"

        if raw["grid_on"] is False:
            v["grid_state"] = "Outage"
        elif grid_w is None:
            v["grid_state"] = None
        else:
            v["grid_state"] = "Importing" if grid_w > 50 else "Exporting" if grid_w < -50 else "Balanced"

        house = v["house_w"]
        v["solar_share_now"] = (
            None if house is None or grid_w is None
            else 0 if house <= 0 else round(max((1 - max(grid_w, 0) / house) * 100, 0))
        )

        status, vendor = raw["status"], raw["vendor"]
        if status is None:
            v["inverter_status"] = "Offline"
        elif vendor not in (None, "0x0"):
            v["inverter_status"] = f"Fault {vendor}"
        elif status == "I_STATUS_MPPT":
            v["inverter_status"] = "Producing"
        elif status in ("I_STATUS_SLEEPING", "I_STATUS_OFF", "I_STATUS_STARTING"):
            v["inverter_status"] = "Sleeping"
        else:
            v["inverter_status"] = status.replace("I_STATUS_", "").title()


    def _accumulate(self, raw: dict[str, Any]) -> None:
        """Battery in/out from SoE deltas; PV = inverter AC delta + stored delta."""
        ac, soe = raw["ac_energy"], raw["soe"]
        if ac is None or soe is None:
            return
        acc, last, cap = self.data["acc"], self.data["last"], self.settings["battery_kwh"]
        if last.get("ac") is not None and last.get("soe") is not None:
            d_ac = ac - last["ac"]
            d_store = (soe - last["soe"]) / 100 * cap
            if not 0 <= d_ac < 50:  # counter reset or replacement
                d_ac = 0
            if abs(d_store) > cap:
                d_store = 0
            if d_store > 0:
                acc["charged"] += d_store
            else:
                acc["discharged"] -= d_store
            if self._sun_up(raw["inv_w"]):
                # Carry small negatives forward so SoE rounding can't bias PV upward.
                pending = self.data["pending"] + d_ac + d_store
                if pending > 0:
                    acc["solar"] += pending
                    pending = 0
                self.data["pending"] = max(pending, -0.05)
            else:
                self.data["pending"] = 0
        last["ac"], last["soe"] = ac, soe

    def _counters(self, raw: dict[str, Any]) -> dict[str, float | None]:
        acc = self.data["acc"]
        return {"solar": acc["solar"], "charged": acc["charged"], "discharged": acc["discharged"],
                "import": raw["import"], "export": raw["export"]}

    def _compute_today(self, raw: dict[str, Any]) -> None:
        today = dt_util.now().date().isoformat()
        cur = self._counters(raw)
        day = self.data["today"]

        if day is None or day["date"] != today:
            closing = self.data.get("last_today")
            if day is not None and closing and closing.get("date") == day["date"]:
                self.data["history"].append([
                    day["date"], closing["solar"], closing["house"],
                    closing["import"], closing["export"],
                ])
                self.data["history"] = self.data["history"][-HISTORY_DAYS:]
            day = self.data["today"] = {"date": today, "start": dict(cur), "cost": 0.0, "credit": 0.0}

        start = day["start"]
        for key, value in cur.items():
            if start.get(key) is None and value is not None:
                start[key] = value

        def delta(key):
            if cur[key] is None or start.get(key) is None:
                return None
            return max(cur[key] - start[key], 0)

        t = {k: delta(k) for k in cur}
        v = self.values

        # Month-to-date grid import, for tiered rate plans.
        month_key = today[:7]
        month = self.data.get("month") or {}
        if month.get("month") != month_key or month.get("start") is None:
            month = self.data["month"] = {"month": month_key, "start": cur["import"]}
        usage_month = None if cur["import"] is None or month["start"] is None else max(cur["import"] - month["start"], 0)

        rate = self.tariff.current(raw["price"], raw["period"], t["import"], usage_month)
        v["price"], v["period"], v["tier"] = rate["price"], rate["period"], rate["tier"]
        v["export_price"], v["next_price"] = rate["export"], rate["next"]

        last = self.data["last"]
        for key, field, price in (("import", "cost", rate["price"]), ("export", "credit", rate["export"])):
            prev = last.get(f"{key}_kwh")
            if cur[key] is not None:
                if price is not None and prev is not None and 0 <= cur[key] - prev < 50:
                    day[field] += (cur[key] - prev) * price
                last[f"{key}_kwh"] = cur[key]

        house = None
        if None not in t.values():
            house = max(t["import"] - t["export"] + t["solar"] - t["charged"] + t["discharged"], 0)
        v["today"] = t
        v["house_today"] = house
        v["self_sufficiency_today"] = (
            None if not house or t["import"] is None
            else round(max((1 - t["import"] / house) * 100, 0))
        )
        v["cost_today"] = round(day["cost"] - day["credit"], 2)
        if house is not None:
            self.data["last_today"] = {
                "date": today, "solar": round(t["solar"], 2), "house": round(house, 2),
                "import": round(t["import"], 2), "export": round(t["export"], 2),
            }

    # ----- performance -----

    def _typical_day(self, date_str: str) -> float | None:
        table = self.data["typical"].get("per_kw")
        if not table:
            return None
        d = datetime.date.fromisoformat(date_str)
        return table[d.month - 1] / calendar.monthrange(d.year, d.month)[1] * self.array_kw

    @callback
    def _compute_metrics(self) -> None:
        v = self.values
        history = self.data["history"]
        v["array_kw"] = round(self.array_kw, 2)
        typical_today = self._typical_day(dt_util.now().date().isoformat())
        v["typical_today"] = None if typical_today is None else round(typical_today, 2)
        solar_today = (v.get("today") or {}).get("solar")
        v["today_vs_typical"] = (
            None if solar_today is None or not v["typical_today"]
            else round(solar_today / v["typical_today"] * 100)
        )

        norms, best = [], (0.0, None)
        for day in history:
            typical = self._typical_day(day[0])
            if typical:
                norms.append(day[1] / typical)
            if day[1] > best[0]:
                best = (day[1], day[0])

        def top3(values):
            top = sorted(values, reverse=True)[:3]
            return sum(top) / len(top) if top else 0

        last7, sol7 = norms[-7:], [d[1] for d in history[-7:]]
        c7, call, cprior = top3(last7), top3(norms), top3(norms[-30:-7])
        trend = (c7 / cprior - 1) * 100 if cprior > 0 else None

        v["days"] = len(history)
        v["avg7"] = round(sum(sol7) / len(sol7), 1) if sol7 else None
        v["vs_typical"] = round(sum(last7) / len(last7) * 100) if last7 else None
        v["efficiency_spec"] = round(c7 * 100) if last7 else None
        v["efficiency_history"] = round(c7 / call * 100) if call > 0 else None
        v["trend_pct"] = None if trend is None else round(trend, 1)
        v["health"] = (
            "Learning" if trend is None
            else "Declining" if trend <= -3 else "Improving" if trend >= 3 else "Stable"
        )
        v["cleaning"] = (
            "Learning" if len(history) < 10
            else "Yes" if (v["efficiency_history"] or 100) < 90 else "No"
        )
        v["best_kwh"], v["best_date"] = best
        v["yesterday_solar"] = history[-1][1] if history else None
        v["yesterday_house"] = history[-1][2] if history else None

        v["lifetime_avg"] = None
        if self.settings.get("commissioned"):
            commissioned = datetime.date.fromisoformat(self.settings["commissioned"])
            days = (dt_util.now().date() - commissioned).days
            ac = self._num(self._source("sensor", f"{self.inverter.uid_base}_ac_energy_kwh"))
            if ac is not None and days > 0:
                v["lifetime_avg"] = round(ac / days, 1)

    async def _async_refresh_typical(self, _now=None) -> None:
        """Look up typical monthly output for this site's location and orientation."""
        if not self.entry.options.get(CONF_TYPICAL_LOOKUP, True):
            if self.data["typical"].get("per_kw"):
                self.data["typical"] = {"per_kw": None, "source": None, "params": None}
                self._save_later()
            return
        lat, lon = round(self.hass.config.latitude, 2), round(self.hass.config.longitude, 2)
        params = [lat, lon, self.settings["tilt"], self.settings["azimuth"]]
        typical = self.data["typical"]
        if typical.get("params") == params and (typical.get("source") or "").startswith("pvwatts"):
            return
        query = {
            "api_key": "DEMO_KEY", "lat": lat, "lon": lon, "system_capacity": 1,
            "tilt": self.settings["tilt"], "azimuth": self.settings["azimuth"],
            "array_type": 1, "module_type": 0, "losses": 14, "timeframe": "monthly",
        }
        try:
            async with asyncio.timeout(30):
                resp = await async_get_clientsession(self.hass).get(PVWATTS_URL, params=query)
                body = await resp.json(content_type=None)
            monthly = body["outputs"]["ac_monthly"]
            self.data["typical"] = {
                "per_kw": [round(x, 1) for x in monthly],
                "source": f"pvwatts v8 ({lat}, {lon}) tilt {params[2]} azimuth {params[3]}",
                "params": params,
            }
            _LOGGER.info("Typical solar output updated from PVWatts: %s", self.data["typical"]["source"])
        except Exception as err:  # keep the previous/built-in table
            _LOGGER.warning("PVWatts lookup failed (%s); retrying in 6 h", err)
            self._unsubs.append(async_call_later(self.hass, PVWATTS_RETRY_S, self._async_refresh_typical))
            return
        self._save_later()
        self._compute_metrics()
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))


# ----------------------------------------------------------------------------
# Entities
# ----------------------------------------------------------------------------

W, KWH, PCT = UnitOfPower.WATT, UnitOfEnergy.KILO_WATT_HOUR, PERCENTAGE
POWER, ENERGY, BATTERY, MONEY = (SensorDeviceClass.POWER, SensorDeviceClass.ENERGY,
                                 SensorDeviceClass.BATTERY, SensorDeviceClass.MONETARY)
MEAS, TOTAL_INC = SensorStateClass.MEASUREMENT, SensorStateClass.TOTAL_INCREASING


def _today(key):
    return lambda m: None if (m.values.get("today") or {}).get(key) is None else round(m.values["today"][key], 2)


# (key, entity object id, name, unit, device class, state class, icon, value)
SENSORS = [
    # live
    ("grid_power", "energy_grid_power", "Grid Power", W, POWER, MEAS, "mdi:transmission-tower", lambda m: m.values.get("grid_w")),
    ("house_power", "energy_house_power", "House Power", W, POWER, MEAS, "mdi:home-lightning-bolt", lambda m: m.values.get("house_w")),
    ("solar_power", "energy_solar_power", "Solar Power", W, POWER, MEAS, "mdi:solar-power", lambda m: m.values.get("solar_w")),
    ("battery_power", "energy_battery_power", "Battery Power", W, POWER, MEAS, "mdi:home-battery", lambda m: m.values.get("battery_w")),
    ("battery_level", "energy_battery_level", "Battery Level", PCT, BATTERY, MEAS, None, lambda m: m.values.get("battery_level")),
    ("battery_state", "energy_battery_state", "Battery State", None, None, None, "mdi:battery-sync", lambda m: m.values.get("battery_state")),
    ("grid_state", "energy_grid_state", "Grid State", None, None, None, "mdi:transmission-tower", lambda m: m.values.get("grid_state")),
    ("solar_share_now", "energy_solar_share_now", "Solar Share Now", PCT, None, MEAS, "mdi:solar-power-variant", lambda m: m.values.get("solar_share_now")),
    ("inverter_status", "energy_inverter_status", "Inverter Status", None, None, None, "mdi:solar-panel", lambda m: m.values.get("inverter_status")),
    ("price_now", "energy_price_now", "Price Now", "CUR/kWh", None, MEAS, "mdi:currency-usd", lambda m: m.values.get("price")),
    ("export_price_now", "energy_export_price_now", "Export Price Now", "CUR/kWh", None, MEAS, "mdi:cash-plus", lambda m: m.values.get("export_price")),
    ("price_period", "energy_price_period", "Price Period", None, None, None, "mdi:clock-time-four-outline", lambda m: m.values.get("period")),
    # running totals (continue the statistics of the former template sensors)
    ("solar_production", "solar_pv_production_energy", "Solar Production", KWH, ENERGY, TOTAL_INC, "mdi:solar-power", lambda m: round(m.data["acc"]["solar"], 3)),
    ("battery_charged", "battery_charged_energy", "Battery Charged", KWH, ENERGY, TOTAL_INC, "mdi:battery-arrow-up", lambda m: round(m.data["acc"]["charged"], 3)),
    ("battery_discharged", "battery_discharged_energy", "Battery Discharged", KWH, ENERGY, TOTAL_INC, "mdi:battery-arrow-down", lambda m: round(m.data["acc"]["discharged"], 3)),
    # today
    ("solar_today", "energy_solar_today", "Solar Today", KWH, ENERGY, None, "mdi:solar-power", _today("solar")),
    ("house_today", "energy_house_today", "House Today", KWH, ENERGY, None, "mdi:home-lightning-bolt-outline", lambda m: None if m.values.get("house_today") is None else round(m.values["house_today"], 2)),
    ("grid_import_today", "energy_grid_import_today", "Grid Import Today", KWH, ENERGY, None, "mdi:transmission-tower-import", _today("import")),
    ("grid_export_today", "energy_grid_export_today", "Grid Export Today", KWH, ENERGY, None, "mdi:transmission-tower-export", _today("export")),
    ("battery_charged_today", "energy_battery_charged_today", "Battery Charged Today", KWH, ENERGY, None, "mdi:battery-arrow-up", _today("charged")),
    ("battery_discharged_today", "energy_battery_discharged_today", "Battery Discharged Today", KWH, ENERGY, None, "mdi:battery-arrow-down", _today("discharged")),
    ("self_sufficiency_today", "energy_self_sufficiency_today", "Self Sufficiency Today", PCT, None, MEAS, "mdi:leaf", lambda m: m.values.get("self_sufficiency_today")),
    ("grid_cost_today", "energy_grid_cost_today", "Grid Cost Today", "CUR", MONEY, None, "mdi:currency-usd", lambda m: m.values.get("cost_today")),
    # performance
    ("solar_array_size", "energy_solar_array_size", "Solar Array Size", "kW", None, None, "mdi:solar-panel-large", lambda m: m.values.get("array_kw")),
    ("solar_typical_today", "energy_solar_typical_today", "Solar Typical Today", KWH, None, None, "mdi:weather-sunny", lambda m: m.values.get("typical_today")),
    ("solar_today_vs_typical", "energy_solar_today_vs_typical", "Solar Today vs Typical", PCT, None, MEAS, "mdi:gauge", lambda m: m.values.get("today_vs_typical")),
    ("solar_7_day_average", "energy_solar_7_day_average", "Solar 7 Day Average", KWH, None, MEAS, "mdi:chart-line", lambda m: m.values.get("avg7")),
    ("solar_vs_typical", "energy_solar_vs_typical", "Solar vs Typical", PCT, None, MEAS, "mdi:map-marker-radius", lambda m: m.values.get("vs_typical")),
    ("solar_efficiency_spec", "energy_solar_efficiency_spec", "Solar Efficiency vs Spec", PCT, None, MEAS, "mdi:solar-panel-large", lambda m: m.values.get("efficiency_spec")),
    ("solar_efficiency_history", "energy_solar_efficiency_history", "Solar Efficiency vs History", PCT, None, MEAS, "mdi:history", lambda m: m.values.get("efficiency_history")),
    ("solar_health_trend", "energy_solar_health_trend", "Solar Health Trend", None, None, None, "mdi:heart-pulse", lambda m: m.values.get("health")),
    ("solar_cleaning_recommended", "energy_solar_cleaning_recommended", "Solar Cleaning Recommended", None, None, None, "mdi:spray-bottle", lambda m: m.values.get("cleaning")),
    ("solar_best_day", "energy_solar_best_day", "Solar Best Day", KWH, None, None, "mdi:trophy", lambda m: m.values.get("best_kwh") or None),
    ("solar_best_day_date", "energy_solar_best_day_date", "Solar Best Day Date", None, None, None, "mdi:calendar-star", lambda m: m.values.get("best_date")),
    ("solar_yesterday", "energy_solar_yesterday", "Solar Yesterday", KWH, None, None, "mdi:solar-power", lambda m: m.values.get("yesterday_solar")),
    ("house_yesterday", "energy_house_yesterday", "House Yesterday", KWH, None, None, "mdi:home-lightning-bolt-outline", lambda m: m.values.get("yesterday_house")),
    ("solar_lifetime_daily_average", "energy_solar_lifetime_daily_average", "Solar Lifetime Daily Average", KWH, None, None, "mdi:sigma", lambda m: m.values.get("lifetime_avg")),
    ("daily_history", "energy_daily_history", "Daily History", "days", None, None, "mdi:history", lambda m: m.values.get("days")),
]

ATTRIBUTES: dict[str, Callable[[EnergyModel], dict[str, Any]]] = {
    "grid_power": lambda m: {"sign": "+ importing, - exporting"},
    "battery_power": lambda m: {"sign": "+ charging, - discharging", "estimated": True},
    "solar_power": lambda m: {"estimated": True},
    "grid_cost_today": lambda m: {"export_credit": m.tariff.state.get("export_mode")},
    "price_now": lambda m: {"tier": m.values.get("tier"), "source": m.tariff.state.get("source")},
    "solar_typical_today": lambda m: {"source": m.data["typical"].get("source")},
    "solar_health_trend": lambda m: {"change_pct": m.values.get("trend_pct")},
    "solar_cleaning_recommended": lambda m: {"rule": "Yes when clear-day output is >10% below this system's season-adjusted best"},
    "solar_efficiency_spec": lambda m: {"meaning": "Best 3 of last 7 days vs typical for this array size; ~100-115% healthy"},
    "solar_efficiency_history": lambda m: {"meaning": "Best 3 of last 7 days vs this system's own best, season-adjusted"},
    "solar_lifetime_daily_average": lambda m: {"since": m.settings.get("commissioned")},
    "daily_history": lambda m: {"days": m.data["history"]},
}


class EnergyEntityMixin:
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, model: EnergyModel, key: str, object_id: str, domain: str):
        self._model = model
        self._attr_unique_id = f"{model.uid_base}_{key}"
        self._attr_device_info = model.device_info
        self.entity_id = f"{domain}.{object_id}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(
            self.hass, signal_update(self._model.entry.entry_id), self.async_write_ha_state
        ))


class EnergySensor(EnergyEntityMixin, SensorEntity):
    _unrecorded_attributes = frozenset({"days"})

    def __init__(self, model, key, object_id, name, unit, device_class, state_class, icon, value_fn):
        super().__init__(model, key, object_id, "sensor")
        self._key = key
        self._value_fn = value_fn
        self._attr_name = name
        if unit and "CUR" in unit:
            unit = unit.replace("CUR", model.hass.config.currency)
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        if icon:
            self._attr_icon = icon

    @property
    def native_value(self):
        try:
            return self._value_fn(self._model)
        except (KeyError, TypeError, ZeroDivisionError):
            return None

    @property
    def extra_state_attributes(self):
        fn = ATTRIBUTES.get(self._key)
        return fn(self._model) if fn else None


# (key, object id, name, min, max, step, unit, icon)
NUMBERS = [
    ("panel_count", "energy_solar_panel_count", "Solar Panel Count", 1, 200, 1, None, "mdi:solar-panel"),
    ("panel_watts", "energy_solar_panel_watts", "Solar Panel Watts", 50, 1000, 5, UnitOfPower.WATT, "mdi:flash"),
    ("tilt", "energy_solar_array_tilt", "Solar Array Tilt", 0, 90, 1, "°", "mdi:angle-acute"),
    ("azimuth", "energy_solar_array_azimuth", "Solar Array Azimuth", 0, 359, 1, "°", "mdi:compass"),
    ("battery_kwh", "energy_battery_capacity", "Battery Usable Capacity", 0.5, 200, 0.01, KWH, "mdi:battery-high"),
]


class EnergyNumber(EnergyEntityMixin, NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(self, model, key, object_id, name, nmin, nmax, step, unit, icon):
        super().__init__(model, key, object_id, "number")
        self._key = key
        self._attr_name = name
        self._attr_native_min_value = nmin
        self._attr_native_max_value = nmax
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon

    @property
    def native_value(self):
        return self._model.settings[self._key]

    async def async_set_native_value(self, value: float) -> None:
        if self._key in ("panel_count", "tilt", "azimuth"):
            value = int(value)
        self._model.async_set_setting(self._key, value)


class EnergyCommissionedDate(EnergyEntityMixin, DateEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:calendar-check"
    _attr_name = "Solar Commissioned"

    def __init__(self, model):
        super().__init__(model, "commissioned", "energy_solar_commissioned", "date")

    @property
    def native_value(self) -> datetime.date | None:
        value = self._model.settings.get("commissioned")
        return datetime.date.fromisoformat(value) if value else None

    async def async_set_value(self, value: datetime.date) -> None:
        self._model.async_set_setting("commissioned", value.isoformat())


def _model(hass: HomeAssistant, entry: ConfigEntry) -> EnergyModel | None:
    return hass.data[DOMAIN][entry.entry_id].get("energy")


def energy_sensors(hass: HomeAssistant, entry: ConfigEntry) -> list[SensorEntity]:
    from .tariff import tariff_sensors

    model = _model(hass, entry)
    if not model:
        return []
    from .snapshot import DashboardImageUrl

    return [EnergySensor(model, *spec) for spec in SENSORS] + tariff_sensors(model) + [DashboardImageUrl(model)]


def energy_numbers(hass: HomeAssistant, entry: ConfigEntry) -> list[NumberEntity]:
    from .tariff import tariff_numbers

    model = _model(hass, entry)
    if not model:
        return []
    return [EnergyNumber(model, *spec) for spec in NUMBERS] + tariff_numbers(model)


def energy_images(hass: HomeAssistant, entry: ConfigEntry) -> list:
    from .snapshot import DashboardImage

    model = _model(hass, entry)
    return [DashboardImage(model)] if model else []


def energy_selects(hass: HomeAssistant, entry: ConfigEntry) -> list:
    from .tariff import tariff_selects

    model = _model(hass, entry)
    return tariff_selects(model) if model else []


def energy_dates(hass: HomeAssistant, entry: ConfigEntry) -> list[DateEntity]:
    model = _model(hass, entry)
    return [EnergyCommissionedDate(model)] if model else []
