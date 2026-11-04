"""Forward finished calls to rdio-scanner, ClearCut and Broadcastify Calls, with retries.

The Broadcastify format mirrors trunk-recorder's broadcastify_uploader plugin, the client
Broadcastify takes conventional calls from today. A conventional channel's talkgroup is
its frequency slot on the node.

ClearCut has no published docs. The upload format comes from the clearcut_uploader
plugin in galenguyer/trunk-recorder (branch ``clearcut``). The read and transcribe
calls come from aidan-lemay/ClearCut_Mobile. ClearCut makes the transcript itself
after an upload and adds it to the call object.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import pipelines

log = logging.getLogger("sentrysdr.uploaders")

UPLOAD_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
TRANSCRIBE_TIMEOUT = httpx.Timeout(180.0, connect=15.0)
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_TRANSCRIPT_CHARS = 20000


class UploadError(Exception):
    pass


class UploadRejected(UploadError):
    """The destination refused this call for good, so retrying can't help."""


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


def broadcastify_metadata(recording, target) -> dict:
    """The same keys trunk-recorder's call JSON has. SDRs can't report the digital-only ones.

    The start is worked back from the end, as trunk-recorder does, so it fits the audio.
    """
    length_ms = round(recording.duration * 1000)
    stop_ms = int(recording.ended_at * 1000)
    start_ms = stop_ms - length_ms
    return {
        "freq": int(recording.frequency_hz), "freq_error": 0, "signal": 0, "noise": 0,
        "source_num": 0, "recorder_num": 0, "tdma_slot": -1, "phase2_tdma": 0,
        "start_time": start_ms // 1000, "stop_time": stop_ms // 1000,
        "start_time_ms": start_ms, "stop_time_ms": stop_ms,
        "emergency": 0, "priority": 0, "mode": 0, "duplex": 0, "encrypted": 0,
        "call_length": round(recording.duration), "call_length_ms": length_ms,
        "talkgroup": int(target.broadcastify_slot), "talkgroup_tag": target.name,
        "talkgroup_description": "", "talkgroup_group_tag": "", "talkgroup_group": "",
        "color_code": -1,
        "audio_type": "analog" if recording.modulation in pipelines.ANALOG else "digital",
        "short_name": f"sentrysdr-{target.broadcastify_system_ref}",
        "freqList": [{
            "freq": int(recording.frequency_hz), "time": start_ms // 1000, "pos": 0.0,
            "len": round(recording.duration, 2), "error_count": 0, "spike_count": 0,
        }],
    }


def _clean_reply(text: str, api_key: str) -> str:
    """Replies get stored and shown: remove the key and any signed upload address."""
    if api_key:
        text = text.replace(api_key, "[key]")
    return re.sub(r"https?://\S+", "[address]", text).strip()[:200]


async def send_broadcastify(cfg, client: httpx.AsyncClient, server: str, system_id: int, api_key: str, recording, target, audio: Path) -> str:
    """Register the call, then upload its audio. Returns a note if the call was skipped."""
    with tempfile.TemporaryDirectory(dir=audio.parent.parent) as tmp:
        m4a = Path(tmp) / f"{recording.id}.m4a"
        proc = await asyncio.create_subprocess_exec(
            *pipelines.broadcastify_audio_args(cfg, str(audio), str(m4a)),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        if await proc.wait() != 0:
            raise UploadError("could not convert audio for Broadcastify")
        response = await client.post(
            server,
            data={"callDuration": f"{recording.duration:.3f}", "systemId": str(system_id), "apiKey": api_key},
            files={"metadata": ("call_meta.json", json.dumps(broadcastify_metadata(recording, target)), "application/json")},
        )
        _check("Broadcastify", response)
        code, _, reply = response.text[:2000].strip().partition(" ")
        if code == "1" and reply.startswith("SKIPPED"):
            return f"Skipped by Broadcastify: {_clean_reply(reply, api_key)}"
        if code == "1" and reply.startswith("REJECTED"):
            raise UploadRejected(f"Rejected by Broadcastify: {_clean_reply(reply, api_key)}")
        if code != "0":
            raise UploadError(f"Broadcastify replied: {_clean_reply(f'{code} {reply}', api_key)}")
        parts = urlsplit(reply.strip())
        if parts.scheme != "https" or not parts.hostname:
            raise UploadError("Broadcastify didn't return a usable upload address")
        upload = await client.put(reply.strip(), content=m4a.read_bytes(), headers={"Content-Type": "audio/aac"})
    if not 200 <= upload.status_code < 300:
        raise UploadError(f"Broadcastify audio upload returned HTTP {upload.status_code}")
    return ""


async def broadcastify_test(client: httpx.AsyncClient, server: str, system_id: int, api_key: str) -> str:
    """Broadcastify's credential check. It sends no call. Returns its reply as text."""
    response = await client.post(server, files={
        "test": (None, "1"), "apiKey": (None, api_key), "systemId": (None, str(system_id)),
    })
    _check("Broadcastify", response)
    return _clean_reply(response.text[:2000], api_key) or "(an empty reply)"


async def _json_request(client: httpx.AsyncClient, method: str, url: str, params: dict, timeout=UPLOAD_TIMEOUT):
    """Fetch JSON from a remote server, without trusting its size or contents."""
    async with client.stream(method, url, params=params, timeout=timeout) as response:
        if response.status_code != 200:
            raise UploadError(f"ClearCut returned HTTP {response.status_code}")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > MAX_RESPONSE_BYTES:
                raise UploadError("ClearCut response was too large")
    try:
        return json.loads(bytes(body))
    except ValueError as exc:
        raise UploadError("ClearCut returned something other than JSON") from exc


async def clearcut_calls(client: httpx.AsyncClient, server: str, system: str, talkgroup: int) -> list[dict]:
    """The recent calls ClearCut lists for one talkgroup."""
    calls = await _json_request(
        client, "GET", server.rstrip("/") + "/api/v1/calls", {"system": system, "talkgroup": str(talkgroup)},
    )
    return [call for call in calls if isinstance(call, dict)] if isinstance(calls, list) else []


async def clearcut_transcribe(client: httpx.AsyncClient, server: str, call_id: str) -> dict:
    """Ask ClearCut to transcribe a call. Returns the updated call."""
    call = await _json_request(
        client, "POST", server.rstrip("/") + "/api/v1/calls/transcribe", {"id": call_id}, TRANSCRIBE_TIMEOUT,
    )
    return call if isinstance(call, dict) else {}


def match_call(calls: list[dict], started_at: float) -> dict | None:
    """Find our upload by start time: we send it as whole seconds, so it matches exactly."""
    expected = int(started_at)
    return next((call for call in calls if call.get("startTime") == expected), None)


def transcript_text(call: dict) -> str | None:
    transcript = call.get("transcript")
    text = transcript.get("text") if isinstance(transcript, dict) else None
    if not isinstance(text, str) or not text.strip():
        return None
    return text.strip()[:MAX_TRANSCRIPT_CHARS]
