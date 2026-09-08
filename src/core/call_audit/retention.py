"""Dry-run-first call retention. No timer is enabled by installation.

Apply requires the same maintenance lock as ingestion/media, preventing replay
or transcription from racing expiry. Tombstones prevent journal resurrection.
"""
import argparse
from datetime import datetime, timedelta, timezone
import gzip
import json
import os
from pathlib import Path
import re

from .events import timestamp
from .maintenance import maintenance_lock
from .media import recording_path, TRACKS
from .store import AuditStore


def plan(store, root, recording_days=30, metadata_days=365, now=None):
    if recording_days < 0 or metadata_days < 0:
        raise ValueError('Retention days must be nonnegative; zero disables expiration')
    if metadata_days and (not recording_days or metadata_days < recording_days):
        raise ValueError('Metadata retention must be at least recording retention')
    now = now or datetime.now(timezone.utc)
    with store.connect() as db:
        recordings = [r[0] for r in db.execute('SELECT call_id FROM inbound_calls WHERE ended_at < ?',
                      (timestamp(now - timedelta(days=recording_days)),))] if recording_days else []
        metadata = [r[0] for r in db.execute('SELECT call_id FROM inbound_calls WHERE ended_at < ?',
                    (timestamp(now - timedelta(days=metadata_days)),))] if metadata_days else []
    files = []
    for call_id in recordings:
        for track in TRACKS:
            path = recording_path(root, call_id, track)
            if path.exists():
                if not path.is_file():
                    raise ValueError('Refusing non-regular recording')
                files.append(path.name)
    return {'recording_call_ids': recordings, 'metadata_call_ids': metadata, 'recording_files': files}


def expired_journals(store, journal_dir, cel_path, cutoff, today):
    """Only closed, fully parsed archives entirely older than the cutoff.

    Never touch today's engine journals, legacy undated engine files, or the
    active CEL file. Logrotate preserves all CEL archives until this cleanup.
    """
    candidates = []
    for path in Path(journal_dir).glob('engine-*.jsonl*'):
        match = re.search(r'-(\d{8})\.jsonl(?:\.gz)?$', path.name)
        if match and match[1] < today:
            candidates.append((path, 'engine'))
    if cel_path:
        base = Path(cel_path)
        candidates.extend((path, 'cel') for path in base.parent.glob(base.name + '.*'))
    result = []
    with store.connect() as db:
        protected = {row[0] for row in db.execute('SELECT call_id FROM inbound_calls WHERE ended_at IS NULL OR ended_at >= ?', (cutoff,))}
    for path, source in candidates:
        if path.is_symlink() or not path.is_file():
            continue
        old, count = True, 0
        try:
            opener = gzip.open if path.suffix == '.gz' else open
            with opener(path, 'rb') as stream:
                while line := stream.readline(65537):
                    if len(line) > 65536 or not line.endswith(b'\n'):
                        old = False
                        break
                    row = json.loads(line)
                    call_id = row.get('call_id') if source == 'engine' else next((v for k, v in row.items() if k.lower() == 'uniqueid'), None)
                    if call_id in protected:
                        old = False
                        break
                    at = row['timestamp'] if source == 'engine' else next(v for k, v in row.items() if k.lower() == 'eventtime')
                    at = timestamp(at if source == 'engine' else float(at))
                    if at >= cutoff:
                        old = False
                        break
                    count += 1
                consumed = stream.tell()
        except (OSError, ValueError, KeyError, StopIteration, TypeError):
            old = False
        info = path.stat()
        ingested = store.checkpoint(f'{source}:{info.st_dev}:{info.st_ino}') >= consumed if old else False
        if old and count and ingested:
            result.append(path)
    return result


def cleanup(store, root, *, apply=False, recording_days=30, metadata_days=365, now=None,
            journal_dir=None, cel_path=None):
    now = now or datetime.now(timezone.utc)
    with maintenance_lock(store, exclusive=apply):
        result = plan(store, root, recording_days, metadata_days, now)
        archives = expired_journals(store, journal_dir, cel_path, timestamp(now - timedelta(days=metadata_days)),
                                    now.strftime('%Y%m%d')) if journal_dir and metadata_days else []
        result['journal_archives'] = [path.name for path in archives]
        result['applied'] = apply
        if not apply:
            return result
        # Files first: if any unlink fails, keep database metadata for a retry.
        for call_id in result['recording_call_ids']:
            for track in TRACKS:
                recording_path(root, call_id, track).unlink(missing_ok=True)
        with store.connect() as db:
            for call_id in result['recording_call_ids']:
                db.execute("UPDATE call_recordings SET status='expired' WHERE call_id=?", (call_id,))
                db.execute('UPDATE inbound_calls SET recording_available=0,recording_path=NULL WHERE call_id=?', (call_id,))
                for job in [f'recording:{call_id}:{track}' for track in TRACKS] + [f'transcription:{call_id}']:
                    db.execute("INSERT INTO audit_jobs(id,call_id,kind,status) VALUES (?,?,?,'expired') ON CONFLICT(id) DO UPDATE SET status='expired',next_attempt_at=NULL",
                               (job, call_id, 'retention'))
            for call_id in result['metadata_call_ids']:
                db.execute('INSERT OR IGNORE INTO audit_retention_tombstones VALUES (?,?)', (call_id, timestamp(now)))
                for table in ('call_events', 'call_channel_links', 'call_transcript_segments', 'call_recordings', 'audit_jobs'):
                    db.execute(f'DELETE FROM {table} WHERE call_id=?', (call_id,))
                db.execute('DELETE FROM inbound_calls WHERE call_id=?', (call_id,))
        # A malformed archive is kept for investigation. Current files stay open
        # and untouched. Fully expired archives can be removed after DB commit.
        for path in archives:
            path.unlink()
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Actually expire files/rows; default only prints a plan')
    args = parser.parse_args()
    store = AuditStore(os.getenv('CALL_AUDIT_DB_PATH', '/app/data/call_audit.db'))
    if not Path(store.path).is_file():
        parser.exit(1, 'Audit database must be initialized before retention runs\n')
    try:
        result = cleanup(store, os.getenv('CALL_RECORDING_DIR', '/mnt/operator-zero-recordings'), apply=args.apply,
                         recording_days=int(os.getenv('CALL_RECORDING_RETENTION_DAYS', '30')),
                         metadata_days=int(os.getenv('CALL_METADATA_RETENTION_DAYS', '365')),
                         journal_dir=os.getenv('CALL_AUDIT_JOURNAL_DIR', '/app/data/call-audit-journal'),
                         cel_path=os.getenv('CALL_AUDIT_CEL_PATH'))
        print(json.dumps(result, indent=2))
    except BlockingIOError:
        parser.exit(1, 'Audit worker is busy; retry retention later. No deletion started.\n')
    except (OSError, ValueError) as exc:
        parser.exit(1, f'Retention stopped ({type(exc).__name__}); inspect permissions/configuration and retry\n')


if __name__ == '__main__':
    main()
