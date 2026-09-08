CREATE TABLE IF NOT EXISTS audit_schema_migrations (
    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbound_calls (
    call_id TEXT PRIMARY KEY,
    asterisk_unique_id TEXT NOT NULL UNIQUE,
    asterisk_linked_id TEXT,
    twilio_call_sid TEXT,
    sip_call_id TEXT,
    caller_number TEXT,
    caller_name TEXT,
    called_number TEXT,
    started_at TEXT,
    answered_at TEXT,
    ended_at TEXT,
    duration_seconds REAL,
    answered_duration_seconds REAL,
    operator_zero_started INTEGER NOT NULL DEFAULT 0,
    caller_spoke INTEGER,
    transcript_available INTEGER NOT NULL DEFAULT 0,
    recording_available INTEGER NOT NULL DEFAULT 0,
    disconnect_initiator TEXT NOT NULL DEFAULT 'unknown_disconnect',
    hangup_cause INTEGER,
    hangup_cause_text TEXT,
    outcome TEXT NOT NULL DEFAULT 'in_progress',
    recording_path TEXT,
    audit_status TEXT NOT NULL DEFAULT 'partial',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS inbound_calls_started ON inbound_calls(started_at DESC);
CREATE INDEX IF NOT EXISTS inbound_calls_linked ON inbound_calls(asterisk_linked_id);
CREATE TABLE IF NOT EXISTS call_events (
    id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL REFERENCES inbound_calls(call_id),
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS call_events_timeline ON call_events(call_id,timestamp,id);
CREATE TABLE IF NOT EXISTS call_channel_links (
    call_id TEXT NOT NULL REFERENCES inbound_calls(call_id),
    channel_id TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(call_id,channel_id)
);
CREATE TABLE IF NOT EXISTS call_transcript_segments (
    id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL REFERENCES inbound_calls(call_id),
    speaker TEXT NOT NULL CHECK(speaker IN ('caller','operator_zero','system','unknown')),
    start_offset_ms INTEGER,
    end_offset_ms INTEGER,
    text TEXT NOT NULL,
    confidence REAL,
    provider TEXT,
    provider_item_id TEXT,
    is_final INTEGER NOT NULL DEFAULT 0,
    timing_source TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS call_recordings (
    id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL REFERENCES inbound_calls(call_id),
    track TEXT NOT NULL,
    path TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_jobs (
    id TEXT PRIMARY KEY,
    call_id TEXT REFERENCES inbound_calls(call_id),
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS ingestion_checkpoints (
    source_id TEXT PRIMARY KEY, offset INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_retention_tombstones (
    call_id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL
);
