"""Forward finished calls to rdio-scanner and ClearCut, with retries.

ClearCut has no published docs. Its format comes from the clearcut_uploader
plugin in galenguyer/trunk-recorder (branch ``clearcut``). That plugin only
checks for HTTP 200, so reading transcripts back is not implemented here.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import pipelines

log = logging.getLogger("sentrysdr.uploaders")

UPLOAD_TIMEOUT = httpx.Timeout(60.0, connect=15.0)


class UploadError(Exception):
    pass


def rdio_form(recording, target, api_key: str) -> dict[str, str]:
    started = datetime.fromtimestamp(recording.started_at, timezone.utc)
    return {
        "key": api_key,
        "dateTime": started.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "system": str(target.rdio_system),
        "talkgroup": str(target.rdio_talkgroup),
        "frequency": str(recording.frequency_hz),
        "source": "0",
    }


def clearcut_form(recording, target, api_key: str) -> dict[str, str]:
    return {
        "freq": str(recording.frequency_hz),
        "error_count": "0",
        "spike_count": "0",
        "start_time": str(int(recording.started_at)),
        "stop_time": str(int(recording.ended_at)),
        "call_length": str(int(round(recording.duration))),
        "talkgroup_num": str(target.clearcut_id),
        "emergency": "0",
        "api_key": api_key,
        "source_list": "[]",
    }


def _check(destination: str, response: httpx.Response) -> None:
    if response.status_code != 200:
        # Response bodies may echo credentials, so only the status is recorded.
        raise UploadError(f"{destination} returned HTTP {response.status_code}")


async def send_rdio(client: httpx.AsyncClient, server: str, api_key: str, recording, target, audio: Path) -> None:
    with audio.open("rb") as handle:
        response = await client.post(
            server.rstrip("/") + "/api/call-upload",
            data=rdio_form(recording, target, api_key),
            files={"audio": (audio.name, handle, "audio/mpeg")},
        )
    _check("rdio-scanner", response)


async def send_clearcut(cfg, client: httpx.AsyncClient, server: str, api_key: str, recording, target, audio: Path) -> None:
    with tempfile.TemporaryDirectory(dir=audio.parent.parent) as tmp:
        m4a = Path(tmp) / f"{recording.id}.m4a"
        proc = await asyncio.create_subprocess_exec(
            *pipelines.m4a_args(cfg, str(audio), str(m4a)),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        if await proc.wait() != 0:
            raise UploadError("could not convert audio for ClearCut")
        with m4a.open("rb") as handle:
            response = await client.post(
                server.rstrip("/") + "/api/v1/upload",
                data=clearcut_form(recording, target, api_key),
                files={"call": (m4a.name, handle, "application/octet-stream")},
                headers={"Expect": ""},
            )
    _check("ClearCut", response)
