"""Provision the HA Energy dashboard and a Solar & Energy dashboard.

Everything is built from this config entry's own entities (looked up by unique
id), so it works on any site regardless of entity naming. Re-running it is safe:
it replaces the grid/solar/battery sources of the Energy dashboard (other sources
such as water or gas, and individual devices, are kept) and overwrites the
Solar & Energy dashboard it owns.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN
from .energy import CONF_PRICE_ENTITY, EnergyEntityMixin, EnergyModel

_LOGGER = logging.getLogger(__name__)

DEFAULT_URL_PATH = "solar-energy"
DEFAULT_TITLE = "Solar & Energy"


def _resolver(hass: HomeAssistant, model: EnergyModel):
    reg = er.async_get(hass)

    def ent(platform: str, unique_id: str) -> str | None:
        return reg.async_get_entity_id(platform, DOMAIN, unique_id)

    def energy(key: str, platform: str = "sensor") -> str | None:
        return ent(platform, f"{model.uid_base}_{key}")

    return ent, energy


def _grid_meter(model: EnergyModel):
    return next((m for m in model.hub.meters if "Import" in str(m.option)), None)


def _has_battery(model: EnergyModel) -> bool:
    return bool(model.hub.batteries or model.hub.der_batteries)


async def async_provision_energy(hass: HomeAssistant, entry: ConfigEntry, model: EnergyModel) -> None:
    from homeassistant.components.energy.data import async_get_manager

    ent, energy = _resolver(hass, model)
    price = entry.options.get(CONF_PRICE_ENTITY)
    sources: list[dict[str, Any]] = []

    meter = _grid_meter(model)
    if meter is not None:
        grid = {
            "type": "grid",
            "stat_energy_from": ent("sensor", f"{meter.uid_base}_imported_kwh"),
            "stat_energy_to": ent("sensor", f"{meter.uid_base}_exported_kwh"),
            "stat_cost": None,
            "entity_energy_price": price,
            "number_energy_price": None,
            "stat_compensation": None,
            "entity_energy_price_export": price,
            "number_energy_price_export": None,
            "cost_adjustment_day": 0.0,
        }
        if meter_power := ent("sensor", f"{meter.uid_base}_ac_power"):
            grid["power_config"] = {"stat_rate_inverted": meter_power}
        sources.append(grid)

    solar = {"type": "solar", "stat_energy_from": energy("solar_production"),
             "config_entry_solar_forecast": None}
    if solar_power := energy("solar_power"):
        solar["stat_rate"] = solar_power
    sources.append(solar)

    if _has_battery(model):
        battery = {
            "type": "battery",
            "stat_energy_from": energy("battery_discharged"),
            "stat_energy_to": energy("battery_charged"),
            "stat_soc": energy("battery_level"),
            "capacity": model.settings["battery_kwh"],
        }
        if battery_power := energy("battery_power"):
            battery["power_config"] = {"stat_rate_inverted": battery_power}
        sources.append(battery)

    if any(not s.get("stat_energy_from") for s in sources):
        raise HomeAssistantError("Energy entities are not registered yet; try again after the first poll")

    manager = await async_get_manager(hass)
    current = (manager.data or {}).get("energy_sources", [])
    kept = [s for s in current if s.get("type") not in ("grid", "solar", "battery")]
    await manager.async_update({"energy_sources": kept + sources})
    _LOGGER.info("Energy dashboard provisioned with %d SolarEdge sources", len(sources))


def _dashboard_config(hass: HomeAssistant, model: EnergyModel, title: str) -> dict[str, Any]:
    _, energy = _resolver(hass, model)

    def rows(*specs):
        return [{"entity": e, "name": n} for e, n in ((energy(k, *p), n) for k, n, *p in specs) if e]

    def entities_card(card_title, *specs):
        return {"type": "entities", "title": card_title, "entities": rows(*specs)}

    battery = _has_battery(model)
    gauges = [
        {"type": "gauge", "entity": energy("solar_efficiency_spec"), "name": "Efficiency vs spec",
         "min": 0, "max": 130, "severity": {"green": 95, "yellow": 85, "red": 0}},
        {"type": "gauge", "entity": energy("self_sufficiency_today"), "name": "Self-sufficiency today",
         "min": 0, "max": 100, "severity": {"green": 60, "yellow": 30, "red": 0}},
    ]
    if battery:
        gauges.insert(0, {"type": "gauge", "entity": energy("battery_level"), "name": "Battery",
                          "min": 0, "max": 100, "severity": {"green": 50, "yellow": 20, "red": 0}})

    live = [("solar_power", "Solar"), ("house_power", "House"),
            ("grid_power", "Grid (+ buying / - selling)")]
    if battery:
        live += [("battery_power", "Battery (+ charging / - discharging)"), ("battery_state", "Battery state")]
    live += [("grid_state", "Grid state"), ("solar_share_now", "Solar share now"),
             ("price_period", "Rate period"), ("price_now", "Price now"), ("inverter_status", "Inverter")]

    today = [("solar_today", "Solar"), ("solar_typical_today", "Typical for today"),
             ("solar_today_vs_typical", "Solar vs typical"), ("house_today", "House used"),
             ("grid_import_today", "Bought from grid"), ("grid_export_today", "Sold to grid")]
    if battery:
        today += [("battery_charged_today", "Battery charged"), ("battery_discharged_today", "Battery discharged")]
    today += [("self_sufficiency_today", "Self-sufficiency"), ("grid_cost_today", "Grid cost")]

    settings = [("panel_count", "Number of panels", "number"),
                ("panel_watts", "Watts per panel", "number"),
                ("tilt", "Roof tilt", "number"),
                ("azimuth", "Roof direction (180 = south)", "number")]
    if battery:
        settings.append(("battery_kwh", "Battery usable capacity", "number"))
    settings += [("commissioned", "System commissioned", "date"), ("solar_array_size", "Array size")]

    graph = [e for e in (
        {"entity": energy("solar_power"), "name": "Solar"},
        {"entity": energy("house_power"), "name": "House"},
        {"entity": energy("grid_power"), "name": "Grid"},
        {"entity": energy("battery_level"), "name": "Battery %"} if battery else None,
    ) if e and e["entity"]]

    cards = [
        {"type": "horizontal-stack", "cards": [g for g in gauges if g["entity"]]},
        {"type": "energy-date-selection"},
        {"type": "energy-distribution", "title": "Energy distribution", "link_dashboard": True},
        {"type": "energy-sankey", "title": "Where the energy went"},
        entities_card("Live", *live),
        entities_card("Today", *today),
        entities_card(
            "Solar performance",
            ("solar_efficiency_spec", "Efficiency vs spec (clear days)"),
            ("solar_efficiency_history", "Efficiency vs own best"),
            ("solar_vs_typical", "Last 7 days vs typical"),
            ("solar_health_trend", "Health trend"),
            ("solar_cleaning_recommended", "Cleaning recommended"),
            ("solar_7_day_average", "7-day average"),
            ("solar_yesterday", "Yesterday"),
            ("solar_best_day", "Best day"),
            ("solar_best_day_date", "Best day date"),
            ("solar_lifetime_daily_average", "Lifetime daily average"),
        ),
        entities_card("System settings (edit to match the installation)", *settings),
        {"type": "history-graph", "title": "Last 24 hours", "hours_to_show": 24, "entities": graph},
    ]
    return {"title": title, "views": [{"title": title, "path": "overview", "cards": cards}]}


def _dashboards_collection(hass: HomeAssistant):
    """The live storage-dashboards collection (held by HA's websocket handler)."""
    handler = hass.data.get("websocket_api", {}).get("lovelace/dashboards/create", (None,))[0]
    handler = getattr(handler, "__wrapped__", handler)
    return getattr(getattr(handler, "__self__", None), "storage_collection", None)


async def async_provision_dashboard(
    hass: HomeAssistant, model: EnergyModel, url_path: str, title: str
) -> None:
    from homeassistant.components.lovelace.const import LOVELACE_DATA

    lovelace = hass.data.get(LOVELACE_DATA)
    if lovelace is None:
        raise HomeAssistantError("Lovelace is not loaded")

    if url_path not in lovelace.dashboards:
        collection = _dashboards_collection(hass)
        if collection is None:
            raise HomeAssistantError(
                f"Could not create the dashboard automatically. Create an empty dashboard "
                f"with URL '{url_path}' in Settings > Dashboards, then run this again."
            )
        await collection.async_create_item({
            "url_path": url_path, "title": title, "icon": "mdi:solar-power-variant",
            "show_in_sidebar": True, "require_admin": False,
        })

    dashboard = lovelace.dashboards.get(url_path)
    if dashboard is None or not hasattr(dashboard, "async_save") or dashboard.mode != "storage":
        raise HomeAssistantError(f"Dashboard '{url_path}' is not a UI-managed (storage) dashboard")
    await dashboard.async_save(_dashboard_config(hass, model, title))
    _LOGGER.info("Dashboard '%s' provisioned", url_path)


class ProvisionButton(EnergyEntityMixin, ButtonEntity):
    """One-press setup of the Energy dashboard and the Solar & Energy dashboard."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:view-dashboard-edit"
    _attr_name = "Provision Dashboards"

    def __init__(self, model: EnergyModel):
        super().__init__(model, "provision_dashboards", "energy_provision_dashboards", "button")

    async def async_press(self) -> None:
        await async_provision(self.hass, self._model.entry)


def energy_buttons(hass: HomeAssistant, entry: ConfigEntry) -> list[ButtonEntity]:
    model = hass.data[DOMAIN][entry.entry_id].get("energy")
    return [ProvisionButton(model)] if model else []


async def async_provision(
    hass: HomeAssistant,
    entry: ConfigEntry,
    energy_dashboard: bool = True,
    dashboard: bool = True,
    url_path: str = DEFAULT_URL_PATH,
    title: str = DEFAULT_TITLE,
) -> None:
    model: EnergyModel | None = hass.data[DOMAIN][entry.entry_id].get("energy")
    if model is None:
        raise HomeAssistantError("This SolarEdge entry has no inverter, nothing to provision")
    if energy_dashboard:
        await async_provision_energy(hass, entry, model)
    if dashboard:
        await async_provision_dashboard(hass, model, url_path, title)
