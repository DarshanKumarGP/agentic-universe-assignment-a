"""Build the five reports from the ledger.

Everything is derived from scratch on every run, from the append-only tables, so the output always
reflects *everything ingested so far* and can never drift from the ledger (no incremental state
to corrupt). Linking events to leads happens here, which is what makes late-arriving leads work.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set, Tuple

from .config import IST
from .facts import Call, lead_bucket
from .schema_def import Catalogue, WorkflowSchema

BUCKETS = ("yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed")

# Non-override events, with the lead they link to via (workflow_id, phone_hash). LEFT JOIN: an
# event with no phone / an unknown phone simply gets lead_id NULL (= orphan).
_LINKED_EVENTS = """
SELECT e.workflow_id, e.call_id, e.event_type, e.occurred_ts, e.event_id, e.disposition_code, l.lead_id
FROM events e
LEFT JOIN leads l ON l.workflow_id = e.workflow_id AND l.phone_hash = e.phone_hash
WHERE e.event_type != 'disposition.override'
"""
_OVERRIDES = """
SELECT workflow_id, call_id, occurred_ts, event_id, disposition_code
FROM events WHERE event_type = 'disposition.override'
"""


@dataclass
class Report:
    batch_summary: List[list]
    daily_overview: List[list]
    lead_report: List[list]
    rejections: List[list]
    data_quality: dict


def _iso_ist(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST).isoformat(timespec="seconds")


def _ist_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST).date().isoformat()


def assemble_calls(conn: sqlite3.Connection, catalogue: Catalogue) -> Tuple[Dict[tuple, Call], int, int]:
    """Group linked events into calls. Returns (calls, orphan_events, unknown_disposition_events)."""
    calls: Dict[Tuple[str, str], Call] = {}
    orphans = unknown = 0

    for wf, call_id, etype, ts, event_id, code, lead_id in conn.execute(_LINKED_EVENTS):
        # Orphan: cannot be tied to a valid lead (no/invalid/unknown phone) or to a call and a time.
        if lead_id is None or call_id is None or ts is None:
            orphans += 1
            continue
        call = calls.get((wf, call_id))
        if call is None:
            call = calls[(wf, call_id)] = Call(call_id, lead_id)
        if etype == "call.initiated":
            if call.initiated_ts is None or ts < call.initiated_ts:
                call.initiated_ts = ts
                call.lead_id = lead_id
        elif etype == "call.connected":
            call.connected = True
        elif etype == "call.telephony_failed":
            call.failed = True
        elif etype == "disposition.set":
            if code is None or code not in catalogue.get(wf, {}):
                unknown += 1                      # counted and ignored
            else:
                call.add_set(ts, event_id, code)

    # Overrides carry no phone: they link through the call_id of the call's other events.
    for wf, call_id, ts, event_id, code in conn.execute(_OVERRIDES):
        call = calls.get((wf, call_id)) if call_id is not None else None
        if call is None or ts is None:
            orphans += 1
        elif code is None or code not in catalogue.get(wf, {}):
            unknown += 1
        else:
            call.add_override(ts, event_id, code)
    return calls, orphans, unknown


def build_report(conn: sqlite3.Connection, schemas: Dict[str, WorkflowSchema],
                 catalogue: Catalogue) -> Report:
    calls, orphans, unknown = assemble_calls(conn, catalogue)

    by_lead: Dict[int, List[Call]] = defaultdict(list)
    for call in calls.values():
        if call.initiated_ts is not None:  # a call exists only if its call.initiated is present+linked
            by_lead[call.lead_id].append(call)

    # ---- per-lead rows + batch buckets -------------------------------------------------------
    lead_rows: List[tuple] = []
    batch_counts: Dict[Tuple[str, str], Dict[str, int]] = {}
    for lead_id, wf, ref, batch_id, last4 in conn.execute(
            "SELECT lead_id, workflow_id, external_ref, batch_id, phone_last4 FROM leads"):
        dispositions = catalogue.get(wf, {})
        lead_calls = sorted(by_lead.get(lead_id, []), key=lambda c: (c.initiated_ts, c.call_id))
        bucket = lead_bucket(lead_calls, dispositions)
        counts = batch_counts.setdefault((wf, batch_id), dict.fromkeys(("total_leads",) + BUCKETS, 0))
        counts["total_leads"] += 1
        counts[bucket] += 1

        current = next((c.effective_code for c in reversed(lead_calls) if c.effective_code), "")
        ever_success = any(c.effective_code and dispositions[c.effective_code].is_success
                           for c in lead_calls)
        lead_rows.append((wf, ref or "", lead_id, [
            wf, ref or "", batch_id, f"+91******{last4}", bucket, len(lead_calls),
            sum(c.connected for c in lead_calls),
            _iso_ist(lead_calls[-1].initiated_ts) if lead_calls else "",
            current, "true" if ever_success else "false"]))
    lead_rows.sort(key=lambda t: t[:3])

    # ---- daily overview (per workflow per IST date of call.initiated) --------------------------
    daily: Dict[Tuple[str, str], list] = {}
    for (wf, _), call in calls.items():
        if call.initiated_ts is None:
            continue
        entry = daily.setdefault((wf, _ist_date(call.initiated_ts)), [0, 0, set()])
        entry[0] += 1
        entry[1] += call.connected
        code = call.effective_code
        if code and catalogue[wf][code].is_success:
            entry[2].add(call.lead_id)           # distinct leads with a successful call that day
    daily_rows = [[wf, day, n, conn_n, f"{conn_n / n:.4f}", len(success)]
                  for (wf, day), (n, conn_n, success) in sorted(daily.items())]

    batch_rows = [[wf, batch, c["total_leads"], *(c[b] for b in BUCKETS)]
                  for (wf, batch), c in sorted(batch_counts.items())]

    # ---- rejections + data quality -------------------------------------------------------------
    rejection_rows = [list(r) for r in conn.execute(
        "SELECT workflow_id, batch_id, row_number, reason FROM rejections "
        "ORDER BY workflow_id, batch_id, row_number")]
    reasons = {"invalid_phone": 0}
    for schema in schemas.values():
        for f in schema.fields:
            if f.required:
                reasons.setdefault(f"missing_required:{f.name}", 0)
    for (reason, n) in conn.execute("SELECT reason, COUNT(*) FROM rejections GROUP BY reason"):
        reasons[reason] = n

    totals = conn.execute(
        "SELECT COALESCE(SUM(rows_total),0), COALESCE(SUM(rows_rejected),0), COALESCE(SUM(duplicates_merged),0), "
        "COALESCE(SUM(field_warnings),0), COALESCE(SUM(events_total),0), COALESCE(SUM(malformed_events),0), "
        "COALESCE(SUM(duplicate_events),0) FROM ingested_files").fetchone()
    quality = {
        "lead_rows_total": totals[0],
        "lead_rows_rejected": totals[1],
        "rejections_by_reason": dict(sorted(reasons.items())),
        "duplicate_lead_rows_merged": totals[2],
        "field_warnings": totals[3],
        "events_total": totals[4],
        "malformed_events": totals[5],
        "duplicate_events_dropped": totals[6],
        "orphan_events": orphans,
        "unknown_disposition_events": unknown,
    }
    return Report(batch_rows, daily_rows, [r[3] for r in lead_rows], rejection_rows, quality)


_HEADERS = {
    "batch_summary.csv": ["workflow_id", "batch_id", "total_leads", *BUCKETS],
    "daily_overview.csv": ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate",
                           "success_leads"],
    "lead_report.csv": ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
                        "connected_calls", "last_call_at", "current_disposition", "ever_success"],
    "rejections.csv": ["workflow_id", "batch_id", "row_number", "reason"],
}


def _atomic_write(path: Path, write) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        write(fh)
    os.replace(tmp, path)  # readers never see a half-written report


def write_report(report: Report, out_dir: Path) -> List[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = {"batch_summary.csv": report.batch_summary, "daily_overview.csv": report.daily_overview,
            "lead_report.csv": report.lead_report, "rejections.csv": report.rejections}
    written = []
    for name, header in _HEADERS.items():
        def write(fh, header=header, body=rows[name]):
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(header)
            w.writerows(body)
        _atomic_write(out_dir / name, write)
        written.append(out_dir / name)
    dq = out_dir / "data_quality.json"
    _atomic_write(dq, lambda fh: fh.write(json.dumps(report.data_quality, indent=2) + "\n"))
    written.append(dq)
    return written
