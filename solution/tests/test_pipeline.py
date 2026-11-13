"""End-to-end behaviour on small hand-made drops."""
import csv
import json
import sqlite3

import pytest

from ledger.db import FileChangedError, connect
from ledger.ingest import ingest_path
from ledger.reports import build_report, write_report
from tests.helpers import ev, write_events, write_leads

NAME, PHONE_A = "Asha Rao", "98765 43210"


def run(conn, vault, schemas, *paths):
    return [ingest_path(conn, vault, schemas, p) for p in paths]


def report(conn, schemas, catalogue):
    return build_report(conn, schemas, catalogue)


def make_drops(tmp_path):
    d1, d2 = tmp_path / "drop_1", tmp_path / "drop_2"
    write_leads(d1, "batch_001", "\n".join([
        f'{NAME},+91 {PHONE_A},POL-1,01/12/2026,"₹12,000",EN',     # row 1 -> lead
        "Bad Phone,12345,POL-2,,,",                                   # row 2 -> invalid_phone
        ",9876543211,POL-3,,,",                                       # row 3 -> missing customer_name
        ",,,,,",                                                      # blank: not counted
        "Asha R,09876543210,POL-1B,31/02/2026,Rs. 100,",              # row 4 -> merged duplicate
        "Ravi K,9876543299,POL-9,,N/A,Hindi",                         # row 5 -> lead + 2 warnings
    ]) + "\n")
    # Calls for 9876543212, whose lead only arrives in drop_2 (event-before-lead).
    write_events(d1, "events_0001", [
        ev("e1", "call.initiated", "c1", "2026-10-05T10:00:00+05:30"),
        ev("e2", "call.connected", "c1", "2026-10-05 10:00:10"),
        ev("e3", "call.ended", "c1", "2026-10-05T04:35:00Z", duration_sec=20, end_reason="completed"),
        ev("e4", "disposition.set", "c1", "2026-10-05T10:06:00+05:30", disposition_code="callback_requested"),
        ev("e5", "disposition.override", "c1", "2026-10-05T12:00:00+05:30", phone=None,
           disposition_code="renewed", overridden_by="sup_1", reason="qa_review"),
        ev("e1", "call.initiated", "c1", "2026-10-05T10:00:00+05:30"),    # duplicate event_id
        "{truncated json",                                                 # malformed
        json.dumps({"event_type": "call.initiated"}),                      # no event_id -> malformed
        ev("e6", "call.initiated", "cX", "2026-10-05T10:00:00+05:30", phone="9000000001"),  # never uploaded
        ev("e7", "call.initiated", "c2", "2026-10-06T10:00:00+05:30", phone=PHONE_A),
        ev("e8", "call.telephony_failed", "c2", "2026-10-06T10:00:03+05:30", phone=PHONE_A, reason="sip_503"),
        ev("e9", "call.initiated", "c3", "2026-10-07T10:00:00+05:30", phone="9876543299"),
        ev("e10", "call.connected", "c3", "2026-10-07T10:00:05+05:30", phone="9876543299"),
        ev("e11", "disposition.set", "c3", "2026-10-07T10:03:00+05:30", phone="9876543299",
           disposition_code="NOT_A_CODE"),                                 # unknown -> ignored
        "",                                                                # empty line: not counted
    ])
    write_leads(d2, "batch_002", "Meera N,9876543212,POL-7,2026-12-05,5000,hi\n")
    write_events(d2, "events_1001", [ev("e1", "call.initiated", "c1", "2026-10-05T10:00:00+05:30")])
    return d1, d2


def test_end_to_end_numbers(tmp_path, conn, vault, schemas, catalogue):
    d1, d2 = make_drops(tmp_path)
    run(conn, vault, schemas, d1)

    # After drop_1 the calls of 9876543212 are orphans (their lead has not arrived yet).
    mid = report(conn, schemas, catalogue)
    assert mid.data_quality["orphan_events"] == 5 + 1   # c1's 5 events (override too) + the unknown phone
    assert [r[2] for r in mid.batch_summary] == [2]     # Asha + Ravi

    run(conn, vault, schemas, d2)
    r = report(conn, schemas, catalogue)
    dq = r.data_quality
    assert dq["lead_rows_total"] == 6 and dq["lead_rows_rejected"] == 2
    assert dq["rejections_by_reason"] == {"invalid_phone": 1, "missing_required:customer_name": 1,
                                          "missing_required:phone": 0, "missing_required:policy_number": 0}
    assert dq["duplicate_lead_rows_merged"] == 1
    assert dq["field_warnings"] == 1 + 2   # merged row: bad date (1); Ravi: bad amount + bad language (2)
    assert dq["events_total"] == 14 + 1 and dq["malformed_events"] == 2 and dq["duplicate_events_dropped"] == 2
    assert dq["orphan_events"] == 1        # only the never-uploaded number; c1's events now link
    assert dq["unknown_disposition_events"] == 1

    # Rejections: row numbers count non-blank data rows (blank row skipped); no PII.
    assert r.rejections == [["motor_renewal", "batch_001", 2, "invalid_phone"],
                            ["motor_renewal", "batch_001", 3, "missing_required:customer_name"]]

    by_ref = {row[1]: row for row in r.lead_report}
    asha = by_ref["POL-1B"]   # merged: last non-null reference wins
    assert asha[2] == "batch_001" and asha[3] == "+91******3210"
    assert asha[4:] == ["in_progress_telephony_failed", 1, 0, "2026-10-06T10:00:00+05:30", "", "false"]
    meera = by_ref["POL-7"]   # the late-arriving lead: c1 is renewed via supervisor override
    assert meera[2] == "batch_002"
    assert meera[4:] == ["completed", 1, 1, "2026-10-05T10:00:00+05:30", "renewed", "true"]
    ravi = by_ref["POL-9"]    # unknown disposition ignored -> no disposition
    assert ravi[4:] == ["in_progress_dialed", 1, 1, "2026-10-07T10:00:00+05:30", "", "false"]

    daily = {(row[0], row[1]): row for row in r.daily_overview}
    assert daily[("motor_renewal", "2026-10-05")][2:] == [1, 1, "1.0000", 1]
    assert daily[("motor_renewal", "2026-10-06")][2:] == [1, 0, "0.0000", 0]
    assert r.batch_summary == [["motor_renewal", "batch_001", 2, 0, 1, 1, 0],
                               ["motor_renewal", "batch_002", 1, 0, 0, 0, 1]]


def test_reingest_changes_nothing(tmp_path, conn, vault, schemas, catalogue):
    d1, d2 = make_drops(tmp_path)
    run(conn, vault, schemas, d1, d2)
    before = report(conn, schemas, catalogue)
    counts = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("leads", "events", "rejections", "ingested_files")]
    again = run(conn, vault, schemas, d1, d2)
    assert all(s.files_ingested == 0 and s.files_skipped > 0 for s in again)
    assert report(conn, schemas, catalogue) == before
    assert counts == [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("leads", "events", "rejections", "ingested_files")]


def test_incremental_equals_all_at_once(tmp_path, vault, schemas, catalogue):
    d1, d2 = make_drops(tmp_path / "in")
    root = d1.parent
    a = connect(str(tmp_path / "a.db"), vault)
    run(a, vault, schemas, d1)
    run(a, vault, schemas, d2)
    b = connect(str(tmp_path / "b.db"), vault)
    run(b, vault, schemas, root)                      # a folder of drops, natural order
    assert report(a, schemas, catalogue) == report(b, schemas, catalogue)


def test_crash_mid_file_leaves_nothing_behind(tmp_path, conn, vault, schemas, catalogue, monkeypatch):
    d1, _ = make_drops(tmp_path)
    import ledger.leads as leads
    real = leads._upsert_lead
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash")
        return real(*a, **k)

    monkeypatch.setattr(leads, "_upsert_lead", boom)
    with pytest.raises(RuntimeError):
        run(conn, vault, schemas, d1)
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0] == 0
    monkeypatch.setattr(leads, "_upsert_lead", real)
    run(conn, vault, schemas, d1)                      # a clean re-run completes normally
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 2


def test_ledger_is_append_only(tmp_path, conn, vault, schemas):
    d1, _ = make_drops(tmp_path)
    run(conn, vault, schemas, d1)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE events SET disposition_code='x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM events")


def test_no_plaintext_pii_in_database(tmp_path, db_path, conn, vault, schemas):
    d1, d2 = make_drops(tmp_path)
    run(conn, vault, schemas, d1, d2)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    raw = open(db_path, "rb").read()
    for secret in (b"Asha", b"Meera", b"Ravi", b"9876543210", b"9876543212", b"9876543299", b"98765 43210"):
        assert secret not in raw, secret
    assert b"+91******" not in raw   # masking is a report concern; the DB only holds the last 4 digits


def test_changed_file_with_same_name_is_refused(tmp_path, conn, vault, schemas):
    d1, _ = make_drops(tmp_path)
    run(conn, vault, schemas, d1)
    write_leads(d1, "batch_001", "Someone Else,9000000000,POL-X,,,\n")
    with pytest.raises(FileChangedError):
        run(conn, vault, schemas, d1)


def test_report_files_format(tmp_path, conn, vault, schemas, catalogue):
    d1, d2 = make_drops(tmp_path)
    run(conn, vault, schemas, d1, d2)
    out = tmp_path / "out"
    paths = write_report(report(conn, schemas, catalogue), out)
    assert sorted(p.name for p in paths) == ["batch_summary.csv", "daily_overview.csv", "data_quality.json",
                                             "lead_report.csv", "rejections.csv"]
    with open(out / "lead_report.csv", newline="") as fh:
        lines = list(csv.reader(fh))
    assert lines[0] == ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
                        "connected_calls", "last_call_at", "current_disposition", "ever_success"]
    assert [row[1] for row in lines[1:]] == sorted(row[1] for row in lines[1:])
    assert json.loads((out / "data_quality.json").read_text())["events_total"] == 15
    assert "\r" not in (out / "lead_report.csv").read_text()


def test_unknown_schema_folder_is_skipped_not_fatal(tmp_path, conn, vault, schemas):
    d = tmp_path / "drop_1"
    write_leads(d, "batch_001", "A,9876543210,P1,,,\n", wf="mystery_flow")
    s = ingest_path(conn, vault, schemas, d)
    assert s.files_ingested == 0 and any("mystery_flow" in n for n in s.notes)
