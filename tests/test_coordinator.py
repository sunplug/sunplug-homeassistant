"""Tests for custom_components.sunplug.coordinator.SunplugSender."""

from __future__ import annotations

import logging
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.sunplug.api import PostResult
from custom_components.sunplug.const import (
    CONF_INGEST_URL,
    CONF_TOKEN,
    MIN_SEND_INTERVAL_S,
    REPAIR_THRESHOLD_S,
)
from custom_components.sunplug.coordinator import SunplugSender, _EntityTracking
from custom_components.sunplug.energy_map import EnergyRoles
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from .helpers import MOCK_INGEST_URL, MOCK_TOKEN, set_power_state


def _make_sender(hass) -> SunplugSender:
    entry = MockConfigEntry(
        domain="sunplug",
        data={CONF_TOKEN: MOCK_TOKEN, CONF_INGEST_URL: MOCK_INGEST_URL},
    )
    entry.add_to_hass(hass)
    return SunplugSender(hass, entry)


# -- interval_s -------------------------------------------------------------


async def test_interval_s_defaults_to_floor_without_samples(hass):
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    assert sender.interval_s == MIN_SEND_INTERVAL_S


async def test_interval_s_uses_slowest_required_entity(hass):
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    fast = _EntityTracking()
    fast.gaps_s = [30, 31, 29]
    slow = _EntityTracking()
    slow.gaps_s = [120, 118, 121]
    sender._tracking = {"sensor.solar_power": fast, "sensor.grid_power": slow}
    assert sender.interval_s == 120


async def test_interval_s_floors_at_30(hass):
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    quick = _EntityTracking()
    quick.gaps_s = [5, 6, 5]
    sender._tracking = {"sensor.solar_power": quick, "sensor.grid_power": quick}
    assert sender.interval_s == MIN_SEND_INTERVAL_S


# -- staleness ----------------------------------------------------------------


async def test_stale_uses_floor_when_no_measured_period(hass):
    sender = _make_sender(hass)
    now = dt_util.utcnow()
    track = _EntityTracking(last_reported=now - timedelta(minutes=14))
    sender._tracking = {"sensor.x": track}
    assert sender._is_stale("sensor.x", now) is False
    track.last_reported = now - timedelta(minutes=16)
    assert sender._is_stale("sensor.x", now) is True


async def test_stale_uses_3x_measured_period_when_bigger_than_floor(hass):
    sender = _make_sender(hass)
    now = dt_util.utcnow()
    track = _EntityTracking(last_reported=now - timedelta(seconds=1500))
    track.gaps_s = [600, 610, 590]  # median ~600s -> 3x = 1800s, bigger than 900s floor
    sender._tracking = {"sensor.x": track}
    assert sender._is_stale("sensor.x", now) is False
    track.last_reported = now - timedelta(seconds=1900)
    assert sender._is_stale("sensor.x", now) is True


async def test_missing_entity_is_stale(hass):
    sender = _make_sender(hass)
    assert sender._is_stale("sensor.missing", dt_util.utcnow()) is True


# -- payload building ---------------------------------------------------------


async def test_build_payload_none_when_solar_unknown(hass):
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(),
        "sensor.grid_power": _EntityTracking(last_reported=dt_util.utcnow()),
    }
    set_power_state(hass, "sensor.grid_power", 500)
    # solar_power has no state at all -> unknown
    assert sender._build_payload() is None


async def test_build_payload_none_when_grid_unknown(hass):
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    now = dt_util.utcnow()
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(last_reported=now),
        "sensor.grid_power": _EntityTracking(last_reported=now),
    }
    set_power_state(hass, "sensor.solar_power", 500)
    hass.states.async_set("sensor.grid_power", "unavailable")
    assert sender._build_payload() is None


async def test_build_payload_converts_watts_and_clamps_solar(hass):
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    now = dt_util.utcnow()
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(last_reported=now),
        "sensor.grid_power": _EntityTracking(last_reported=now),
    }
    set_power_state(hass, "sensor.solar_power", -5, unit="W")  # clamp to 0
    set_power_state(hass, "sensor.grid_power", 1.5, unit="kW")
    payload = sender._build_payload()
    assert payload["production_kw"] == 0.0
    assert payload["net_import_kw"] == 1.5
    assert payload["source"] == "homeassistant"
    assert payload["ingest_url"] == MOCK_INGEST_URL


async def test_build_payload_watts_to_kw_conversion_value(hass):
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    now = dt_util.utcnow()
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(last_reported=now),
        "sensor.grid_power": _EntityTracking(last_reported=now),
    }
    set_power_state(hass, "sensor.solar_power", 1234, unit="W")
    set_power_state(hass, "sensor.grid_power", 0, unit="W")
    payload = sender._build_payload()
    assert payload["production_kw"] == 1.234


async def test_build_payload_battery_absent_is_null(hass):
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(
        solar=["sensor.solar_power"], grid=["sensor.grid_power"], battery_configured=False
    )
    now = dt_util.utcnow()
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(last_reported=now),
        "sensor.grid_power": _EntityTracking(last_reported=now),
    }
    set_power_state(hass, "sensor.solar_power", 100)
    set_power_state(hass, "sensor.grid_power", 100)
    payload = sender._build_payload()
    assert payload["battery_discharge_kw"] is None


async def test_build_payload_battery_configured_but_unknown_is_omitted(hass):
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(
        solar=["sensor.solar_power"],
        grid=["sensor.grid_power"],
        battery=["sensor.battery_power"],
        battery_configured=True,
    )
    now = dt_util.utcnow()
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(last_reported=now),
        "sensor.grid_power": _EntityTracking(last_reported=now),
        "sensor.battery_power": _EntityTracking(last_reported=now),
    }
    set_power_state(hass, "sensor.solar_power", 100)
    set_power_state(hass, "sensor.grid_power", 100)
    hass.states.async_set("sensor.battery_power", "unknown")
    payload = sender._build_payload()
    assert "battery_discharge_kw" not in payload


async def test_build_payload_battery_present_and_soc_fraction(hass):
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(
        solar=["sensor.solar_power"],
        grid=["sensor.grid_power"],
        battery=["sensor.battery_power"],
        battery_soc=["sensor.battery_soc"],
        battery_configured=True,
    )
    now = dt_util.utcnow()
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(last_reported=now),
        "sensor.grid_power": _EntityTracking(last_reported=now),
        "sensor.battery_power": _EntityTracking(last_reported=now),
        "sensor.battery_soc": _EntityTracking(last_reported=now),
    }
    set_power_state(hass, "sensor.solar_power", 100)
    set_power_state(hass, "sensor.grid_power", 100)
    set_power_state(hass, "sensor.battery_power", 300, unit="W")
    hass.states.async_set("sensor.battery_soc", "62.5", {"unit_of_measurement": "%"})
    payload = sender._build_payload()
    assert payload["battery_discharge_kw"] == 0.3
    assert payload["battery_soc"] == 0.625


async def test_build_payload_rounds_to_3_decimals(hass):
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    now = dt_util.utcnow()
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(last_reported=now),
        "sensor.grid_power": _EntityTracking(last_reported=now),
    }
    set_power_state(hass, "sensor.solar_power", 1234.5678, unit="W")
    set_power_state(hass, "sensor.grid_power", 0, unit="W")
    payload = sender._build_payload()
    assert payload["production_kw"] == 1.235


async def test_build_payload_ha_integrations_mapping(hass):
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    now = dt_util.utcnow()
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(last_reported=now),
        "sensor.grid_power": _EntityTracking(last_reported=now),
    }
    set_power_state(hass, "sensor.solar_power", 100, platform="enphase_envoy")
    set_power_state(hass, "sensor.grid_power", 100, platform="shelly")
    payload = sender._build_payload()
    assert payload["ha_integrations"] == {
        "solar": "enphase_envoy",
        "grid": "shelly",
    }


async def test_build_payload_stale_entity_treated_as_unknown(hass):
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    now = dt_util.utcnow()
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(last_reported=now - timedelta(minutes=20)),
        "sensor.grid_power": _EntityTracking(last_reported=now),
    }
    set_power_state(hass, "sensor.solar_power", 100)
    set_power_state(hass, "sensor.grid_power", 100)
    assert sender._build_payload() is None


# -- result handling ------------------------------------------------------------


async def test_handle_result_ok_clears_dirty(hass):
    sender = _make_sender(hass)
    sender._dirty = True
    await sender._async_handle_result(PostResult(status=200, ok=True))
    assert sender._dirty is False
    assert sender.last_post.status == "ok"


async def test_handle_result_401_starts_reauth(hass):
    sender = _make_sender(hass)
    with patch.object(sender.entry, "async_start_reauth") as mock_reauth:
        await sender._async_handle_result(PostResult(status=401))
    mock_reauth.assert_called_once_with(hass)
    assert sender.last_post.status == "unauthorized"


async def test_handle_result_422_logs_once(hass, caplog):
    sender = _make_sender(hass)
    sender._dirty = True
    await sender._async_handle_result(
        PostResult(status=422, error_body="bad payload")
    )
    assert sender._dirty is False
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1

    sender._dirty = True
    await sender._async_handle_result(PostResult(status=422, error_body="bad payload"))
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1  # not logged again


async def test_handle_result_429_keeps_dirty(hass):
    sender = _make_sender(hass)
    sender._dirty = True
    await sender._async_handle_result(PostResult(status=429))
    assert sender._dirty is True
    assert sender.last_post.status == "rate_limited"


async def test_handle_result_network_error_keeps_dirty(hass):
    sender = _make_sender(hass)
    sender._dirty = True
    await sender._async_handle_result(
        PostResult(status=None, exception="ClientError")
    )
    assert sender._dirty is True
    assert sender.last_post.status == "error"
    assert sender.last_post.error_class == "ClientError"


# -- ingest_url adoption --------------------------------------------------------


async def test_adopt_ingest_url_accepted(hass):
    sender = _make_sender(hass)
    sender._async_maybe_adopt_ingest_url("https://new.sunplug.app/v1/ingest/x")
    assert sender.entry.data[CONF_INGEST_URL] == "https://new.sunplug.app/v1/ingest/x"


@pytest.mark.parametrize(
    "url",
    [
        "http://new.sunplug.app/v1/ingest/x",  # not https
        "https://sunplug.app.evil.example/v1/ingest/x",  # wrong host
        "https://evil.example/v1/ingest/x",
    ],
)
async def test_adopt_ingest_url_rejected(hass, url):
    sender = _make_sender(hass)
    original = sender.entry.data[CONF_INGEST_URL]
    sender._async_maybe_adopt_ingest_url(url)
    assert sender.entry.data[CONF_INGEST_URL] == original


# -- repairs ----------------------------------------------------------------


async def test_repair_created_and_cleared(hass):
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    set_power_state(hass, "sensor.solar_power", 100, platform="solaredge")

    slow = _EntityTracking()
    slow.gaps_s = [400, 410, 405]
    sender._tracking = {"sensor.solar_power": slow, "sensor.grid_power": _EntityTracking()}

    sender._async_maybe_update_repair("sensor.solar_power")

    registry = ir.async_get(hass)
    issue = registry.async_get_issue("sunplug", "slow_entity_sensor.solar_power")
    assert issue is not None
    assert issue.translation_key == "slow_entity_with_alternative"
    assert issue.translation_placeholders["alternative"] == "SolarEdge Modbus Multi (HACS)"

    # Period drops back under the threshold -> issue cleared.
    slow.gaps_s = [60, 61, 59]
    sender._async_maybe_update_repair("sensor.solar_power")
    assert registry.async_get_issue("sunplug", "slow_entity_sensor.solar_power") is None


async def test_repair_not_created_below_threshold(hass):
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    ok = _EntityTracking()
    ok.gaps_s = [60, 61, 59]
    sender._tracking = {"sensor.solar_power": ok, "sensor.grid_power": _EntityTracking()}
    sender._async_maybe_update_repair("sensor.solar_power")
    registry = ir.async_get(hass)
    assert registry.async_get_issue("sunplug", "slow_entity_sensor.solar_power") is None


# -- cadence end-to-end -------------------------------------------------------


async def test_cadence_no_send_without_new_data(hass, aioclient_mock, freezer):
    """No mapped entity reported anything new -> nothing is posted."""
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(),
        "sensor.grid_power": _EntityTracking(),
    }
    await sender._async_send()
    assert len(aioclient_mock.mock_calls) == 0


async def test_cadence_sends_and_respects_min_spacing(hass, aioclient_mock, freezer):
    start = dt_util.utcnow()
    freezer.move_to(start)

    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    sender._unsub = []
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(),
        "sensor.grid_power": _EntityTracking(),
    }

    aioclient_mock.post(MOCK_INGEST_URL, json={"ok": True}, status=200)

    from homeassistant.helpers.event import (
        async_track_state_change_event,
        async_track_state_report_event,
    )

    tracked = ["sensor.solar_power", "sensor.grid_power"]
    sender._unsub.append(
        async_track_state_change_event(hass, tracked, sender._async_state_event)
    )
    sender._unsub.append(
        async_track_state_report_event(hass, tracked, sender._async_state_event)
    )

    set_power_state(hass, "sensor.solar_power", 100)
    set_power_state(hass, "sensor.grid_power", 50)
    # Real timers (even a scheduled "delay 0") never fire from
    # async_block_till_done alone in this harness; async_fire_time_changed
    # with fire_all=True is what actually runs a due callback deterministically.
    async_fire_time_changed(hass, dt_util.utcnow(), fire_all=True)
    await hass.async_block_till_done()
    assert len(aioclient_mock.mock_calls) == 1

    # A change 5s later must not send immediately (min 30s spacing); it is
    # scheduled instead, and we deliberately do NOT force-fire it here.
    freezer.move_to(start + timedelta(seconds=5))
    set_power_state(hass, "sensor.solar_power", 150)
    await hass.async_block_till_done()
    assert len(aioclient_mock.mock_calls) == 1
    assert sender._pending_send_unsub is not None

    # Advance to the 30s boundary: force the still-pending scheduled send to run.
    target = start + timedelta(seconds=31)
    freezer.move_to(target)
    async_fire_time_changed(hass, target, fire_all=True)
    await hass.async_block_till_done()
    assert len(aioclient_mock.mock_calls) == 2


async def test_cadence_state_reported_counts_as_fresh_data(hass, aioclient_mock, freezer):
    """An unchanged value that is re-reported (EVENT_STATE_REPORTED) still sends."""
    start = dt_util.utcnow()
    freezer.move_to(start)

    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    sender._unsub = []
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(),
        "sensor.grid_power": _EntityTracking(),
    }
    aioclient_mock.post(MOCK_INGEST_URL, json={"ok": True}, status=200)

    from homeassistant.helpers.event import (
        async_track_state_change_event,
        async_track_state_report_event,
    )

    tracked = ["sensor.solar_power", "sensor.grid_power"]
    sender._unsub.append(
        async_track_state_change_event(hass, tracked, sender._async_state_event)
    )
    sender._unsub.append(
        async_track_state_report_event(hass, tracked, sender._async_state_event)
    )

    set_power_state(hass, "sensor.solar_power", 100)
    set_power_state(hass, "sensor.grid_power", 50)
    async_fire_time_changed(hass, dt_util.utcnow(), fire_all=True)
    await hass.async_block_till_done()
    assert len(aioclient_mock.mock_calls) == 1

    # Same value, same attributes -> EVENT_STATE_REPORTED, not
    # EVENT_STATE_CHANGED, 31s later (past the 30s floor so it can send
    # immediately once dt_util reflects the new time).
    target = start + timedelta(seconds=31)
    freezer.move_to(target)
    set_power_state(hass, "sensor.solar_power", 100)
    async_fire_time_changed(hass, target, fire_all=True)
    await hass.async_block_till_done()
    assert len(aioclient_mock.mock_calls) == 2


async def test_cadence_interval_s_rises_for_slow_sensor(hass, aioclient_mock, freezer):
    start = dt_util.utcnow()
    freezer.move_to(start)
    aioclient_mock.post(MOCK_INGEST_URL, json={"ok": True}, status=200)

    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    sender._unsub = []
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(),
        "sensor.grid_power": _EntityTracking(),
    }

    from homeassistant.helpers.event import async_track_state_change_event

    tracked = ["sensor.solar_power", "sensor.grid_power"]
    sender._unsub.append(
        async_track_state_change_event(hass, tracked, sender._async_state_event)
    )

    set_power_state(hass, "sensor.grid_power", 50)
    for i in range(3):
        freezer.move_to(start + timedelta(seconds=(i + 1) * 400))
        set_power_state(hass, "sensor.solar_power", 100 + i)
        await hass.async_block_till_done()

    assert sender.interval_s == 400

    if sender._pending_send_unsub is not None:
        sender._pending_send_unsub()


# -- review fixes (lead) --------------------------------------------------------


async def test_a_role_missing_one_of_its_sensors_is_unknown(hass):
    """Two grid connections, one unavailable: half a grid reading is a
    plausible wrong number, so the reading is skipped instead."""
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(
        solar=["sensor.solar_power"],
        grid=["sensor.grid_a_power", "sensor.grid_b_power"],
    )
    now = dt_util.utcnow()
    sender._tracking = {
        entity: _EntityTracking(last_reported=now)
        for entity in ("sensor.solar_power", "sensor.grid_a_power", "sensor.grid_b_power")
    }
    set_power_state(hass, "sensor.solar_power", 1000)
    set_power_state(hass, "sensor.grid_a_power", 400)
    set_power_state(hass, "sensor.grid_b_power", "unavailable")
    assert sender._build_payload() is None

    set_power_state(hass, "sensor.grid_b_power", 100)
    assert sender._build_payload()["net_import_kw"] == 0.5


async def test_a_generated_power_sensor_names_the_integration_behind_it(hass):
    """An inverted or two-sensor config is read through HA's own generated
    sensor; the integration reported is the user's, not `energy`."""
    sender = _make_sender(hass)
    generated = "sensor.grid_raw_power_inverted"
    sender.roles = EnergyRoles(
        solar=["sensor.solar_power"],
        grid=[generated],
        origins={generated: ["sensor.grid_raw_power"]},
    )
    now = dt_util.utcnow()
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(last_reported=now),
        generated: _EntityTracking(last_reported=now),
    }
    set_power_state(hass, "sensor.solar_power", 100, platform="enphase_envoy")
    set_power_state(hass, generated, -300, platform="energy")
    set_power_state(hass, "sensor.grid_raw_power", 300, platform="solaredge_modbus_multi")
    payload = sender._build_payload()
    assert payload["ha_integrations"] == {
        "solar": "enphase_envoy",
        "grid": "solaredge_modbus_multi",
    }


async def test_a_restart_does_not_wait_for_every_sensor_to_report_again(hass):
    """Roles resolved while states already exist start from their last report."""
    from .helpers import async_set_energy_prefs

    set_power_state(hass, "sensor.solar_power", 1500)
    set_power_state(hass, "sensor.grid_power", -700)
    await async_set_energy_prefs(
        hass,
        [
            {"type": "solar", "stat_energy_from": "sensor.solar_energy",
             "stat_rate": "sensor.solar_power"},
            {"type": "grid", "stat_energy_from": None, "stat_energy_to": None,
             "stat_rate": "sensor.grid_power", "cost_adjustment_day": 0.0},
        ],
    )
    sender = _make_sender(hass)
    await sender._async_reload_roles()
    payload = sender._build_payload()
    assert payload is not None
    assert payload["production_kw"] == 1.5
    assert payload["net_import_kw"] == -0.7


async def test_setup_sends_the_current_states_without_waiting_for_a_change(
    hass, aioclient_mock, freezer
):
    """Paired while every sensor sits still: the first reading goes out anyway."""
    from .helpers import async_set_energy_prefs

    set_power_state(hass, "sensor.solar_power", 3200)
    set_power_state(hass, "sensor.grid_power", -1800)
    await async_set_energy_prefs(
        hass,
        [
            {"type": "solar", "stat_energy_from": "sensor.solar_energy",
             "stat_rate": "sensor.solar_power"},
            {"type": "grid", "stat_energy_from": None, "stat_energy_to": None,
             "stat_rate": "sensor.grid_power", "cost_adjustment_day": 0.0},
        ],
    )
    aioclient_mock.post(MOCK_INGEST_URL, json={"ok": True}, status=200)
    sender = _make_sender(hass)
    await sender.async_setup()
    async_fire_time_changed(hass, dt_util.utcnow(), fire_all=True)
    await hass.async_block_till_done()
    assert len(aioclient_mock.mock_calls) == 1
    body = aioclient_mock.mock_calls[0][2]
    assert body["production_kw"] == 3.2
    assert body["net_import_kw"] == -1.8
    await sender.async_unload()


# -- observability: ha_stats, log-when-unavailable, debug logging -------------

_LOGGER_NAME = "custom_components.sunplug.coordinator"


async def test_skip_counts_solar_unknown(hass):
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    now = dt_util.utcnow()
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(last_reported=now),
        "sensor.grid_power": _EntityTracking(last_reported=now),
    }
    hass.states.async_set("sensor.solar_power", "unavailable")
    set_power_state(hass, "sensor.grid_power", 100)
    assert sender._build_payload() is None
    assert sender._stats == {"solar_unknown": 1}


async def test_skip_counts_grid_unknown(hass):
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    now = dt_util.utcnow()
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(last_reported=now),
        "sensor.grid_power": _EntityTracking(last_reported=now),
    }
    set_power_state(hass, "sensor.solar_power", 100)
    hass.states.async_set("sensor.grid_power", "unknown")
    assert sender._build_payload() is None
    assert sender._stats == {"grid_unknown": 1}


async def test_skip_counts_sensor_stale_distinct_from_unknown(hass):
    """A stale required entity counts as sensor_stale, not solar_unknown."""
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    now = dt_util.utcnow()
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(last_reported=now - timedelta(minutes=20)),
        "sensor.grid_power": _EntityTracking(last_reported=now),
    }
    set_power_state(hass, "sensor.solar_power", 100)
    set_power_state(hass, "sensor.grid_power", 100)
    assert sender._build_payload() is None
    assert sender._stats == {"sensor_stale": 1}


async def test_skip_counts_accumulate_across_calls(hass):
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    now = dt_util.utcnow()
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(last_reported=now),
        "sensor.grid_power": _EntityTracking(last_reported=now),
    }
    hass.states.async_set("sensor.solar_power", "unavailable")
    set_power_state(hass, "sensor.grid_power", 100)
    sender._build_payload()
    sender._build_payload()
    assert sender._stats == {"solar_unknown": 2}


async def test_skip_logs_debug_with_reason(hass, caplog):
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    now = dt_util.utcnow()
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(last_reported=now),
        "sensor.grid_power": _EntityTracking(last_reported=now),
    }
    hass.states.async_set("sensor.solar_power", "unavailable")
    set_power_state(hass, "sensor.grid_power", 100)
    sender._build_payload()
    debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert any("solar_unknown" in r.getMessage() for r in debug_records)


async def test_payload_omits_ha_stats_when_empty(hass):
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    now = dt_util.utcnow()
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(last_reported=now),
        "sensor.grid_power": _EntityTracking(last_reported=now),
    }
    set_power_state(hass, "sensor.solar_power", 100)
    set_power_state(hass, "sensor.grid_power", 100)
    payload = sender._build_payload()
    assert "ha_stats" not in payload


async def test_stats_sent_with_next_successful_post_and_reset_after_200(
    hass, aioclient_mock, freezer
):
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    now = dt_util.utcnow()
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(last_reported=now),
        "sensor.grid_power": _EntityTracking(last_reported=now),
    }

    # A skipped reading (solar unknown) builds up a pending count, nothing sent.
    hass.states.async_set("sensor.solar_power", "unavailable")
    set_power_state(hass, "sensor.grid_power", 100)
    sender._dirty = True
    await sender._async_send()
    assert sender._stats == {"solar_unknown": 1}
    assert len(aioclient_mock.mock_calls) == 0

    # Solar becomes known again; the next successful post carries the count.
    aioclient_mock.post(MOCK_INGEST_URL, json={"ok": True}, status=200)
    set_power_state(hass, "sensor.solar_power", 500)
    sender._dirty = True
    await sender._async_send()
    assert len(aioclient_mock.mock_calls) == 1
    body = aioclient_mock.mock_calls[0][2]
    assert body["ha_stats"] == {"solar_unknown": 1}
    assert sender._stats == {}


async def test_stats_kept_after_5xx(hass, aioclient_mock, freezer):
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    now = dt_util.utcnow()
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(last_reported=now),
        "sensor.grid_power": _EntityTracking(last_reported=now),
    }
    set_power_state(hass, "sensor.solar_power", 500)
    set_power_state(hass, "sensor.grid_power", 100)
    sender._stats = {"grid_unknown": 2}
    aioclient_mock.post(MOCK_INGEST_URL, status=503)
    sender._dirty = True
    await sender._async_send()
    assert sender._stats == {"grid_unknown": 2, "post_server": 1}


async def test_post_429_counted(hass):
    sender = _make_sender(hass)
    await sender._async_handle_result(PostResult(status=429))
    assert sender._stats == {"post_rate_limited": 1}


async def test_post_network_error_counted(hass):
    sender = _make_sender(hass)
    await sender._async_handle_result(PostResult(status=None, exception="ClientError"))
    assert sender._stats == {"post_network": 1}


async def test_post_5xx_counted(hass):
    sender = _make_sender(hass)
    await sender._async_handle_result(PostResult(status=500))
    assert sender._stats == {"post_server": 1}


async def test_post_401_and_422_not_counted(hass):
    sender = _make_sender(hass)
    with patch.object(sender.entry, "async_start_reauth"):
        await sender._async_handle_result(PostResult(status=401))
    await sender._async_handle_result(PostResult(status=422, error_body="x"))
    assert sender._stats == {}


async def test_post_failing_once_back_logging(hass, caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    sender = _make_sender(hass)
    await sender._async_handle_result(PostResult(status=500))
    await sender._async_handle_result(PostResult(status=500))
    await sender._async_handle_result(PostResult(status=429))
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1

    await sender._async_handle_result(PostResult(status=200, ok=True))
    infos = [r for r in caplog.records if r.levelname == "INFO"]
    assert len(infos) == 1

    # A further failure after recovery warns again (not suppressed forever).
    await sender._async_handle_result(PostResult(status=500))
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2


async def test_reading_unavailable_once_back_logging(hass, caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    now = dt_util.utcnow()
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(last_reported=now),
        "sensor.grid_power": _EntityTracking(last_reported=now),
    }
    hass.states.async_set("sensor.solar_power", "unavailable")
    set_power_state(hass, "sensor.grid_power", 100)

    sender._build_payload()
    sender._build_payload()
    sender._build_payload()
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1

    set_power_state(hass, "sensor.solar_power", 500)
    sender._build_payload()
    infos = [r for r in caplog.records if r.levelname == "INFO"]
    assert len(infos) == 1


async def test_debug_line_on_post(hass, aioclient_mock, caplog):
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    sender = _make_sender(hass)
    sender.roles = EnergyRoles(solar=["sensor.solar_power"], grid=["sensor.grid_power"])
    now = dt_util.utcnow()
    sender._tracking = {
        "sensor.solar_power": _EntityTracking(last_reported=now),
        "sensor.grid_power": _EntityTracking(last_reported=now),
    }
    set_power_state(hass, "sensor.solar_power", 500)
    set_power_state(hass, "sensor.grid_power", 100)
    aioclient_mock.post(MOCK_INGEST_URL, json={"ok": True}, status=200)
    sender._dirty = True
    await sender._async_send()
    debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert any(
        "status=200" in r.getMessage() and "roles=solar,grid" in r.getMessage()
        for r in debug_records
    )


async def test_diagnostics_includes_stats_and_unavailable_state(hass):
    sender = _make_sender(hass)
    sender._stats = {"post_network": 2}
    sender._reading_unavailable = True
    sender._post_failing = True
    data = sender.diagnostics_data()
    assert data["ha_stats"] == {"post_network": 2}
    assert data["reading_unavailable"] is True
    assert data["post_failing"] is True


async def test_counts_added_while_a_post_is_in_flight_survive_its_200(hass):
    """Only what a post carried is cleared by its 200."""
    sender = _make_sender(hass)
    sender._stats = {"grid_unknown": 3, "post_server": 1}
    carried = dict(sender._stats)
    sender._stats["grid_unknown"] += 2  # counted while the post was in flight
    await sender._async_handle_result(PostResult(status=200), carried)
    assert sender._stats == {"grid_unknown": 2}
