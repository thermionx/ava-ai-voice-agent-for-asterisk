"""Read-only inbound audit API, protected by the existing admin authentication."""

from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import stat

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from auth import get_current_user

def private_response(response: Response):
    response.headers["Cache-Control"] = "private, no-store"


router = APIRouter(prefix="/call-audit", dependencies=[Depends(get_current_user), Depends(private_response)])
LOG = logging.getLogger(__name__)
CALL_FIELDS = "call_id asterisk_unique_id asterisk_linked_id twilio_call_sid caller_number caller_name called_number started_at answered_at ended_at duration_seconds answered_duration_seconds operator_zero_started caller_spoke transcript_available recording_available disconnect_initiator hangup_cause hangup_cause_text outcome audit_status".split()
EVENT_FIELDS = set("reason evidence provider provider_item_id track error_type attempt http_status destination channel_id role disconnect_initiator hangup_cause hangup_cause_text hangup_source dial_status audio_drained".split())
PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


def db_path():
    return Path(os.getenv("CALL_AUDIT_DB_PATH", "/app/data/call_audit.db"))


@contextmanager
def database():
    db = None
    try:
        db = sqlite3.connect(db_path().resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        yield db
    except (sqlite3.Error, OSError):
        LOG.warning("Inbound audit database unavailable")
        raise HTTPException(503, "Inbound call history is temporarily unavailable")
    finally:
        if db is not None:
            db.close()


def call_summary(row):
    result = {key: row[key] for key in CALL_FIELDS}
    for key in ("operator_zero_started", "caller_spoke", "transcript_available", "recording_available"):
        result[key] = None if result[key] is None else bool(result[key])
    return result


def require_call(db, call_id):
    row = db.execute("SELECT * FROM inbound_calls WHERE call_id=?", (call_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Call not found")
    return row


@router.get("/status")
def status():
    enabled = os.getenv("CALL_AUDIT_ENABLED", "false").lower() in {"true", "1", "yes"}
    return {"enabled": enabled, "available": db_path().is_file()}


@router.get("/calls")
def list_calls(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
               search: str = Query("", max_length=200), outcome: str = Query("", max_length=80)):
    conditions, params = [], []
    if search.strip():
        pattern = "%" + search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        conditions.append("(c.caller_number LIKE ? ESCAPE '\\' OR c.caller_name LIKE ? ESCAPE '\\' OR c.called_number LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM call_transcript_segments s WHERE s.call_id=c.call_id AND s.text LIKE ? ESCAPE '\\'))")
        params.extend([pattern] * 4)
    if outcome:
        conditions.append("c.outcome=?")
        params.append(outcome)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with database() as db:
        total = db.execute("SELECT COUNT(*) FROM inbound_calls c" + where, params).fetchone()[0]
        rows = db.execute("SELECT c.* FROM inbound_calls c" + where + " ORDER BY COALESCE(c.started_at,c.ended_at,c.created_at) DESC,c.call_id LIMIT ? OFFSET ?", params + [page_size, (page - 1) * page_size]).fetchall()
        calls = []
        for row in rows:
            call = call_summary(row)
            preview = db.execute("SELECT speaker,substr(text,1,240) AS text,is_final FROM call_transcript_segments WHERE call_id=? ORDER BY CASE speaker WHEN 'caller' THEN 0 ELSE 1 END,COALESCE(start_offset_ms,0),created_at LIMIT 1", (row["call_id"],)).fetchone()
            call["transcript_preview"] = dict(preview) if preview else None
            calls.append(call)
        return {"calls": calls, "total": total, "page": page, "page_size": page_size,
                "total_pages": max(1, (total + page_size - 1) // page_size)}


@router.get("/calls/{call_id}")
def call_detail(call_id: str):
    with database() as db:
        call = call_summary(require_call(db, call_id))
        call["transcript_segments"] = [dict(row) for row in db.execute("SELECT * FROM call_transcript_segments WHERE call_id=? ORDER BY COALESCE(start_offset_ms,0),created_at,id", (call_id,))]
        call["events"] = []
        for row in db.execute("SELECT timestamp,event_type,source,details_json FROM call_events WHERE call_id=? ORDER BY timestamp,id", (call_id,)):
            try:
                details = json.loads(row["details_json"])
            except (ValueError, TypeError):
                details = {}
            if not isinstance(details, dict):
                details = {}
            call["events"].append({"timestamp": row["timestamp"], "event_type": row["event_type"], "source": row["source"],
                                   "details": {key: value for key, value in details.items() if key in EVENT_FIELDS}})
        call["recordings"] = [dict(row) for row in db.execute("SELECT track,status FROM call_recordings WHERE call_id=? ORDER BY track", (call_id,))]
        return call


def byte_range(value, size):
    if not value:
        return 0, size - 1
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if not match or not any(match.groups()):
        raise HTTPException(416, "Invalid recording range", headers={"Content-Range": f"bytes */{size}"})
    first, last = match.groups()
    start = int(first) if first else max(0, size - int(last))
    end = min(size - 1, int(last)) if first and last else size - 1
    if start >= size or end < start:
        raise HTTPException(416, "Invalid recording range", headers={"Content-Range": f"bytes */{size}"})
    return start, end


@router.get("/calls/{call_id}/recordings/{track}")
def recording(call_id: str, track: str, request: Request):
    # Never accept a filesystem path from a browser or trust one from the DB.
    if track not in {"mixed", "caller", "sent"} or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", call_id):
        raise HTTPException(404, "Recording not found")
    with database() as db:
        require_call(db, call_id)
        row = db.execute("SELECT path FROM call_recordings WHERE call_id=? AND track=? AND status='available'", (call_id, track)).fetchone()
    expected = f"{call_id}-{track}.wav"
    if row is None or row["path"] != expected:
        raise HTTPException(404, "Recording not available")
    try:
        root = Path(os.getenv("CALL_RECORDING_DIR", "/mnt/asterisk_recordings/operator-zero")).resolve(strict=True)
        fd = os.open(root / expected, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise HTTPException(404, "Recording not available")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 44:
            raise HTTPException(404, "Recording not available")
        start, end = byte_range(request.headers.get("range"), info.st_size)
    except Exception:
        os.close(fd)
        raise

    audio_file = os.fdopen(fd, "rb")

    def chunks():
        with audio_file as audio:
            audio.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = audio.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {**PRIVATE_HEADERS, "Accept-Ranges": "bytes", "Content-Length": str(end - start + 1),
               "Content-Disposition": 'inline; filename="call-recording.wav"'}
    partial = bool(request.headers.get("range"))
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{info.st_size}"
    return StreamingResponse(chunks(), media_type="audio/wav", status_code=206 if partial else 200, headers=headers,
                             background=BackgroundTask(audio_file.close))
