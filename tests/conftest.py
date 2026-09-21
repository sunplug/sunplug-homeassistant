"""Fixtures for Sunplug tests."""

from __future__ import annotations

pytest_plugins = "pytest_homeassistant_custom_component"

# NOTE: `enable_custom_integrations` is intentionally NOT wired up as an
# autouse fixture here. It depends on `hass`, and making it autouse forces
# `hass` to be created before any test-requested `recorder_mock` gets a
# chance to configure the recorder's database URL first (recorder_mock must
# be the first fixture pytest resolves in a test that uses it). Tests that
# need custom-integration loading request `enable_custom_integrations`
# explicitly instead.
