# Innovo Solar Edge

Local (Modbus/TCP) SolarEdge integration for Home Assistant with a built-in
**energy and solar-performance model**, designed to be provisioned on client
systems and read by a home-control platform from a single
integration.

Based on [SolarEdge Modbus Multi](https://github.com/WillCodeForCats/solaredge-modbus-multi)
v4.0.4 (Apache-2.0); see [NOTICE](NOTICE) for what changed. It uses its own
domain, `innovo_solar_edge`, so it can't collide with or be overwritten by the
upstream integration.

## What it adds

All inverter, meter and battery entities of the upstream integration, plus a
**SolarEdge Energy** device with:

- **Live power** (every poll): solar, house, grid (+ buying / − selling),
  battery (+ charging / − discharging), battery level and state, grid state
  (incl. outage), share of the house on solar/battery, inverter status, price
  and rate period.
- **Running totals** (kWh) for solar production and battery in/out, suitable
  for the HA Energy dashboard.
- **Today** totals (reset at midnight): solar, house, grid import/export,
  battery in/out, self-sufficiency and grid cost.
- **Performance**: typical output for the site and time of year (NREL PVWatts),
  efficiency vs spec and vs the system's own best, 7-day average, health trend
  (Improving / Stable / Declining), cleaning recommendation (Yes / No), best
  day, yesterday, lifetime daily average.
- **System settings** as entities: panel count, panel watts, tilt, azimuth,
  battery usable capacity (defaults to the battery nameplate), commissioning
  date.
- **Provisioning**: one action builds the HA Energy dashboard and a
  *Solar & Energy* dashboard from the site's own entities.

Battery and solar power are estimates when the battery only reports state of
energy (common with third-party batteries): battery power is the SoE slope over
two minutes × usable capacity, and solar = inverter AC output + battery power.
The model adds no Modbus traffic; it reads the integration's own entities.

State (settings, running totals, today's counters and up to 365 daily
snapshots) is stored in `.storage/innovo_solar_edge.energy.<entry_id>` and
survives restarts and recorder purges.

## Install (HACS)

1. HACS → ⋮ → **Custom repositories** → add
   `https://github.com/InnovoDeveloper/innovo-solar-edge`, category **Integration**.
2. Install **Innovo Solar Edge** and restart Home Assistant.
3. Enable **Modbus TCP** on the inverter (SetApp → Site Communication → Modbus TCP,
   port 1502). Only one Modbus TCP client can connect at a time.
4. Settings → Devices & services → **Add integration** → *Innovo Solar Edge*
   (or accept the discovered inverter).

Manual install: copy `custom_components/innovo_solar_edge` into the HA config's
`custom_components/` folder and restart.

## Provision a site

1. Integration → **Configure**:
   - turn on **Detect batteries** if the site has a battery;
   - leave **Auto-Detect Additional Entities** off unless power-control
     registers are needed (many inverters time out on them);
   - optional: **Electricity price sensor** ($/kWh) for daily cost;
   - optional: turn off the **PVWatts lookup** if the home location must not be
     sent to NREL (typical/spec metrics are then unavailable).
   A 10–15 s polling interval gives a near-live feed.
2. On the **SolarEdge Energy** device, set *Solar Panel Count* and *Solar Panel
   Watts* (a repair notice reminds you until they're set), and optionally tilt,
   azimuth and the commissioning date.
3. Press **Provision Dashboards** on the same device, or call the action:

   ```yaml
   action: innovo_solar_edge.provision_dashboards
   data:
     energy_dashboard: true   # grid, solar, battery sources (others kept)
     dashboard: true          # creates/overwrites the Solar & Energy dashboard
     url_path: solar-energy
     title: Solar & Energy
   ```

   If HA can't create the dashboard automatically, create an empty dashboard with
   URL `solar-energy` and run it again.

## Controllers

See [docs/CONTROLLERS.md](docs/CONTROLLERS.md) for the entity reference, sign
conventions and how to read and write values through the HA REST API.

## Migrating from SolarEdge Modbus Multi

To move an existing upstream install without losing entity ids or history, stop
HA, install this integration, then run on the HA host:

```bash
python3 scripts/migrate_domain.py /path/to/homeassistant/config
```

It moves the config entry, devices and entities to `innovo_solar_edge`, removes
the upstream folder (keeping a backup under `/root/`), and marks the upstream
HACS repository as not installed. Start HA afterwards.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
