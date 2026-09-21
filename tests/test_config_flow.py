"""Tests for the Sunplug config flow."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.sunplug.const import CONF_INGEST_URL, CONF_TOKEN, DOMAIN

from .helpers import (
    MOCK_INGEST_URL,
    MOCK_TOKEN,
    async_set_energy_prefs,
    mock_config_entry_data,
    set_power_state,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def _async_configure_energy(hass):
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


async def test_full_flow_success(recorder_mock, hass, enable_custom_integrations, aioclient_mock):
    await _async_configure_energy(hass)
    aioclient_mock.post(
        "https://api.sunplug.app/agent/claim",
        json={"token": MOCK_TOKEN, "ingest_url": MOCK_INGEST_URL},
        status=200,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pairing_code": " ab12cd "}
    )
    assert result2["type"] is FlowResultType.CREATE_ENTRY
    assert result2["data"][CONF_TOKEN] == MOCK_TOKEN
    assert result2["data"][CONF_INGEST_URL] == MOCK_INGEST_URL

    # The code was stripped/uppercased before being sent.
    _, _, body, _ = aioclient_mock.mock_calls[0]
    assert body["code"] == "AB12CD"
    assert body["client"] == "homeassistant"


async def test_invalid_code(recorder_mock, hass, enable_custom_integrations, aioclient_mock):
    await _async_configure_energy(hass)
    aioclient_mock.post("https://api.sunplug.app/agent/claim", status=400)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pairing_code": "abcdef"}
    )
    assert result2["type"] is FlowResultType.FORM
    assert result2["errors"] == {"base": "invalid_code"}


async def test_cannot_connect(recorder_mock, hass, enable_custom_integrations, aioclient_mock):
    await _async_configure_energy(hass)
    import aiohttp

    aioclient_mock.post("https://api.sunplug.app/agent/claim", exc=aiohttp.ClientError())

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pairing_code": "abcdef"}
    )
    assert result2["type"] is FlowResultType.FORM
    assert result2["errors"] == {"base": "cannot_connect"}


async def test_no_power_sensors_aborts(recorder_mock, hass, enable_custom_integrations):
    # No Energy prefs configured at all.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_power_sensors"


async def test_already_configured_aborts(recorder_mock, hass, enable_custom_integrations, aioclient_mock):
    await _async_configure_energy(hass)
    aioclient_mock.post(
        "https://api.sunplug.app/agent/claim",
        json={"token": MOCK_TOKEN, "ingest_url": MOCK_INGEST_URL},
        status=200,
    )

    with patch(
        "homeassistant.helpers.instance_id.async_get", return_value="fixed-instance-id"
    ):
        entry = MockConfigEntry(
            domain=DOMAIN, unique_id="fixed-instance-id", data=mock_config_entry_data()
        )
        entry.add_to_hass(hass)

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"pairing_code": "abcdef"}
        )
    assert result2["type"] is FlowResultType.ABORT
    assert result2["reason"] == "already_configured"


async def test_reauth_flow_updates_entry(recorder_mock, hass, enable_custom_integrations, aioclient_mock):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="fixed-instance-id", data=mock_config_entry_data()
    )
    entry.add_to_hass(hass)

    new_token = "new-token"
    new_ingest = "https://ingest.sunplug.app/v1/ingest/new"
    aioclient_mock.post(
        "https://api.sunplug.app/agent/claim",
        json={"token": new_token, "ingest_url": new_ingest},
        status=200,
    )

    with patch(
        "custom_components.sunplug.coordinator.SunplugSender.async_setup",
        return_value=None,
    ):
        result = await entry.start_reauth_flow(hass)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"pairing_code": "newcod"}
        )
        await hass.async_block_till_done()

    assert result2["type"] is FlowResultType.ABORT
    assert result2["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == new_token
    assert entry.data[CONF_INGEST_URL] == new_ingest
