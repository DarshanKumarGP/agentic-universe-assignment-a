"""SQLite schema, connection setup and file-level idempotency bookkeeping."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from .crypto import Vault

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per input file ever ingested. Guarantees idempotent re-runs and carries the per-file
-- counters that feed data_quality.json (so re-ingesting a file can never double count).
CREATE TABLE IF NOT EXISTS ingested_files(
    file_id           INTEGER PRIMARY KEY,
    kind              TEXT NOT NULL CHECK (kind IN ('leads', 'events')),
    workflow_id       TEXT NOT NULL DEFAULT '',          -- '' for event files
    drop_name         TEXT NOT NULL,
    file_name         TEXT NOT NULL,
    sha256            TEXT NOT NULL,
    ingested_at       TEXT NOT NULL,
    rows_total        INTEGER NOT NULL DEFAULT 0,        -- non-blank lead rows
    rows_rejected     INTEGER NOT NULL DEFAULT 0,
    duplicates_merged INTEGER NOT NULL DEFAULT 0,
    field_warnings    INTEGER NOT NULL DEFAULT 0,
    events_total      INTEGER NOT NULL DEFAULT 0,        -- non-empty event lines
    malformed_events  INTEGER NOT NULL DEFAULT 0,
    duplicate_events  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (kind, workflow_id, file_name)
);

-- Leads. Phone is stored only as a keyed hash (+ last 4 digits for masking); other PII is
-- encrypted in pii_blob. external_ref / batch_id / non-PII fields are plain.
CREATE TABLE IF NOT EXISTS leads(
    lead_id       INTEGER PRIMARY KEY,
    workflow_id   TEXT NOT NULL,
    phone_hash    TEXT NOT NULL,
    phone_last4   TEXT NOT NULL CHECK (length(phone_last4) = 4),
    external_ref  TEXT,
    batch_id      TEXT NOT NULL,                         -- set by the first valid row, never changes
    fields_json   TEXT NOT NULL,                         -- non-PII field values
    pii_blob      BLOB NOT NULL,                         -- encrypted PII field values
    first_file_id INTEGER NOT NULL REFERENCES ingested_files(file_id),
    UNIQUE (workflow_id, phone_hash)
);

CREATE TABLE IF NOT EXISTS rejections(
    rejection_id INTEGER PRIMARY KEY,
    file_id      INTEGER NOT NULL REFERENCES ingested_files(file_id),
    workflow_id  TEXT NOT NULL,
    batch_id     TEXT NOT NULL,
    row_number   INTEGER NOT NULL,
    reason       TEXT NOT NULL
);

-- The interaction ledger: append-only. Linking to a lead is NOT stored; it is resolved when
-- reports are built, so an event that arrived before its lead is linked automatically later.
CREATE TABLE IF NOT EXISTS events(
    event_id         TEXT PRIMARY KEY,
    event_type       TEXT NOT NULL,
    workflow_id      TEXT,
    call_id          TEXT,
    occurred_at_raw  TEXT,
    occurred_ts      REAL,                               -- UTC epoch seconds (naive input = IST)
    phone_hash       TEXT,                               -- NULL when absent/invalid (raw phone never stored)
    disposition_code TEXT,
    payload_json     TEXT NOT NULL,
    file_id          INTEGER NOT NULL REFERENCES ingested_files(file_id)
);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'interaction ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'interaction ledger is append-only'); END;
"""


class FileChangedError(RuntimeError):
    """A file with an already-ingested name now has different content."""


class WrongKeyError(RuntimeError):
    """The database was created with a different PII secret."""


def connect(path: str, vault: Vault) -> sqlite3.Connection:
    # isolation_level=None -> we manage transactions explicitly (see `transaction`).
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key='key_fingerprint'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES('key_fingerprint', ?)", (vault.fingerprint(),))
    elif row[0] != vault.fingerprint():
        raise WrongKeyError(
            f"{path} was created with a different PII secret; set LEDGER_SECRET or restore "
            f"{path}.key. Phones cannot be matched with a different key.")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def register_file(conn: sqlite3.Connection, kind: str, workflow_id: str, drop_name: str,
                  file_name: str, sha256: str) -> Optional[int]:
    """Claim a file for ingestion. Returns its file_id, or None if it was already ingested.

    Must be called inside the same transaction that ingests the file's content, so the claim and
    the data commit (or roll back) together: a crash can never leave a half-ingested file marked done.
    """
    row = conn.execute(
        "SELECT file_id, sha256 FROM ingested_files WHERE kind=? AND workflow_id=? AND file_name=?",
        (kind, workflow_id, file_name)).fetchone()
    if row is not None:
        if row[1] == sha256:
            return None
        raise FileChangedError(
            f"{kind} file '{file_name}' was ingested before with different content; refusing to "
            "ingest it again (the ledger is append-only).")
    cur = conn.execute(
        "INSERT INTO ingested_files(kind, workflow_id, drop_name, file_name, sha256, ingested_at) "
        "VALUES (?,?,?,?,?,?)",
        (kind, workflow_id, drop_name, file_name, sha256, datetime.now(timezone.utc).isoformat()))
    return cur.lastrowid
