"""S1/S1 Plus named-property protocol recovered from Android 9.11.15.13.

WorkRepeatTimes is an optional device setting; CleanTimes is a StartDefaultClean input.
For firmware without that setting, the coordinator persists the input preference.
Status reads use the working SwitchBot proxy; settings and actions use the
regional SDK transport. Never substitute S10 numeric properties or a fixed EU host.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .s1_sdk import S1ProtocolError, S1SdkClient

if TYPE_CHECKING:
    from aiohttp import ClientSession

IDLE_STATUSES = frozenset({0, 6, 7, 8})  # standby, charging, charged, dormant
ACTION_INPUTS = {"PauseRobot", "ReturnChargeBase"}


def validate_passes(value: Any) -> int:
    """Accept the wire values 1 and 2, not booleans, indices or coerced floats."""
    if type(value) is not int or value not in (1, 2):
        raise S1ProtocolError("Cleaning passes must be integer 1 or 2")
    return value


def reported_passes(properties: dict[str, Any]) -> int | None:
    """Keep absent or invalid telemetry unknown rather than inventing one pass."""
    value = properties.get("WorkRepeatTimes")
    return value if type(value) is int and value in (1, 2) else None


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            raise S1ProtocolError("S1 returned malformed property JSON") from None
    if not isinstance(value, dict):
        raise S1ProtocolError("S1 returned an invalid property object")
    return value


class S1Api:
    """Use an HA-managed session and current App authorization headers."""

    def __init__(
        self,
        session: ClientSession,
        endpoint: str,
        headers: dict[str, str],
        product_key: str,
        device_mac: str,
        group_id: str | None,
        *,
        sdk: S1SdkClient | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".api.switchbot.net")
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise S1ProtocolError("Invalid SwitchBot regional endpoint")
        self.session = session
        self.endpoint = endpoint.rstrip("/")
        self.headers = headers
        self.product_key = product_key
        self.device_mac = device_mac
        self.group_id = group_id
        self.sdk = sdk or S1SdkClient(
            session, headers.get("uuid", uuid.uuid4().hex).replace("-", "")
        )

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        async with (
            asyncio.timeout(30),
            self.session.post(
                self.endpoint + path,
                json=body,
                headers=self.headers | {"requestid": str(uuid.uuid4())},
                allow_redirects=False,
            ) as response,
        ):
            if response.status in (401, 403):
                raise S1ProtocolError("S1 App session rejected; reauthenticate the integration")
            if response.status != 200:
                raise S1ProtocolError(f"S1 API returned HTTP {response.status}")
            result = await response.json()
        if not isinstance(result, dict) or type(result.get("statusCode")) is not int:
            raise S1ProtocolError("S1 API returned an invalid status")
        if result["statusCode"] != 100:
            raise S1ProtocolError(
                f"S1 {path.rsplit('/', 1)[-1]} rejected request (statusCode {result['statusCode']})"
            )
        return result

    async def get_status(self) -> dict[str, Any]:
        result = await self._post("/wonder/devicestatus/v1/getstatus", {"items": [self.device_mac]})
        items = _object(result.get("body")).get("items")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise S1ProtocolError("S1 status did not return the selected device")
        status = items[0]
        if status.get("device_mac", self.device_mac) != self.device_mac:
            raise S1ProtocolError("S1 status belongs to a different device")
        return status

    async def get_properties(self) -> dict[str, Any]:
        await self.sdk.authorize(self.endpoint, self.headers, self.group_id)
        return await self.sdk.get_properties(self.product_key, self.device_mac)

    async def set_properties(self, items: dict[str, int]) -> None:
        """Write only the two APK-backed properties implemented by this client."""
        if len(items) != 1:
            raise S1ProtocolError("Set exactly one S1 property")
        if "WorkRepeatTimes" in items:
            validate_passes(items["WorkRepeatTimes"])
        elif "SuctionPowLevel" in items:
            value = items["SuctionPowLevel"]
            if type(value) is not int or value not in (0, 1, 2, 3):
                raise S1ProtocolError("S1 suction must be integer 0 through 3")
        else:
            raise S1ProtocolError("Unsupported S1 property")
        if not self.group_id:
            raise S1ProtocolError("S1 family identifier is missing; reconfigure the integration")
        await self.sdk.authorize(self.endpoint, self.headers, self.group_id)
        await self.sdk.set_properties(self.product_key, self.device_mac, items)

    async def ensure_idle_for_passes(self) -> None:
        """Changing the next-task preference never interrupts an active task."""
        status = await self.get_status()
        if status.get("online_status") != "online":
            raise S1ProtocolError("S1 is offline")
        working = status.get("WorkingStatus")
        if type(working) is not int or working not in IDLE_STATUSES:
            raise S1ProtocolError("Finish the current task before changing cleaning passes")

    async def set_cleaning_passes(self, times: int) -> dict[str, Any]:
        validate_passes(times)
        await self.ensure_idle_for_passes()
        # Prove that the selected SDK device is readable before sending a write.
        # v0.5.1 hid read failures behind `unknown` then attempted set_property.
        before = await self.get_properties()
        if reported_passes(before) is None:
            raise S1ProtocolError(
                "S1 get_property did not report valid WorkRepeatTimes; no setting was sent"
            )
        await self.set_properties({"WorkRepeatTimes": times})
        # Repeat only reads. A timed-out write may already have reached the robot.
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(2)
            properties = await self.get_properties()
            if reported_passes(properties) == times:
                return properties
        raise S1ProtocolError(
            "S1 accepted the write but cleaning passes were not confirmed; check the App"
        )

    async def start(self, *, times: int | None = None) -> int:
        """Send either a deliberate local preference or fresh device telemetry."""
        if times is not None:
            validate_passes(times)
            await self.sdk.authorize(self.endpoint, self.headers, self.group_id)
        else:
            properties = await self.get_properties()
            times = reported_passes(properties)
        if times is None:
            raise S1ProtocolError(
                "S1 cleaning passes are unknown; refresh or set them before starting"
            )
        await self.sdk.action(
            self.product_key, self.device_mac, "StartDefaultClean", {"CleanTimes": times}
        )
        return times

    async def action(self, identifier: str) -> None:
        if identifier not in ACTION_INPUTS:
            raise S1ProtocolError("Unsupported S1 action")
        await self.sdk.authorize(self.endpoint, self.headers, self.group_id)
        await self.sdk.action(self.product_key, self.device_mac, identifier, {})
