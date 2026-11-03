"""Live audio fan-out: PCM in, a shared MP3 stream out, one encoder per source.

The encoder runs only while someone is listening. Silence is inserted while the
squelch is closed so that browsers do not treat the stream as ended.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Callable

from . import pipelines
from .config import Config

log = logging.getLogger("sentrysdr.live")

TICK = 0.1
SILENCE_SLACK_SECONDS = 0.3
QUEUE_CHUNKS = 64
MAX_BUFFER_BYTES = 1_000_000


def upsample_double(pcm: bytes) -> bytes:
    """Repeat each 16-bit sample: turns 8 kHz decoder audio into 16 kHz."""
    return b"".join(pcm[i:i + 2] * 2 for i in range(0, len(pcm) - 1, 2))


def silence_needed(elapsed: float, written: int, rate: int) -> int:
    """Bytes of silence to keep the stream at real time.

    PCM arrives in bursts (rtl_fm about every 128 ms), so an empty tick doesn't mean
    the squelch closed. Silence is added only once the stream has really fallen
    behind, otherwise it would chop gaps into active speech.
    """
    behind = int(elapsed * rate * 2) - written
    if behind <= int(SILENCE_SLACK_SECONDS * rate * 2):
        return 0
    return behind & ~1  # whole 16-bit samples


class _Stream:
    def __init__(self, cfg: Config, key: str):
        self.cfg, self.key = cfg, key
        self.subscribers: set[asyncio.Queue] = set()
        self.buffer = bytearray()
        self.rate = pipelines.PCM_RATE  # fixed, so retuning between modes can't change pitch
        self.proc: asyncio.subprocess.Process | None = None
        self.tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *pipelines.live_encoder_args(self.cfg, self.rate),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.tasks = [asyncio.create_task(self._pump()), asyncio.create_task(self._fanout())]

    async def _pump(self) -> None:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            started = time.monotonic()
            written = 0
            while True:
                await asyncio.sleep(TICK)
                if self.buffer:
                    data, self.buffer = bytes(self.buffer), bytearray()
                else:
                    data = bytes(silence_needed(time.monotonic() - started, written, self.rate))
                if data:
                    written += len(data)
                    self.proc.stdin.write(data)
                    await self.proc.stdin.drain()

    async def _fanout(self) -> None:
        while chunk := await self.proc.stdout.read(4096):
            for queue in list(self.subscribers):
                if queue.full():  # a slow listener skips audio rather than stalling others
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                queue.put_nowait(chunk)

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        # Nothing to flush, and SIGTERM would hang on a stdout pipe nobody is reading.
        if self.proc and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()
            await self.proc.wait()


class LiveHub:
    def __init__(self, cfg: Config, on_demand: Callable[[str, bool], None] | None = None):
        self.cfg = cfg
        self.on_demand = on_demand  # tells a remote slave to start or stop sending PCM
        self.streams: dict[str, _Stream] = {}
        self._lock = asyncio.Lock()

    def feed(self, key: str, pcm: bytes, rate: int) -> None:
        stream = self.streams.get(key)
        if stream is None:
            return
        if rate != stream.rate:
            pcm = upsample_double(pcm) if rate * 2 == stream.rate else b""
        if len(stream.buffer) < MAX_BUFFER_BYTES:
            stream.buffer.extend(pcm)

    def active_keys(self) -> list[str]:
        return list(self.streams)

    async def subscribe(self, key: str) -> asyncio.Queue:
        async with self._lock:
            stream = self.streams.get(key)
            if stream is None:
                stream = _Stream(self.cfg, key)
                await stream.start()
                self.streams[key] = stream
                if self.on_demand:
                    self.on_demand(key, True)
            queue: asyncio.Queue = asyncio.Queue(QUEUE_CHUNKS)
            stream.subscribers.add(queue)
            return queue

    async def unsubscribe(self, key: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            stream = self.streams.get(key)
            if stream is None:
                return
            stream.subscribers.discard(queue)
            if stream.subscribers:
                return
            del self.streams[key]
            if self.on_demand:
                self.on_demand(key, False)
        await stream.stop()  # outside the lock, so other streams aren't held up

    async def shutdown(self) -> None:
        for stream in list(self.streams.values()):
            await stream.stop()
        self.streams.clear()
