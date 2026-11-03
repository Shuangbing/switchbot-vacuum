"""S1's regional Qihoo SDK transport, not the native setInfo proxy.

Android VacuumUtil gets getRegion + applyToken before entering the Flutter SDK.
The SDK sends signed form/GET requests authenticated with a delegated token.
No credential is persisted or sent to an arbitrary region URL.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .k10_api import signed_params

if TYPE_CHECKING:
    from aiohttp import ClientSession


class S1ProtocolError(Exception):
    """A bounded error without response bodies, signed URLs or credential text."""


def regional_endpoint(host: Any) -> str:
    """Accept only HTTPS vendor hosts returned by the App's getRegion route."""
    if not isinstance(host, str) or not host:
        raise S1ProtocolError("S1 getRegion returned no SDK host")
    parsed = urlsplit(host if "://" in host else "https://" + host)
    name = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or not (name.endswith(".botslab.com") or name == "app.home.360.cn")
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise S1ProtocolError("S1 getRegion returned an unsupported SDK host")
    return "https://" + name


class S1SdkClient:
    """Cache delegated credentials per selected family, using an HA-owned session."""

    def __init__(self, session: ClientSession, installation_id: str) -> None:
        self.session = session
        self.installation_id = installation_id
        self.endpoint: str | None = None
        self.token: str | None = None
        self.token_expiry = 0.0
        self.region_code: str | None = None
        self._context: tuple[str, str, str] | None = None
        self._auth_lock = asyncio.Lock()

    async def _app_request(
        self,
        method: str,
        endpoint: str,
        path: str,
        headers: dict[str, str],
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Validate again at the credential boundary, even when called without S1Api.
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
        async with (
            asyncio.timeout(30),
            self.session.request(
                method,
                endpoint.rstrip("/") + path,
                headers=headers | {"requestid": str(uuid.uuid4())},
                json=body,
                allow_redirects=False,
            ) as response,
        ):
            if response.status != 200:
                raise S1ProtocolError(
                    f"S1 {path.rsplit('/', 1)[-1]} returned HTTP {response.status}"
                )
            result = await response.json()
        if not isinstance(result, dict) or type(result.get("statusCode")) is not int:
            raise S1ProtocolError(f"S1 {path.rsplit('/', 1)[-1]} returned an invalid status")
        if result["statusCode"] != 100:
            raise S1ProtocolError(
                f"S1 {path.rsplit('/', 1)[-1]} rejected request (statusCode {result['statusCode']})"
            )
        body = result.get("body")
        if not isinstance(body, dict):
            raise S1ProtocolError(f"S1 {path.rsplit('/', 1)[-1]} returned an invalid body")
        return body

    async def authorize(
        self,
        wonder_endpoint: str,
        app_headers: dict[str, str],
        group_id: str | None,
    ) -> None:
        if not group_id:
            raise S1ProtocolError("S1 family identifier is missing; reconfigure the integration")
        app_token = app_headers.get("authorization")
        if not isinstance(app_token, str) or not app_token:
            raise S1ProtocolError("S1 App session is missing; reauthenticate the integration")
        context = (wonder_endpoint.rstrip("/"), group_id, app_token)
        async with self._auth_lock:
            if (
                self._context == context
                and self.token
                and self.endpoint
                and self.token_expiry > time.time() + 60
            ):
                return
            self.token, self.endpoint, self.token_expiry, self._context = None, None, 0, None
            region = await self._app_request(
                "GET",
                wonder_endpoint,
                "/wonder/sweeper360/v1/user/getRegion",
                app_headers,
            )
            hosts = region.get("hosts")
            if not isinstance(hosts, list) or not hosts:
                raise S1ProtocolError("S1 getRegion returned no SDK host")
            endpoint = regional_endpoint(hosts[0])
            delegated = await self._app_request(
                "POST",
                wonder_endpoint,
                "/wonder/sweeper360/v1/auth/applyToken",
                app_headers,
                {"groupID": group_id},
            )
            token, expiry = delegated.get("token"), delegated.get("valid_time")
            if (
                not isinstance(token, str)
                or not token
                or type(expiry) not in (int, float)
                or not math.isfinite(expiry)
                or expiry <= time.time()
            ):
                raise S1ProtocolError("S1 applyToken returned an invalid or expired SDK session")
            self.endpoint, self.token, self.token_expiry = endpoint, token, expiry
            self.region_code = region.get("code") if isinstance(region.get("code"), str) else None
            self._context = context

    async def _request(
        self,
        method: str,
        path: str,
        product_key: str,
        device_mac: str,
        form: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not self.endpoint or not self.token:
            raise S1ProtocolError("S1 SDK session is not initialized")
        params = signed_params(self.installation_id)
        params.update(appver="9.11.15.13", product_key=product_key, device_name=device_mac)
        if self.region_code:
            params["ci_cy"] = self.region_code
        async with (
            asyncio.timeout(30),
            self.session.request(
                method,
                self.endpoint + path,
                params=params,
                data=form,
                headers={"Authorization": "tokens " + self.token},
                allow_redirects=False,
            ) as response,
        ):
            if response.status in (401, 403):
                self.token, self.token_expiry = None, 0
                raise S1ProtocolError("S1 SDK session rejected; refresh before retrying the action")
            if response.status != 200:
                raise S1ProtocolError(
                    f"S1 SDK {path.rsplit('/', 1)[-1]} returned HTTP {response.status}"
                )
            result = await response.json()
        if not isinstance(result, dict) or type(result.get("code")) is not int:
            raise S1ProtocolError("S1 SDK returned an invalid status")
        if result["code"] != 0:
            raise S1ProtocolError(
                f"S1 SDK {path.rsplit('/', 1)[-1]} rejected request (code {result['code']})"
            )
        return result

    async def get_properties(self, product_key: str, device_mac: str) -> dict[str, Any]:
        result = await self._request(
            "GET",
            "/v1/iot/device/get_property",
            product_key,
            device_mac,
        )
        data = result.get("data")
        properties = data.get("result") if isinstance(data, dict) else None
        if isinstance(properties, str):
            try:
                properties = json.loads(properties)
            except ValueError:
                raise S1ProtocolError("S1 SDK returned malformed property JSON") from None
        if not isinstance(properties, dict):
            raise S1ProtocolError("S1 SDK returned an invalid property object")
        return properties

    async def set_properties(
        self,
        product_key: str,
        device_mac: str,
        items: dict[str, int],
    ) -> None:
        await self._request(
            "POST",
            "/v1/iot/device/set_property",
            product_key,
            device_mac,
            {
                "product_key": product_key,
                "device_name": device_mac,
                "items": json.dumps(items, separators=(",", ":")),
            },
        )

    async def action(
        self,
        product_key: str,
        device_mac: str,
        identifier: str,
        inputs: dict[str, int],
    ) -> None:
        # Send exactly once. code=0 is acceptance, not verified task completion.
        await self._request(
            "POST",
            "/v1/iot/device/invoke_service",
            product_key,
            device_mac,
            {
                "product_key": product_key,
                "device_name": device_mac,
                "identifier": identifier,
                "input": json.dumps(inputs, separators=(",", ":")),
            },
        )
