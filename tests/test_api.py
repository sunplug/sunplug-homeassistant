"""Tests for custom_components.sunplug.api."""

from __future__ import annotations

import aiohttp
import pytest

from custom_components.sunplug import api as api_module
from custom_components.sunplug.api import (
    CannotConnect,
    InvalidCode,
    async_claim,
    async_post_reading,
)
from custom_components.sunplug.const import API_BASE, CLAIM_PATH
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .helpers import MOCK_INGEST_URL, MOCK_TOKEN


@pytest.fixture(autouse=True)
def _reset_override_warned():
    """Each test gets a fresh 'have we warned yet' state."""
    api_module._override_warned = False
    yield
    api_module._override_warned = False


async def test_claim_success_sends_client_homeassistant(hass, aioclient_mock):
    aioclient_mock.post(
        f"{API_BASE}{CLAIM_PATH}",
        json={"token": MOCK_TOKEN, "ingest_url": MOCK_INGEST_URL},
        status=200,
    )
    session = async_get_clientsession(hass)
    result = await async_claim(session, "abc123")

    assert result.token == MOCK_TOKEN
    assert result.ingest_url == MOCK_INGEST_URL
    assert len(aioclient_mock.mock_calls) == 1
    _, _, body, _ = aioclient_mock.mock_calls[0]
    assert body == {"code": "abc123", "name": "Home Assistant", "client": "homeassistant"}


async def test_claim_invalid_code(hass, aioclient_mock):
    aioclient_mock.post(f"{API_BASE}{CLAIM_PATH}", status=400)
    session = async_get_clientsession(hass)
    with pytest.raises(InvalidCode):
        await async_claim(session, "badcod")


async def test_claim_cannot_connect_on_network_error(hass, aioclient_mock):
    aioclient_mock.post(f"{API_BASE}{CLAIM_PATH}", exc=aiohttp.ClientError())
    session = async_get_clientsession(hass)
    with pytest.raises(CannotConnect):
        await async_claim(session, "abc123")


async def test_claim_cannot_connect_on_server_error(hass, aioclient_mock):
    aioclient_mock.post(f"{API_BASE}{CLAIM_PATH}", status=500)
    session = async_get_clientsession(hass)
    with pytest.raises(CannotConnect):
        await async_claim(session, "abc123")


async def test_post_reading_ok(hass, aioclient_mock):
    aioclient_mock.post(MOCK_INGEST_URL, json={"ok": True}, status=200)
    session = async_get_clientsession(hass)
    result = await async_post_reading(session, MOCK_INGEST_URL, MOCK_TOKEN, {"a": 1})
    assert result.status == 200
    assert result.ok is True
    _, _, _, headers = aioclient_mock.mock_calls[0]
    assert headers["Authorization"] == f"Bearer {MOCK_TOKEN}"


async def test_post_reading_adopts_new_ingest_url(hass, aioclient_mock):
    new_url = "https://ingest2.sunplug.app/v1/ingest/xyz"
    aioclient_mock.post(
        MOCK_INGEST_URL, json={"ok": True, "ingest_url": new_url}, status=200
    )
    session = async_get_clientsession(hass)
    result = await async_post_reading(session, MOCK_INGEST_URL, MOCK_TOKEN, {})
    assert result.new_ingest_url == new_url


@pytest.mark.parametrize("status", [401, 422, 429, 500])
async def test_post_reading_error_statuses(hass, aioclient_mock, status):
    aioclient_mock.post(MOCK_INGEST_URL, status=status, text="detail")
    session = async_get_clientsession(hass)
    result = await async_post_reading(session, MOCK_INGEST_URL, MOCK_TOKEN, {})
    assert result.status == status
    assert result.ok is False


async def test_post_reading_network_error(hass, aioclient_mock):
    aioclient_mock.post(MOCK_INGEST_URL, exc=aiohttp.ClientError())
    session = async_get_clientsession(hass)
    result = await async_post_reading(session, MOCK_INGEST_URL, MOCK_TOKEN, {})
    assert result.status is None
    assert result.exception == "ClientError"


# -- SUNPLUG_API_BASE staging override ---------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        "https://staging.sunplug.app",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://host.docker.internal:8000",
    ],
)
async def test_claim_honors_valid_api_base_override(
    hass, aioclient_mock, monkeypatch, caplog, override
):
    monkeypatch.setenv("SUNPLUG_API_BASE", override)
    aioclient_mock.post(
        f"{override}{CLAIM_PATH}",
        json={"token": MOCK_TOKEN, "ingest_url": MOCK_INGEST_URL},
        status=200,
    )
    session = async_get_clientsession(hass)

    result = await async_claim(session, "abc123")

    assert result.token == MOCK_TOKEN
    assert len(aioclient_mock.mock_calls) == 1
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert override in warnings[0].getMessage()


async def test_claim_valid_override_warns_only_once(
    hass, aioclient_mock, monkeypatch, caplog
):
    override = "https://staging.sunplug.app"
    monkeypatch.setenv("SUNPLUG_API_BASE", override)
    aioclient_mock.post(
        f"{override}{CLAIM_PATH}",
        json={"token": MOCK_TOKEN, "ingest_url": MOCK_INGEST_URL},
        status=200,
    )
    session = async_get_clientsession(hass)

    await async_claim(session, "abc123")
    await async_claim(session, "abc123")

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1


@pytest.mark.parametrize(
    "override",
    [
        "http://staging.sunplug.app",  # http, non-local host
        "https://",  # scheme with no host
        "ftp://staging.sunplug.app",  # wrong scheme
        "not-a-url",
    ],
)
async def test_claim_ignores_invalid_api_base_override(
    hass, aioclient_mock, monkeypatch, caplog, override
):
    monkeypatch.setenv("SUNPLUG_API_BASE", override)
    aioclient_mock.post(
        f"{API_BASE}{CLAIM_PATH}",
        json={"token": MOCK_TOKEN, "ingest_url": MOCK_INGEST_URL},
        status=200,
    )
    session = async_get_clientsession(hass)

    result = await async_claim(session, "abc123")

    assert result.token == MOCK_TOKEN
    assert len(aioclient_mock.mock_calls) == 1
    assert str(aioclient_mock.mock_calls[0][1]).startswith(API_BASE)
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 0


async def test_claim_no_override_uses_default_api_base(hass, aioclient_mock, monkeypatch):
    monkeypatch.delenv("SUNPLUG_API_BASE", raising=False)
    aioclient_mock.post(
        f"{API_BASE}{CLAIM_PATH}",
        json={"token": MOCK_TOKEN, "ingest_url": MOCK_INGEST_URL},
        status=200,
    )
    session = async_get_clientsession(hass)
    result = await async_claim(session, "abc123")
    assert result.token == MOCK_TOKEN
