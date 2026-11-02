"""Enumerate RTL-SDR dongles without claiming them.

Only librtlsdr's string queries are used: they do not hold the device, so SDRs
missing from ``sdr.serials`` stay free for other software.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging

log = logging.getLogger("sentrysdr.sdr")
_lib = None


def _library():
    global _lib
    if _lib is None:
        name = ctypes.util.find_library("rtlsdr") or "librtlsdr.so.0"
        _lib = ctypes.CDLL(name)
        _lib.rtlsdr_get_device_count.restype = ctypes.c_uint32
        _lib.rtlsdr_get_device_usb_strings.argtypes = [
            ctypes.c_uint32, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
        ]
    return _lib


def enumerate_serials() -> list[str]:
    """Return serial numbers in librtlsdr index order ('' if unreadable)."""
    try:
        lib = _library()
    except OSError as exc:
        log.warning("librtlsdr unavailable: %s", exc)
        return []
    serials = []
    for index in range(lib.rtlsdr_get_device_count()):
        manufacturer = ctypes.create_string_buffer(256)
        product = ctypes.create_string_buffer(256)
        serial = ctypes.create_string_buffer(256)
        if lib.rtlsdr_get_device_usb_strings(index, manufacturer, product, serial) == 0:
            serials.append(serial.value.decode(errors="replace"))
        else:
            serials.append("")
    return serials


def index_for_serial(serial: str, devices: list[str] | None = None) -> int | None:
    """Map a serial to a device index, refusing an ambiguous match.

    rtl_fm's ``-d`` reads a numeric string as an index first, so a dongle with
    the factory serial 00000001 could open a different, unconfigured device.
    Passing a resolved index avoids that; duplicates are refused for the same
    reason (fix them with rtl_eeprom -s).
    """
    devices = enumerate_serials() if devices is None else devices
    matches = [i for i, value in enumerate(devices) if value == serial]
    return matches[0] if len(matches) == 1 else None


def has_duplicate(serial: str, devices: list[str]) -> bool:
    return devices.count(serial) > 1
