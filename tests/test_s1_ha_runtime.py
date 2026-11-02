"""S1 discovery, coordinator and entities against Home Assistant 2026.10 APIs."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.vacuum import VacuumActivity, VacuumEntityFeature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.switchbot_vacuum import binary_sensor, config_flow, select, sensor, vacuum
from custom_components.switchbot_vacuum import coordinator as module
from custom_components.switchbot_vacuum.const import (
    DEVICE_TYPE_K10,
    DEVICE_TYPE_S1,
    DEVICE_TYPE_S1_PLUS,
    DEVICE_TYPE_S10,
    DOMAIN,
)
from custom_components.switchbot_vacuum.s1_api import S1ProtocolError


def coordinator(device_type=DEVICE_TYPE_S1):
    coord = MagicMock()
    coord.entry = SimpleNamespace(data={"device_type": device_type}, entry_id="s1", title="My S1")
    coord.device_mac = "AABBCCDDEEFF"
    coord.device_name = "My S1"
    coord.data = {
        "online": True,
        "work_status": 7,
        "battery": 85,
        "clean_mode": {"times": 2, "fan_level": 0},
    }
    coord.async_s1_control = AsyncMock()
    return coord


def actual_coordinator():
    coord = object.__new__(module.SwitchBotS10Coordinator)
    coord.entry = SimpleNamespace(
        data={"device_type": DEVICE_TYPE_S1, "group_id": "family"}, title="My S1"
    )
    coord.hass = MagicMock()
    coord.device_mac = "AABBCCDDEEFF"
    coord.data = coordinator().data
    coord._s1_lock = asyncio.Lock()
    coord._ensure_token = AsyncMock()
    coord.wonderlab_endpoint = "https://wonderlabs.jp.api.switchbot.net"
    coord._headers = MagicMock(return_value={"authorization": "fake-app-token"})
    coord.async_request_refresh = AsyncMock()
    coord.async_set_updated_data = MagicMock(side_effect=lambda data: setattr(coord, "data", data))
    return coord


@pytest.mark.parametrize("kind", [DEVICE_TYPE_S1, DEVICE_TYPE_S1_PLUS])
async def test_s1_select_created_without_k10_eu_restriction(kind):
    coord = coordinator(kind)
    coord.wonderlab_endpoint = "https://wonderlabs.jp.api.switchbot.net"
    hass = SimpleNamespace(data={DOMAIN: {"s1": coord}})
    add = MagicMock()
    await select.async_setup_entry(hass, coord.entry, add)
    entity = add.call_args.args[0][0]
    assert isinstance(entity, select.SwitchBotCleaningPasses)
    assert entity.options == ["1", "2"]
    assert entity.current_option == "2"
    await entity.async_select_option("1")
    coord.async_s1_control.assert_awaited_once_with("passes", 1)


@pytest.mark.parametrize("value", [None, True, 0, 3, "2"])
def test_select_never_invents_current_option(value):
    coord = coordinator()
    coord.data["clean_mode"]["times"] = value
    assert select.SwitchBotCleaningPasses(coord).current_option is None


async def test_select_invalid_option_is_rejected_without_io():
    coord = coordinator()
    entity = select.SwitchBotCleaningPasses(coord)
    with pytest.raises(HomeAssistantError):
        await entity.async_select_option("3")
    coord.async_s1_control.assert_not_awaited()


def test_missing_s1_attributes_never_default_to_idle_or_one_pass():
    coord = coordinator()
    coord.data = {"online": True, "clean_mode": {}}
    entity = vacuum.SwitchBotS10Vacuum(coord)
    assert entity.activity is None
    assert entity.extra_state_attributes["times"] is None
    assert select.SwitchBotCleaningPasses(coord).current_option is None


@pytest.mark.parametrize(
    "code,activity",
    [
        (0, VacuumActivity.IDLE),
        (1, VacuumActivity.CLEANING),
        (2, VacuumActivity.CLEANING),
        (3, VacuumActivity.CLEANING),
        (4, VacuumActivity.PAUSED),
        (5, VacuumActivity.RETURNING),
        (6, VacuumActivity.DOCKED),
        (7, VacuumActivity.DOCKED),
        (8, VacuumActivity.IDLE),
        (9, VacuumActivity.ERROR),
        (11, VacuumActivity.DOCKED),
        (99, None),
    ],
)
def test_s1_status_does_not_use_s10_numeric_meanings(code, activity):
    coord = coordinator()
    coord.data["work_status"] = code
    entity = vacuum.SwitchBotS10Vacuum(coord)
    assert entity.activity == activity
    assert not entity.supported_features & VacuumEntityFeature.SEND_COMMAND
    assert entity.fan_speed == "quiet"
    assert "water_level" not in entity.extra_state_attributes


async def test_standard_vacuum_actions_route_to_s1_only():
    coord = coordinator()
    entity = vacuum.SwitchBotS10Vacuum(coord)
    await entity.async_start()
    await entity.async_pause()
    await entity.async_stop()
    await entity.async_return_to_base()
    await entity.async_set_fan_speed("max")
    await entity.async_set_cleaning_passes(2)
    assert [c.args for c in coord.async_s1_control.await_args_list] == [
        ("start",),
        ("pause",),
        ("pause",),
        ("dock",),
        ("suction", 3),
        ("passes", 2),
    ]
    coord.async_send_action.assert_not_called()
    coord.async_send_command.assert_not_called()


async def test_s1_room_or_raw_command_never_becomes_whole_house_clean():
    coord = coordinator()
    entity = vacuum.SwitchBotS10Vacuum(coord)
    with pytest.raises(HomeAssistantError):
        await entity.async_clean_rooms(["Kitchen"])
    with pytest.raises(HomeAssistantError):
        await entity.async_send_command("any", {"function_id": 1001})
    coord.async_s1_control.assert_not_awaited()
    coord.async_send_command.assert_not_called()


@pytest.mark.parametrize("kind", [DEVICE_TYPE_K10, DEVICE_TYPE_S10])
async def test_new_setting_service_is_explicitly_s1_only(kind):
    coord = coordinator(kind)
    with pytest.raises(HomeAssistantError):
        await vacuum.SwitchBotS10Vacuum(coord).async_set_cleaning_passes(2)
    coord.async_s1_control.assert_not_awaited()


async def test_cloud_count_updates_only_after_confirmed_readback():
    coord = actual_coordinator()
    api = MagicMock()
    api.set_cleaning_passes = AsyncMock(return_value={"WorkRepeatTimes": 1})
    coord._s1_client = AsyncMock(return_value=api)
    await coord.async_s1_control("passes", 1)
    assert coord.data["clean_mode"]["times"] == 1
    api.start.assert_not_called()
    coord.async_request_refresh.assert_not_awaited()


async def test_failed_readback_invalidates_the_displayed_count():
    coord = actual_coordinator()
    api = MagicMock()
    api.set_cleaning_passes = AsyncMock(side_effect=S1ProtocolError("Not confirmed"))
    coord._s1_client = AsyncMock(return_value=api)
    with pytest.raises(HomeAssistantError, match="Not confirmed"):
        await coord.async_s1_control("passes", 1)
    assert coord.data["clean_mode"]["times"] is None


async def test_transport_failure_does_not_leak_sensitive_url_or_retry():
    coord = actual_coordinator()
    api = MagicMock()
    api.set_cleaning_passes = AsyncMock(side_effect=TimeoutError("https://fake/SECRET"))
    coord._s1_client = AsyncMock(return_value=api)
    with pytest.raises(HomeAssistantError) as error:
        await coord.async_s1_control("passes", 1)
    assert "SECRET" not in str(error.value)
    assert api.set_cleaning_passes.await_count == 1


async def test_missing_optional_count_keeps_vacuum_telemetry():
    coord = actual_coordinator()
    api = MagicMock()
    api.get_status = AsyncMock(
        return_value={"online_status": "online", "WorkingStatus": 7, "BatteryLevel": 85}
    )
    api.get_properties = AsyncMock(side_effect=S1ProtocolError("No properties"))
    coord._s1_client = AsyncMock(return_value=api)
    data = await coord._async_update_data_s1()
    assert data["battery"] == 85
    assert data["work_status"] == 7
    assert data["clean_mode"]["times"] is None
    assert data["rooms"] == {}


async def test_required_status_failure_raises_update_failed_without_secrets():
    coord = actual_coordinator()
    coord._s1_client = AsyncMock(side_effect=TimeoutError("SECRET"))
    with pytest.raises(UpdateFailed) as error:
        await coord._async_update_data_s1()
    assert "SECRET" not in str(error.value)


async def test_s1_update_never_calls_s10_shadow_api_or_refreshes_maps():
    coord = actual_coordinator()
    coord.device_mac = None
    coord.entry.data["device_mac"] = "AABBCCDDEEFF"
    coord._async_update_data_s1 = AsyncMock(return_value={"battery": 85})
    coord.async_get_properties = AsyncMock(side_effect=AssertionError("S10 API"))
    assert await coord._async_update_data() == {"battery": 85}
    coord.async_get_properties.assert_not_awaited()


async def test_s1_force_refresh_skips_s10_map_properties():
    coord = actual_coordinator()
    coord.async_get_properties = AsyncMock(side_effect=AssertionError("S10 map API"))
    await coord.async_refresh_rooms()
    coord.async_get_properties.assert_not_awaited()


async def test_pass_change_and_start_are_serialized():
    coord = actual_coordinator()
    entered, release = asyncio.Event(), asyncio.Event()
    api = MagicMock()

    async def write_and_confirm(times):
        entered.set()
        await release.wait()
        return {"WorkRepeatTimes": times}

    api.set_cleaning_passes = AsyncMock(side_effect=write_and_confirm)
    api.start = AsyncMock()
    coord._s1_client = AsyncMock(return_value=api)
    setting = asyncio.create_task(coord.async_s1_control("passes", 1))
    await asyncio.wait_for(entered.wait(), timeout=1)
    starting = asyncio.create_task(coord.async_s1_control("start"))
    await asyncio.sleep(0)
    api.start.assert_not_awaited()
    release.set()
    await asyncio.gather(setting, starting)
    api.start.assert_awaited_once()


async def test_invalid_control_count_never_invalidates_real_telemetry():
    coord = actual_coordinator()
    coord._s1_client = AsyncMock()
    with pytest.raises(HomeAssistantError):
        await coord.async_s1_control("passes", True)
    assert coord.data["clean_mode"]["times"] == 2
    coord._s1_client.assert_not_awaited()


async def test_old_entry_discovers_its_selected_family_before_write():
    coord = actual_coordinator()
    coord.entry.data.pop("group_id")
    api = MagicMock(group_id=None)
    api.set_cleaning_passes = AsyncMock(return_value={"WorkRepeatTimes": 1})
    coord._s1_client = AsyncMock(return_value=api)
    coord.async_discover_devices = AsyncMock(
        return_value=[
            {"device_mac": "OTHER", "group_id": "other-family"},
            {"device_mac": coord.device_mac, "group_id": "selected-family"},
        ]
    )
    await coord.async_s1_control("passes", 1)
    assert api.group_id == "selected-family"


async def test_regional_client_uses_ha_session_and_apk_product_key():
    coord = actual_coordinator()
    session = MagicMock()
    with patch.object(module, "async_get_clientsession", return_value=session):
        api = await coord._s1_client()
    assert api.session is session
    assert api.product_key == "189c56a889be"
    assert api.group_id == "family"
    coord._ensure_token.assert_awaited_once()


async def test_s1_does_not_create_s10_error_or_summary_sensors():
    coord = coordinator()
    hass = SimpleNamespace(data={DOMAIN: {"s1": coord}})
    add = MagicMock()
    await sensor.async_setup_entry(hass, coord.entry, add)
    entities = add.call_args.args[0]
    assert len(entities) == 1 and isinstance(entities[0], sensor.SwitchBotBattery)
    assert entities[0].native_value == 85
    other_add = MagicMock()
    await binary_sensor.async_setup_entry(hass, coord.entry, other_add)
    other_add.assert_not_called()


@pytest.mark.parametrize("value", [None, -1, 101, True, "85"])
def test_battery_unknown_values_not_fabricated(value):
    coord = coordinator()
    coord.data["battery"] = value
    assert sensor.SwitchBotBattery(coord).native_value is None


async def test_s1_config_flow_retains_selected_family_and_type():
    flow = config_flow.SwitchBotS10ConfigFlow()
    flow.hass = MagicMock()
    flow.context = {"source": "user"}
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    flow._devices = [
        {
            "device_mac": "AABBCCDDEEFF",
            "device_name": "My S1",
            "device_type": DEVICE_TYPE_S1,
            "product_key": "189c56a889be",
            "group_id": "selected-family",
        }
    ]
    result = await flow.async_step_device({"device_mac": "AABBCCDDEEFF"})
    assert result["data"]["group_id"] == "selected-family"
    assert result["data"]["device_type"] == DEVICE_TYPE_S1


async def test_discovery_includes_s1_and_s1_plus_but_not_unrelated_devices():
    coord = actual_coordinator()
    response = MagicMock()
    response.json = AsyncMock(
        return_value={
            "body": {
                "Items": [
                    {"device_mac": str(i), "device_detail": {"device_type": kind}}
                    for i, kind in enumerate([DEVICE_TYPE_S1, DEVICE_TYPE_S1_PLUS, "WoHand"])
                ]
            }
        }
    )
    context = AsyncMock()
    context.__aenter__.return_value = response
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.post = MagicMock(return_value=context)
    with patch.object(module.aiohttp, "ClientSession", return_value=session):
        devices = await coord.async_discover_devices()
    assert [d["device_type"] for d in devices] == [DEVICE_TYPE_S1, DEVICE_TYPE_S1_PLUS]


@pytest.mark.parametrize("value", [True, "2", 1.5, 0, 3])
def test_service_schema_rejects_coercion(value):
    with pytest.raises(vacuum.vol.Invalid):
        vacuum._passes_schema(value)
