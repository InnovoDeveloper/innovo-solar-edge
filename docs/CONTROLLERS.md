# Reading Innovo Solar Edge from a controller

Controllers should read **Home Assistant**, never the inverter
directly: the inverter accepts only one Modbus TCP client and Home Assistant
holds it.

## Access

Create a long-lived access token in HA (Profile → Security) and use the REST API:

```
GET  /api/states/<entity_id>                       # read one value
GET  /api/states                                   # read all (filter by entity_id)
POST /api/services/number/set_value                # change a setting
     {"entity_id": "number.energy_solar_panel_watts", "value": 400}
POST /api/services/innovo_solar_edge/provision_dashboards   # rebuild dashboards
```

For a push feed instead of polling, subscribe to `state_changed` over the HA
WebSocket API.

Live values update on every inverter poll (set the polling interval to 10–15 s
in the integration options).

## Entity ids

New installs create these ids. If an id is taken or renamed on a site, find the
entity by its unique id suffix (`<inverter>_energy_<key>`) on the
*SolarEdge Energy* device of the `innovo_solar_edge` integration.

### Live

| Entity | Unit | Meaning |
|---|---|---|
| `sensor.energy_solar_power` | W | Solar production now (estimated with SoE-only batteries) |
| `sensor.energy_house_power` | W | House consumption now |
| `sensor.energy_grid_power` | W | **+ buying**, − selling |
| `sensor.energy_battery_power` | W | **+ charging**, − discharging (estimated with SoE-only batteries) |
| `sensor.energy_battery_level` | % | Battery state of energy |
| `sensor.energy_battery_state` | text | `Charging` / `Discharging` / `Full` / `Idle` |
| `sensor.energy_grid_state` | text | `Importing` / `Exporting` / `Balanced` / `Outage` |
| `sensor.energy_solar_share_now` | % | Share of the house not supplied by the grid |
| `sensor.energy_inverter_status` | text | `Producing` / `Sleeping` / `Offline` / `Fault <code>` |
| `sensor.energy_price_now` | $/kWh | From the configured price sensor |
| `sensor.energy_price_period` | text | `period` attribute of the price sensor, if any |

### Today (reset at local midnight)

| Entity | Unit |
|---|---|
| `sensor.energy_solar_today` | kWh |
| `sensor.energy_house_today` | kWh |
| `sensor.energy_grid_import_today` | kWh |
| `sensor.energy_grid_export_today` | kWh |
| `sensor.energy_battery_charged_today` | kWh |
| `sensor.energy_battery_discharged_today` | kWh |
| `sensor.energy_self_sufficiency_today` | % |
| `sensor.energy_grid_cost_today` | $ (exports credited at the same price) |

### Performance

| Entity | Unit | Meaning |
|---|---|---|
| `sensor.energy_solar_typical_today` | kWh | Typical output for this site, array size and date |
| `sensor.energy_solar_today_vs_typical` | % | Today's progress vs typical |
| `sensor.energy_solar_7_day_average` | kWh | |
| `sensor.energy_solar_vs_typical` | % | Last 7 days vs typical (includes cloudy days) |
| `sensor.energy_solar_efficiency_spec` | % | Best 3 of last 7 days vs typical; ~100–115 % is healthy |
| `sensor.energy_solar_efficiency_history` | % | Best 3 of last 7 days vs the system's own season-adjusted best |
| `sensor.energy_solar_health_trend` | text | `Learning` / `Improving` / `Stable` / `Declining` (attribute `change_pct`) |
| `sensor.energy_solar_cleaning_recommended` | text | `Learning` / `Yes` / `No` |
| `sensor.energy_solar_best_day`, `sensor.energy_solar_best_day_date` | kWh, date | |
| `sensor.energy_solar_yesterday`, `sensor.energy_house_yesterday` | kWh | |
| `sensor.energy_solar_lifetime_daily_average` | kWh | Needs the commissioning date |
| `sensor.energy_solar_array_size` | kW | Panel count × panel watts |
| `sensor.energy_daily_history` | days | Attribute `days`: `[date, solar, house, import, export]` |

"Learning" means fewer than 10 days of history (health trend: until there are
prior weeks to compare).

### Electricity rates

| Entity | Meaning |
|---|---|
| `sensor.energy_price_now` | Import price now ($/kWh); attribute `tier` for tiered plans |
| `sensor.energy_export_price_now` | Export credit now ($/kWh) |
| `sensor.energy_price_period` | Period name from the plan (e.g. `On-peak`) or `Override` |
| `sensor.energy_next_price_change` | Timestamp of the next change; attributes `next_price`, `next_period` |
| `sensor.energy_tariff` | Plan name (attributes: seasons, currency, source, export credit) |
| `select.energy_tariff_source` | `Rate plan` or `Price sensor` (the sensor chosen in the options) |
| `select.energy_export_credit` | `Same as import` / `Fixed rate` / `No credit` |
| `number.energy_export_rate` | Fixed export rate |
| `number.energy_price_override` | Price override; anything above 0 wins over everything else, 0 turns it off |

Set a whole plan:

```
POST /api/services/innovo_solar_edge/set_tariff
{"plan": {
  "name": "TOU 4-9", "currency": "USD", "tier_period": "month",
  "seasons": [
    {"name": "Summer", "months": [6, 7, 8, 9],
     "weekday": [["00:00", 0.40, "Off-peak"], ["16:00", 0.64, "On-peak"], ["21:00", 0.40, "Off-peak"]],
     "weekend": [["00:00", 0.40, "Off-peak"], ["16:00", 0.52, "Mid-peak"], ["21:00", 0.40, "Off-peak"]]},
    {"name": "Winter", "months": [1, 2, 3, 4, 5, 10, 11, 12],
     "weekday": [["00:00", 0.38, "Off-peak"], ["08:00", 0.34, "Super off-peak"],
                 ["16:00", 0.50, "Mid-peak"], ["21:00", 0.38, "Off-peak"]]}
  ]}}
```

Rows start at a time and run until the next row; each day starts at `00:00`;
seasons must cover all 12 months; a missing `weekend` reuses the weekday rows.
A rate can be tiered: `[{"upto": 40, "rate": 0.07065}, {"rate": 0.11142}]`, where
`upto` counts kWh of grid import per `tier_period` (`day` or calendar `month`).
Holidays are treated as normal days.

Change prices by period name (e.g. after a rate change):

```
POST /api/services/innovo_solar_edge/set_tariff_rates
{"rates": {"On-peak": 0.66, "Off-peak": 0.41}, "season": "Summer"}
```

Temporary override (e.g. a critical-peak event), then clear it:

```
POST /api/services/number/set_value {"entity_id": "number.energy_price_override", "value": 1.20}
POST /api/services/number/set_value {"entity_id": "number.energy_price_override", "value": 0}
```

### Running totals (kWh, never reset)

`sensor.solar_pv_production_energy`, `sensor.battery_charged_energy`,
`sensor.battery_discharged_energy`.

### Settings (writable)

`number.energy_solar_panel_count`, `number.energy_solar_panel_watts`,
`number.energy_solar_array_tilt`, `number.energy_solar_array_azimuth`
(180 = south), `number.energy_battery_capacity` (kWh),
`date.energy_solar_commissioned`, and `button.energy_provision_dashboards`.

The underlying inverter, meter and battery entities of the integration
(`sensor.<name>_i1_*`, `sensor.<name>_i1_m1_*`, …) remain available for detail
views.
