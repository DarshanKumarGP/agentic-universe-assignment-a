"""Lead ingestion: row validation (pure) + persistence with merge semantics."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .crypto import Vault
from .db import register_file, transaction
from .normalize import normalize_phone, parse_date, parse_decimal, parse_enum
from .schema_def import Field, WorkflowSchema


@dataclass
class RowOutcome:
    reason: Optional[str]            # rejection reason, or None if the row is a valid lead row
    values: Dict[str, Optional[str]]
    warnings: int                    # optional-but-invalid fields (only meaningful if not rejected)


@dataclass
class FileStats:
    file_name: str
    rows_total: int = 0
    rejected: int = 0
    merged: int = 0
    warnings: int = 0
    new_leads: int = 0


def parse_field(f: Field, raw: str) -> Tuple[bool, Optional[str]]:
    """Validate/normalise one non-empty, trimmed value. Returns (is_valid, normalised_value)."""
    if f.type == "string":
        return True, raw
    if f.type == "phone":
        value = normalize_phone(raw)
    elif f.type == "date":
        value = parse_date(raw)
    elif f.type == "decimal":
        value = parse_decimal(raw)
    elif f.type == "enum":
        value = parse_enum(raw, f.values)
    else:
        raise ValueError(f"unsupported field type {f.type!r} for field {f.name!r}")
    return value is not None, value


def process_row(schema: WorkflowSchema, header_map: Dict[str, int], cells: Sequence[str]) -> RowOutcome:
    """Walk the schema in order; the FIRST failure is the row's single rejection reason."""
    values: Dict[str, Optional[str]] = {}
    warnings = 0
    for f in schema.fields:
        idx = header_map.get(f.name)
        raw = cells[idx].strip() if idx is not None and idx < len(cells) else ""
        if raw == "":
            if f.required:
                return RowOutcome(f"missing_required:{f.name}", {}, 0)
            values[f.name] = None
            continue
        ok, value = parse_field(f, raw)
        if ok:
            values[f.name] = value
        elif f.required:
            return RowOutcome("invalid_phone" if f.type == "phone" else f"invalid_{f.name}", {}, 0)
        else:
            values[f.name] = None
            warnings += 1
    return RowOutcome(None, values, warnings)


def is_blank_row(cells: Sequence[str]) -> bool:
    return all(c.strip() == "" for c in cells)


def _upsert_lead(conn: sqlite3.Connection, vault: Vault, schema: WorkflowSchema,
                 values: Dict[str, Optional[str]], batch_id: str, file_id: int) -> bool:
    """Create the lead, or merge into the existing one. Returns True if it was a merge."""
    phone = values[schema.phone_field]
    phone_hash = vault.phone_hash(phone)
    row = conn.execute(
        "SELECT lead_id, fields_json, pii_blob FROM leads WHERE workflow_id=? AND phone_hash=?",
        (schema.workflow_id, phone_hash)).fetchone()

    if row is None:
        merged = dict(values)
    else:  # last write wins, but only for fields where the new value is non-null
        merged = {**json.loads(row[1]), **vault.decrypt(row[2])}
        merged.update({k: v for k, v in values.items() if v is not None})
    for f in schema.fields:  # defaults apply after merging, to fields that are still null
        if merged.get(f.name) is None and f.default is not None:
            merged[f.name] = f.default

    pii = {f.name: merged.get(f.name) for f in schema.fields if f.is_pii}
    plain = {f.name: merged.get(f.name) for f in schema.fields if not f.is_pii}
    ref_field = schema.reference_field
    external_ref = merged.get(ref_field) if ref_field else None
    fields_json = json.dumps(plain, sort_keys=True, ensure_ascii=False)

    if row is None:
        conn.execute(
            "INSERT INTO leads(workflow_id, phone_hash, phone_last4, external_ref, batch_id, "
            "fields_json, pii_blob, first_file_id) VALUES (?,?,?,?,?,?,?,?)",
            (schema.workflow_id, phone_hash, phone[-4:], external_ref, batch_id, fields_json,
             vault.encrypt(pii), file_id))
        return False
    conn.execute("UPDATE leads SET external_ref=?, fields_json=?, pii_blob=? WHERE lead_id=?",
                 (external_ref, fields_json, vault.encrypt(pii), row[0]))
    return True


def ingest_lead_file(conn: sqlite3.Connection, vault: Vault, schema: WorkflowSchema,
                     path: Path, drop_name: str) -> Optional[FileStats]:
    """Ingest one batch file atomically. Returns None if the file was already ingested."""
    data = Path(path).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    batch_id = Path(path).stem
    reader = csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace"), newline=""))

    with transaction(conn):
        file_id = register_file(conn, "leads", schema.workflow_id, drop_name, Path(path).name, sha)
        if file_id is None:
            return None
        stats = FileStats(Path(path).name)
        header_map = schema.map_header(next(reader, []))
        rejections: List[tuple] = []
        for cells in reader:
            if is_blank_row(cells):          # blank rows are skipped and not counted
                continue
            stats.rows_total += 1            # so row_number == count of non-blank data rows
            outcome = process_row(schema, header_map, cells)
            if outcome.reason is not None:
                stats.rejected += 1
                rejections.append((file_id, schema.workflow_id, batch_id, stats.rows_total, outcome.reason))
                continue
            stats.warnings += outcome.warnings
            if _upsert_lead(conn, vault, schema, outcome.values, batch_id, file_id):
                stats.merged += 1
            else:
                stats.new_leads += 1
        conn.executemany(
            "INSERT INTO rejections(file_id, workflow_id, batch_id, row_number, reason) VALUES (?,?,?,?,?)",
            rejections)
        conn.execute(
            "UPDATE ingested_files SET rows_total=?, rows_rejected=?, duplicates_merged=?, "
            "field_warnings=? WHERE file_id=?",
            (stats.rows_total, stats.rejected, stats.merged, stats.warnings, file_id))
    return stats
