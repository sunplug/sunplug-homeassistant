"""Resolve Home Assistant Energy dashboard preferences to Sunplug roles.

No manual entity mapping is offered by this integration: the Energy
dashboard preferences (``homeassistant.components.energy``) are the only
source of truth for which entities represent solar production, grid power
and battery power.

Precedence for a source's live power entity (VERIFIED against
``homeassistant/components/energy/data.py`` on the ``dev`` branch,
``EnergyManager._process_grid_power`` / ``_process_battery_power``):
Whenever a source carries ``power_config``, ``EnergyManager.async_update``
resolves it into a concrete ``stat_rate`` on the source *at save time* —
either the user's own sensor (standard config) or a generated entity id for
the inverted / two-sensor configs. That resolved ``stat_rate`` is what is
persisted and handed back as ``manager.data``. So a top-level ``stat_rate``
is always authoritative when present. ``power_config`` is only consulted as
a defensive fallback (e.g. preferences read before that processing, or a
future schema change) and is not expected to be exercised in practice.

Solar sources never carry ``power_config`` in the current schema (only
``stat_rate``), so no fallback is needed there.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from homeassistant.components.energy.data import async_get_manager
from homeassistant.core import HomeAssistant, valid_entity_id

from .const import ROLE_BATTERY, ROLE_GRID, ROLE_SOLAR


@dataclass
class EnergyRoles:
    """Entities backing each Sunplug role, resolved from Energy prefs."""

    solar: list[str] = field(default_factory=list)
    grid: list[str] = field(default_factory=list)
    battery: list[str] = field(default_factory=list)
    battery_soc: list[str] = field(default_factory=list)
    battery_configured: bool = False
    # For a power sensor HA generates from ``power_config`` (inverted or
    # two-sensor), the user's own sensors behind it. The generated entity
    # belongs to the ``energy`` integration; the integration worth naming is
    # the one that owns these.
    origins: dict[str, list[str]] = field(default_factory=dict)

    @property
    def has_required(self) -> bool:
        """Return whether the minimum solar + grid power sensors exist."""
        return bool(self.solar) and bool(self.grid)

    def entities_for(self, role: str) -> list[str]:
        """Return the tracked power entities for a role."""
        if role == ROLE_SOLAR:
            return self.solar
        if role == ROLE_GRID:
            return self.grid
        if role == ROLE_BATTERY:
            return self.battery
        return []

    def all_tracked_entities(self) -> set[str]:
        """Return every entity that should be observed for state changes."""
        return {*self.solar, *self.grid, *self.battery, *self.battery_soc}


def _origin_entities(source: dict, stat_rate: str) -> list[str]:
    """The user's own sensors behind a source's resolved power entity."""
    config = source.get("power_config") or {}
    own = [
        config.get(key)
        for key in ("stat_rate_inverted", "stat_rate_from", "stat_rate_to")
        if config.get(key) and valid_entity_id(config[key])
    ]
    return [entity for entity in own if entity != stat_rate]


def _power_entity(source: dict) -> str | None:
    """Return the live power entity id for one energy source, if any.

    Only the resolved ``stat_rate`` is trusted (see module docstring for the
    evidence that HA always populates it when ``power_config`` is set). A raw,
    unprocessed ``power_config`` with no ``stat_rate`` is not interpreted here:
    doing so for the inverted or two-sensor cases would require either
    guessing a sign or combining two entities, and a wrong guess would
    silently corrupt readings, which is worse than treating the source as
    unmapped until HA's own processing catches up.
    """
    stat_rate = source.get("stat_rate")
    if stat_rate and valid_entity_id(stat_rate):
        return stat_rate
    return None


async def async_resolve_roles(hass: HomeAssistant) -> EnergyRoles:
    """Resolve the current Energy dashboard preferences to Sunplug roles."""
    manager = await async_get_manager(hass)
    roles = EnergyRoles()

    if manager.data is None:
        return roles

    for source in manager.data.get("energy_sources", []):
        source_type = source.get("type")
        if source_type not in ("solar", "grid", "battery"):
            continue
        if source_type == "battery":
            roles.battery_configured = True
        entity_id = _power_entity(source)
        if entity_id:
            {"solar": roles.solar, "grid": roles.grid, "battery": roles.battery}[
                source_type
            ].append(entity_id)
            if origin := _origin_entities(source, entity_id):
                roles.origins[entity_id] = origin
        if source_type == "battery":
            stat_soc = source.get("stat_soc")
            if stat_soc and valid_entity_id(stat_soc):
                roles.battery_soc.append(stat_soc)

    return roles
