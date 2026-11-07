"""Invariant fuzzing: random interleavings of callers, corrections, retries, barge-ins, restarts, injected
instructions and backend faults. Every step must leave the invariants in invariants.py intact.

Seeded and dependency-free. On failure the test prints the seed and the step trace; replay it with
VAAT_SEED=<seed> python -m pytest tests/test_invariants.py
"""
import os
import random

import pytest

from booking_core import CallSession, Schedule
from invariants import violations

RUNS = int(os.environ.get("VAAT_FUZZ_RUNS", "300"))
STEPS = int(os.environ.get("VAAT_FUZZ_STEPS", "60"))
SEEDS = [int(os.environ["VAAT_SEED"])] if "VAAT_SEED" in os.environ else range(RUNS)
SLOTS = ["mon-09", "mon-11", "tue-09", "tue-11"]
CALLERS = [("org-a", "+972500000001"), ("org-a", "+972500000002"), ("org-b", "+972500000001")]
# Weighted toward the real flow (offer -> confirm -> book -> move), with faults and abuse mixed in.
OPS = ["field", "slots", "slots", "confirm", "confirm", "book", "book", "book", "move", "move", "fault", "fault",
       "speak", "recover", "restart", "inject", "late_write", "late_write"]


def new_caller(call_id: str, org: str, phone: str, sch: Schedule) -> CallSession:
    s = CallSession(call_id, org, phone, sch)
    for name, value in (("service", "boiler repair"), ("date", "Monday"), ("address", "Herzl 10")):
        s.set_field(name, value)
    return s


def _write(s: CallSession, sch: Schedule, slot: str, action) -> str:
    consent, n = s.confirmed == (s.version, slot), len(sch.visits)
    result = action(slot)
    if len(sch.visits) > n and not consent:
        raise AssertionError(f"INV-8 {s.call_id} wrote {slot} without current consent")
    return result


def _pick(rnd: random.Random, s: CallSession) -> str:
    if s.confirmed and rnd.random() < 0.8:
        return s.confirmed[1]
    return rnd.choice(s.offered or SLOTS)


def step(rnd: random.Random, sch: Schedule, sessions: list, i: int, op: str) -> str:
    s = sessions[i]
    if op == "field":
        s.set_field(rnd.choice(["service", "date", "address"]), rnd.choice(["x", "y", "z", ""]))
    elif op == "slots":
        stale = rnd.random() < 0.2
        return str(s.receive_slots(rnd.sample(SLOTS, 2), for_version=s.version - (1 if stale else 0)))
    elif op == "confirm":
        return s.confirm(rnd.choice(s.offered or SLOTS))
    elif op == "book":
        return _write(s, sch, _pick(rnd, s), s.book)
    elif op == "move":  # the caller asks to move the visit: new date, new offer, (usually) agreement, reschedule
        s.set_field("date", rnd.choice(["Tuesday", "Monday"]))
        s.receive_slots(rnd.sample(SLOTS, 2), for_version=s.version)
        if rnd.random() < 0.85:
            s.confirm(rnd.choice(s.offered))
        return _write(s, sch, _pick(rnd, s), s.reschedule)
    elif op == "fault":
        sch.fail_next = rnd.choice(Schedule.FAULTS)
        return sch.fail_next
    elif op == "speak":
        return s.speak("Your visit is confirmed.", played=rnd.choice([None, 4]), announces=s.pending_announcement)
    elif op == "recover":
        return s.recover()
    elif op == "restart":
        sessions[i] = CallSession.restore(s.call_id, s.org_id, s.phone, sch, s.log)
    elif op == "inject":
        result = s.tool_call("find_visits", {"org_id": rnd.choice(["org-a", "org-b"])})
        if "error" not in result:
            raise AssertionError(f"INV-5 {s.call_id} accepted a tenant argument from the conversation")
    elif op == "late_write" and s.status == "unknown":
        slot = next(e["slot"] for e in reversed(s.log) if e["event"] == "unknown")
        try:
            sch.book(s.org_id, s.phone, slot, s.unknown_key)  # the backend applies a queued write late
        except Exception as exc:  # the slot may have been taken meanwhile: then the write never happens
            return type(exc).__name__
    return ""


@pytest.mark.parametrize("seed", SEEDS)
def test_invariants_hold_under_random_faults(seed):
    rnd = random.Random(seed)
    sch = Schedule()
    sessions = [new_caller(f"C{n}", org, phone, sch) for n, (org, phone) in enumerate(CALLERS)]
    trace = []
    for k in range(STEPS):
        i, op = rnd.randrange(len(sessions)), rnd.choice(OPS)
        result = ""
        try:
            result = step(rnd, sch, sessions, i, op)
            found = violations(sch, sessions)
        except AssertionError as exc:
            found = [str(exc)]
        except Exception as exc:  # the core must never crash on caller or backend behaviour
            found = [f"crash: {exc!r}"]
        trace.append(f"{k:02d} {sessions[i].call_id}.{op} -> {result if not found else '!'}")
        if found:
            pytest.fail(f"seed {seed}, step {k}:\n" + "\n".join(found) + "\n\ntrace:\n" + "\n".join(trace))
