"""Worker-only recorder recovery; all file operations stay off the GUI thread."""
from __future__ import annotations

import json
import os
import tempfile


def _spool_path(db):
    return db.path + '.recorder-spool.json'


def _read_spool(db):
    path = _spool_path(db)
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as source:
        rows = json.load(source)
    if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != 4 for row in rows):
        raise ValueError('The local recorder recovery file is malformed; it has been preserved.')
    return [tuple(row) for row in rows]


def commit_market_events(db, events):
    recovered = _read_spool(db)
    db.insert_market_events([*recovered, *events])
    if recovered:
        os.unlink(_spool_path(db))


def spool_market_events(db, events):
    """Preserve a failed batch on shutdown with atomic replacement + fsync."""
    path = _spool_path(db)
    recovered = _read_spool(db)
    handle, temporary = tempfile.mkstemp(prefix='recorder-', suffix='.tmp', dir=os.path.dirname(path))
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as output:
            json.dump([*recovered, *events], output, separators=(',', ':'))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
