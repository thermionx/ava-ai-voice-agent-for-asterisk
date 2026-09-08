"""Worker-owned SQLite projection of an append-only audit journal."""

from datetime import datetime
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from .events import timestamp, validate
from .outcomes import classify


class AuditStore:
    def __init__(self, path):
        self.path = str(path)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=0.25)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            has_versions = db.execute("SELECT 1 FROM sqlite_master WHERE name='audit_schema_migrations'").fetchone()
            versions = [r[0] for r in db.execute("SELECT version FROM audit_schema_migrations")] if has_versions else []
            if any(v > 1 for v in versions):
                raise ValueError("Audit database schema is newer than this worker")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(Path(__file__).with_name("schema.sql").read_text())
            db.execute("INSERT OR IGNORE INTO audit_schema_migrations VALUES (1,?)", (timestamp(),))
        Path(self.path).chmod(0o600)

    def ingest(self, event, checkpoint=None):
        validate(event)
        with self.connect() as db:
            self._ingest(db, event)
            if checkpoint:
                self._checkpoint(db, *checkpoint)

    @staticmethod
    def _checkpoint(db, source_id, offset):
        db.execute("INSERT INTO ingestion_checkpoints VALUES (?,?) ON CONFLICT(source_id) DO UPDATE SET offset=excluded.offset",
                   (source_id, offset))

    def checkpoint(self, source_id, offset=None):
        with self.connect() as db:
            if offset is not None:
                self._checkpoint(db, source_id, offset)
                return offset
            row = db.execute("SELECT offset FROM ingestion_checkpoints WHERE source_id=?", (source_id,)).fetchone()
            return row[0] if row else 0

    def _ingest(self, db, event):
        call_id = event["call_id"]
        if db.execute("SELECT 1 FROM audit_retention_tombstones WHERE call_id=?", (call_id,)).fetchone():
            return
        now = timestamp()
        db.execute("""INSERT OR IGNORE INTO inbound_calls
            (call_id,asterisk_unique_id,created_at,updated_at) VALUES (?,?,?,?)""", (call_id, call_id, now, now))
        inserted = db.execute("INSERT OR IGNORE INTO call_events VALUES (?,?,?,?,?,?)",
                              (event["event_id"], call_id, event["timestamp"], event["event_type"],
                               event["source"], json.dumps(event["details"], sort_keys=True))).rowcount
        if not inserted:
            return
        events = self._events(db, call_id)
        call = dict(db.execute("SELECT * FROM inbound_calls WHERE call_id=?", (call_id,)).fetchone())
        # Rebuild lifecycle fields from observations, making late/out-of-order
        # events deterministic. Media enrichment fields belong to later workers.
        call.update(started_at=None, answered_at=None, ended_at=None, caller_spoke=None,
                    operator_zero_started=False, caller_number=None, caller_name=None,
                    called_number=None, asterisk_linked_id=None, twilio_call_sid=None,
                    sip_call_id=None, hangup_cause=None, hangup_cause_text=None)
        metadata = {}
        metadata_priority = {}
        for item in events:
            kind, at, details = item["event_type"], item["timestamp"], item["details"]
            if kind in {"inbound_call_received", "channel_metadata"}:
                priority = 2 if details.get("original_metadata") else 1
                for key in ("caller_number", "caller_name", "called_number", "asterisk_linked_id", "twilio_call_sid", "sip_call_id"):
                    value = details.get(key)
                    if value not in (None, "", "unknown") and priority > metadata_priority.get(key, 0):
                        metadata[key], metadata_priority[key] = str(value), priority
            if kind == "inbound_call_received":
                call["started_at"] = min(call["started_at"] or at, at)
            elif kind == "asterisk_answered":
                call["answered_at"] = min(call["answered_at"] or at, at)
            elif kind == "operator_zero_session_started":
                call["operator_zero_started"] = True
            elif kind == "caller_speech_started":
                call["caller_spoke"] = True
            elif kind == "speech_analysis_completed" and call["caller_spoke"] is not True:
                if isinstance(details.get("caller_spoke"), bool):
                    call["caller_spoke"] = details["caller_spoke"]
            elif kind == "channel_ended":
                call["ended_at"] = min(call["ended_at"] or at, at)
                if details.get("hangup_cause") is not None:
                    call["hangup_cause"] = int(details["hangup_cause"])
                if details.get("hangup_cause_text"):
                    call["hangup_cause_text"] = str(details["hangup_cause_text"])
            elif kind == "channel_linked" and details.get("channel_id"):
                db.execute("INSERT OR IGNORE INTO call_channel_links VALUES (?,?,?,?)",
                           (call_id, details["channel_id"], details.get("role", "unknown"), at))
        call.update(metadata)
        self._project_transcripts(db, call, events)
        call["duration_seconds"] = self._duration(call["started_at"], call["ended_at"])
        call["answered_duration_seconds"] = self._duration(call["answered_at"], call["ended_at"])
        call["outcome"], call["disconnect_initiator"] = classify(call, events)
        call["audit_status"] = "partial" if not call["started_at"] or any(e["event_type"] == "audit_gap" for e in events) else "observed"
        call["updated_at"] = now
        columns = [k for k in call if k not in {"call_id", "created_at"}]
        db.execute("UPDATE inbound_calls SET " + ",".join(f"{k}=?" for k in columns) + " WHERE call_id=?",
                   [call[k] for k in columns] + [call_id])

    @staticmethod
    def _project_transcripts(db, call, events):
        segments = {}
        for event in events:
            if event["event_type"] != "transcript_segment":
                continue
            d = event["details"]
            key = d.get("segment_id")
            if not key or d.get("speaker") not in {"caller", "operator_zero", "system", "unknown"} or not str(d.get("text", "")).strip():
                continue
            priority = (bool(d.get("is_final")), int(d.get("revision", 0)), event["timestamp"])
            if key not in segments or priority > segments[key][0]:
                segments[key] = (priority, event)
        for key, (_, event) in segments.items():
            d = event["details"]
            def offset(name):
                if d.get(name + "_offset_ms") is not None:
                    return max(0, int(d[name + "_offset_ms"]))
                if not call["started_at"] or not d.get(name + "_at"):
                    return None
                return max(0, round((datetime.fromisoformat(d[name + "_at"]) - datetime.fromisoformat(call["started_at"])).total_seconds() * 1000))
            # Scope even provider-chosen IDs to this call.
            import hashlib
            segment_id = hashlib.sha256(f"{call['call_id']}:{key}".encode()).hexdigest()
            db.execute("""INSERT INTO call_transcript_segments
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                start_offset_ms=excluded.start_offset_ms,end_offset_ms=excluded.end_offset_ms,
                text=excluded.text,confidence=excluded.confidence,is_final=excluded.is_final""",
                (segment_id, call["call_id"], d["speaker"], offset("start"), offset("end"), d["text"],
                 d.get("confidence"), d.get("provider"), d.get("provider_item_id"), bool(d.get("is_final")),
                 d.get("timing_source"), event["timestamp"]))
            if d["speaker"] == "caller":
                call["caller_spoke"] = True
        call["transcript_available"] = bool(segments)

    @staticmethod
    def _duration(start, end):
        if not start or not end:
            return None
        return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())

    @staticmethod
    def _events(db, call_id):
        return [{"event_id": r["id"], "event_type": r["event_type"], "timestamp": r["timestamp"],
                 "source": r["source"], "details": json.loads(r["details_json"])}
                for r in db.execute("SELECT * FROM call_events WHERE call_id=? ORDER BY timestamp,id", (call_id,))]

    def get(self, call_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM inbound_calls WHERE call_id=?", (call_id,)).fetchone()
            if row is None:
                return None
            call = dict(row)
            call["events"] = self._events(db, call_id)
            call["transcript_segments"] = [dict(r) for r in db.execute(
                "SELECT * FROM call_transcript_segments WHERE call_id=? ORDER BY COALESCE(start_offset_ms,0),created_at,id", (call_id,))]
            call["recordings"] = [dict(r) for r in db.execute(
                "SELECT * FROM call_recordings WHERE call_id=? ORDER BY track", (call_id,))]
            return call

    def list_calls(self, limit=50, offset=0):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM inbound_calls ORDER BY COALESCE(started_at,ended_at,created_at) DESC,call_id LIMIT ? OFFSET ?",
                (min(200, max(1, int(limit))), max(0, int(offset))))]
