"""Bounded, nonblocking observations. All disk I/O runs off the call loop."""

from collections import OrderedDict
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import queue
import threading
import time
import uuid

from .ari import normalize
from .events import observation

LOG = logging.getLogger("call_audit.publisher")


class AuditPublisher:
    def __init__(self, directory, capacity=2048):
        self.directory = Path(directory)
        self.queue = queue.Queue(maxsize=capacity)
        self.known = OrderedDict()
        self.dropped = 0
        self._stop = threading.Event()
        self._thread = None
        self._last_warning = 0.0

    @classmethod
    def from_env(cls):
        if os.getenv("CALL_AUDIT_ENABLED", "false").lower() not in {"true", "1", "yes"}:
            return None
        try:
            publisher = cls(os.getenv("CALL_AUDIT_JOURNAL_DIR", "/app/data/call-audit-journal"))
            publisher.start()
            return publisher
        except Exception as exc:
            LOG.warning("Audit publisher unavailable (%s); call routing continues", type(exc).__name__)
            return None

    def start(self):
        self._thread = threading.Thread(target=self._write_loop, name="call-audit-journal", daemon=True)
        self._thread.start()

    def _warn(self, reason):
        if time.monotonic() - self._last_warning > 60:
            self._last_warning = time.monotonic()
            LOG.warning("Audit publication incomplete (%s); call routing continues", reason)

    def _enqueue(self, event):
        try:
            self.queue.put_nowait(event)
            return True
        except queue.Full:
            self.dropped += 1
            self._warn("queue full")
            return False

    async def observe_ari(self, event):
        try:
            for item in normalize(event, self.known):
                self._enqueue(item)
            # Keep recently ended calls for late observations without leaking
            # an unbounded channel registry on a long-running PBX.
            while len(self.known) > 10000:
                self.known.popitem(last=False)
        except Exception as exc:
            self._warn(type(exc).__name__)

    def emit(self, call_id, event_type, **details):
        if call_id not in self.known:
            return False
        try:
            return self._enqueue(observation(call_id, event_type, details=details))
        except Exception as exc:
            self._warn(type(exc).__name__)
            return False

    def _write_loop(self):
        # One journal per publisher process prevents interleaved writes. Closed
        # files are replayed by the independent worker after restarts.
        prefix = f"engine-{uuid.uuid4().hex}"
        current_day = None
        pending = None
        stream = None
        try:
            while not self._stop.is_set() or not self.queue.empty():
                try:
                    if pending is None:
                        pending = self.queue.get(timeout=0.1)
                    day = datetime.now(timezone.utc).strftime("%Y%m%d")
                    if stream is not None and day != current_day:
                        stream.close()
                        stream = None
                    if stream is None:
                        current_day = day
                        path = self.directory / f"{prefix}-{day}.jsonl"
                        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                        stream = os.fdopen(fd, "a", encoding="utf-8")
                    stream.write(json.dumps(pending, ensure_ascii=True) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                    pending = None
                    self.queue.task_done()
                except queue.Empty:
                    continue
                except Exception as exc:
                    self._warn(type(exc).__name__)
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass
                        stream = None
                    if self._stop.wait(1):
                        break
        finally:
            if stream is not None:
                stream.close()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


def publish(owner, call_id, event_type, **details):
    """Exception boundary at every engine observation point, including mocks."""
    try:
        publisher = getattr(owner, "call_audit", None)
        if publisher is not None:
            publisher.emit(call_id, event_type, **details)
    except Exception:
        LOG.warning("Audit observation failed; call routing continues")
