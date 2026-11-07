"""Safety invariants for a booking voice agent.

Examples show that one path works. Invariants must hold after every step of every call, whatever the caller
says and whatever the backend does. Call `assert_invariants` at the end of any test, including a conversation
simulated with an LLM caller, to check what the agent actually did to the schedule.
"""
from __future__ import annotations

from collections import Counter

from booking_core import CallSession, Schedule

INVARIANTS = {
    "INV-1": "No double booking: in one organisation a slot holds at most one booked visit.",
    "INV-2": "Verified success only: a session that says booked or rescheduled points to a booked visit of this "
             "caller, in this organisation, at the slot the caller confirmed.",
    "INV-3": "One write per intent: every visit was created under exactly one idempotency key.",
    "INV-4": "No false confirmation: while an outcome is unknown or half done, the agent has nothing positive to announce.",
    "INV-5": "Tenant isolation: tool results never contain another organisation's or another caller's visits.",
    "INV-6": "Recoverable: rebuilding the session from its event log gives the same state.",
    "INV-7": "Reschedule is all or a person: a session never rests at booked while the visit it replaces is unsettled.",
    "INV-8": "No write without current consent: a visit is created only for the slot and request version the caller "
             "confirmed (checked at every write by the fuzzer).",
}

_STATE = ("fields", "version", "offered", "confirmed", "status", "visit_id", "slot", "pending_announcement",
          "unknown_key", "replacing")


def violations(schedule: Schedule, sessions: list[CallSession]) -> list[str]:
    out: list[str] = []

    taken = Counter((v.org_id, v.slot) for v in schedule.visits.values() if v.status == "booked")
    out += [f"INV-1 double booking {k}" for k, n in taken.items() if n > 1]

    for s in sessions:
        if s.status in ("booked", "rescheduled"):
            v = schedule.visits.get(s.visit_id or "")
            if not v or v.status != "booked" or v.org_id != s.org_id or v.client_phone != s.phone or v.slot != s.slot:
                out.append(f"INV-2 {s.call_id} says {s.status} but visit {s.visit_id} is {v}")
        if s.status in ("unknown", "needs_human") and s.pending_announcement in ("booked", "rescheduled"):
            out.append(f"INV-4 {s.call_id} would announce {s.pending_announcement} while {s.status}")
        if s.replacing and s.status in ("booked", "rescheduled"):
            out.append(f"INV-7 {s.call_id} rests at {s.status} while {s.replacing} is not cancelled")
        for item in s.tool_call("find_visits")["visits"]:
            v = schedule.visits[item["visit_id"]]
            if v.org_id != s.org_id or v.client_phone != s.phone:
                out.append(f"INV-5 {s.call_id} sees {v.visit_id} of {v.org_id}/{v.client_phone}")
        r =CallSession.restore(s.call_id, s.org_id, s.phone, schedule, s.log)
        diff = [a for a in _STATE if getattr(r, a) != getattr(s, a)]
        if diff:
            out.append(f"INV-6 {s.call_id} restore differs in {diff}")

    created = Counter(vid for vid, _ in schedule.keys.values())
    if set(created) != set(schedule.visits) or any(n > 1 for n in created.values()):
        out.append("INV-3 visits and idempotency keys do not match one to one")
    return out


def assert_invariants(schedule: Schedule, sessions: list[CallSession]) -> None:
    found = violations(schedule, sessions)
    assert not found, "\n".join(found)
