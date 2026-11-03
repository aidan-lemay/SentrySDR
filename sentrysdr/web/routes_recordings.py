"""Browse, play, download and protect recordings.

Recordings are independent of the current source/target/recorder setup: each one
stores copies of those names, so past pairings stay searchable after deletion.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from .. import uploaders
from ..models import Recording, UploadJob, User
from .deps import ctx_of, current_user, get_db, ip_of, require

router = APIRouter(prefix="/api/recordings")

# Each request can cost ClearCut's owner real transcription time.
TRANSCRIBE_LIMIT = 30
TRANSCRIBE_WINDOW = 3600


def _view(recording: Recording, uploads: list[UploadJob] | None = None) -> dict:
    return {
        "id": recording.id, "node_name": recording.node_name, "serial": recording.serial,
        "source_id": recording.source_id, "source_name": recording.source_name,
        "target_id": recording.target_id, "target_name": recording.target_name,
        "recorder_name": recording.recorder_name, "frequency_hz": recording.frequency_hz,
        "modulation": recording.modulation, "started_at": recording.started_at,
        "ended_at": recording.ended_at, "duration": recording.duration,
        "size_bytes": recording.size_bytes, "protected": recording.protected,
        "transcript": recording.transcript,
        "clearcut_system": recording.clearcut_system,
        "uploads": [{"destination": u.destination, "status": u.status} for u in uploads or []],
    }


@router.get("")
def list_recordings(
    search: str | None = Query(default=None, max_length=120),
    source_id: int | None = None, target_id: int | None = None,
    start: float | None = None, end: float | None = None, protected: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
    _user: User = Depends(require("listen_recordings")), db=Depends(get_db),
):
    conditions = []
    if search:
        pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        conditions.append(or_(
            Recording.target_name.like(pattern, escape="\\"), Recording.source_name.like(pattern, escape="\\"),
            Recording.recorder_name.like(pattern, escape="\\"), Recording.transcript.like(pattern, escape="\\"),
        ))
    if source_id is not None:
        conditions.append(Recording.source_id == source_id)
    if target_id is not None:
        conditions.append(Recording.target_id == target_id)
    if start is not None:
        conditions.append(Recording.started_at >= start)
    if end is not None:
        conditions.append(Recording.started_at <= end)
    if protected is not None:
        conditions.append(Recording.protected.is_(protected))
    total = db.scalar(select(func.count()).select_from(Recording).where(*conditions))
    rows = list(db.scalars(
        select(Recording).where(*conditions).order_by(Recording.started_at.desc()).limit(limit).offset(offset)
    ))
    jobs: dict[str, list[UploadJob]] = {}
    if rows:
        for job in db.scalars(select(UploadJob).where(UploadJob.recording_id.in_([r.id for r in rows]))):
            jobs.setdefault(job.recording_id, []).append(job)
    return {"total": total, "items": [_view(r, jobs.get(r.id)) for r in rows]}


def _get(db, recording_id: str) -> Recording:
    recording = db.get(Recording, recording_id)
    if recording is None:
        raise HTTPException(404, "Recording not found")
    return recording


def _file(request: Request, recording: Recording):
    path = ctx_of(request).recording_path(recording)
    if path is None:
        raise HTTPException(404, "Audio file is missing")
    return path


@router.get("/{recording_id}/audio")
def play(recording_id: str, request: Request, _user: User = Depends(require("listen_recordings")), db=Depends(get_db)):
    return FileResponse(_file(request, _get(db, recording_id)), media_type="audio/mpeg",
                        headers={"Cache-Control": "private, max-age=3600"})


@router.get("/{recording_id}/download")
def download(recording_id: str, request: Request, user: User = Depends(require("download")), db=Depends(get_db)):
    recording = _get(db, recording_id)
    path = _file(request, recording)
    ctx_of(request).audit("recording.downloaded", user=user, ip=ip_of(request), target=recording.id)
    return FileResponse(path, media_type="audio/mpeg", filename=f"sentrysdr-{recording.id}.mp3")


class ProtectBody(BaseModel):
    protected: bool


@router.post("/{recording_id}/protect")
def protect(recording_id: str, body: ProtectBody, request: Request, user: User = Depends(require("protect")), db=Depends(get_db)):
    recording = _get(db, recording_id)
    recording.protected = body.protected
    db.commit()
    ctx_of(request).audit("recording.protect", user=user, ip=ip_of(request), target=recording.id, details={"protected": body.protected})
    return _view(recording)


@router.post("/{recording_id}/transcribe")
async def transcribe(recording_id: str, request: Request, user: User = Depends(require("transcribe")), db=Depends(get_db)):
    """Ask ClearCut to transcribe a call that was uploaded to it."""
    ctx = ctx_of(request)
    recording = _get(db, recording_id)
    if recording.transcript:
        return _view(recording)
    uploaded = db.scalar(select(UploadJob).where(
        UploadJob.recording_id == recording.id, UploadJob.destination == "clearcut", UploadJob.status == "done",
    ))
    server = ctx.get_setting("clearcut_server")
    if not uploaded or not server or not recording.clearcut_system:
        raise HTTPException(409, "This call hasn't been uploaded to ClearCut")
    key = f"transcribe:{user.id}"
    if ctx.limiter.blocked(key, TRANSCRIBE_LIMIT, TRANSCRIBE_WINDOW):
        raise HTTPException(429, "Too many transcript requests. Try again later.")
    ctx.limiter.hit(key, TRANSCRIBE_WINDOW)
    try:
        async with httpx.AsyncClient(transport=ctx.http_transport, follow_redirects=False) as client:
            call_id = recording.clearcut_call_id
            if not call_id:
                calls = await uploaders.clearcut_calls(client, server, recording.clearcut_system, recording.clearcut_talkgroup)
                match = uploaders.match_call(calls, recording.started_at)
                if match is None or not match.get("id"):
                    raise HTTPException(409, "ClearCut isn't listing this call yet. Try again shortly.")
                call_id = str(match["id"])[:64]
                recording.clearcut_call_id = call_id
            call = await uploaders.clearcut_transcribe(client, server, call_id)
    except (uploaders.UploadError, httpx.HTTPError) as exc:
        raise HTTPException(502, "ClearCut couldn't transcribe this call right now") from exc
    recording.transcript = uploaders.transcript_text(call)
    db.commit()
    ctx.audit("recording.transcribe_requested", user=user, ip=ip_of(request), target=recording.id)
    return _view(recording)


@router.delete("/{recording_id}")
def delete_recording(recording_id: str, request: Request, user: User = Depends(current_user), db=Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(403, "Administrator access required")
    recording = _get(db, recording_id)
    if recording.protected:
        raise HTTPException(409, "Remove protection before deleting this recording")
    path = ctx_of(request).recording_path(recording)
    if path:
        path.unlink(missing_ok=True)
    db.delete(recording)
    db.commit()
    ctx_of(request).audit("recording.deleted", user=user, ip=ip_of(request), target=recording_id)
    return {"detail": "Deleted"}
