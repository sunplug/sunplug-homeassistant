"""Constants for the Sunplug integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "sunplug"

# --- Backend API ---
API_BASE: Final = "https://api.sunplug.app"
CLAIM_PATH: Final = "/agent/claim"

CONF_TOKEN: Final = "token"
CONF_INGEST_URL: Final = "ingest_url"

# --- Cadence ---
# Never send more often than this, regardless of how often entities report.
MIN_SEND_INTERVAL_S: Final = 30
# Number of inter-report gaps kept per tracked entity to estimate its period.
GAP_HISTORY_LEN: Final = 10
# An entity counts as stale when its last report is older than
# max(STALE_MULTIPLIER * measured_period, STALE_FLOOR_S).
STALE_MULTIPLIER: Final = 3
STALE_FLOOR_S: Final = 15 * 60
# Repair issue raised when a required entity's measured period exceeds this.
REPAIR_THRESHOLD_S: Final = 300

# --- Energy roles ---
ROLE_SOLAR: Final = "solar"
ROLE_GRID: Final = "grid"
ROLE_BATTERY: Final = "battery"
REQUIRED_ROLES: Final = (ROLE_SOLAR, ROLE_GRID)

# --- Repairs ---
ISSUE_SLOW_ENTITY: Final = "slow_entity"

# --- Failure vocabulary reported to Sunplug (ha_stats) ---
# Fixed set of reasons a reading was skipped, or a post failed, counted since
# the last successful post. Order is not meaningful; only non-zero counts are
# ever sent.
STAT_REASONS: Final = (
    "solar_unknown",
    "grid_unknown",
    "sensor_stale",
    "post_network",
    "post_server",
    "post_rate_limited",
)

# Known cloud-polling integrations that have a faster local alternative.
# Mapping of integration domain -> local alternative name shown in the repair.
# Integrations not listed here (or intentionally omitted, e.g. tibber without a
# Pulse add-on, which cannot be distinguished from the config entry alone) get
# a generic message with no suggested alternative.
KNOWN_SLOW_CLOUD_INTEGRATIONS: Final[dict[str, str]] = {
    "solaredge": "SolarEdge Modbus Multi (HACS)",
    "growatt_server": "Growatt local (HACS)",
    "solis": "Solis Modbus (HACS)",
    "victron_remote_monitoring": "Victron GX Modbus or MQTT",
}
