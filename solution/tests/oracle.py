"""Independent, deliberately naive reference implementation of the spec (in memory, no SQL, no hashing).

It shares only the value parsers/row validator with the pipeline (those have their own unit tests).
Everything else - merging, event dedupe, linking, effective disposition, buckets, report rows -
is re-derived here a different way, so a bug in the SQL/ledger path shows up as a diff.
"""
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from ledger.config import IST
from ledger.leads import is_blank_row, process_row
from ledger.normalize import normalize_phone, parse_timestamp

BUCKETS = ["yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed"]


def run_oracle(drops, schemas, catalogue):
    leads, dq, rejections, seen, events = {}, Counter(), [], set(), []
    for drop in drops:
        for wf_dir in sorted((drop / "leads").iterdir()):
            schema = schemas[wf_dir.name]
            for f in sorted(wf_dir.glob("*.csv")):
                with open(f, encoding="utf-8-sig", newline="") as fh:
                    table = list(csv.reader(fh))
                hmap, n = schema.map_header(table[0]), 0
                for cells in table[1:]:
                    if is_blank_row(cells):
                        continue
                    n += 1
                    dq["lead_rows_total"] += 1
                    out = process_row(schema, hmap, cells)
                    if out.reason:
                        dq["lead_rows_rejected"] += 1
                        rejections.append((wf_dir.name, f.stem, n, out.reason))
                        dq["why:" + out.reason] += 1
                        continue
                    dq["field_warnings"] += out.warnings
                    key = (wf_dir.name, out.values[schema.phone_field])
                    if key in leads:
                        dq["duplicate_lead_rows_merged"] += 1
                        leads[key]["v"].update({k: v for k, v in out.values.items() if v is not None})
                    else:
                        leads[key] = {"batch": f.stem, "v": dict(out.values)}
        if (drop / "events").is_dir():
            for f in sorted((drop / "events").glob("*.jsonl")):
                for line in open(f, encoding="utf-8"):
                    if not line.strip():
                        continue
                    dq["events_total"] += 1
                    try:
                        e = json.loads(line)
                        assert isinstance(e, dict) and str(e.get("event_id") or "").strip()
                    except Exception:
                        dq["malformed_events"] += 1
                        continue
                    if e["event_id"] in seen:
                        dq["duplicate_events_dropped"] += 1
                        continue
                    seen.add(e["event_id"])
                    events.append(e)

    ref_of = {}
    for (wf, phone), lead in leads.items():
        for f in schemas[wf].fields:
            if lead["v"].get(f.name) is None and f.default is not None:
                lead["v"][f.name] = f.default
        ref_of[(wf, phone)] = lead["v"][schemas[wf].reference_field]

    def lead_of_phone(e):
        p = normalize_phone(str(e["phone"])) if e.get("phone") not in (None, "") else None
        return (e["workflow_id"], p) if (e["workflow_id"], p) in leads else None

    call_lead = {}
    for e in events:
        if e["event_type"] != "disposition.override" and lead_of_phone(e) and e.get("call_id"):
            call_lead.setdefault((e["workflow_id"], e["call_id"]), lead_of_phone(e))
    # a call belongs to the lead of its call.initiated event when it has one
    for e in events:
        if e["event_type"] == "call.initiated" and lead_of_phone(e) and e.get("call_id"):
            call_lead[(e["workflow_id"], e["call_id"])] = lead_of_phone(e)

    per_call = defaultdict(list)
    for e in events:
        key = (e["workflow_id"], e.get("call_id"))
        if e["event_type"] == "disposition.override":
            lead = call_lead.get(key)
        else:
            lead = lead_of_phone(e) if e.get("call_id") else None
        if lead is None or parse_timestamp(e.get("occurred_at")) is None:
            dq["orphan_events"] += 1
            continue
        code = (e.get("payload") or {}).get("disposition_code")
        if e["event_type"].startswith("disposition.") and code not in catalogue.get(e["workflow_id"], {}):
            dq["unknown_disposition_events"] += 1
            continue
        per_call[key].append((parse_timestamp(e["occurred_at"]), e["event_id"], e))

    calls_by_lead = defaultdict(list)
    for key, evs in per_call.items():
        inits = [x for x in evs if x[2]["event_type"] == "call.initiated"]
        if not inits or key not in call_lead:
            continue
        evs.sort(key=lambda x: (x[0], x[1]))
        kinds = {x[2]["event_type"] for x in evs}
        sets = [x for x in evs if x[2]["event_type"] == "disposition.set"]
        ovs = [x for x in evs if x[2]["event_type"] == "disposition.override"]
        pick = (ovs or sets)
        eff = pick[-1][2]["payload"]["disposition_code"] if pick else None
        calls_by_lead[call_lead[key]].append({
            "id": key[1], "ts": min(x[0] for x in inits), "connected": "call.connected" in kinds,
            "failed": "call.telephony_failed" in kinds, "eff": eff})

    lead_rows, batches, daily = [], defaultdict(Counter), defaultdict(lambda: [0, 0, set()])
    for lead_key, lead in leads.items():
        wf = lead_key[0]
        cat = catalogue[wf]
        calls = sorted(calls_by_lead.get(lead_key, []), key=lambda c: (c["ts"], c["id"]))
        if not calls:
            bucket = "yet_to_reach_out"
        elif any(c["eff"] and cat[c["eff"]].is_terminal for c in calls):
            bucket = "completed"
        elif all(c["failed"] for c in calls):
            bucket = "in_progress_telephony_failed"
        else:
            bucket = "in_progress_dialed"
        batches[(wf, lead["batch"])]["total_leads"] += 1
        batches[(wf, lead["batch"])][bucket] += 1
        cur = [c["eff"] for c in calls if c["eff"]]
        lead_rows.append([wf, ref_of[lead_key], lead["batch"], "+91******" + lead_key[1][-4:], bucket,
                          str(len(calls)), str(sum(c["connected"] for c in calls)),
                          datetime.fromtimestamp(calls[-1]["ts"], IST).isoformat(timespec="seconds") if calls else "",
                          cur[-1] if cur else "",
                          "true" if any(c["eff"] and cat[c["eff"]].is_success for c in calls) else "false"])
        for c in calls:
            d = daily[(wf, datetime.fromtimestamp(c["ts"], IST).date().isoformat())]
            d[0] += 1
            d[1] += c["connected"]
            if c["eff"] and cat[c["eff"]].is_success:
                d[2].add(lead_key)
    lead_rows.sort(key=lambda r: (r[0], r[1]))

    return {
        "lead_report": lead_rows,
        "batch_summary": [[wf, b, str(c["total_leads"]), *[str(c[k]) for k in BUCKETS]]
                          for (wf, b), c in sorted(batches.items())],
        "daily_overview": [[wf, day, str(n), str(c), f"{c / n:.4f}", str(len(s))]
                           for (wf, day), (n, c, s) in sorted(daily.items())],
        "rejections": [[wf, b, str(n), r] for wf, b, n, r in sorted(rejections)],
        "dq": dq,
    }
