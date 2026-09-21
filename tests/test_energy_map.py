"""Tests for custom_components.sunplug.energy_map."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.sunplug.energy_map import async_resolve_roles

from .helpers import async_set_energy_prefs


async def test_no_prefs_returns_empty_roles(hass):
    roles = await async_resolve_roles(hass)
    assert roles.solar == []
    assert roles.grid == []
    assert roles.battery == []
    assert roles.battery_configured is False
    assert roles.has_required is False


async def test_single_stat_rate_solar_and_grid(hass):
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
    roles = await async_resolve_roles(hass)
    assert roles.solar == ["sensor.solar_power"]
    assert roles.grid == ["sensor.grid_power"]
    assert roles.has_required is True


async def test_multiple_grid_sources_are_summed_entity_list(hass):
    """Multiple grid connections all contribute their stat_rate entity."""
    grid_source = {
        "type": "grid",
        "stat_energy_from": None,
        "stat_energy_to": None,
        "stat_cost": None,
        "entity_energy_price": None,
        "number_energy_price": None,
        "stat_compensation": None,
        "entity_energy_price_export": None,
        "number_energy_price_export": None,
        "cost_adjustment_day": 0.0,
    }
    await async_set_energy_prefs(
        hass,
        [
            {**grid_source, "stat_rate": "sensor.grid_a_power"},
            {**grid_source, "stat_rate": "sensor.grid_b_power"},
        ],
    )
    roles = await async_resolve_roles(hass)
    assert sorted(roles.grid) == ["sensor.grid_a_power", "sensor.grid_b_power"]


async def test_battery_absent_when_not_configured(hass):
    await async_set_energy_prefs(
        hass,
        [
            {
                "type": "solar",
                "stat_energy_from": "sensor.solar_energy",
                "stat_rate": "sensor.solar_power",
                "config_entry_solar_forecast": None,
            }
        ],
    )
    roles = await async_resolve_roles(hass)
    assert roles.battery_configured is False
    assert roles.battery == []
    assert roles.battery_soc == []


async def test_battery_configured_with_soc(hass):
    await async_set_energy_prefs(
        hass,
        [
            {
                "type": "battery",
                "stat_energy_from": "sensor.battery_energy_out",
                "stat_energy_to": "sensor.battery_energy_in",
                "stat_rate": "sensor.battery_power",
                "stat_soc": "sensor.battery_soc",
            }
        ],
    )
    roles = await async_resolve_roles(hass)
    assert roles.battery_configured is True
    assert roles.battery == ["sensor.battery_power"]
    assert roles.battery_soc == ["sensor.battery_soc"]


@pytest.mark.parametrize(
    ("power_config", "expected_stat_rate"),
    [
        # Inverted single sensor: HA's EnergyManager resolves power_config into
        # a top-level stat_rate at save time (VERIFIED: data.py
        # EnergyManager._process_battery_power, via
        # energy.helpers.generate_power_sensor_entity_id). Note it *recomputes*
        # stat_rate from power_config on every async_update call — any
        # top-level stat_rate we might pass in is not preserved, which is why
        # this test goes through the real manager rather than asserting a
        # hand-picked value. We only ever trust that resolved stat_rate,
        # never power_config directly.
        (
            {"stat_rate_inverted": "sensor.battery_raw_power"},
            "sensor.battery_raw_power_inverted",
        ),
        # Two-sensor: same precedence applies.
        (
            {
                "stat_rate_from": "sensor.battery_discharge_raw",
                "stat_rate_to": "sensor.battery_charge_raw",
            },
            "sensor.energy_battery_battery_discharge_raw_battery_charge_raw_net_power",
        ),
    ],
)
async def test_power_config_present_defers_to_resolved_stat_rate(
    hass, power_config, expected_stat_rate
):
    await async_set_energy_prefs(
        hass,
        [
            {
                "type": "battery",
                "stat_energy_from": "sensor.battery_energy_out",
                "stat_energy_to": "sensor.battery_energy_in",
                "power_config": power_config,
            }
        ],
    )
    roles = await async_resolve_roles(hass)
    assert roles.battery == [expected_stat_rate]


async def test_raw_power_config_without_resolved_stat_rate_is_unmapped(hass):
    """Defensive path: a power_config with no resolved stat_rate yet is skipped.

    This should not occur via the real EnergyManager (it always resolves
    stat_rate on async_update), but energy_map must not guess a sign or
    combine two sensors on its own.
    """
    with patch(
        "custom_components.sunplug.energy_map.async_get_manager",
        new=AsyncMock(
            return_value=type(
                "M",
                (),
                {
                    "data": {
                        "energy_sources": [
                            {
                                "type": "grid",
                                "stat_energy_from": None,
                                "stat_energy_to": None,
                                "power_config": {
                                    "stat_rate_inverted": "sensor.grid_raw_power"
                                },
                                "cost_adjustment_day": 0.0,
                            }
                        ],
                        "device_consumption": [],
                    }
                },
            )()
        ),
    ):
        roles = await async_resolve_roles(hass)
    assert roles.grid == []


async def test_origins_record_the_users_own_sensors_behind_a_generated_one(hass):
    with patch(
        "custom_components.sunplug.energy_map.async_get_manager",
        return_value=type(
            "M",
            (),
            {
                "data": {
                    "energy_sources": [
                        {"type": "solar", "stat_energy_from": "sensor.pv",
                         "stat_rate": "sensor.pv_power"},
                        {
                            "type": "battery",
                            "stat_energy_from": None,
                            "stat_energy_to": None,
                            "stat_rate": "sensor.energy_battery_d_c_net_power",
                            "power_config": {
                                "stat_rate_from": "sensor.d",
                                "stat_rate_to": "sensor.c",
                            },
                        },
                    ],
                    "device_consumption": [],
                }
            },
        )(),
    ):
        roles = await async_resolve_roles(hass)
    assert roles.origins == {
        "sensor.energy_battery_d_c_net_power": ["sensor.d", "sensor.c"]
    }
    assert "sensor.pv_power" not in roles.origins
