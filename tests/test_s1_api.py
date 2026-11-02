"""APK-backed S1 payloads and failure semantics; every HTTP response is fake."""

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PATH = Path(__file__).resolve().parents[1] / "custom_components/switchbot_vacuum/s1_api.py"
SPEC = importlib.util.spec_from_file_location("s1_api_isolated", PATH)
s1 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(s1)

MAC = "AABBCCDDEEFF"
HOST = "https://wonderlabs.jp.api.switchbot.net"


def client(responses=()):
    session = MagicMock()
    contexts = []
    for status, payload in responses:
        response = MagicMock(status=status)
        response.json = AsyncMock(return_value=payload)
        context = AsyncMock()
        context.__aenter__.return_value = response
        contexts.append(context)
    session.post.side_effect = contexts
    api = s1.S1Api(
        session, HOST, {"authorization": "fake-app-token"}, "189c56a889be", MAC, "family-id"
    )
    return api, session


def info(properties):
    return 200, {"statusCode": 100, "body": properties}


def status(working=7, online="online"):
    return info({"items": [{"device_mac": MAC, "WorkingStatus": working, "online_status": online}]})


@pytest.mark.parametrize("value", [0, 3, -1, None, "2", True, False, 1.5, 2.0, {}, []])
def test_passes_are_strict_wire_integers(value):
    with pytest.raises(s1.S1ProtocolError):
        s1.validate_passes(value)
    assert s1.reported_passes({"WorkRepeatTimes": value}) is None


@pytest.mark.parametrize("times", [1, 2])
async def test_setting_payload_and_readback_do_not_start_cleaning(times):
    api, session = client([status(), info({}), info({"WorkRepeatTimes": times})])
    assert (await api.set_cleaning_passes(times))["WorkRepeatTimes"] == times
    calls = session.post.call_args_list
    assert len(calls) == 3
    write = calls[1]
    assert write.args[0] == HOST + "/wonder/sweeper360/v1/device/setInfo"
    assert write.kwargs["json"] == {
        "productKey": "189c56a889be",
        "deviceName": MAC,
        "groupID": "family-id",
        "items": {"WorkRepeatTimes": times},
    }
    assert write.kwargs["headers"]["authorization"] == "fake-app-token"
    assert all(c.kwargs["allow_redirects"] is False for c in calls)
    assert len({c.kwargs["headers"]["requestid"] for c in calls}) == len(calls)
    assert all("setAction" not in c.args[0] for c in calls)


@pytest.mark.parametrize("working", [1, 2, 3, 4, 5, 9, 10, 11, 31, None, True, "7"])
async def test_active_paused_unknown_task_never_changes_setting(working):
    api, session = client([status(working)])
    with pytest.raises(s1.S1ProtocolError, match="Finish the current task"):
        await api.set_cleaning_passes(2)
    assert session.post.call_count == 1


async def test_offline_never_writes():
    api, session = client([status(online="offline")])
    with pytest.raises(s1.S1ProtocolError, match="offline"):
        await api.set_cleaning_passes(2)
    assert session.post.call_count == 1


async def test_stale_readback_polls_without_repeating_write():
    api, session = client(
        [status(), info({}), info({"WorkRepeatTimes": 1}), info({}), info({"WorkRepeatTimes": 2})]
    )
    with patch.object(s1.asyncio, "sleep", new_callable=AsyncMock) as sleep:
        assert (await api.set_cleaning_passes(2))["WorkRepeatTimes"] == 2
    assert sleep.await_count == 2
    assert sum(c.args[0].endswith("setInfo") for c in session.post.call_args_list) == 1


async def test_accepted_write_without_readback_is_not_success():
    api, session = client([status(), info({}), info({}), info({}), info({})])
    with (
        patch.object(s1.asyncio, "sleep", new_callable=AsyncMock),
        pytest.raises(s1.S1ProtocolError, match="not confirmed"),
    ):
        await api.set_cleaning_passes(2)
    assert session.post.call_count == 5


async def test_network_failure_does_not_repeat_write():
    api, session = client([status()])
    session.post.side_effect = [session.post.side_effect.__next__(), TimeoutError("fake-secret")]
    with pytest.raises(TimeoutError):
        await api.set_cleaning_passes(2)
    assert session.post.call_count == 2


@pytest.mark.parametrize("times", [1, 2])
async def test_start_sends_reported_count_not_fixed_one(times):
    api, session = client([info({"WorkRepeatTimes": times}), info({})])
    assert await api.start() == times
    assert session.post.call_args.kwargs["json"] == {
        "productKey": "189c56a889be",
        "deviceName": MAC,
        "identifier": "StartDefaultClean",
        "input": {"CleanTimes": times},
    }


async def test_unknown_passes_does_not_start():
    api, session = client([info({})])
    with pytest.raises(s1.S1ProtocolError, match="unknown"):
        await api.start()
    assert session.post.call_count == 1


@pytest.mark.parametrize("identifier", ["PauseRobot", "ReturnChargeBase"])
async def test_action_payload(identifier):
    api, session = client([info({})])
    await api.action(identifier)
    assert session.post.call_args.kwargs["json"]["identifier"] == identifier
    assert session.post.call_args.kwargs["json"]["input"] == {}


@pytest.mark.parametrize(
    "body",
    [
        {"WorkRepeatTimes": 2},
        '{"WorkRepeatTimes":2}',
        {"result": '{"WorkRepeatTimes":2}'},
        {"items": {"WorkRepeatTimes": 2}},
    ],
)
async def test_property_response_shapes(body):
    api, _ = client([info(body)])
    assert await api.get_properties() == {"WorkRepeatTimes": 2}


@pytest.mark.parametrize("body", [None, [], "invalid-json", {"result": []}])
async def test_invalid_property_response(body):
    api, _ = client([info(body)])
    with pytest.raises(s1.S1ProtocolError):
        await api.get_properties()


@pytest.mark.parametrize("code", [401, "fake-secret", True, {"token": "fake-secret"}])
async def test_rejected_response_never_echoes_cloud_secrets(code):
    api, _ = client([(200, {"statusCode": code, "message": "fake-secret"})])
    with pytest.raises(s1.S1ProtocolError) as error:
        await api.get_properties()
    assert "fake-secret" not in str(error.value)


@pytest.mark.parametrize("http_status", [301, 302, 401, 403, 500])
async def test_http_errors_are_not_accepted(http_status):
    api, _ = client([(http_status, {"statusCode": 100})])
    with pytest.raises(s1.S1ProtocolError):
        await api.get_properties()


async def test_missing_family_prevents_write():
    api, session = client()
    api.group_id = None
    with pytest.raises(s1.S1ProtocolError, match="family"):
        await api.set_properties({"WorkRepeatTimes": 2})
    session.post.assert_not_called()


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
    api, session = client()
    with pytest.raises(s1.S1ProtocolError):
        await api.set_properties(items)
    session.post.assert_not_called()


def test_unexpected_endpoint_rejected():
    with pytest.raises(s1.S1ProtocolError):
        s1.S1Api(MagicMock(), "https://example.invalid", {}, "key", MAC, "family")


async def test_status_identity_checked():
    api, _ = client([info({"items": [{"device_mac": "OTHER"}]})])
    with pytest.raises(s1.S1ProtocolError, match="different device"):
        await api.get_status()
