"""Post-call recording validation and recoverable transcription jobs.

Run separately from lifecycle ingestion so API latency cannot delay call rows.
Only complete, stable PCM WAV files below a configured root are processed.
"""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import os
from pathlib import Path
import re
import stat
import time
import wave

from .events import observation, timestamp
from .store import AuditStore
from .maintenance import maintenance_lock
from .transcription import from_env

LOG = logging.getLogger("call_audit.media")
TRACKS = ("mixed", "caller", "sent")
SAFE_ID = re.compile(r"[A-Za-z0-9_.-]{1,160}\Z")


def recording_path(root, call_id, track):
    if not SAFE_ID.fullmatch(call_id) or track not in TRACKS:
        raise ValueError("Invalid recording identifier")
    root = Path(root).resolve(strict=True)
    path = root / f"{call_id}-{track}.wav"
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError("Recording must be a regular file inside the recording directory")
    return path


def inspect_wav(path):
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Recording is not a regular file")
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getcomptype() != "NONE":
            raise ValueError("Expected mono PCM16 recording")
        rate, frames = wav.getframerate(), wav.getnframes()
        if rate not in {8000, 16000, 24000, 32000, 48000} or frames <= 0:
            raise ValueError("Empty or unsupported recording")
        # Read the declared payload to catch an unfinished header/truncated file.
        remaining = frames
        while remaining:
            expected = min(remaining, rate * 10)
            if len(wav.readframes(expected)) != expected * 2:
                raise ValueError("Incomplete WAV payload")
            remaining -= expected
    after = path.stat(follow_symlinks=False)
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("Recording still changing")
    return rate, frames


def uncovered_intervals(call, duration_ms, origin_ms):
    """Reuse finalized realtime utterances; recover greeting, gaps and final tail.

    Approximate receive timestamps need padding. Never use mere transcript-arrival
    timestamps to claim that audio was covered. Partial text is never coverage.
    """
    covered = []
    for s in call["transcript_segments"]:
        if s["speaker"] == "caller" and s["is_final"] and s["timing_source"] == "observed_speech_bounds":
            start, end = s["start_offset_ms"], s["end_offset_ms"]
            if start is not None and end is not None and end - start > 500:
                covered.append((max(0, start - origin_ms + 250), min(duration_ms, end - origin_ms - 250)))
    cursor = 0
    gaps = []
    for start, end in sorted(covered):
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration_ms:
        gaps.append((cursor, duration_ms))
    return gaps


class MediaWorker:
    def __init__(self, store, root, transcriber=None, transcription_enabled=False, settle_seconds=5):
        self.store, self.root = store, Path(root)
        self.transcriber = transcriber
        self.transcription_enabled = transcription_enabled
        self.settle_seconds = settle_seconds

    def _job_done(self, job_id):
        with self.store.connect() as db:
            row = db.execute("SELECT status,next_attempt_at FROM audit_jobs WHERE id=?", (job_id,)).fetchone()
            return bool(row and (row[0] in {"complete", "expired"} or (row[1] and row[1] > timestamp())))

    @staticmethod
    def _complete(db, job_id, call_id, kind):
        db.execute("""INSERT INTO audit_jobs(id,call_id,kind,status) VALUES (?,?,?,'complete')
            ON CONFLICT(id) DO UPDATE SET status='complete',next_attempt_at=NULL""", (job_id, call_id, kind))

    def _failure(self, call_id, job_id, kind, exc):
        LOG.warning("Call media work deferred (%s)", type(exc).__name__)
        with self.store.connect() as db:
            row = db.execute("SELECT attempts FROM audit_jobs WHERE id=?", (job_id,)).fetchone()
            attempts = (row[0] if row else 0) + 1
            retry = timestamp(datetime.now(timezone.utc) + timedelta(seconds=min(3600, 5 * 2 ** min(attempts, 10))))
            db.execute("""INSERT INTO audit_jobs(id,call_id,kind,status,attempts,next_attempt_at)
                VALUES (?,?,?,'retry',?,?) ON CONFLICT(id) DO UPDATE SET status='retry',
                attempts=excluded.attempts,next_attempt_at=excluded.next_attempt_at""", (job_id, call_id, kind, attempts, retry))
            self.store._ingest(db, observation(call_id, kind + "_failed", source="media",
                                              details={"error_type": type(exc).__name__, "attempt": attempts}))

    def process(self, call):
        if not call["ended_at"]:
            return
        now = time.time()
        if now - datetime.fromisoformat(call["ended_at"]).timestamp() < self.settle_seconds:
            return
        if not any(e["event_type"] == "recording_started" for e in call["events"]):
            return
        recording_jobs = [f"recording:{call['call_id']}:{track}" for track in TRACKS]
        transcription_job = f"transcription:{call['call_id']}"
        if all(self._job_done(job) for job in recording_jobs) and (not self.transcription_enabled or self._job_done(transcription_job)):
            return
        stopped = any(e["event_type"] == "recording_stopped" for e in call["events"])
        stable_age = self.settle_seconds if stopped else max(30, self.settle_seconds)
        available = {}
        for track in TRACKS:
            job = f"recording:{call['call_id']}:{track}"
            try:
                path = recording_path(self.root, call["call_id"], track)
                if now - path.stat().st_mtime < stable_age:
                    continue
                rate, frames = inspect_wav(path)
                available[track] = (path, rate, frames)
                if self._job_done(job):
                    continue
                with self.store.connect() as db:
                    db.execute("INSERT OR REPLACE INTO call_recordings VALUES (?,?,?,?,?,?)",
                               (job, call["call_id"], track, path.name, "available", timestamp()))
                    db.execute("UPDATE inbound_calls SET recording_available=1,recording_path=COALESCE(?,recording_path) WHERE call_id=?",
                               (path.name if track == "mixed" else None, call["call_id"]))
                    self.store._ingest(db, observation(call["call_id"], "recording_available", source="media", details={"track": track}))
                    self._complete(db, job, call["call_id"], "recording")
            except Exception as exc:
                if not self._job_done(job):
                    self._failure(call["call_id"], job, "recording", exc)
        if self.transcription_enabled and "caller" in available:
            job = f"transcription:{call['call_id']}"
            if not self._job_done(job):
                try:
                    self._transcribe(call, *available["caller"], job)
                except Exception as exc:
                    self._failure(call["call_id"], job, "transcription", exc)

    def _transcribe(self, call, path, rate, frames, job):
        start = call["started_at"]
        origin = call["answered_at"] or start
        if not start or not origin:
            raise ValueError("Recording timing anchor unavailable")
        origin_ms = round((datetime.fromisoformat(origin) - datetime.fromisoformat(start)).total_seconds() * 1000)
        intervals = uncovered_intervals(call, round(frames * 1000 / rate), origin_ms)
        found_speech = False
        all_silent = intervals == [(0, round(frames * 1000 / rate))]
        with wave.open(str(path), "rb") as wav:
            for begin, end in intervals:
                # Five-minute chunks keep memory/upload sizes bounded. Include
                # 250ms overlap between chunks so a boundary word can be recovered.
                for chunk_start in range(begin, end, 300000):
                    chunk_end = min(end, chunk_start + 300250)
                    chunk_id = f"{job}:{chunk_start}:{chunk_end}"
                    if self._job_done(chunk_id):
                        all_silent = False  # A replay does not re-analyze completed chunks.
                        continue
                    wav.setpos(min(frames, chunk_start * rate // 1000))
                    pcm = wav.readframes((chunk_end - chunk_start) * rate // 1000)
                    all_silent = all_silent and not any(pcm)
                    # Digital silence can be skipped without dropping quiet speech.
                    utterances = self.transcriber.transcribe(pcm, rate) if any(pcm) else []
                    with self.store.connect() as db:
                        for index, utterance in enumerate(utterances):
                            text = utterance.text.strip()
                            if not text:
                                continue
                            found_speech = True
                            begin_ms = origin_ms + chunk_start + max(0, utterance.start_ms)
                            end_ms = origin_ms + min(chunk_end, chunk_start + max(utterance.start_ms, utterance.end_ms))
                            # Keep original realtime words. Skip only an identical
                            # utterance near the same time, not repetitions elsewhere.
                            norm = lambda value: " ".join(re.findall(r"\w+", value.lower()))
                            rows = db.execute("SELECT text,start_offset_ms,end_offset_ms FROM call_transcript_segments WHERE call_id=? AND speaker='caller'", (call["call_id"],))
                            if any(norm(r[0]) == norm(text) and r[1] is not None and r[2] is not None
                                   and begin_ms <= r[2] + 500 and end_ms >= r[1] - 500 for r in rows):
                                continue
                            self.store._ingest(db, observation(call["call_id"], "transcript_segment", source="media", details={
                                "segment_id": hashlib.sha256(f"{chunk_id}:{index}".encode()).hexdigest(),
                                "speaker": "caller", "text": text, "is_final": True, "revision": 1,
                                "start_offset_ms": begin_ms, "end_offset_ms": end_ms,
                                "confidence": utterance.confidence, "provider": self.transcriber.name,
                                "timing_source": "recording_answer_anchor_approximate",
                            }))
                        self._complete(db, chunk_id, call["call_id"], "transcription_chunk")
        with self.store.connect() as db:
            self.store._ingest(db, observation(call["call_id"], "transcription_completed", source="media",
                                              details={"provider": self.transcriber.name, "reused_realtime_finals": True}))
            # Absence of recognized words is not proof of absence of speech.
            if found_speech:
                self.store._ingest(db, observation(call["call_id"], "speech_analysis_completed", source="media", details={"caller_spoke": True}))
            elif all_silent:
                self.store._ingest(db, observation(call["call_id"], "speech_analysis_completed", source="media", details={"caller_spoke": False, "evidence": "digital_silence_in_complete_recording"}))
            self._complete(db, job, call["call_id"], "transcription")

    def scan(self):
        failures = 0
        with self.store.connect() as db:
            ids = [r[0] for r in db.execute("""SELECT call_id FROM inbound_calls WHERE ended_at IS NOT NULL
                ORDER BY ended_at DESC""")]
        for call_id in ids:
            try:
                self.process(self.store.get(call_id))
            except Exception as exc:
                failures += 1
                LOG.warning("Call media unavailable (%s); telephone services are independent", type(exc).__name__)
        return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.umask(0o077)
    if os.getenv("CALL_AUDIT_ENABLED", "false").lower() not in {"true", "1", "yes"}:
        return 0
    if os.getenv("CALL_RECORDING_ENABLED", "false").lower() not in {"true", "1", "yes"}:
        return 0
    store = AuditStore(os.getenv("CALL_AUDIT_DB_PATH", "/app/data/call_audit.db"))
    enabled = os.getenv("CALL_TRANSCRIPTION_ENABLED", "false").lower() in {"true", "1", "yes"}
    while True:
        try:
            with maintenance_lock(store):
                store.initialize()
                worker = MediaWorker(store, os.getenv("CALL_RECORDING_DIR", "/mnt/asterisk_recordings/operator-zero"),
                                     from_env() if enabled else None, enabled)
                failures = worker.scan()
            if args.once:
                return 1 if failures else 0
        except Exception as exc:
            LOG.warning("Call media worker deferred (%s)", type(exc).__name__)
            if args.once:
                return 1
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
