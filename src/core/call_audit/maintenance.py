"""Coordinate retention with the two independent database workers."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path


@contextmanager
def maintenance_lock(store, exclusive=False):
    path = Path(store.path + '.maintenance.lock')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)
