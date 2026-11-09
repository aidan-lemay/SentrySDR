"""Keyword alerts: each user's words, what matched them, and calls or messages marked important by hand."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select

from .. import keywords
from ..models import AcarsMessage, Recording, User, UserKeyword
from .deps import ctx_of, current_user, effective_permissions, get_db, ip_of, require

router = APIRouter(prefix="/api")

ALERT_LOOKBACK_SECONDS = 7 * 86400  # someone who has never looked sees the last week
ALERT_LIST_LIMIT = 30


def user_words(db, user: User, kind: str) -> list[str]:
    """kind is "calls" or "messages"."""
    return list(db.scalars(select(UserKeyword.word).where(
        UserKeyword.user_id == user.id, UserKeyword.applies.in_((kind, "both"))).order_by(UserKeyword.word)))


def important_condition(text_column, flag_column, words: list[str]):
    """Marked important by anyone, or matching one of this user's words."""
    expression = keywords.pattern(words)
    marked = flag_column.is_(True)
    return or_(marked, text_column.regexp_match(expression)) if expression else marked


# --- a user's own keywords ---------------------------------------------------
class KeywordBody(BaseModel):
    word: str = Field(min_length=2, max_length=80)
    applies: str = "both"


@router.get("/me/keywords")
def list_keywords(user: User = Depends(current_user), db=Depends(get_db)):
    rows = db.scalars(select(UserKeyword).where(UserKeyword.user_id == user.id).order_by(UserKeyword.word))
    return [{"id": k.id, "word": k.word, "applies": k.applies} for k in rows]


@router.post("/me/keywords", status_code=201)
def add_keyword(body: KeywordBody, user: User = Depends(current_user), db=Depends(get_db)):
    word = keywords.normalize(body.word)
    if len(word) < 2:
        raise HTTPException(422, "A keyword needs at least two characters")
    if body.applies not in keywords.APPLIES:
        raise HTTPException(422, "A keyword applies to calls, messages or both")
    existing = list(db.scalars(select(UserKeyword).where(UserKeyword.user_id == user.id)))
    if len(existing) >= keywords.MAX_KEYWORDS:
        raise HTTPException(409, f"At most {keywords.MAX_KEYWORDS} keywords each")
    if any(k.word == word for k in existing):
        raise HTTPException(409, "You already have that keyword")
    keyword = UserKeyword(user_id=user.id, word=word, applies=body.applies)
    db.add(keyword)
    db.commit()
    return {"id": keyword.id, "word": keyword.word, "applies": keyword.applies}


@router.delete("/me/keywords/{keyword_id}")
def remove_keyword(keyword_id: int, user: User = Depends(current_user), db=Depends(get_db)):
    keyword = db.get(UserKeyword, keyword_id)
    if keyword is None or keyword.user_id != user.id:  # someone else's is as good as missing
        raise HTTPException(404, "Keyword not found")
    db.delete(keyword)
    db.commit()
    return {"detail": "Removed"}


# --- alerts ------------------------------------------------------------------
def _since(user: User) -> float:
    return user.alerts_seen_at or (time.time() - ALERT_LOOKBACK_SECONDS)


@router.get("/alerts")
def alerts(user: User = Depends(current_user), db=Depends(get_db)):
    """What matched this user's words, or was marked important, since they last looked."""
    since, perms, items, count = _since(user), effective_permissions(user), [], 0
    if "listen_recordings" in perms:
        words = user_words(db, user, "calls")
        arrived = func.coalesce(Recording.transcript_at, Recording.ended_at)
        expression = keywords.pattern(words)
        new = [and_(Recording.important.is_(True), Recording.important_at > since)]
        if expression:
            new.append(and_(Recording.transcript.regexp_match(expression), arrived > since))
        condition = or_(*new)
        count += db.scalar(select(func.count()).select_from(Recording).where(condition))
        for r in db.scalars(select(Recording).where(condition).order_by(Recording.started_at.desc()).limit(ALERT_LIST_LIMIT)):
            items.append({
                "kind": "call", "id": r.id, "at": max(r.transcript_at or 0, r.ended_at, r.important_at or 0),
                "title": r.target_name or "Call", "text": r.transcript or "", "started_at": r.started_at,
                "duration": r.duration, "matches": keywords.matches(words, r.transcript),
                "important": bool(r.important), "important_by": r.important_by,
            })
    if "maps" in perms:
        words = user_words(db, user, "messages")
        expression = keywords.pattern(words)
        new = [and_(AcarsMessage.important.is_(True), AcarsMessage.important_at > since)]
        if expression:
            new.append(and_(AcarsMessage.text.regexp_match(expression), AcarsMessage.received_at > since))
        condition = or_(*new)
        count += db.scalar(select(func.count()).select_from(AcarsMessage).where(condition))
        for m in db.scalars(select(AcarsMessage).where(condition).order_by(AcarsMessage.received_at.desc()).limit(ALERT_LIST_LIMIT)):
            items.append({
                "kind": "message", "id": m.id, "at": max(m.received_at, m.important_at or 0),
                "title": " ".join(filter(None, (m.flight, m.tail))) or "ACARS message", "text": m.text,
                "matches": keywords.matches(words, m.text), "important": bool(m.important), "important_by": m.important_by,
            })
    items.sort(key=lambda i: i["at"], reverse=True)
    return {"count": count, "since": since, "items": items[:ALERT_LIST_LIMIT]}


@router.post("/alerts/seen")
def alerts_seen(user: User = Depends(current_user), db=Depends(get_db)):
    db.merge(user).alerts_seen_at = time.time()
    db.commit()
    return {"detail": "Seen"}


# --- marking important by hand -------------------------------------------------
class ImportantBody(BaseModel):
    important: bool


def _mark(item, body: ImportantBody, user: User) -> None:
    item.important = body.important or None
    item.important_at = time.time() if body.important else None
    item.important_by = user.email if body.important else None


@router.post("/recordings/{recording_id}/important")
def mark_recording(recording_id: str, body: ImportantBody, request: Request,
                   user: User = Depends(require("protect")), db=Depends(get_db)):
    recording = db.get(Recording, recording_id)
    if recording is None:
        raise HTTPException(404, "Recording not found")
    _mark(recording, body, user)
    if body.important:
        recording.protected = True  # an important call shouldn't be deleted by retention
    db.commit()
    ctx_of(request).audit("recording.important", user=user, ip=ip_of(request), target=recording.id,
                          details={"important": body.important})
    return {"important": bool(recording.important), "protected": recording.protected}


@router.post("/messages/{message_id}/important")
def mark_message(message_id: int, body: ImportantBody, request: Request,
                 user: User = Depends(require("protect")), db=Depends(get_db)):
    message = db.get(AcarsMessage, message_id)
    if message is None:
        raise HTTPException(404, "Message not found")
    if "maps" not in effective_permissions(user):
        raise HTTPException(403, "Missing permission: maps")
    _mark(message, body, user)
    db.commit()
    ctx_of(request).audit("message.important", user=user, ip=ip_of(request), target=str(message.id),
                          details={"important": body.important})
    return {"important": bool(message.important)}
