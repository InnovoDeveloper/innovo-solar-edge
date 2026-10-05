"""Electricity rate plans: manual time-of-use plans, price sensor, overrides.

A plan is plain data, editable in the options flow (text rows) or over the API
(`innovo_solar_edge.set_tariff` with JSON, `set_tariff_rates` by period name):

    {
      "name": "My TOU plan", "currency": "USD", "tier_period": "month",
      "seasons": [
        {"name": "Summer", "months": [6, 7, 8, 9],
         "weekday": [["00:00", 0.40, "Off-peak"], ["16:00", 0.64, "On-peak"], ["21:00", 0.40, "Off-peak"]],
         "weekend": [["00:00", 0.40, "Off-peak"], ["16:00", 0.52, "Mid-peak"], ["21:00", 0.40, "Off-peak"]]},
        {"name": "Winter", "months": [1, 2, 3, 4, 5, 10, 11, 12], "weekday": [...]}
      ]
    }

Each row starts at a time and lasts until the next row. A rate is $/kWh or a
list of tiers [{"upto": 40, "rate": 0.07}, {"rate": 0.11}], where "upto" is kWh
of grid import in the tier period ("day" or calendar "month"). Missing
"weekend" rows reuse the weekday rows. Holidays are treated as normal days.

Price precedence: price override number > price sensor (if selected) > plan.
"""

from __future__ import annotations

import copy
import datetime
import logging
import re
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

SRC_PLAN = "Rate plan"
SRC_SENSOR = "Price sensor"
SOURCES = [SRC_PLAN, SRC_SENSOR]

EXPORT_SAME = "Same as import"
EXPORT_FIXED = "Fixed rate"
EXPORT_NONE = "No credit"
EXPORT_MODES = [EXPORT_SAME, EXPORT_FIXED, EXPORT_NONE]

DEFAULT_STATE = {
    "source": SRC_PLAN,
    "plan": None,
    "override": 0.0,
    "export_mode": EXPORT_SAME,
    "export_rate": 0.0,
}

ROW = re.compile(r"^\s*(\d{1,2}):(\d{2})\s+([0-9]*\.?[0-9]+)\s*(.*?)\s*$")


# ----------------------------------------------------------------------------
# Plan evaluation
# ----------------------------------------------------------------------------

def _minutes(hhmm: str) -> int:
    hour, minute = hhmm.split(":")[:2]
    return int(hour) * 60 + int(minute)


def _rows_for(plan: dict, when: datetime.datetime) -> list:
    for season in plan.get("seasons", []):
        if when.month in season.get("months", []):
            rows = season.get("weekend") if when.weekday() >= 5 else None
            return sorted(rows or season.get("weekday") or [], key=lambda r: _minutes(r[0]))
    return []


def _rate(value: Any, usage: float | None) -> tuple[float | None, int | None]:
    if isinstance(value, (int, float)):
        return float(value), None
    if isinstance(value, list) and value:
        for index, tier in enumerate(value):
            if tier.get("upto") is None or (usage or 0) < tier["upto"]:
                return float(tier["rate"]), index + 1
        return float(value[-1]["rate"]), len(value)
    return None, None


def plan_price(plan: dict, when: datetime.datetime, usage_day: float | None,
               usage_month: float | None) -> tuple[float | None, str | None, int | None]:
    rows = _rows_for(plan, when)
    if not rows:
        return None, None, None
    now = when.hour * 60 + when.minute
    row = rows[-1]  # before the first row of the day, the last row carries over
    for candidate in rows:
        if _minutes(candidate[0]) <= now:
            row = candidate
    usage = usage_day if plan.get("tier_period") == "day" else usage_month
    price, tier = _rate(row[1], usage)
    return price, (row[2] if len(row) > 2 and row[2] else None), tier


def validate_plan(plan: dict) -> dict:
    if not isinstance(plan, dict) or not plan.get("seasons"):
        raise HomeAssistantError("Plan needs a 'seasons' list")
    months: set[int] = set()
    for season in plan["seasons"]:
        if not season.get("months") or not season.get("weekday"):
            raise HomeAssistantError("Each season needs 'months' and 'weekday' rows")
        overlap = months & set(season["months"])
        if overlap:
            raise HomeAssistantError(f"Months {sorted(overlap)} are in more than one season")
        months.update(season["months"])
        for key in ("weekday", "weekend"):
            rows = season.get(key) or []
            if rows and min(_minutes(r[0]) for r in rows) != 0:
                raise HomeAssistantError(f"{season.get('name', 'Season')} {key} rows must start at 00:00")
            for row in rows:
                if not 0 <= _minutes(row[0]) < 24 * 60 or _rate(row[1], 0)[0] is None:
                    raise HomeAssistantError(f"Bad row {row}")
    if months != set(range(1, 13)):
        raise HomeAssistantError(f"Seasons must cover all 12 months (missing {sorted(set(range(1, 13)) - months)})")
    if plan.get("tier_period", "month") not in ("day", "month"):
        raise HomeAssistantError("tier_period must be 'day' or 'month'")
    return plan


# ----------------------------------------------------------------------------
# Text form used by the options flow: "16:00 0.64 On-peak" per line (or ';')
# ----------------------------------------------------------------------------

def parse_rows(text: str) -> list[list]:
    rows = []
    for part in re.split(r"[;\n]+", text or ""):
        if not part.strip():
            continue
        match = ROW.match(part)
        if not match:
            raise HomeAssistantError(f"Can't read '{part.strip()}' - use 'HH:MM price name'")
        hour, minute, price, label = match.groups()
        rows.append([f"{int(hour):02d}:{minute}", float(price), label or None])
    return sorted(rows, key=lambda r: _minutes(r[0]))


def format_rows(rows: list | None) -> str:
    out = []
    for row in rows or []:
        if isinstance(row[1], (int, float)):
            out.append(f"{row[0]} {row[1]:g} {row[2] or ''}".strip())
    return "\n".join(out)


def parse_months(text: str) -> list[int]:
    months: set[int] = set()
    for part in re.split(r"[,\s]+", (text or "").strip()):
        if not part:
            continue
        if "-" in part:
            start, end = (int(x) for x in part.split("-", 1))
            span = range(start, end + 1) if start <= end else [*range(start, 13), *range(1, end + 1)]
            months.update(span)
        else:
            months.add(int(part))
    if not months <= set(range(1, 13)):
        raise HomeAssistantError("Months must be 1-12")
    return sorted(months)


def format_months(months: list[int] | None) -> str:
    return ",".join(str(m) for m in months or [])


# ----------------------------------------------------------------------------
# Manager (owned by EnergyModel)
# ----------------------------------------------------------------------------

class TariffManager:
    def __init__(self, model, default_source: str):
        self.model = model
        stored = model.data.get("tariff") or {}
        self.state = model.data["tariff"] = {**copy.deepcopy(DEFAULT_STATE), "source": default_source, **stored}

    @property
    def plan(self) -> dict | None:
        return self.state.get("plan")

    def _changed(self) -> None:
        self.model.save_soon()
        self.model.recompute()
        async_dispatcher_send(self.model.hass, self.model.signal)

    def set_plan(self, plan: dict) -> None:
        plan = validate_plan(copy.deepcopy(plan))
        plan.setdefault("name", "Rate plan")
        plan.setdefault("currency", self.model.hass.config.currency)
        plan.setdefault("tier_period", "month")
        plan["updated"] = dt_util.now().isoformat(timespec="seconds")
        self.state["plan"] = plan
        self.state["source"] = SRC_PLAN
        self._changed()

    def set_rates(self, rates: dict[str, float], season: str | None = None) -> int:
        """Change rates by period name, e.g. {"On-peak": 0.64}."""
        if not self.plan:
            raise HomeAssistantError("No rate plan yet - create one first")
        plan = copy.deepcopy(self.plan)
        wanted = {k.strip().lower(): float(v) for k, v in rates.items()}
        changed = 0
        for s in plan["seasons"]:
            if season and s.get("name", "").lower() != season.lower():
                continue
            for key in ("weekday", "weekend"):
                for row in s.get(key) or []:
                    label = (row[2] if len(row) > 2 and row[2] else "").lower()
                    if label in wanted and isinstance(row[1], (int, float)):
                        row[1] = wanted[label]
                        changed += 1
        if not changed:
            raise HomeAssistantError(f"No rows named {list(rates)}")
        self.set_plan(plan)
        return changed

    def set_value(self, key: str, value) -> None:
        self.state[key] = value
        self._changed()

    def current(self, sensor_price, sensor_period, usage_day, usage_month) -> dict[str, Any]:
        now = dt_util.now()
        tier = nxt = None
        if self.state.get("override"):
            price, period = float(self.state["override"]), "Override"
        elif self.state["source"] == SRC_SENSOR:
            price, period = sensor_price, sensor_period
        elif self.plan:
            price, period, tier = plan_price(self.plan, now, usage_day, usage_month)
            nxt = self._next_change(now, price, usage_day, usage_month)
        else:
            price, period = None, None

        mode = self.state.get("export_mode", EXPORT_SAME)
        if mode == EXPORT_SAME:
            export = price
        elif mode == EXPORT_FIXED:
            export = float(self.state.get("export_rate") or 0)
        else:
            export = 0.0
        return {"price": price, "period": period, "tier": tier, "export": export, "next": nxt}

    def _next_change(self, now, price, usage_day, usage_month):
        step = datetime.timedelta(minutes=15)
        when = now.replace(second=0, microsecond=0) - datetime.timedelta(minutes=now.minute % 15) + step
        for _ in range(8 * 96):
            p, label, _tier = plan_price(self.plan, when, usage_day, usage_month)
            if p != price:
                return {"at": when, "price": p, "period": label}
            when += step
        return None

    def attributes(self) -> dict[str, Any]:
        plan = self.plan or {}
        return {
            "source": self.state["source"],
            "currency": plan.get("currency"),
            "tier_period": plan.get("tier_period"),
            "updated": plan.get("updated"),
            "seasons": plan.get("seasons"),
            "export_credit": self.state.get("export_mode"),
            "override": self.state.get("override") or None,
        }


# ----------------------------------------------------------------------------
# Entities
# ----------------------------------------------------------------------------

def _base():
    from .energy import EnergyEntityMixin
    return EnergyEntityMixin


class TariffSelect(_base(), SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, model, key, object_id, name, icon, options):
        super().__init__(model, key, object_id, "select")
        self._key = key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_options = options

    @property
    def current_option(self) -> str | None:
        return self._model.tariff.state.get(self._key)

    async def async_select_option(self, option: str) -> None:
        self._model.tariff.set_value(self._key, option)


class TariffNumber(_base(), NumberEntity):
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 10
    _attr_native_step = 0.00001

    def __init__(self, model, key, object_id, name, icon):
        super().__init__(model, key, object_id, "number")
        self._key = key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_native_unit_of_measurement = f"{model.hass.config.currency}/kWh"

    @property
    def native_value(self) -> float:
        return float(self._model.tariff.state.get(self._key) or 0)

    async def async_set_native_value(self, value: float) -> None:
        self._model.tariff.set_value(self._key, float(value))


class TariffSensor(_base(), SensorEntity):
    _unrecorded_attributes = frozenset({"seasons"})
    _attr_icon = "mdi:file-document-outline"
    _attr_name = "Tariff"

    def __init__(self, model):
        super().__init__(model, "tariff", "energy_tariff", "sensor")

    @property
    def native_value(self):
        tariff = self._model.tariff
        if tariff.state["source"] == SRC_SENSOR:
            return "Price sensor"
        return tariff.plan.get("name") if tariff.plan else "Not set"

    @property
    def extra_state_attributes(self):
        return self._model.tariff.attributes()


class NextPriceChange(_base(), SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-alert-outline"
    _attr_name = "Next Price Change"

    def __init__(self, model):
        super().__init__(model, "next_price_change", "energy_next_price_change", "sensor")

    @property
    def native_value(self):
        nxt = self._model.values.get("next_price")
        return nxt["at"] if nxt else None

    @property
    def extra_state_attributes(self):
        nxt = self._model.values.get("next_price") or {}
        return {"next_price": nxt.get("price"), "next_period": nxt.get("period")}


def tariff_selects(model) -> list[SelectEntity]:
    return [
        TariffSelect(model, "source", "energy_tariff_source", "Tariff Source", "mdi:database-cog", SOURCES),
        TariffSelect(model, "export_mode", "energy_export_credit", "Export Credit", "mdi:transmission-tower-export", EXPORT_MODES),
    ]


def tariff_numbers(model) -> list[NumberEntity]:
    return [
        TariffNumber(model, "override", "energy_price_override", "Price Override", "mdi:currency-usd-off"),
        TariffNumber(model, "export_rate", "energy_export_rate", "Export Rate", "mdi:cash-plus"),
    ]


def tariff_sensors(model) -> list[SensorEntity]:
    return [TariffSensor(model), NextPriceChange(model)]
