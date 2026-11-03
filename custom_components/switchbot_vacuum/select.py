"""K10+ vacuum and mop selection."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEVICE_TYPE_K10, DOMAIN, S1_DEVICE_TYPES
from .coordinator import SwitchBotS10Coordinator
from .k10_api import WONDER


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if entry.data.get("device_type") in S1_DEVICE_TYPES:
        async_add_entities([SwitchBotCleaningPasses(coordinator)])
        return
    if entry.data.get("device_type") == DEVICE_TYPE_K10 and (
        coordinator.wonderlab_endpoint or ""
    ).rstrip("/") == WONDER:
        async_add_entities([SwitchBotCleaningMode(coordinator)])


class SwitchBotCleaningMode(CoordinatorEntity[SwitchBotS10Coordinator], SelectEntity):
    """Select a cleaning mode without starting or stopping a task."""

    _attr_has_entity_name = True
    _attr_name = "Cleaning mode"
    _attr_icon = "mdi:robot-vacuum"

    def __init__(self, coordinator: SwitchBotS10Coordinator) -> None:
        super().__init__(coordinator)
        self._attr_options = ["vacuum", "mop"]
        self._attr_unique_id = f"{coordinator.device_mac}_cleaning_mode"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, coordinator.device_mac)})

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.get("k10_controls_available", False)

    @property
    def current_option(self) -> str | None:
        mode = self.coordinator.data.get("clean_mode", {}).get("type")
        return {"sweep": "vacuum", "mop": "mop"}.get(mode)

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            raise HomeAssistantError("Select vacuum or mop")
        await self.coordinator.async_k10_control("mode", "sweep" if option == "vacuum" else "mop")


class SwitchBotCleaningPasses(CoordinatorEntity[SwitchBotS10Coordinator], SelectEntity):
    """S1 device setting; current_option is always cloud-reported, never restored locally."""

    _attr_has_entity_name = True
    _attr_translation_key = "cleaning_passes"
    _attr_icon = "mdi:repeat"

    def __init__(self, coordinator: SwitchBotS10Coordinator) -> None:
        super().__init__(coordinator)
        self._attr_options = ["1", "2"]
        self._attr_unique_id = f"{coordinator.device_mac}_cleaning_passes"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, coordinator.device_mac)})

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.get("online") is True

    @property
    def current_option(self) -> str | None:
        value = self.coordinator.data.get("clean_mode", {}).get("times")
        return str(value) if type(value) is int and value in (1, 2) else None

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Expose the bounded read failure instead of an unexplained unknown count."""
        return {"sdk_property_error": self.coordinator.data.get("s1_properties_error")}

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            raise HomeAssistantError("Select 1 or 2 cleaning passes")
        await self.coordinator.async_s1_control("passes", int(option))
