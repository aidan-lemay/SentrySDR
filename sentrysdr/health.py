"""Machine health: clock sync and disk space. Read cheaply, without privileges."""
from __future__ import annotations

import ctypes
import ctypes.util
import shutil
from pathlib import Path

TIME_ERROR = 5  # adjtimex: the clock isn't synchronised


class _Timex(ctypes.Structure):
    # struct timex from <sys/timex.h>, as the kernel lays it out on 64-bit and 32-bit Linux.
    _fields_ = [("modes", ctypes.c_uint), ("offset", ctypes.c_long), ("freq", ctypes.c_long),
                ("maxerror", ctypes.c_long), ("esterror", ctypes.c_long), ("status", ctypes.c_int),
                ("constant", ctypes.c_long), ("precision", ctypes.c_long), ("tolerance", ctypes.c_long),
                ("time_sec", ctypes.c_long), ("time_usec", ctypes.c_long), ("tick", ctypes.c_long),
                ("ppsfreq", ctypes.c_long), ("jitter", ctypes.c_long), ("shift", ctypes.c_int),
                ("stabil", ctypes.c_long), ("jitcnt", ctypes.c_long), ("calcnt", ctypes.c_long),
                ("errcnt", ctypes.c_long), ("stbcnt", ctypes.c_long), ("tai", ctypes.c_int),
                ("padding", ctypes.c_int * 11)]


def clock_synced() -> bool | None:
    """True if the kernel says the clock is kept in sync (NTP, chrony, timesyncd). None if it can't tell."""
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        timex = _Timex()  # modes 0: only reads, so no privilege is needed
        state = libc.adjtimex(ctypes.byref(timex))
    except (OSError, AttributeError):
        return None
    if state < 0:
        return None
    return state != TIME_ERROR


def disk(path: Path) -> dict | None:
    """Free and total bytes on the filesystem holding path."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return {"free": usage.free, "total": usage.total}
