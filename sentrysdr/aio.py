"""asyncio helpers."""
from __future__ import annotations

import asyncio
from typing import Awaitable, TypeVar

T = TypeVar("T")


async def wait_for(awaitable: Awaitable[T], timeout: float) -> T:
    """Like asyncio.wait_for, without Python 3.11's lost cancellation.

    On 3.11, asyncio.wait_for can swallow a cancellation that arrives just as the
    inner operation finishes. A worker reading audio nonstop then ignores being
    stopped, and a retune or shutdown waits forever. asyncio.timeout doesn't.
    Raises TimeoutError, which is asyncio.TimeoutError on 3.11 and later.
    """
    async with asyncio.timeout(timeout):
        return await awaitable
