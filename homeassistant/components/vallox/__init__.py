"""Support for Vallox ventilation units."""
from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import logging
from typing import Any, NamedTuple
from uuid import UUID

from vallox_websocket_api import PROFILE as VALLOX_PROFILE, Vallox
from vallox_websocket_api.exceptions import ValloxApiException
from vallox_websocket_api.vallox import get_uuid as calculate_uuid
import voluptuous as vol

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType, StateType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DEFAULT_FAN_SPEED_AWAY,
    DEFAULT_FAN_SPEED_BOOST,
    DEFAULT_FAN_SPEED_HOME,
    DEFAULT_NAME,
    DOMAIN,
    METRIC_KEY_PROFILE_FAN_SPEED_AWAY,
    METRIC_KEY_PROFILE_FAN_SPEED_BOOST,
    METRIC_KEY_PROFILE_FAN_SPEED_HOME,
    STATE_SCAN_INTERVAL,
    STR_TO_VALLOX_PROFILE_SETTABLE,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    vol.All(
        cv.deprecated(DOMAIN),
        {
            DOMAIN: vol.Schema(
                {
                    vol.Required(CONF_HOST): vol.All(ipaddress.ip_address, cv.string),
                    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
                }
            )
        },
    ),
    extra=vol.ALLOW_EXTRA,
)

PLATFORMS: list[str] = [
    Platform.SENSOR,
    Platform.FAN,
]

ATTR_PROFILE = "profile"
ATTR_PROFILE_FAN_SPEED = "fan_speed"

SERVICE_SCHEMA_SET_PROFILE = vol.Schema(
    {
        vol.Required(ATTR_PROFILE): vol.All(
            cv.string, vol.In(STR_TO_VALLOX_PROFILE_SETTABLE)
        )
    }
)

SERVICE_SCHEMA_SET_PROFILE_FAN_SPEED = vol.Schema(
    {
        vol.Required(ATTR_PROFILE_FAN_SPEED): vol.All(
            vol.Coerce(int), vol.Clamp(min=0, max=100)
        )
    }
)


class ServiceMethodDetails(NamedTuple):
    """Details for SERVICE_TO_METHOD mapping."""

    method: str
    schema: vol.Schema


SERVICE_SET_PROFILE = "set_profile"
SERVICE_SET_PROFILE_FAN_SPEED_HOME = "set_profile_fan_speed_home"
SERVICE_SET_PROFILE_FAN_SPEED_AWAY = "set_profile_fan_speed_away"
SERVICE_SET_PROFILE_FAN_SPEED_BOOST = "set_profile_fan_speed_boost"

SERVICE_TO_METHOD = {
    SERVICE_SET_PROFILE: ServiceMethodDetails(
        method="async_set_profile",
        schema=SERVICE_SCHEMA_SET_PROFILE,
    ),
    SERVICE_SET_PROFILE_FAN_SPEED_HOME: ServiceMethodDetails(
        method="async_set_profile_fan_speed_home",
        schema=SERVICE_SCHEMA_SET_PROFILE_FAN_SPEED,
    ),
    SERVICE_SET_PROFILE_FAN_SPEED_AWAY: ServiceMethodDetails(
        method="async_set_profile_fan_speed_away",
        schema=SERVICE_SCHEMA_SET_PROFILE_FAN_SPEED,
    ),
    SERVICE_SET_PROFILE_FAN_SPEED_BOOST: ServiceMethodDetails(
        method="async_set_profile_fan_speed_boost",
        schema=SERVICE_SCHEMA_SET_PROFILE_FAN_SPEED,
    ),
}


@dataclass
class ValloxState:
    """Describes the current state of the unit."""

    metric_cache: dict[str, Any] = field(default_factory=dict)
    profile: VALLOX_PROFILE = VALLOX_PROFILE.NONE

    def get_metric(self, metric_key: str) -> StateType:
        """Return cached state value."""

        if (value := self.metric_cache.get(metric_key)) is None:
            return None

        if not isinstance(value, (str, int, float)):
            return None

        return value

    def get_uuid(self) -> UUID | None:
        """Return cached UUID value."""
        uuid = calculate_uuid(self.metric_cache)
        if not isinstance(uuid, UUID):
            raise ValueError
        return uuid


class ValloxDataUpdateCoordinator(DataUpdateCoordinator):
    """The DataUpdateCoordinator for Vallox."""

    data: ValloxState


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration from configuration.yaml (DEPRECATED)."""
    if DOMAIN not in config:
        return True

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data=config[DOMAIN],
        )
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the client and boot the platforms."""
    host = entry.data[CONF_HOST]
    name = entry.data[CONF_NAME]

    client = Vallox(host)

    async def async_update_data() -> ValloxState:
        """Fetch state update."""
        _LOGGER.debug("Updating Vallox state cache")

        try:
            metric_cache = await client.fetch_metrics()
            profile = await client.get_profile()

        except (OSError, ValloxApiException) as err:
            raise UpdateFailed("Error during state cache update") from err

        return ValloxState(metric_cache, profile)

    coordinator = ValloxDataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{name} DataUpdateCoordinator",
        update_interval=STATE_SCAN_INTERVAL,
        update_method=async_update_data,
    )

    await coordinator.async_config_entry_first_refresh()

    service_handler = ValloxServiceHandler(client, coordinator)
    for vallox_service, service_details in SERVICE_TO_METHOD.items():
        hass.services.async_register(
            DOMAIN,
            vallox_service,
            service_handler.async_handle,
            schema=service_details.schema,
        )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
        "name": name,
    }

    hass.config_entries.async_setup_platforms(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

        if hass.data[DOMAIN]:
            return unload_ok

        for service in SERVICE_TO_METHOD:
            hass.services.async_remove(DOMAIN, service)

    return unload_ok


class ValloxServiceHandler:
    """Services implementation."""

    def __init__(
        self, client: Vallox, coordinator: DataUpdateCoordinator[ValloxState]
    ) -> None:
        """Initialize the proxy."""
        self._client = client
        self._coordinator = coordinator

    async def async_set_profile(self, profile: str = "Home") -> bool:
        """Set the ventilation profile."""
        _LOGGER.debug("Setting ventilation profile to: %s", profile)

        _LOGGER.warning(
            "Attention: The service 'vallox.set_profile' is superseded by the "
            "'fan.set_preset_mode' service. It will be removed in the future, please migrate to "
            "'fan.set_preset_mode' to prevent breakage"
        )

        try:
            await self._client.set_profile(STR_TO_VALLOX_PROFILE_SETTABLE[profile])
            return True

        except (OSError, ValloxApiException) as err:
            _LOGGER.error("Error setting ventilation profile: %s", err)
            return False

    async def async_set_profile_fan_speed_home(
        self, fan_speed: int = DEFAULT_FAN_SPEED_HOME
    ) -> bool:
        """Set the fan speed in percent for the Home profile."""
        _LOGGER.debug("Setting Home fan speed to: %d%%", fan_speed)

        try:
            await self._client.set_values(
                {METRIC_KEY_PROFILE_FAN_SPEED_HOME: fan_speed}
            )
            return True

        except (OSError, ValloxApiException) as err:
            _LOGGER.error("Error setting fan speed for Home profile: %s", err)
            return False

    async def async_set_profile_fan_speed_away(
        self, fan_speed: int = DEFAULT_FAN_SPEED_AWAY
    ) -> bool:
        """Set the fan speed in percent for the Away profile."""
        _LOGGER.debug("Setting Away fan speed to: %d%%", fan_speed)

        try:
            await self._client.set_values(
                {METRIC_KEY_PROFILE_FAN_SPEED_AWAY: fan_speed}
            )
            return True

        except (OSError, ValloxApiException) as err:
            _LOGGER.error("Error setting fan speed for Away profile: %s", err)
            return False

    async def async_set_profile_fan_speed_boost(
        self, fan_speed: int = DEFAULT_FAN_SPEED_BOOST
    ) -> bool:
        """Set the fan speed in percent for the Boost profile."""
        _LOGGER.debug("Setting Boost fan speed to: %d%%", fan_speed)

        try:
            await self._client.set_values(
                {METRIC_KEY_PROFILE_FAN_SPEED_BOOST: fan_speed}
            )
            return True

        except (OSError, ValloxApiException) as err:
            _LOGGER.error("Error setting fan speed for Boost profile: %s", err)
            return False

    async def async_handle(self, call: ServiceCall) -> None:
        """Dispatch a service call."""
        service_details = SERVICE_TO_METHOD.get(call.service)
        params = call.data.copy()

        if service_details is None:
            return

        if not hasattr(self, service_details.method):
            _LOGGER.error("Service not implemented: %s", service_details.method)
            return

        result = await getattr(self, service_details.method)(**params)

        # This state change affects other entities like sensors. Force an immediate update that can
        # be observed by all parties involved.
        if result:
            await self._coordinator.async_request_refresh()
