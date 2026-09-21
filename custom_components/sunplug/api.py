"""Sunplug backend API client."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from .const import API_BASE, CLAIM_PATH

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=10)


class SunplugApiError(Exception):
    """Base error talking to the Sunplug API."""


class CannotConnect(SunplugApiError):
    """Network error or unexpected response talking to the Sunplug API."""


class InvalidCode(SunplugApiError):
    """The pairing code was rejected by the backend."""


@dataclass
class ClaimResult:
    """Result of a successful pairing-code claim."""

    token: str
    ingest_url: str


async def async_claim(
    session: aiohttp.ClientSession, code: str, name: str = "Home Assistant"
) -> ClaimResult:
    """Claim a pairing code and obtain a token + ingest URL."""
    try:
        response = await session.post(
            f"{API_BASE}{CLAIM_PATH}",
            json={"code": code, "name": name, "client": "homeassistant"},
            timeout=_TIMEOUT,
        )
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise CannotConnect(str(err)) from err

    if response.status == 400:
        raise InvalidCode("Pairing code was rejected")
    if response.status != 200:
        raise CannotConnect(f"Unexpected status {response.status}")

    try:
        body = await response.json()
    except (aiohttp.ContentTypeError, ValueError) as err:
        raise CannotConnect("Invalid response body") from err

    token = body.get("token")
    ingest_url = body.get("ingest_url")
    if not token or not ingest_url:
        raise CannotConnect("Response missing token or ingest_url")

    return ClaimResult(token=token, ingest_url=ingest_url)


@dataclass
class PostResult:
    """Outcome of posting a reading to the ingest endpoint."""

    status: int | None
    ok: bool = False
    new_ingest_url: str | None = None
    error_body: str | None = None
    exception: str | None = None


async def async_post_reading(
    session: aiohttp.ClientSession,
    ingest_url: str,
    token: str,
    payload: dict[str, Any],
) -> PostResult:
    """POST a reading to the ingest endpoint. Never raises for HTTP/network errors."""
    try:
        response = await session.post(
            ingest_url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT,
        )
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        return PostResult(status=None, exception=type(err).__name__)

    async with response:
        if response.status == 200:
            try:
                body = await response.json()
            except (aiohttp.ContentTypeError, ValueError):
                body = {}
            return PostResult(
                status=200,
                ok=bool(body.get("ok")),
                new_ingest_url=body.get("ingest_url"),
            )

        text = await response.text()
        return PostResult(status=response.status, error_body=text[:500])
