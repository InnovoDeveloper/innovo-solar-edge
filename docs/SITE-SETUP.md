# Setting up a new site

A checklist for installing Innovo Solar Edge on a new Home Assistant system,
built from real installs. Work through it top to bottom; most problems on past
sites came from one of these steps being skipped.

## 1. Before you start

| Check | Why |
|---|---|
| Modbus TCP is on: SetApp → *Site Communication* → *Modbus TCP*, port 1502 | The integration reads the inverter locally; no cloud account is used. |
| Nothing else is connected over Modbus TCP | SolarEdge inverters accept **one** Modbus TCP client at a time. A second client (another HA, a logger, a second integration) makes both fail intermittently. |
| The inverter's RS485 device list matches the real hardware: SetApp → *Site Communication* → *RS485-1* | A configured device that doesn't exist (a second battery, an extra meter) causes battery communication errors (**3x6B**) and meter errors (**3x6E**), and stops the battery charging. Remove anything that isn't physically there. |
| Home Assistant's home location, time zone and currency are set: Settings → System → General | The sun position and the typical-output lookup (NREL PVWatts) use the location. A location of 0, 0 is flagged as a repair. |

## 2. Install and configure

1. Install through HACS (see the [README](../README.md#install-hacs)) and restart.
2. Add **Innovo Solar Edge** with the inverter's IP address and port 1502. The
   *Inverter Device ID List* is normally `1`. Followers connected to a leader
   inverter over RS485 have their own IDs (e.g. `1,2`).
3. **Configure** the integration:
   - **Detect batteries**: on if the site has a battery.
   - **Auto-Detect Additional Entities**: leave **off**. Many inverters time
     out on the power-control registers ("Global Dynamic Power Control
     Timeout").
   - **Polling interval**: 10–15 s gives a near-live feed.

The energy model and dashboards then set themselves up. About a minute after
the inverter first reports, the integration fills in whatever the site doesn't
have yet: the HA Energy dashboard sources and a **Solar & Energy** dashboard.
Existing dashboards are not overwritten.

## 3. What each kind of site gets

The integration detects the equipment and adapts the dashboards and the
screens. Nothing needs to be selected by hand.

| Site | Live and today | Money | Dashboards and screens |
|---|---|---|---|
| **Solar only** (inverter, no meter, no battery) | Solar power and production, typical output, efficiency | **Solar value**: each kWh valued at the rate in force when it was produced | Solar-only versions; no home or grid figures |
| **Solar + SolarEdge meter** | + home use, grid import/export, self-sufficiency | + grid cost and savings | Full energy flow |
| **Solar + meter + battery** | + battery power, charge, time to full or reserve | + savings from the battery | Full energy flow with battery |

Home use, grid figures and costs need a **SolarEdge energy meter** (CTs on the
main feed). The inverter alone only measures what the panels produce. If a
client wants costs and self-sufficiency, quote the meter.

## 4. Panel settings: enter them and check them

Set these on the **SolarEdge Energy** device (or the *System settings* card of
the Solar & Energy dashboard):

| Setting | Where to find the real value |
|---|---|
| **Solar Panel Count** | The optimizer count in the SolarEdge monitoring portal or SetApp: one optimizer per panel |
| **Solar Panel Watts** | The panel model's datasheet or label (typically 350–500 W) |
| **Tilt** / **Azimuth** | Roof pitch in degrees; direction the panels face (180 = south, 270 = west) |
| **Commissioned** | Install date; used for the lifetime daily average |

Typical output, efficiency, health and the cleaning indicator all depend on
these numbers, so check them against what the inverter measures:

- **The array must be rated above the highest DC input ever measured.** The
  integration remembers that peak (attribute `peak_dc_input_kw` on *Solar
  Array Size*). If the settings are smaller, a repair explains it and gives the
  likely minimum. On a clear day the DC input rarely exceeds about 80% of the
  array rating, so the array is at least `peak ÷ 0.8`.
- **Settings more than twice the inverter's rating** are flagged too. That
  usually means the array total was typed as the watts per panel.
- **Lifetime energy cross-check.** The inverter's lifetime counter (*AC
  Energy*, kWh) divided by the years since install and by the site's yearly
  yield per kW gives a rough array size. Yearly yield is roughly 1,100–1,300
  kWh per kW in southern Canada and the northern US, and 1,500–1,800 in
  California; [PVWatts](https://pvwatts.nrel.gov) gives the figure for an
  address. Snow and outages make this a lower bound.
- **After a few full days**, *Efficiency vs spec* should sit around 95–115%.
  Far outside that, the settings (or the panels) need a look.

Example from a real install: settings entered as 6 × 1000 W (6 kW), while the
inverter measured 9.9 kW of DC input at 3 pm on a clear October day. The array
had to be at least 12.4 kW. Fitting the afternoon's output to clear-sky
sunlight put it at 14–16 kW.

## 5. Electricity rates

Choose where prices come from (*Tariff Source*):

- **Rate plan**: enter the utility's time-of-use plan under Configure →
  *Electricity rate plan*, or send it as JSON with
  `innovo_solar_edge.set_tariff`.
- **Price sensor**: take the price from another integration or a template
  sensor ($/kWh).
- **Price override**: a number entity that a controller can set directly;
  0 = off. It takes precedence over everything else.

Example plan: Ontario time-of-use (OEB rates for November 2025 to October
2026). Ontario rates change every November 1; update them when the OEB
publishes new ones.

```json
{
  "name": "Ontario Time-of-Use (OEB, Nov 2025 - Oct 2026)",
  "currency": "CAD",
  "tier_period": "month",
  "seasons": [
    {"name": "Winter", "months": [11, 12, 1, 2, 3, 4],
     "weekday": [["00:00", 0.098, "Off-peak"], ["07:00", 0.203, "On-peak"],
                 ["11:00", 0.157, "Mid-peak"], ["17:00", 0.203, "On-peak"],
                 ["19:00", 0.098, "Off-peak"]],
     "weekend": [["00:00", 0.098, "Off-peak"]]},
    {"name": "Summer", "months": [5, 6, 7, 8, 9, 10],
     "weekday": [["00:00", 0.098, "Off-peak"], ["07:00", 0.157, "Mid-peak"],
                 ["11:00", 0.203, "On-peak"], ["17:00", 0.157, "Mid-peak"],
                 ["19:00", 0.098, "Off-peak"]],
     "weekend": [["00:00", 0.098, "Off-peak"]]}
  ]
}
```

Each row starts at its time and runs until the next one. Weekend rows default
to the weekday rows if left out. Tiered rates, export credit and setting
prices over the API are described in [CONTROLLERS.md](CONTROLLERS.md).

## 6. Dashboards and screens

- **Rebuild the Solar & Energy dashboard** after changing equipment (adding a
  meter or battery) or updating the integration: press *Provision Dashboards*
  on the SolarEdge Energy device, or call
  `innovo_solar_edge.provision_dashboards`. This overwrites that one dashboard;
  the Energy dashboard keeps its non-SolarEdge sources (water, gas, devices).
- **Screens for controllers**: seven 720 × 720 images, served as image
  entities. To also publish them as plain files under `/local`, turn on
  *Publish the dashboard image as a file under /local* in Configure →
  *Integration settings*. See [CONTROLLERS.md](CONTROLLERS.md).
- A screen shows "Please come back again later…" until it has data. *Solar
  Health* and *Week* need the first full day.

## 7. Battery sites

- **Backup reserve**: the share of the battery held back for outages. A low
  reserve (e.g. 5–10%) leaves more energy for the expensive evening hours.
- **Storage profile reverts**: if the battery profile (e.g. *Maximize
  self-consumption*) keeps reverting after reconnecting, it is being pushed from
  the SolarEdge monitoring account. Change it there or ask SolarEdge support;
  local changes are overwritten.
- The battery's usable capacity defaults to its nameplate. Adjust *Battery
  usable capacity* if the battery reports differently.

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Inverter shows **3x6B** (battery communication) | A battery configured on RS485 that isn't there, or the battery was off | Fix the RS485 device list (step 1); power-cycle the battery's main switch |
| Inverter shows **3x6E** (meter communication) | An extra meter configured on RS485 | Remove it from the RS485 device list |
| Entities unavailable every few minutes | A second Modbus TCP client | Find and stop the other client |
| "Global Dynamic Power Control Timeout" | Power-control registers probed | Turn *Auto-Detect Additional Entities* off |
| Typical output or efficiency empty | Location not set, or PVWatts unreachable | Set the location; the lookup retries every 6 h |
| Efficiency far from 100% | Panel settings wrong | See step 4 |
| Solar & Energy dashboard missing cards after an update | It is only built once | Press *Provision Dashboards* |
| "The entity no longer has a state class" repairs | Left over from an older version | Developer Tools → Statistics → fix each entry; or wait for the nightly check |
