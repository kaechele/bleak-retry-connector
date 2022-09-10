from __future__ import annotations

__version__ = "1.12.3"


import asyncio
import contextlib
import inspect
import logging
import platform
from collections.abc import Callable, Generator
from typing import Any

import async_timeout
from bleak import BleakClient, BleakError
from bleak.backends.device import BLEDevice
from bleak.backends.service import BleakGATTServiceCollection
from bleak.exc import BleakDBusError

IS_LINUX = CAN_CACHE_SERVICES = platform.system() == "Linux"

if IS_LINUX:
    from .dbus import disconnect_device

if CAN_CACHE_SERVICES:
    with contextlib.suppress(ImportError):  # pragma: no cover
        from bleak.backends.bluezdbus import defs  # pragma: no cover
        from bleak.backends.bluezdbus.manager import (  # pragma: no cover
            get_global_bluez_manager,
        )

UNREACHABLE_RSSI = -1000

BLEAK_HAS_SERVICE_CACHE_SUPPORT = (
    "dangerous_use_bleak_cache" in inspect.signature(BleakClient.connect).parameters
)


# Make sure bleak and dbus-next have time
# to run their cleanup callbacks or the
# retry call will just fail in the same way.
BLEAK_DBUS_BACKOFF_TIME = 0.25


RSSI_SWITCH_THRESHOLD = 6

__all__ = [
    "establish_connection",
    "close_stale_connections",
    "get_device",
    "BleakClientWithServiceCache",
    "BleakAbortedError",
    "BleakNotFoundError",
    "BleakDisconnectedError",
]

BLEAK_EXCEPTIONS = (AttributeError, BleakError)

_LOGGER = logging.getLogger(__name__)

MAX_TRANSIENT_ERRORS = 9

# Shorter time outs and more attempts
# seems to be better for dbus, and corebluetooth
# is happy either way. Ideally we want everything
# to finish in < 60s or declare we cannot connect

MAX_CONNECT_ATTEMPTS = 4
BLEAK_TIMEOUT = 14.25

# Bleak may not always timeout
# since the dbus connection can stall
# so we have an additional timeout to
# be sure we do not block forever
BLEAK_SAFETY_TIMEOUT = 15.75

# These errors are transient with dbus, and we should retry
TRANSIENT_ERRORS = {"le-connection-abort-by-local", "br-connection-canceled"}

DEVICE_MISSING_ERRORS = {"org.freedesktop.DBus.Error.UnknownObject"}

# Currently the same as transient error
ABORT_ERRORS = TRANSIENT_ERRORS

ABORT_ADVICE = (
    "Interference/range; "
    "External Bluetooth adapter w/extension may help; "
    "Extension cables reduce USB 3 port interference"
)

DEVICE_MISSING_ADVICE = (
    "The device disappeared; " "Try restarting the scanner or moving the device closer"
)


class BleakNotFoundError(BleakError):
    """The device was not found."""


class BleakConnectionError(BleakError):
    """The device was not found."""


class BleakAbortedError(BleakError):
    """The connection was aborted."""


class BleakClientWithServiceCache(BleakClient):
    """A BleakClient that implements service caching."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the BleakClientWithServiceCache."""
        super().__init__(*args, **kwargs)
        self._cached_services: BleakGATTServiceCollection | None = None

    @property
    def _has_service_cache(self) -> bool:
        """Check if we can cache services and there is a cache."""
        return (
            not BLEAK_HAS_SERVICE_CACHE_SUPPORT
            and CAN_CACHE_SERVICES
            and self._cached_services is not None
        )

    async def connect(
        self, *args: Any, dangerous_use_bleak_cache: bool = False, **kwargs: Any
    ) -> bool:
        """Connect to the specified GATT server.

        Returns:
            Boolean representing connection status.

        """
        if self._has_service_cache and await self._services_vanished():
            _LOGGER.debug("Clear cached services since they have vanished")
            self._cached_services = None

        connected = await super().connect(
            *args, dangerous_use_bleak_cache=dangerous_use_bleak_cache, **kwargs
        )

        if (
            connected
            and not dangerous_use_bleak_cache
            and not BLEAK_HAS_SERVICE_CACHE_SUPPORT
        ):
            self.set_cached_services(self.services)

        return connected

    async def get_services(
        self, *args: Any, dangerous_use_bleak_cache: bool = False, **kwargs: Any
    ) -> BleakGATTServiceCollection:
        """Get the services."""
        if self._has_service_cache:
            _LOGGER.debug("Cached services found: %s", self._cached_services)
            self.services = self._cached_services
            self._services_resolved = True
            return self._cached_services

        try:
            return await super().get_services(
                *args, dangerous_use_bleak_cache=dangerous_use_bleak_cache, **kwargs
            )
        except Exception:  # pylint: disable=broad-except
            # If getting services fails, we must disconnect
            # to avoid a connection leak
            _LOGGER.debug("Disconnecting from device since get_services failed")
            await self.disconnect()
            raise

    async def _services_vanished(self) -> bool:
        """Check if the services have vanished."""
        with contextlib.suppress(Exception):
            device_path = self._device_path
            manager = await get_global_bluez_manager()
            for service_path, service_ifaces in manager._properties.items():
                if (
                    service_path.startswith(device_path)
                    and defs.GATT_SERVICE_INTERFACE in service_ifaces
                ):
                    return False
        return True

    def set_cached_services(self, services: BleakGATTServiceCollection | None) -> None:
        """Set the cached services."""
        self._cached_services = services


def ble_device_has_changed(original: BLEDevice, new: BLEDevice) -> bool:
    """Check if the device has changed."""
    if original.address != new.address:
        return True
    if (
        isinstance(original.details, dict)
        and "path" in original.details
        and "path" in new.details
        and original.details["path"] != new.details["path"]
    ):
        return True
    return False


def ble_device_description(device: BLEDevice) -> str:
    """Get the device description."""
    if isinstance(device.details, dict) and "path" in device.details:
        return device.details["path"]
    return device.address


def _get_possible_paths(path: str) -> Generator[str, None, None]:
    """Get the possible paths."""
    # The path is deterministic so we splice up the string
    # /org/bluez/hci2/dev_FA_23_9D_AA_45_46
    for i in range(0, 9):
        yield f"{path[0:14]}{i}{path[15:]}"


async def freshen_ble_device(device: BLEDevice) -> BLEDevice | None:
    """Freshen the device.

    If the device is from BlueZ it may be stale
    because bleak does not send callbacks if only
    the RSSI changes so we may need to find the
    path to the device ourselves.
    """
    if not isinstance(device.details, dict) or "path" not in device.details:
        return None
    return await get_bluez_device(device.details["path"], device.rssi)


def address_to_bluez_path(address: str) -> str:
    """Convert an address to a BlueZ path."""
    return f"/org/bluez/hciX/dev_{address.upper().replace(':', '_')}"


async def get_device(address: str) -> BLEDevice | None:
    """Get the device."""
    if not IS_LINUX:
        return None
    return await get_bluez_device(
        address_to_bluez_path(address), _log_disappearance=False
    )


async def get_bluez_device(
    path: str, rssi: int | None = None, _log_disappearance: bool = True
) -> BLEDevice | None:
    """Get a BLEDevice object for a BlueZ DBus path."""
    best_path = device_path = path
    rssi_to_beat = device_rssi = rssi or UNREACHABLE_RSSI

    try:
        manager = await get_global_bluez_manager()
        properties = manager._properties
        if (
            device_path not in properties
            or defs.DEVICE_INTERFACE not in properties[device_path]
        ):
            # device has disappeared so take
            # anything over the current path
            if _log_disappearance:
                _LOGGER.debug("Device %s has disappeared", device_path)
            rssi_to_beat = device_rssi = UNREACHABLE_RSSI

        for path in _get_possible_paths(device_path):
            if (
                path == device_path
                or path not in properties
                or defs.DEVICE_INTERFACE not in properties[path]
            ):
                continue
            rssi = properties[path][defs.DEVICE_INTERFACE].get("RSSI")
            if rssi_to_beat != UNREACHABLE_RSSI and (
                not rssi
                or rssi - RSSI_SWITCH_THRESHOLD < device_rssi
                or rssi < rssi_to_beat
            ):
                continue
            best_path = path
            rssi_to_beat = rssi or UNREACHABLE_RSSI
            _LOGGER.debug("Found device %s with better RSSI %s", path, rssi)

        if best_path == device_path:
            return None

        props = properties[best_path][defs.DEVICE_INTERFACE]
        return BLEDevice(
            props["Address"],
            props["Alias"],
            {"path": best_path, "props": props},
            rssi_to_beat,
            uuids=props.get("UUIDs", []),
            manufacturer_data={
                k: bytes(v) for k, v in props.get("ManufacturerData", {}).items()
            },
        )
    except Exception:  # pylint: disable=broad-except
        _LOGGER.debug("Freshen failed for %s", path, exc_info=True)

    return None


async def device_is_connected(device: BLEDevice) -> bool:
    """Check if the device is connected."""
    if not isinstance(device.details, dict) or "path" not in device.details:
        return False
    try:
        manager = await get_global_bluez_manager()
        properties = manager._properties
        path = device.details["path"]
        return bool(properties[path][defs.DEVICE_INTERFACE].get("Connected"))
    except Exception:  # pylint: disable=broad-except
        return False


async def close_stale_connections(device: BLEDevice) -> None:
    """Close stale connections."""
    if IS_LINUX and await device_is_connected(device):
        description = ble_device_description(device)
        _LOGGER.debug("%s - %s: Unexpectedly connected", device.name, description)
        await disconnect_device(device)


async def establish_connection(
    client_class: type[BleakClient],
    device: BLEDevice,
    name: str,
    disconnected_callback: Callable[[BleakClient], None] | None = None,
    max_attempts: int = MAX_CONNECT_ATTEMPTS,
    cached_services: BleakGATTServiceCollection | None = None,
    ble_device_callback: Callable[[], BLEDevice] | None = None,
    **kwargs: Any,
) -> BleakClient:
    """Establish a connection to the device."""
    timeouts = 0
    connect_errors = 0
    transient_errors = 0
    attempt = 0
    can_use_cached_services = True

    def _raise_if_needed(name: str, description: str, exc: Exception) -> None:
        """Raise if we reach the max attempts."""
        if (
            timeouts + connect_errors < max_attempts
            and transient_errors < MAX_TRANSIENT_ERRORS
        ):
            return
        msg = f"{name} - {description}: Failed to connect: {exc}"
        # Sure would be nice if bleak gave us typed exceptions
        if isinstance(exc, asyncio.TimeoutError) or "not found" in str(exc):
            raise BleakNotFoundError(msg) from exc
        if isinstance(exc, BleakError) and any(
            error in str(exc) for error in ABORT_ERRORS
        ):
            raise BleakAbortedError(f"{msg}: {ABORT_ADVICE}") from exc
        if isinstance(exc, BleakError) and any(
            error in str(exc) for error in DEVICE_MISSING_ERRORS
        ):
            raise BleakNotFoundError(f"{msg}: {DEVICE_MISSING_ADVICE}") from exc
        raise BleakConnectionError(msg) from exc

    create_client = True

    while True:
        attempt += 1
        original_device = device

        # Its possible the BLEDevice can change between
        # between connection attempts so we do not want
        # to keep trying to connect to the old one if it has changed.
        if ble_device_callback is not None:
            device = ble_device_callback()

        if fresh_device := await freshen_ble_device(device):
            device = fresh_device
            can_use_cached_services = False

        if not create_client:
            create_client = ble_device_has_changed(original_device, device)

        description = ble_device_description(device)

        _LOGGER.debug(
            "%s - %s: Connecting (attempt: %s, last rssi: %s)",
            name,
            description,
            attempt,
            device.rssi,
        )

        if create_client:
            client = client_class(device, **kwargs)
            if disconnected_callback:
                client.set_disconnected_callback(disconnected_callback)
            if (
                can_use_cached_services
                and cached_services
                and isinstance(client, BleakClientWithServiceCache)
            ):
                client.set_cached_services(cached_services)
            create_client = False

        if IS_LINUX and await device_is_connected(device):
            _LOGGER.debug("%s - %s: Unexpectedly connected", name, description)
            await disconnect_device(device)

        try:
            async with async_timeout.timeout(BLEAK_SAFETY_TIMEOUT):
                await client.connect(
                    timeout=BLEAK_TIMEOUT,
                    dangerous_use_bleak_cache=bool(cached_services),
                )
        except asyncio.TimeoutError as exc:
            timeouts += 1
            _LOGGER.debug(
                "%s - %s: Timed out trying to connect (attempt: %s, last rssi: %s)",
                name,
                description,
                attempt,
                device.rssi,
            )
            _raise_if_needed(name, description, exc)
        except BrokenPipeError as exc:
            # BrokenPipeError is raised by dbus-next when the device disconnects
            #
            # bleak.exc.BleakDBusError: [org.bluez.Error] le-connection-abort-by-local
            # During handling of the above exception, another exception occurred:
            # Traceback (most recent call last):
            # File "bleak/backends/bluezdbus/client.py", line 177, in connect
            #   reply = await self._bus.call(
            # File "dbus_next/aio/message_bus.py", line 63, in write_callback
            #   self.offset += self.sock.send(self.buf[self.offset:])
            # BrokenPipeError: [Errno 32] Broken pipe
            transient_errors += 1
            _LOGGER.debug(
                "%s - %s: Failed to connect: %s (attempt: %s, last rssi: %s)",
                name,
                description,
                str(exc),
                attempt,
                device.rssi,
            )
            _raise_if_needed(name, description, exc)
        except EOFError as exc:
            transient_errors += 1
            _LOGGER.debug(
                "%s - %s: Failed to connect: %s, backing off: %s (attempt: %s, last rssi: %s)",
                name,
                description,
                str(exc),
                BLEAK_DBUS_BACKOFF_TIME,
                attempt,
                device.rssi,
            )
            await asyncio.sleep(BLEAK_DBUS_BACKOFF_TIME)
            _raise_if_needed(name, description, exc)
        except BLEAK_EXCEPTIONS as exc:
            bleak_error = str(exc)
            if any(error in bleak_error for error in TRANSIENT_ERRORS):
                transient_errors += 1
            else:
                connect_errors += 1
            if isinstance(exc, BleakDBusError):
                _LOGGER.debug(
                    "%s - %s: Failed to connect: %s, backing off: %s (attempt: %s, last rssi: %s)",
                    name,
                    description,
                    bleak_error,
                    BLEAK_DBUS_BACKOFF_TIME,
                    attempt,
                    device.rssi,
                )
                await asyncio.sleep(BLEAK_DBUS_BACKOFF_TIME)
            else:
                _LOGGER.debug(
                    "%s - %s: Failed to connect: %s (attempt: %s, last rssi: %s)",
                    name,
                    description,
                    bleak_error,
                    attempt,
                    device.rssi,
                )
            _raise_if_needed(name, description, exc)
        else:
            _LOGGER.debug(
                "%s - %s: Connected (attempt: %s, last rssi: %s)",
                name,
                description,
                attempt,
                device.rssi,
            )
            return client
        # Ensure the disconnect callback
        # has a chance to run before we try to reconnect
        await asyncio.sleep(0)

    raise RuntimeError("This should never happen")
