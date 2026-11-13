"""Derived facts as pure functions: effective disposition and lead bucket."""
from __future__ import annotations

from typing import Mapping, Optional, Sequence, Tuple

from .schema_def import Disposition

Candidate = Tuple[float, str, str]  # (occurred_ts, event_id, disposition_code)


class Call:
    """Everything the ledger knows about one call, assembled from its linked events."""

    __slots__ = ("call_id", "lead_id", "initiated_ts", "connected", "failed", "set_best", "override_best")

    def __init__(self, call_id: str, lead_id: int):
        self.call_id = call_id
        self.lead_id = lead_id
        self.initiated_ts: Optional[float] = None
        self.connected = False
        self.failed = False                      # has a call.telephony_failed event
        self.set_best: Optional[Candidate] = None
        self.override_best: Optional[Candidate] = None

    @staticmethod
    def _latest(current: Optional[Candidate], ts: float, event_id: str, code: str) -> Candidate:
        # Latest by occurred_at; event_id breaks exact ties so the result never depends on file order.
        candidate = (ts, event_id, code)
        return candidate if current is None or candidate[:2] > current[:2] else current

    def add_set(self, ts: float, event_id: str, code: str) -> None:
        self.set_best = self._latest(self.set_best, ts, event_id, code)

    def add_override(self, ts: float, event_id: str, code: str) -> None:
        self.override_best = self._latest(self.override_best, ts, event_id, code)

    @property
    def effective_code(self) -> Optional[str]:
        """Latest override wins; else latest set; else none. Originals are never edited."""
        if self.override_best is not None:
            return self.override_best[2]
        if self.set_best is not None:
            return self.set_best[2]
        return None


def lead_bucket(calls: Sequence[Call], dispositions: Mapping[str, Disposition]) -> str:
    """First match wins: yet_to_reach_out > completed > in_progress_telephony_failed > in_progress_dialed."""
    if not calls:
        return "yet_to_reach_out"
    for call in calls:
        code = call.effective_code
        if code is not None and dispositions[code].is_terminal:
            return "completed"
    if all(call.failed for call in calls):
        return "in_progress_telephony_failed"
    return "in_progress_dialed"
