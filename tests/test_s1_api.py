"""S1 controls use SDK properties/actions and only the working proxy for status."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.switchbot_vacuum import s1_api as s1

MAC = "AABBCCDDEEFF"
HOST = "https://wonderlabs.jp.api.switchbot.net"
KEY = "189c56a889be"


def client(*, working=7, online="online", properties=None):
    session = MagicMock()
    response = MagicMock(status=200)
    response.json = AsyncMock(
        return_value={
            "statusCode": 100,
            "body": {
                "items": [{"device_mac": MAC, "WorkingStatus": working, "online_status": online}]
            },
        }
    )
    context = AsyncMock()
    context.__aenter__.return_value = response
    session.post.return_value = context
    sdk = MagicMock()
    sdk.authorize = AsyncMock()
    sdk.get_properties = AsyncMock(return_value=properties or {})
    sdk.set_properties = AsyncMock()
    sdk.action = AsyncMock()
    api = s1.S1Api(
        session, HOST, {"authorization": "fake-app-token"}, KEY, MAC, "family-id", sdk=sdk
    )
    return api, session, sdk, response


@pytest.mark.parametrize("value", [0, 3, -1, None, "2", True, False, 1.5, 2.0, {}, []])
def test_passes_are_strict_wire_integers(value):
    with pytest.raises(s1.S1ProtocolError):
        s1.validate_passes(value)
    assert s1.reported_passes({"WorkRepeatTimes": value}) is None


@pytest.mark.parametrize("times", [1, 2])
async def test_setting_uses_sdk_not_rejected_proxy(times):
    api, session, sdk, _ = client(properties={"WorkRepeatTimes": times})
    assert (await api.set_cleaning_passes(times))["WorkRepeatTimes"] == times
    sdk.set_properties.assert_awaited_once_with(KEY, MAC, {"WorkRepeatTimes": times})
    sdk.action.assert_not_awaited()
    # The only native proxy request is the working status preflight.
    assert session.post.call_count == 1
    assert session.post.call_args.args[0].endswith("/getstatus")


@pytest.mark.parametrize("working", [1, 2, 3, 4, 5, 9, 10, 11, 31, None, True, "7"])
async def test_active_paused_unknown_task_never_changes_setting(working):
    api, _, sdk, _ = client(working=working)
    with pytest.raises(s1.S1ProtocolError, match="Finish the current task"):
        await api.set_cleaning_passes(2)
    sdk.authorize.assert_not_awaited()
    sdk.set_properties.assert_not_awaited()


async def test_offline_never_writes():
    api, _, sdk, _ = client(online="offline")
    with pytest.raises(s1.S1ProtocolError, match="offline"):
        await api.set_cleaning_passes(2)
    sdk.set_properties.assert_not_awaited()


async def test_stale_readback_polls_without_repeating_write():
    api, _, sdk, _ = client()
    sdk.get_properties.side_effect = [{"WorkRepeatTimes": 1}, {}, {"WorkRepeatTimes": 2}]
    with patch.object(s1.asyncio, "sleep", new_callable=AsyncMock) as sleep:
        assert (await api.set_cleaning_passes(2))["WorkRepeatTimes"] == 2
    assert sleep.await_count == 2
    assert sdk.set_properties.await_count == 1


async def test_accepted_write_without_readback_is_not_success():
    api, _, sdk, _ = client()
    with (
        patch.object(s1.asyncio, "sleep", new_callable=AsyncMock),
        pytest.raises(s1.S1ProtocolError, match="not confirmed"),
    ):
        await api.set_cleaning_passes(2)
    assert sdk.set_properties.await_count == 1
    assert sdk.get_properties.await_count == 3


async def test_network_failure_does_not_repeat_write():
    api, _, sdk, _ = client()
    sdk.set_properties.side_effect = TimeoutError("fake-secret")
    with pytest.raises(TimeoutError):
        await api.set_cleaning_passes(2)
    assert sdk.set_properties.await_count == 1
    sdk.get_properties.assert_not_awaited()


@pytest.mark.parametrize("times", [1, 2])
async def test_start_sends_reported_count_not_fixed_one(times):
    api, session, sdk, _ = client(properties={"WorkRepeatTimes": times})
    assert await api.start() == times
    sdk.action.assert_awaited_once_with(KEY, MAC, "StartDefaultClean", {"CleanTimes": times})
    session.post.assert_not_called()


async def test_unknown_passes_does_not_start():
    api, _, sdk, _ = client()
    with pytest.raises(s1.S1ProtocolError, match="unknown"):
        await api.start()
    sdk.action.assert_not_awaited()


@pytest.mark.parametrize("identifier", ["PauseRobot", "ReturnChargeBase"])
async def test_action_uses_sdk_not_set_action_proxy(identifier):
    api, session, sdk, _ = client()
    await api.action(identifier)
    sdk.action.assert_awaited_once_with(KEY, MAC, identifier, {})
    session.post.assert_not_called()


@pytest.mark.parametrize("code", [190, 401, "fake-secret", True, {"token": "fake-secret"}])
async def test_rejected_status_response_never_echoes_cloud_secrets(code):
    api, _, _, response = client()
    response.json.return_value = {"statusCode": code, "message": "fake-secret"}
    with pytest.raises(s1.S1ProtocolError) as error:
        await api.get_status()
    assert "fake-secret" not in str(error.value)
    if code == 190:
        assert "getstatus" in str(error.value)


@pytest.mark.parametrize("http_status", [301, 302, 401, 403, 500])
async def test_http_errors_are_not_accepted(http_status):
    api, _, _, response = client()
    response.status = http_status
    with pytest.raises(s1.S1ProtocolError):
        await api.get_status()


async def test_missing_family_prevents_write():
    api, _, sdk, _ = client()
    api.group_id = None
    with pytest.raises(s1.S1ProtocolError, match="family"):
        await api.set_properties({"WorkRepeatTimes": 2})
    sdk.set_properties.assert_not_awaited()


@pytest.mark.parametrize(
    "items",
    [
        {},
        {"Other": 1},
        {"SuctionPowLevel": 4},
        {"SuctionPowLevel": False},
        {"WorkRepeatTimes": 1, "SuctionPowLevel": 2},
    ],
)
async def test_unimplemented_properties_never_sent(items):
    api, _, sdk, _ = client()
    with pytest.raises(s1.S1ProtocolError):
        await api.set_properties(items)
    sdk.set_properties.assert_not_awaited()


def test_unexpected_endpoint_rejected():
    with pytest.raises(s1.S1ProtocolError):
        s1.S1Api(MagicMock(), "https://example.invalid", {}, "key", MAC, "family")


async def test_status_identity_checked():
    api, _, _, response = client()
    response.json.return_value = {"statusCode": 100, "body": {"items": [{"device_mac": "OTHER"}]}}
    with pytest.raises(s1.S1ProtocolError, match="different device"):
        await api.get_status()
