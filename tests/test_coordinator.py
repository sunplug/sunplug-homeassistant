"""Tests for custom_components.sunplug.coordinator.SunplugSender."""

from __future__ import annotations

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
