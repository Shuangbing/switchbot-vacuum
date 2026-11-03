"""Regional SDK and statusCode 190 regression; all HTTP calls are simulated."""

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.switchbot_vacuum.s1_api import S1Api
from custom_components.switchbot_vacuum.s1_sdk import (
    S1ProtocolError,
    S1SdkClient,
    regional_endpoint,
)

WONDER = "https://wonderlabs.jp.api.switchbot.net"
SDK_HOST = "https://ap1-sapp-api.botslab.com"
MAC, KEY = "AABBCCDDEEFF", "189c56a889be"
HEADERS = {"authorization": "fake-app-token", "uuid": "fake-installation"}


def response(payload, http=200):
    result = MagicMock(status=http)
    result.json = AsyncMock(return_value=payload)
    context = AsyncMock()
    context.__aenter__.return_value = result
    return context


def region(host="ap1-sapp-api.botslab.com"):
    return response({"statusCode": 100, "body": {"code": "JP", "hosts": [host]}})


def delegated(token="fake-delegated-token", expiry=None):
    return response(
        {
            "statusCode": 100,
            "body": {
                "token": token,
                "valid_time": time.time() + 3600 if expiry is None else expiry,
            },
        }
    )


def properties(value):
    return response({"code": 0, "data": {"result": value}})


def client(*responses):
    session = MagicMock()
    session.request.side_effect = responses
    session.post.side_effect = lambda url, **kw: session.request("POST", url, **kw)
    return S1SdkClient(session, "installation-m2"), session


async def test_apk_authentication_sequence_uses_jp_host_and_selected_family():
    sdk, session = client(region(), delegated(), properties('{"WorkRepeatTimes":2}'))
    await sdk.authorize(WONDER, HEADERS, "selected-family")
    assert await sdk.get_properties(KEY, MAC) == {"WorkRepeatTimes": 2}
    region_call, auth_call, read = session.request.call_args_list
    assert region_call.args == ("GET", WONDER + "/wonder/sweeper360/v1/user/getRegion")
    assert auth_call.args == ("POST", WONDER + "/wonder/sweeper360/v1/auth/applyToken")
    assert auth_call.kwargs["json"] == {"groupID": "selected-family"}
    assert read.args == ("GET", SDK_HOST + "/v1/iot/device/get_property")
    assert read.kwargs["headers"] == {"Authorization": "tokens fake-delegated-token"}
    assert read.kwargs["params"]["product_key"] == KEY
    assert read.kwargs["params"]["device_name"] == MAC
    assert read.kwargs["params"]["ci_cy"] == "JP"
    assert "fake-app-token" not in repr(read)
    assert "fake-delegated-token" not in repr(region_call)
    assert "fake-delegated-token" not in repr(auth_call)
    assert all(c.kwargs["allow_redirects"] is False for c in session.request.call_args_list)


async def test_sdk_set_is_form_encoded_with_json_string_and_fresh_signatures():
    sdk, session = client(region(), delegated(), response({"code": 0}), response({"code": 0}))
    await sdk.authorize(WONDER, HEADERS, "family")
    await sdk.set_properties(KEY, MAC, {"WorkRepeatTimes": 2})
    await sdk.set_properties(KEY, MAC, {"WorkRepeatTimes": 1})
    first, second = session.request.call_args_list[-2:]
    assert first.args == ("POST", SDK_HOST + "/v1/iot/device/set_property")
    assert first.kwargs["data"] == {
        "product_key": KEY,
        "device_name": MAC,
        "items": '{"WorkRepeatTimes":2}',
    }
    assert "json" not in first.kwargs
    assert first.kwargs["params"]["sign_no"] != second.kwargs["params"]["sign_no"]
    assert all(k in first.kwargs["params"] for k in ("appkey", "m2", "sign_ts", "sign"))


async def test_cached_delegated_session_does_not_authenticate_each_poll():
    sdk, session = client(region(), delegated(), properties({}), properties({}))
    await sdk.authorize(WONDER, HEADERS, "family")
    await sdk.get_properties(KEY, MAC)
    await sdk.authorize(WONDER, HEADERS, "family")
    await sdk.get_properties(KEY, MAC)
    assert session.request.call_count == 4


async def test_expired_delegated_session_is_replaced_before_next_request():
    sdk, session = client(region(), delegated(), region(), delegated("new-sdk-token"))
    await sdk.authorize(WONDER, HEADERS, "family")
    sdk.token_expiry = 1
    await sdk.authorize(WONDER, HEADERS, "family")
    assert session.request.call_count == 4
    assert sdk.token == "new-sdk-token"


async def test_changed_family_never_reuses_another_family_token():
    sdk, session = client(region(), delegated(), region(), delegated("other-family-token"))
    await sdk.authorize(WONDER, HEADERS, "family-one")
    await sdk.authorize(WONDER, HEADERS, "family-two")
    assert session.request.call_count == 4
    assert session.request.call_args.kwargs["json"] == {"groupID": "family-two"}


async def test_changed_app_login_invalidates_the_delegated_context():
    sdk, session = client(region(), delegated(), region(), delegated("new-login-token"))
    await sdk.authorize(WONDER, HEADERS, "family")
    await sdk.authorize(WONDER, HEADERS | {"authorization": "new-app-token"}, "family")
    assert session.request.call_count == 4
    assert sdk.token == "new-login-token"


async def test_app_bootstrap_uses_distinct_request_ids():
    sdk, session = client(region(), delegated())
    await sdk.authorize(WONDER, HEADERS, "family")
    first, second = session.request.call_args_list
    assert first.kwargs["headers"]["requestid"] != second.kwargs["headers"]["requestid"]
    assert first.kwargs["headers"]["authorization"] == HEADERS["authorization"]
    assert "requestid" not in HEADERS


@pytest.mark.parametrize(
    "host",
    [
        "https://evil.example",
        "http://ap1-sapp-api.botslab.com",
        "https://ap1-sapp-api.botslab.com.evil.example",
        "https://user:secret@ap1-sapp-api.botslab.com",
        "https://ap1-sapp-api.botslab.com/path",
        "https://ap1-sapp-api.botslab.com?secret=value",
        "",
        None,
        [],
    ],
)
def test_unexpected_sdk_host_rejected(host):
    with pytest.raises(S1ProtocolError):
        regional_endpoint(host)


@pytest.mark.parametrize(
    "host,expected",
    [
        ("ap1-sapp-api.botslab.com", SDK_HOST),
        ("https://eu1-sapp-api.botslab.com/", "https://eu1-sapp-api.botslab.com"),
        ("app.home.360.cn", "https://app.home.360.cn"),
    ],
)
def test_observed_vendor_hosts_are_not_hardcoded_to_eu(host, expected):
    assert regional_endpoint(host) == expected


async def test_invalid_host_does_not_obtain_or_transmit_delegated_token():
    sdk, session = client(region("https://evil.example"))
    with pytest.raises(S1ProtocolError):
        await sdk.authorize(WONDER, HEADERS, "family")
    assert session.request.call_count == 1
    assert sdk.token is None


@pytest.mark.parametrize("expiry", [0, -1, True, "123", float("nan"), float("inf")])
async def test_invalid_expiry_is_not_cached(expiry):
    sdk, _ = client(region(), delegated(expiry=expiry))
    with pytest.raises(S1ProtocolError, match="expired SDK session"):
        await sdk.authorize(WONDER, HEADERS, "family")
    assert sdk.token is None


async def test_missing_family_sends_no_request():
    sdk, session = client()
    with pytest.raises(S1ProtocolError, match="family"):
        await sdk.authorize(WONDER, HEADERS, None)
    session.request.assert_not_called()


@pytest.mark.parametrize("code", [190, 401, True, "SECRET", {"token": "SECRET"}])
async def test_app_errors_name_the_failed_step_without_echoing_body(code):
    sdk, _ = client(response({"statusCode": code, "message": "SECRET"}))
    with pytest.raises(S1ProtocolError) as error:
        await sdk.authorize(WONDER, HEADERS, "family")
    assert "getRegion" in str(error.value)
    assert "SECRET" not in str(error.value)


async def test_apply_token_error_is_identified_separately():
    sdk, _ = client(region(), response({"statusCode": 190, "message": "SECRET"}))
    with pytest.raises(S1ProtocolError, match="applyToken rejected.*190"):
        await sdk.authorize(WONDER, HEADERS, "family")


@pytest.mark.parametrize("value", [{"WorkRepeatTimes": 2}, '{"WorkRepeatTimes":2}'])
async def test_properties_support_dict_and_sdk_json_string(value):
    sdk, _ = client(region(), delegated(), properties(value))
    await sdk.authorize(WONDER, HEADERS, "family")
    assert await sdk.get_properties(KEY, MAC) == {"WorkRepeatTimes": 2}


@pytest.mark.parametrize("value", [None, [], "not-json", "[]"])
async def test_malformed_property_response_is_not_accepted(value):
    sdk, _ = client(region(), delegated(), properties(value))
    await sdk.authorize(WONDER, HEADERS, "family")
    with pytest.raises(S1ProtocolError):
        await sdk.get_properties(KEY, MAC)


@pytest.mark.parametrize("code", [3, False, "SECRET", {"token": "SECRET"}])
async def test_sdk_failure_is_not_proxy_fallback_or_retried_write(code):
    sdk, session = client(region(), delegated(), response({"code": code, "msg": "SECRET"}))
    await sdk.authorize(WONDER, HEADERS, "family")
    with pytest.raises(S1ProtocolError) as error:
        await sdk.set_properties(KEY, MAC, {"WorkRepeatTimes": 2})
    assert "SECRET" not in str(error.value)
    assert session.request.call_count == 3
    assert all("setInfo" not in c.args[1] for c in session.request.call_args_list)


async def test_auth_http_failure_clears_token_without_repeating_action():
    sdk, session = client(region(), delegated(), response({}, http=401))
    await sdk.authorize(WONDER, HEADERS, "family")
    with pytest.raises(S1ProtocolError, match="session rejected"):
        await sdk.action(KEY, MAC, "StartDefaultClean", {"CleanTimes": 2})
    assert sdk.token is None
    assert session.request.call_count == 3


async def test_network_failure_does_not_repeat_action():
    sdk, session = client(region(), delegated())
    await sdk.authorize(WONDER, HEADERS, "family")
    session.request.side_effect = TimeoutError("SECRET")
    with pytest.raises(TimeoutError):
        await sdk.action(KEY, MAC, "StartDefaultClean", {"CleanTimes": 2})
    assert session.request.call_count == 3


async def test_action_uses_sdk_invoke_service_input():
    sdk, session = client(region(), delegated(), response({"code": 0}))
    await sdk.authorize(WONDER, HEADERS, "family")
    await sdk.action(KEY, MAC, "StartDefaultClean", {"CleanTimes": 2})
    assert session.request.call_args.args[1] == SDK_HOST + "/v1/iot/device/invoke_service"
    assert json.loads(session.request.call_args.kwargs["data"]["input"]) == {"CleanTimes": 2}


async def test_190_regression_status_works_settings_bypass_failing_native_proxy():
    status = response(
        {
            "statusCode": 100,
            "body": {
                "items": [
                    {
                        "device_mac": MAC,
                        "online_status": "online",
                        "WorkingStatus": 7,
                    }
                ]
            },
        }
    )
    sdk, session = client(
        status, region(), delegated(), response({"code": 0}), properties('{"WorkRepeatTimes":2}')
    )
    api = S1Api(session, WONDER, HEADERS, KEY, MAC, "family", sdk=sdk)
    assert await api.set_cleaning_passes(2) == {"WorkRepeatTimes": 2}
    urls = [c.args[1] for c in session.request.call_args_list]
    assert any(url.endswith("/set_property") for url in urls)
    assert all(not url.endswith(("/getInfo", "/setInfo", "/setAction")) for url in urls)
    assert len(urls) == 5
