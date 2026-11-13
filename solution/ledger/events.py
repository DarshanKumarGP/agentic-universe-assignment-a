"""Event ingestion into the append-only ledger: parse, dedupe, store (linking happens at report time)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .crypto import Vault
from .db import register_file, transaction
from .normalize import normalize_phone, parse_timestamp

CHUNK = 5000
DISPOSITION_EVENTS = ("disposition.set", "disposition.override")


@dataclass(slots=True)
class EventRecord:
    event_id: str
    event_type: str
    workflow_id: Optional[str]
    call_id: Optional[str]
    occurred_at_raw: Optional[str]
    occurred_ts: Optional[float]
    phone_hash: Optional[str]
    disposition_code: Optional[str]
    payload_json: str


@dataclass
class EventFileStats:
    file_name: str
    events_total: int = 0
    malformed: int = 0
    duplicates: int = 0
    stored: int = 0


def _opt_str(value: object) -> Optional[str]:
    return None if value is None else str(value)


def parse_event_line(line: str, vault: Vault) -> Optional[EventRecord]:
    """None => malformed (not valid JSON, not an object, or no event_id)."""
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    event_id = obj.get("event_id")
    if event_id is None or str(event_id).strip() == "":
        return None

    event_type = str(obj.get("event_type") or "")
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    phone_hash = None
    raw_phone = obj.get("phone")
    if raw_phone not in (None, ""):
        canonical = normalize_phone(str(raw_phone))
        if canonical:  # the raw phone is never stored, only its keyed hash
            phone_hash = vault.phone_hash(canonical)

    code = payload.get("disposition_code") if event_type in DISPOSITION_EVENTS else None
    raw_ts = obj.get("occurred_at")
    return EventRecord(
        event_id=str(event_id), event_type=event_type, workflow_id=_opt_str(obj.get("workflow_id")),
        call_id=_opt_str(obj.get("call_id")), occurred_at_raw=_opt_str(raw_ts),
        occurred_ts=parse_timestamp(raw_ts), phone_hash=phone_hash,
        disposition_code=_opt_str(code),
        payload_json=json.dumps(payload, sort_keys=True, ensure_ascii=False))


_INSERT = ("INSERT OR IGNORE INTO events(event_id, event_type, workflow_id, call_id, occurred_at_raw, "
           "occurred_ts, phone_hash, disposition_code, payload_json, file_id) VALUES (?,?,?,?,?,?,?,?,?,?)")


def ingest_event_file(conn: sqlite3.Connection, vault: Vault, path: Path,
                      drop_name: str) -> Optional[EventFileStats]:
    """Ingest one JSONL file atomically. Returns None if it was already ingested."""
    path = Path(path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()

    with transaction(conn):
        file_id = register_file(conn, "events", "", drop_name, path.name, sha)
        if file_id is None:
            return None
        stats = EventFileStats(path.name)
        buffer: list = []
        attempted = 0

        def flush() -> None:
            nonlocal attempted
            if not buffer:
                return
            before = conn.total_changes
            # PRIMARY KEY + OR IGNORE: the first copy of an event_id wins, later copies are no-ops.
            conn.executemany(_INSERT, buffer)
            stats.stored += conn.total_changes - before
            attempted += len(buffer)
            buffer.clear()

        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                stats.events_total += 1
                rec = parse_event_line(line, vault)
                if rec is None:
                    stats.malformed += 1
                    continue
                buffer.append((rec.event_id, rec.event_type, rec.workflow_id, rec.call_id,
                               rec.occurred_at_raw, rec.occurred_ts, rec.phone_hash,
                               rec.disposition_code, rec.payload_json, file_id))
                if len(buffer) >= CHUNK:
                    flush()
            flush()

        stats.duplicates = attempted - stats.stored
        conn.execute(
            "UPDATE ingested_files SET events_total=?, malformed_events=?, duplicate_events=? WHERE file_id=?",
            (stats.events_total, stats.malformed, stats.duplicates, file_id))
    return stats
