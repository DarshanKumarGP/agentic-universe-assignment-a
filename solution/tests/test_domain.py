from ledger.facts import Call, lead_bucket
from ledger.schema_def import Disposition

CAT = {"renewed": Disposition(True, True), "not_interested": Disposition(True, False),
       "callback_requested": Disposition(False, False), "payment_link_sent": Disposition(False, False)}


def call(cid="c1", failed=False, sets=(), overrides=()):
    c = Call(cid, 1)
    c.initiated_ts = 1.0
    c.failed = failed
    for ts, eid, code in sets:
        c.add_set(ts, eid, code)
    for ts, eid, code in overrides:
        c.add_override(ts, eid, code)
    return c


def test_effective_none_without_dispositions():
    assert call().effective_code is None


def test_effective_is_latest_set_by_occurred_at_not_arrival_order():
    c = call(sets=[(20, "e2", "renewed"), (10, "e9", "callback_requested")])
    assert c.effective_code == "renewed"


def test_override_beats_set_even_if_set_is_later():
    c = call(sets=[(100, "e1", "renewed")], overrides=[(50, "e2", "not_interested")])
    assert c.effective_code == "not_interested"


def test_latest_override_wins():
    c = call(overrides=[(10, "e1", "renewed"), (30, "e2", "not_interested"), (20, "e3", "payment_link_sent")])
    assert c.effective_code == "not_interested"


def test_tie_broken_by_event_id_deterministically():
    a = call(sets=[(5, "evt_2", "renewed"), (5, "evt_1", "not_interested")])
    b = call(sets=[(5, "evt_1", "not_interested"), (5, "evt_2", "renewed")])
    assert a.effective_code == b.effective_code == "renewed"


def test_bucket_no_calls():
    assert lead_bucket([], CAT) == "yet_to_reach_out"


def test_bucket_completed_beats_everything():
    calls = [call("a", failed=True), call("b", sets=[(1, "e", "renewed")])]
    assert lead_bucket(calls, CAT) == "completed"


def test_bucket_terminal_via_override_only():
    assert lead_bucket([call(sets=[(1, "e1", "callback_requested")],
                             overrides=[(2, "e2", "not_interested")])], CAT) == "completed"


def test_bucket_override_can_remove_terminal():
    assert lead_bucket([call(sets=[(1, "e1", "renewed")], overrides=[(2, "e2", "callback_requested")])],
                       CAT) == "in_progress_dialed"


def test_bucket_all_telephony_failed():
    assert lead_bucket([call("a", failed=True), call("b", failed=True)], CAT) == "in_progress_telephony_failed"


def test_bucket_mixed_failed_and_not_is_dialed():
    assert lead_bucket([call("a", failed=True), call("b")], CAT) == "in_progress_dialed"


def test_bucket_non_terminal_disposition_is_dialed():
    assert lead_bucket([call(sets=[(1, "e", "callback_requested")])], CAT) == "in_progress_dialed"
