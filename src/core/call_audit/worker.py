"""Replay durable engine/CEL observations without access to call control."""

import argparse
import gzip
import json
import logging
import os
from pathlib import Path
import time

from .cel import normalize as normalize_cel
from .store import AuditStore
from .maintenance import maintenance_lock

LOG = logging.getLogger("call_audit.worker")
MAX_LINE = 65536


def ingest_file(store, path, source):
    path = Path(path)
    stat = path.stat()
    # Inodes survive rename rotation. Content-based event IDs additionally
    # dedupe copied/compressed rotated logs and checkpoint replay after crashes.
    source_id = f"{source}:{stat.st_dev}:{stat.st_ino}"
    offset = store.checkpoint(source_id)
    compressed = path.suffix == ".gz"
    if not compressed and offset > stat.st_size:
        LOG.warning("Audit source truncated; replaying from beginning")
        offset = 0
    opener = gzip.open if compressed else open
    with opener(path, "rb") as stream:
        stream.seek(offset)
        while True:
            line = stream.readline(MAX_LINE + 1)
            if not line:
                break
            if len(line) > MAX_LINE:
                while line and not line.endswith(b"\n"):
                    line = stream.readline(MAX_LINE + 1)
                if not line:
                    break
                LOG.error("Oversized audit journal record; source retained for inspection")
                store.checkpoint(source_id, stream.tell())
                continue
            if not line.endswith(b"\n"):
                break  # Writer has not completed this record yet.
            next_offset = stream.tell()
            try:
                raw = json.loads(line)
                if source == "cel":
                    uniqueid = next((v for k, v in raw.items() if k.lower() == "uniqueid"), "")
                    events = normalize_cel(raw, known_call=bool(store.get(str(uniqueid))))
                else:
                    events = [raw]
                # Validate before committing anything. DB errors are NOT
                # swallowed here: they leave the checkpoint unchanged.
                from .events import validate
                for event in events:
                    validate(event)
            except (ValueError, KeyError, TypeError):
                LOG.error("Invalid audit journal record; source retained for inspection")
                # Do not skip unknown input silently or block subsequent calls.
                store.checkpoint(source_id, next_offset)
                continue
            if events:
                for index, event in enumerate(events):
                    checkpoint = (source_id, next_offset) if index == len(events) - 1 else None
                    store.ingest(event, checkpoint=checkpoint)
            else:
                store.checkpoint(source_id, next_offset)


def scan(store, journal_dir, cel_path=None):
    failures = 0
    sources = [(path, "engine") for path in sorted(Path(journal_dir).glob("*.jsonl*"))]
    if cel_path:
        base = Path(cel_path)
        sources.extend((path, "cel") for path in sorted(base.parent.glob(base.name + "*")))
    for path, source in sources:
        if not path.is_file():
            continue
        try:
            ingest_file(store, path, source)
        except Exception as exc:
            failures += 1
            LOG.warning("Audit ingestion deferred (%s)", type(exc).__name__)
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--db", default=os.getenv("CALL_AUDIT_DB_PATH", "/app/data/call_audit.db"))
    parser.add_argument("--journal-dir", default=os.getenv("CALL_AUDIT_JOURNAL_DIR", "/app/data/call-audit-journal"))
    parser.add_argument("--cel-path", default=os.getenv("CALL_AUDIT_CEL_PATH"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.umask(0o077)
    store = AuditStore(args.db)
    while True:
        try:
            with maintenance_lock(store):
                store.initialize()
                failures = scan(store, args.journal_dir, args.cel_path)
            if args.once:
                return 1 if failures else 0
        except Exception as exc:
            LOG.error("Audit worker unavailable (%s); telephone services are independent", type(exc).__name__)
            if args.once:
                return 1
        time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
