"""Tests for custom_components.sunplug.diagnostics."""

from __future__ import annotations

from custom_components.sunplug.diagnostics import async_get_config_entry_diagnostics

from pytest_homeassistant_custom_component.common import MockConfigEntry

from .helpers import async_set_energy_prefs, mock_config_entry_data, set_power_state
from custom_components.sunplug.const import DOMAIN


async def test_diagnostics_redacts_token(recorder_mock, hass, enable_custom_integrations):
    await async_set_energy_prefs(
        hass,
        [
            {
                "type": "solar",
                "stat_energy_from": "sensor.solar_energy",
                "stat_rate": "sensor.solar_power",
                "config_entry_solar_forecast": None,
            },
            {
                "type": "grid",
                "stat_energy_from": "sensor.grid_energy_in",
                "stat_energy_to": None,
                "stat_cost": None,
                "entity_energy_price": None,
                "number_energy_price": None,
                "stat_compensation": None,
                "entity_energy_price_export": None,
                "number_energy_price_export": None,
                "stat_rate": "sensor.grid_power",
                "cost_adjustment_day": 0.0,
            },
        ],
    )
    set_power_state(hass, "sensor.solar_power", 100)
    set_power_state(hass, "sensor.grid_power", 50)

    entry = MockConfigEntry(domain=DOMAIN, data=mock_config_entry_data())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry_data"]["token"] == "**REDACTED**"
    assert diag["sender"]["roles"]["solar"] == ["sensor.solar_power"]
