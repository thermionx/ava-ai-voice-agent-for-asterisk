"""Small, versioned observation envelopes shared by the engine and worker."""

from datetime import datetime, timezone
import hashlib
import json
import re

VERSION = 1
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
EVENT_TYPES = frozenset({
    "inbound_call_received", "channel_metadata", "asterisk_answered",
    "operator_zero_session_started", "operator_zero_greeting_started",
    "operator_zero_greeting_finished", "caller_speech_started",
    "speech_analysis_completed", "hangup_requested", "hangup_accepted",
    "hangup_failed", "channel_ended", "transfer_completed", "ai_session_ended",
    "call_completed", "call_rejected", "call_failed", "channel_linked",
    "recording_failed", "transcription_failed", "audit_gap",
    "recording_started", "recording_stopped", "recording_available",
    "transcript_segment", "transcription_completed", "caller_speech_finished",
})


def timestamp(value=None):
    if value is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(value, (float, int)):
        dt = datetime.fromtimestamp(value, timezone.utc)
    elif isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Audit timestamps must include a timezone")
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def observation(call_id, event_type, *, at=None, source="engine", details=None):
    if not isinstance(call_id, str) or not _ID.fullmatch(call_id):
        raise ValueError("Invalid call ID")
    if event_type not in EVENT_TYPES:
        raise ValueError("Unsupported audit event")
    event = {"version": VERSION, "call_id": call_id, "event_type": event_type,
             "timestamp": timestamp(at), "source": source, "details": details or {}}
    if not isinstance(event["details"], dict):
        raise ValueError("Audit details must be an object")
    cause = event["details"].get("hangup_cause")
    if cause is not None and (not isinstance(cause, int) or isinstance(cause, bool)):
        raise ValueError("Hangup cause must be an integer")
    requested_at = event["details"].get("requested_at")
    if requested_at is not None and timestamp(requested_at) != requested_at:
        raise ValueError("Request timestamp must be normalized UTC")
    raw = json.dumps(event, sort_keys=True, ensure_ascii=True, allow_nan=False)
    if len(raw.encode()) > 32768:
        raise ValueError("Audit event too large")
    event["event_id"] = hashlib.sha256(raw.encode()).hexdigest()
    return event


def validate(event):
    if event.get("version") != VERSION:
        raise ValueError("Unsupported audit event version")
    expected = observation(event["call_id"], event["event_type"], at=event["timestamp"],
                           source=event["source"], details=event["details"])
    if expected != event:
        raise ValueError("Invalid audit event envelope")
    return expected
