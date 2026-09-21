"""Tracks Energy-dashboard entities and pushes readings to Sunplug."""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.const import UnitOfPower
from homeassistant.core import (
    Event,
    EventStateChangedData,
    EventStateReportedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_state_report_event,
)
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import PowerConverter

from .api import PostResult, async_post_reading
from .const import (
    CONF_INGEST_URL,
    CONF_TOKEN,
    DOMAIN,
    GAP_HISTORY_LEN,
    ISSUE_SLOW_ENTITY,
    KNOWN_SLOW_CLOUD_INTEGRATIONS,
    MIN_SEND_INTERVAL_S,
    REPAIR_THRESHOLD_S,
    ROLE_BATTERY,
    ROLE_GRID,
    ROLE_SOLAR,
    STALE_FLOOR_S,
    STALE_MULTIPLIER,
)
from .energy_map import EnergyRoles, async_resolve_roles

if TYPE_CHECKING:
    from . import SunplugConfigEntry

_LOGGER = logging.getLogger(__name__)


@dataclass
class _EntityTracking:
    """Freshness bookkeeping for one tracked entity."""

    last_reported: datetime | None = None
    gaps_s: list[float] = field(default_factory=list)

    def record(self, reported_at: datetime) -> None:
        if self.last_reported is not None:
            gap = (reported_at - self.last_reported).total_seconds()
            if gap > 0:
                self.gaps_s.append(gap)
                if len(self.gaps_s) > GAP_HISTORY_LEN:
                    self.gaps_s.pop(0)
        self.last_reported = reported_at

    @property
    def measured_period_s(self) -> float | None:
        if not self.gaps_s:
            return None
        return statistics.median(self.gaps_s)


@dataclass
class LastPostStatus:
    """Diagnostics-friendly record of the last post attempt."""

    at: datetime | None = None
    status: str | None = None
    error_class: str | None = None


class SunplugSender:
    """Owns Energy-role resolution, cadence and posting to Sunplug."""

    def __init__(self, hass: HomeAssistant, entry: "SunplugConfigEntry") -> None:
        self.hass = hass
        self.entry = entry
        self.roles = EnergyRoles()
        self._tracking: dict[str, _EntityTracking] = {}
        self._unsub: list[Any] = []
        self._dirty = False
        self._next_allowed_ts: float = 0.0
        self._pending_send_unsub: Any = None
        self._unloaded = False
        self._logged_422 = False
        self.last_post = LastPostStatus()
        self._slow_issue_active: set[str] = set()

    # -- setup / teardown -------------------------------------------------

    async def async_setup(self) -> None:
        """Resolve roles, subscribe to state and preference changes."""
        await self._async_reload_roles()

        from homeassistant.components.energy.data import async_get_manager

        manager = await async_get_manager(self.hass)
        manager.async_listen_updates(self._async_prefs_updated)

    async def async_unload(self) -> None:
        """Stop tracking. Best-effort: EnergyManager offers no listener removal."""
        self._unloaded = True
        for unsub in self._unsub:
            unsub()
        self._unsub = []
        if self._pending_send_unsub is not None:
            self._pending_send_unsub()
            self._pending_send_unsub = None

    async def _async_prefs_updated(self) -> None:
        if self._unloaded:
            return
        await self._async_reload_roles()

    async def _async_reload_roles(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub = []

        self.roles = await async_resolve_roles(self.hass)
        tracked = self.roles.all_tracked_entities()

        # Keep tracking state for entities still mapped; drop the rest.
        self._tracking = {
            entity_id: track
            for entity_id, track in self._tracking.items()
            if entity_id in tracked
        }
        for entity_id in tracked:
            track = self._tracking.setdefault(entity_id, _EntityTracking())
            # Seeded from the state machine, so a restart does not wait for
            # every sensor to report again before the first reading goes out.
            if track.last_reported is None and (
                state := self.hass.states.get(entity_id)
            ):
                track.last_reported = state.last_reported

        if not tracked:
            return

        self._unsub.append(
            async_track_state_change_event(
                self.hass, list(tracked), self._async_state_event
            )
        )
        self._unsub.append(
            async_track_state_report_event(
                self.hass, list(tracked), self._async_state_event
            )
        )

    # -- event handling -----------------------------------------------------

    @callback
    def _async_state_event(
        self, event: "Event[EventStateChangedData] | Event[EventStateReportedData]"
    ) -> None:
        new_state: State | None = event.data.get("new_state")
        if new_state is None:
            return

        entity_id = new_state.entity_id
        track = self._tracking.get(entity_id)
        if track is None:
            return

        track.record(new_state.last_reported)
        self._dirty = True
        self._async_maybe_update_repair(entity_id)
        self._async_maybe_send()

    # -- cadence --------------------------------------------------------

    @callback
    def _async_maybe_send(self) -> None:
        if not self._dirty or self._pending_send_unsub is not None:
            # Either nothing new, or a send is already scheduled: multiple
            # entities updating within the same dispatch batch coalesce into
            # a single scheduled send instead of racing separate tasks.
            return
        now = dt_util.utcnow().timestamp()
        delay = max(0.0, self._next_allowed_ts - now)

        @callback
        def _fire(_now: datetime) -> None:
            self._pending_send_unsub = None
            self.hass.async_create_task(self._async_send())

        # Always defer via async_call_later, even for delay 0: this lets any
        # other entity updates already queued in the current event-loop
        # iteration (e.g. a burst from the same coordinator update) land
        # before we read live states in _build_payload.
        self._pending_send_unsub = async_call_later(self.hass, delay, _fire)

    @property
    def interval_s(self) -> int:
        """Measured update period of the slowest required (solar/grid) entity."""
        periods: list[float] = []
        for role in (ROLE_SOLAR, ROLE_GRID):
            for entity_id in self.roles.entities_for(role):
                track = self._tracking.get(entity_id)
                if track and track.measured_period_s is not None:
                    periods.append(track.measured_period_s)
        if not periods:
            return MIN_SEND_INTERVAL_S
        return max(MIN_SEND_INTERVAL_S, round(max(periods)))

    def _is_stale(self, entity_id: str, now: datetime) -> bool:
        track = self._tracking.get(entity_id)
        if track is None or track.last_reported is None:
            return True
        period = track.measured_period_s or MIN_SEND_INTERVAL_S
        threshold = max(STALE_MULTIPLIER * period, STALE_FLOOR_S)
        return (now - track.last_reported).total_seconds() > threshold

    # -- repairs ----------------------------------------------------------

    @callback
    def _async_maybe_update_repair(self, entity_id: str) -> None:
        required_entities = {
            *self.roles.entities_for(ROLE_SOLAR),
            *self.roles.entities_for(ROLE_GRID),
        }
        if entity_id not in required_entities:
            return

        track = self._tracking.get(entity_id)
        period = track.measured_period_s if track else None
        issue_id = f"{ISSUE_SLOW_ENTITY}_{entity_id}"

        if period is not None and period > REPAIR_THRESHOLD_S:
            if entity_id in self._slow_issue_active:
                return
            self._slow_issue_active.add(entity_id)
            integration = self._integration_of(entity_id) or "unknown"
            placeholders = {
                "entity_id": entity_id,
                "integration": integration,
            }
            alternative = KNOWN_SLOW_CLOUD_INTEGRATIONS.get(integration)
            translation_key = "slow_entity"
            if alternative:
                translation_key = "slow_entity_with_alternative"
                placeholders["alternative"] = alternative
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=translation_key,
                translation_placeholders=placeholders,
            )
        elif entity_id in self._slow_issue_active:
            self._slow_issue_active.discard(entity_id)
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    # -- payload building -------------------------------------------------

    def _role_kw(
        self, role_entities: list[str], now: datetime
    ) -> tuple[float | None, datetime | None]:
        """Sum a role's power entities in kW. Returns (value, newest_ts).

        Unknown unless every entity of the role is known and fresh: a sum
        missing one of two grid connections is a plausible, wrong number, and
        Sunplug cannot tell it from a real one. Unknown it can handle.
        """
        total = 0.0
        newest: datetime | None = None
        if not role_entities:
            return None, None
        for entity_id in role_entities:
            if self._is_stale(entity_id, now):
                return None, None
            state = self.hass.states.get(entity_id)
            if state is None or state.state in (None, "unknown", "unavailable"):
                return None, None
            try:
                value = float(state.state)
            except (TypeError, ValueError):
                return None, None
            unit = state.attributes.get("unit_of_measurement", UnitOfPower.WATT)
            try:
                kw = PowerConverter.convert(value, unit, UnitOfPower.KILO_WATT)
            except HomeAssistantError:
                return None, None
            total += kw
            if newest is None or state.last_reported > newest:
                newest = state.last_reported
        return total, newest

    def _soc_fraction(
        self, entities: list[str], now: datetime
    ) -> tuple[float | None, datetime | None]:
        values: list[float] = []
        newest: datetime | None = None
        for entity_id in entities:
            if self._is_stale(entity_id, now):
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state in (None, "unknown", "unavailable"):
                continue
            try:
                value = float(state.state)
            except (TypeError, ValueError):
                continue
            values.append(value)
            if newest is None or state.last_reported > newest:
                newest = state.last_reported
        if not values:
            return None, None
        return statistics.mean(values) / 100, newest

    def _integration_of(self, entity_id: str) -> str | None:
        """The integration owning a power entity, looking through HA's own
        generated sensors to the user's sensors behind them."""
        registry = er.async_get(self.hass)
        domains = set()
        for own in self.roles.origins.get(entity_id) or [entity_id]:
            reg_entry = registry.async_get(own)
            if reg_entry and reg_entry.platform:
                domains.add(reg_entry.platform)
        return ",".join(sorted(domains)) or None

    def _integrations_for(self, role: str) -> str | None:
        domains: set[str] = set()
        for entity_id in self.roles.entities_for(role):
            if found := self._integration_of(entity_id):
                domains.update(found.split(","))
        return ",".join(sorted(domains)) or None

    def _build_payload(self) -> dict[str, Any] | None:
        now = dt_util.utcnow()

        solar_kw, solar_ts = self._role_kw(self.roles.solar, now)
        if solar_kw is None:
            return None
        solar_kw = max(0.0, solar_kw)

        grid_kw, grid_ts = self._role_kw(self.roles.grid, now)
        if grid_kw is None:
            return None

        timestamps = [ts for ts in (solar_ts, grid_ts) if ts is not None]

        payload: dict[str, Any] = {
            "production_kw": round(solar_kw, 3),
            "net_import_kw": round(grid_kw, 3),
            "source": "homeassistant",
            "interval_s": self.interval_s,
        }

        if not self.roles.battery_configured:
            payload["battery_discharge_kw"] = None
        else:
            battery_kw, battery_ts = self._role_kw(self.roles.battery, now)
            if battery_kw is not None:
                payload["battery_discharge_kw"] = round(battery_kw, 3)
                if battery_ts is not None:
                    timestamps.append(battery_ts)
            # else: configured but currently unknown -> key omitted.

        if self.roles.battery_soc:
            soc, soc_ts = self._soc_fraction(self.roles.battery_soc, now)
            if soc is not None:
                payload["battery_soc"] = round(soc, 3)
                if soc_ts is not None:
                    timestamps.append(soc_ts)

        integrations: dict[str, str] = {}
        for role in (ROLE_SOLAR, ROLE_GRID, ROLE_BATTERY):
            domains = self._integrations_for(role)
            if domains:
                integrations[role] = domains
        if integrations:
            payload["ha_integrations"] = integrations

        if not timestamps:
            return None
        payload["tsms"] = int(max(timestamps).timestamp() * 1000)
        payload["ingest_url"] = self.entry.data[CONF_INGEST_URL]

        return payload

    # -- sending ------------------------------------------------------------

    async def _async_send(self) -> None:
        if not self._dirty:
            return
        payload = self._build_payload()
        # Whatever happens, this cadence slot is consumed.
        self._next_allowed_ts = dt_util.utcnow().timestamp() + MIN_SEND_INTERVAL_S

        if payload is None:
            # Required data unknown: nothing to send, but stay dirty so the
            # next qualifying event retries once solar/grid become known.
            return

        session = async_get_clientsession(self.hass)
        ingest_url = self.entry.data[CONF_INGEST_URL]
        token = self.entry.data[CONF_TOKEN]

        result = await async_post_reading(session, ingest_url, token, payload)
        await self._async_handle_result(result)

    async def _async_handle_result(self, result: PostResult) -> None:
        now = dt_util.utcnow()

        if result.status == 200:
            self._dirty = False
            self.last_post = LastPostStatus(at=now, status="ok")
            self._logged_422 = False
            if result.new_ingest_url:
                self._async_maybe_adopt_ingest_url(result.new_ingest_url)
            return

        if result.status == 401:
            self.last_post = LastPostStatus(at=now, status="unauthorized")
            self.entry.async_start_reauth(self.hass)
            return

        if result.status == 422:
            self.last_post = LastPostStatus(
                at=now, status="rejected", error_class="422"
            )
            if not self._logged_422:
                self._logged_422 = True
                _LOGGER.warning(
                    "Sunplug ingest rejected a reading (422): %s",
                    result.error_body,
                )
            # Not a transient error: don't keep retrying the same payload.
            self._dirty = False
            return

        if result.status == 429:
            self.last_post = LastPostStatus(at=now, status="rate_limited")
            # Cadence slot already consumed in _async_send; nothing else to do.
            return

        # Network error or 5xx: keep the reading pending for the next trigger.
        self.last_post = LastPostStatus(
            at=now,
            status="error",
            error_class=result.exception or f"http_{result.status}",
        )

    @callback
    def _async_maybe_adopt_ingest_url(self, new_url: str) -> None:
        from urllib.parse import urlparse

        parsed = urlparse(new_url)
        if (
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.hostname.endswith(".sunplug.app")
        ):
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, CONF_INGEST_URL: new_url},
            )

    # -- diagnostics --------------------------------------------------------

    def diagnostics_data(self) -> dict[str, Any]:
        return {
            "roles": {
                "solar": self.roles.solar,
                "grid": self.roles.grid,
                "battery": self.roles.battery,
                "battery_soc": self.roles.battery_soc,
                "battery_configured": self.roles.battery_configured,
            },
            "integrations": {
                role: self._integrations_for(role)
                for role in (ROLE_SOLAR, ROLE_GRID, ROLE_BATTERY)
            },
            "measured_periods_s": {
                entity_id: track.measured_period_s
                for entity_id, track in self._tracking.items()
            },
            "interval_s": self.interval_s,
            "last_post": {
                "at": self.last_post.at.isoformat() if self.last_post.at else None,
                "status": self.last_post.status,
                "error_class": self.last_post.error_class,
            },
        }
