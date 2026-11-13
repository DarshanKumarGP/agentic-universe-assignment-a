"""Discover drop folders and ingest them in a deterministic order."""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from .crypto import Vault
from .events import ingest_event_file
from .leads import ingest_lead_file
from .schema_def import WorkflowSchema

log = logging.getLogger("ledger")


def _natural_key(p: Path):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.name)]


def discover_drops(path: Path) -> List[Path]:
    """`path` is either one drop (has leads/ or events/) or a folder of drops (drop_1, drop_2...)."""
    path = Path(path)
    if not path.is_dir():
        raise FileNotFoundError(f"{path} is not a directory")
    if (path / "leads").is_dir() or (path / "events").is_dir():
        return [path]
    drops = sorted((d for d in path.iterdir() if d.is_dir()), key=_natural_key)
    found = [d for d in drops if (d / "leads").is_dir() or (d / "events").is_dir()]
    if not found:
        raise FileNotFoundError(f"no drop folders (containing leads/ or events/) under {path}")
    return found


@dataclass
class IngestSummary:
    files_ingested: int = 0
    files_skipped: int = 0
    new_leads: int = 0
    merged: int = 0
    rejected: int = 0
    events_stored: int = 0
    events_duplicate: int = 0
    events_malformed: int = 0
    notes: List[str] = field(default_factory=list)


def ingest_drop(conn: sqlite3.Connection, vault: Vault, schemas: Dict[str, WorkflowSchema],
                drop: Path, summary: IngestSummary) -> None:
    drop = Path(drop)
    leads_root = drop / "leads"
    if leads_root.is_dir():
        for wf_dir in sorted(d for d in leads_root.iterdir() if d.is_dir()):
            schema = schemas.get(wf_dir.name)
            if schema is None:
                summary.notes.append(f"skipped leads/{wf_dir.name}: no schema for this workflow")
                continue
            for csv_path in sorted(wf_dir.glob("*.csv")):
                stats = ingest_lead_file(conn, vault, schema, csv_path, drop.name)
                if stats is None:
                    summary.files_skipped += 1
                    log.info("skip (already ingested) %s/%s", wf_dir.name, csv_path.name)
                    continue
                summary.files_ingested += 1
                summary.new_leads += stats.new_leads
                summary.merged += stats.merged
                summary.rejected += stats.rejected
                log.info("leads  %-16s %-14s rows=%d new=%d merged=%d rejected=%d warnings=%d",
                         wf_dir.name, csv_path.name, stats.rows_total, stats.new_leads, stats.merged,
                         stats.rejected, stats.warnings)

    events_root = drop / "events"
    if events_root.is_dir():
        for jsonl in sorted(events_root.glob("*.jsonl")):
            stats = ingest_event_file(conn, vault, jsonl, drop.name)
            if stats is None:
                summary.files_skipped += 1
                log.info("skip (already ingested) events/%s", jsonl.name)
                continue
            summary.files_ingested += 1
            summary.events_stored += stats.stored
            summary.events_duplicate += stats.duplicates
            summary.events_malformed += stats.malformed
            log.info("events %-30s lines=%d stored=%d duplicate=%d malformed=%d",
                     jsonl.name, stats.events_total, stats.stored, stats.duplicates, stats.malformed)


def ingest_path(conn: sqlite3.Connection, vault: Vault, schemas: Dict[str, WorkflowSchema],
                path: Path) -> IngestSummary:
    summary = IngestSummary()
    for drop in discover_drops(path):
        ingest_drop(conn, vault, schemas, drop, summary)
    return summary
