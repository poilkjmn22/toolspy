"""cable_engine.storage — unified SQLite I/O for cable-match runs.

Replaces the old 3-file scheme (state.json + cache.db + _matches.csv)
with a single SQLite database. See sqlite.py for the schema and the
CableStore high-level API.
"""

from .sqlite import SCHEMA, CableStore, ensure_schema, open_db

__all__ = ['CableStore', 'open_db', 'ensure_schema', 'SCHEMA']
