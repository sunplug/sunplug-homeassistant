# Sunplug for Home Assistant

Sends your home's live solar, grid and battery power to
[Sunplug](https://sunplug.app) so it can plan charging around your own
production.

## Requirements

- A Home Assistant **Energy dashboard** already set up, with at least:
  - one **solar production** power (or energy) sensor, and
  - one **grid** power (or energy) sensor.
- A Sunplug account and a pairing code from the Sunplug app.

This integration does not ask you to pick entities. It reads whatever you
have already configured on the Energy dashboard (`/config/energy`) and uses
those same sensors. If you add, remove or change sensors there later, Sunplug
picks up the change automatically.

## Install

### Via HACS (custom repository)

1. In HACS, open the menu and choose **Custom repositories**.
2. Add `https://github.com/sunplug/sunplug-homeassistant` as an **Integration**.
3. Install **Sunplug**, then restart Home Assistant.

### Manual

Copy `custom_components/sunplug` into your Home Assistant `custom_components`
directory and restart Home Assistant.

## Pairing

1. In Home Assistant, go to **Settings → Devices & services → Add integration**
   and search for **Sunplug**.
2. Open the Sunplug app, generate a pairing code, and enter it in Home
   Assistant.

If your Energy dashboard is not yet configured with a solar and a grid power
sensor, set that up first at `/config/energy` — Sunplug needs it to know
which sensors to read.

## What it sends

Sunplug periodically sends your current solar production, grid import/export
and (if you have one configured) battery power and state of charge. Nothing
is sent faster than every 30 seconds, and only when your sensors have
actually reported new data.
