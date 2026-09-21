# Sunplug for Home Assistant

**Beta.** Reads the power sensors you've already configured on Home
Assistant's [Energy dashboard](https://my.home-assistant.io/redirect/energy/)
and sends your solar, grid and battery power — plus battery charge — to
[Sunplug](https://sunplug.app), so it can plan charging around your own
production.

There is no entity picker. This integration follows whatever sensors are set
under **Settings → Dashboards → Energy**; change them there and Sunplug
follows automatically.

## Requirements

- Home Assistant 2026.3 or later.
- An Energy dashboard already configured with:
  - a **solar production** power sensor, and
  - a **grid** power sensor.

  These must be **power** sensors (W/kW), not energy-only (kWh) sensors —
  Home Assistant lets you configure either, but Sunplug needs live power
  readings.
- A Sunplug account and a pairing code (in the Sunplug app: **Equipment →
  Home Assistant**).

A battery power sensor and a battery charge (state of charge) sensor on the
Energy dashboard are optional; battery power is sent as `null` if you don't
have one configured.

## Install

### HACS (custom repository)

1. In HACS, open the menu → **Custom repositories**.
2. Add `https://github.com/sunplug/sunplug-homeassistant`, category
   **Integration**.
3. Install **Sunplug**, then restart Home Assistant.

### Manual

Copy `custom_components/sunplug` into your Home Assistant `custom_components`
directory and restart Home Assistant.

## Pairing

1. Make sure your Energy dashboard has a solar and a grid power sensor
   (`/config/energy`). Setup stops and points you there otherwise.
2. In Home Assistant: **Settings → Devices & services → Add integration**,
   search for **Sunplug**.
3. In the Sunplug app, go to **Equipment → Home Assistant** to get a pairing
   code, and enter it in Home Assistant.

## What is sent

Every reading contains:

- a timestamp,
- solar power, in kW,
- grid power, in kW (positive = importing from the grid),
- battery power, in kW (positive = discharging), or `null` if no battery
  source is configured on the Energy dashboard,
- battery charge, as a fraction of full,
- how often readings are sent, and
- which Home Assistant integration provides each value — the integration's
  name only (e.g. `shelly`, `enphase_envoy`), never an entity ID, friendly
  name, or address, and
- counts of readings skipped or posts that failed since the last successful
  one, by reason (e.g. a sensor unavailable) — no values, no names.

Nothing else leaves your instance. A reading is sent whenever a mapped sensor
reports a new value, and never more often than every 30 seconds; nothing is
sent while nothing has changed.

## Freshness

Some cloud-polling integrations update slowly — SolarEdge's cloud API roughly
every 15 minutes, Growatt's cloud roughly every 5. Sunplug's usefulness
depends on how current your readings are, so if your solar or grid sensor
updates less often than every 5 minutes, the integration raises a repair
notice in Home Assistant. Where a faster local alternative is known, the notice names it (e.g.
SolarEdge Modbus Multi via HACS, in place of the SolarEdge cloud integration).

See [sunplug.app/kb/home-assistant](https://sunplug.app/kb/home-assistant)
for the full guide.

## Troubleshooting

**Grid values look backwards** (import/export flipped): fix the sign in the
Energy dashboard's grid sensor settings — Home Assistant offers an "inverted"
option for exactly this.

**Readings stop, or the battery is missing from them**: a value counts only
when every sensor behind it is available and fresh. Without solar or grid,
no reading is sent at all; without battery, the reading goes out without it.
Nothing partial or guessed is ever sent.

**Home Assistant asks you to reconnect**: your Sunplug pairing was revoked or
expired. A re-authentication prompt appears on the integration; enter a new
pairing code from the Sunplug app.

**Diagnostics**: **Settings → Devices & services → Sunplug → ⋮ → Download
diagnostics**. The pairing token is redacted.

## Support

- Email: support@sunplug.app
- Issues: [GitHub](https://github.com/sunplug/sunplug-homeassistant/issues)

## License

Apache-2.0
