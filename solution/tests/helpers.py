"""Tiny builders for hand-made drops."""
import json
from pathlib import Path

MOTOR_HEADER = "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language"


def write_leads(drop: Path, batch: str, body: str, header: str = MOTOR_HEADER, wf: str = "motor_renewal"):
    d = drop / "leads" / wf
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{batch}.csv").write_text(header + "\n" + body, encoding="utf-8")


def ev(eid, etype, call, ts, phone="9876543212", wf="motor_renewal", **payload):
    e = {"event_id": eid, "event_type": etype, "workflow_id": wf, "call_id": call,
         "occurred_at": ts, "payload": payload}
    if phone is not None:
        e["phone"] = phone
    return json.dumps(e)


def write_events(drop: Path, name: str, lines):
    d = drop / "events"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
