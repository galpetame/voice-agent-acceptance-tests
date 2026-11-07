"""Reference booking core for a phone-booking voice agent.

The language model never writes to the schedule. It proposes; this core decides whether an action is allowed,
makes every write idempotent and reports only success it has verified. The core is small and has no
dependencies, so every acceptance check runs in milliseconds without telephony, audio or a model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

REQUIRED_FIELDS = ("service", "date", "address")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
TENANT_ARGS = ("org_id", "phone", "call_id")  # bound to the call, never taken from the conversation


class ToolTimeout(Exception):
    """The external system did not answer in time: the write may or may not have happened."""


class SlotTaken(Exception):
    """Another caller booked the slot first."""


class IdempotencyConflict(Exception):
    """The same idempotency key was reused for a different request."""


@dataclass
class Visit:
    visit_id: str
    org_id: str
    client_phone: str
    slot: str
    status: str = "booked"  # booked | cancelled


class Schedule:
    """Stub of the scheduling system. Every write carries an idempotency key, and faults can be injected."""

    FAULTS = ("timeout_before_write", "timeout_after_write", "cancel_timeout")

    def __init__(self) -> None:
        self.visits: dict[str, Visit] = {}
        self.keys: dict[str, tuple[str, tuple[str, str, str]]] = {}  # key -> (visit_id, request)
        self.fail_next: str | None = None
        self._n = 0

    def _fault(self, name: str) -> None:
        if self.fail_next == name:
            self.fail_next = None
            raise ToolTimeout(name)

    def book(self, org_id: str, phone: str, slot: str, key: str) -> str:
        request = (org_id, phone, slot)
        if key in self.keys:  # retry or duplicate event: same visit, no second write
            vid, seen = self.keys[key]
            if seen != request:
                raise IdempotencyConflict(key)
            return vid
        self._fault("timeout_before_write")
        if any(v.org_id == org_id and v.slot == slot and v.status == "booked" for v in self.visits.values()):
            raise SlotTaken(slot)
        self._n += 1
        vid = f"V{self._n}"
        self.visits[vid] = Visit(vid, org_id, phone, slot)
        self.keys[key] = (vid, request)
        self._fault("timeout_after_write")
        return vid

    def lookup(self, key: str) -> str | None:
        hit = self.keys.get(key)
        return hit[0] if hit else None

    def cancel(self, vid: str) -> None:
        self._fault("cancel_timeout")
        self.visits[vid].status = "cancelled"

    def visits_for(self, org_id: str, phone: str) -> list[Visit]:
        return [v for v in self.visits.values() if v.org_id == org_id and v.client_phone == phone]


@dataclass
class CallSession:
    call_id: str
    org_id: str
    phone: str
    schedule: Schedule
    tz: str = "Asia/Jerusalem"
    fields: dict[str, str] = field(default_factory=dict)
    version: int = 0
    offered: list[str] = field(default_factory=list)
    confirmed: tuple[int, str] | None = None  # (version, slot) the caller explicitly agreed to
    status: str = "open"  # open | booked | rescheduled | unknown | needs_human
    visit_id: str | None = None
    slot: str | None = None
    pending_announcement: str | None = None  # what the caller still has to hear: booked | rescheduled | handoff
    unknown_key: str | None = None
    replacing: str | None = None  # old visit of a reschedule that is not finished yet
    transcript: list[dict] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)

    def _event(self, kind: str, **data) -> None:
        self.log.append({"event": kind, **data})

    # --- conversation state -------------------------------------------------------------------------------
    def set_field(self, name: str, value: str) -> None:
        """A correction replaces the old value, invalidates the earlier confirmation and the stale offer."""
        self.fields[name] = value
        self.version += 1
        self.confirmed = None
        self.offered = []
        self._event("set", field=name, value=value, v=self.version)

    def missing_field(self) -> str | None:
        return next((f for f in REQUIRED_FIELDS if not self.fields.get(f)), None)

    def receive_slots(self, slots: list[str], for_version: int) -> bool:
        """A late read result for an older goal is ignored instead of becoming the current offer."""
        if for_version != self.version:
            self._event("stale_slots", v=for_version)
            return False
        self.offered = list(slots)
        self._event("offered", slots=list(slots), v=self.version)
        return True

    def confirm(self, slot: str) -> str:
        """The caller agrees to one offered slot. Agreement binds to this slot and this version of the request."""
        if slot not in self.offered:
            return "not_offered"
        self.confirmed = (self.version, slot)
        self._event("confirmed", slot=slot, v=self.version)
        return "confirmed"

    # --- actions ------------------------------------------------------------------------------------------
    def _key(self, purpose: str) -> str:
        """One key per intent: this call, this action, this version of the request, this slot."""
        assert self.confirmed is not None
        version, slot = self.confirmed
        return f"{self.call_id}:{purpose}:v{version}:{slot}"

    def book(self, slot: str) -> str:
        if self.status in ("unknown", "needs_human"):
            return "handoff"  # no new writes while an earlier outcome is unsettled
        missing = self.missing_field()
        if missing:
            return f"ask:{missing}"  # ask about one field, never fill in invented data
        if self.confirmed != (self.version, slot):
            return "ask_confirmation"  # agreement to an older offer does not authorise this write
        key = self._key("book")
        try:
            vid = self.schedule.book(self.org_id, self.phone, slot, key)
        except SlotTaken:
            self.offered = [s for s in self.offered if s != slot]
            self.confirmed = None
            self._event("slot_taken", slot=slot)
            return "offer_other_slot"
        except ToolTimeout:
            return self._reconcile(key, slot)
        self._booked(vid, slot, "booked")
        return "booked"

    def _booked(self, vid: str, slot: str, kind: str) -> None:
        self.visit_id, self.slot, self.status = vid, slot, "booked"
        self.pending_announcement = "booked"
        self.unknown_key = None
        self._event(kind, visit_id=vid, slot=slot)

    def _reconcile(self, key: str, slot: str) -> str:
        """Outcome unknown: never retry blindly, never confirm falsely. Look the write up by its key."""
        found = self.schedule.lookup(key)
        if found:
            self._booked(found, slot, "reconciled")
            return "booked"
        self.status, self.unknown_key, self.pending_announcement = "unknown", key, "handoff"
        self._event("unknown", key=key, slot=slot)
        return "handoff"

    def recover(self) -> str:
        """After a restart or a later lookup: settle an unknown outcome, or keep it with a person."""
        if self.status != "unknown" or not self.unknown_key:
            return self.status
        found = self.schedule.lookup(self.unknown_key)
        if not found:
            return "handoff"
        self._booked(found, self.schedule.visits[found].slot, "reconciled")
        return self._finish_reschedule() if self.replacing else "booked"

    def _own(self, vid: str | None) -> bool:
        v = self.schedule.visits.get(vid or "")
        return bool(v and v.org_id == self.org_id and v.client_phone == self.phone)

    def reschedule(self, new_slot: str) -> str:
        """Two writes, not one: success is announced only when the new visit exists and the old one is cancelled."""
        old = self.visit_id
        if self.status not in ("booked", "rescheduled") or not self._own(old):
            self._event("refused", action="reschedule", visit_id=old)
            return "refused"
        self.replacing = old
        self._event("reschedule", old_visit_id=old, slot=new_slot)
        result = self.book(new_slot)
        if result == "booked":
            return self._finish_reschedule()
        if self.status != "unknown":  # nothing was written: the old visit simply stands
            self.replacing = None
            self._event("reschedule_dropped", reason=result)
        return result  # an unknown new write stays with `replacing` set until recover() or a person settles it

    def _finish_reschedule(self) -> str:
        old = self.replacing
        try:
            self.schedule.cancel(old)  # type: ignore[arg-type]
        except ToolTimeout:
            self.status, self.pending_announcement = "needs_human", "handoff"
            self._event("cancel_failed", new_visit_id=self.visit_id, old_visit_id=old)
            return "handoff"
        self.replacing = None
        self.status, self.pending_announcement = "rescheduled", "rescheduled"
        self._event("rescheduled", old_visit_id=old, visit_id=self.visit_id, slot=self.slot)
        return "rescheduled"

    # --- tools the model may call -------------------------------------------------------------------------
    def tool_call(self, name: str, args: dict | None = None) -> dict:
        """Organisation, caller and call are bound to the session. Words in speech or documents cannot change them."""
        args = args or {}
        bad = [a for a in args if a in TENANT_ARGS]
        if bad:
            self._event("refused", action=name, args=bad)
            return {"error": f"refused: {', '.join(bad)} is bound to the call"}
        if name == "find_visits":
            return {"visits": [{"visit_id": v.visit_id, "slot": v.slot, "status": v.status}
                               for v in self.schedule.visits_for(self.org_id, self.phone)]}
        self._event("refused", action=name)
        return {"error": f"refused: unknown tool {name}"}

    # --- speech -------------------------------------------------------------------------------------------
    def speak(self, text: str, played: int | None = None, announces: str | None = None) -> str:
        """Record only what the caller actually heard. Barge-in never touches the schedule or the status."""
        said = text if played is None else text[:played]
        interrupted = played is not None and played < len(text)
        self.transcript.append({"role": "agent", "said": said, "interrupted": interrupted})
        cleared = bool(announces) and not interrupted and announces == self.pending_announcement
        if cleared:
            self.pending_announcement = None  # the caller heard the whole result
        self._event("spoke", interrupted=interrupted, cleared=cleared)
        return said

    def history(self) -> list[str]:
        return [t["said"] + (" [interrupted]" if t["interrupted"] else "") for t in self.transcript]

    # --- memory, hand-off and recovery --------------------------------------------------------------------
    def compact(self, keep_last: int = 4) -> dict:
        """Shrink a long call for the model's context while pinning what must survive: the latest value of
        every field, every refusal and the status of every completed or uncertain operation."""
        ops = ("booked", "reconciled", "unknown", "cancel_failed", "rescheduled")
        return {"fields": dict(self.fields), "status": self.status, "visit_id": self.visit_id,
                "refusals": [e for e in self.log if e["event"] in ("slot_taken", "refused")],
                "operations": [e for e in self.log if e["event"] in ops],
                "recent": self.log[-keep_last:]}

    def handoff_packet(self, reason: str) -> dict:
        return {"reason": reason, "call_id": self.call_id, "org_id": self.org_id, "phone": self.phone,
                "fields": dict(self.fields), "status": self.status, "visit_id": self.visit_id,
                "replacing": self.replacing, "unknown_key": self.unknown_key, "log": list(self.log)}

    @classmethod
    def restore(cls, call_id: str, org_id: str, phone: str, schedule: Schedule, log: list[dict]) -> "CallSession":
        """Rebuild a session from its event log, for example after the backend restarted."""
        s = cls(call_id, org_id, phone, schedule)
        for e in log:
            k = e["event"]
            if k == "set":
                s.fields[e["field"]], s.version, s.confirmed, s.offered = e["value"], e["v"], None, []
            elif k == "offered":
                s.offered = list(e["slots"])
            elif k == "confirmed":
                s.confirmed = (e["v"], e["slot"])
            elif k == "slot_taken":
                s.offered, s.confirmed = [x for x in s.offered if x != e["slot"]], None
            elif k in ("booked", "reconciled"):
                s.visit_id, s.slot, s.status, s.unknown_key = e["visit_id"], e["slot"], "booked", None
                s.pending_announcement = "booked"
            elif k == "unknown":
                s.status, s.unknown_key, s.pending_announcement = "unknown", e["key"], "handoff"
            elif k == "reschedule":
                s.replacing = e["old_visit_id"]
            elif k == "reschedule_dropped":
                s.replacing = None
            elif k == "cancel_failed":
                s.status, s.pending_announcement = "needs_human", "handoff"
            elif k == "rescheduled":
                s.status, s.pending_announcement, s.replacing = "rescheduled", "rescheduled", None
            elif k == "spoke" and e.get("cleared"):
                s.pending_announcement = None
        s.log =[dict(e) for e in log]
        return s

    # --- dates --------------------------------------------------------------------------------------------
    def resolve_date(self, phrase: str, now: datetime) -> date | None:
        """Resolve a relative date in the caller's timezone. Returns None when the phrase is ambiguous."""
        today = now.astimezone(ZoneInfo(self.tz)).date()
        p = phrase.strip().lower()
        offsets = {"today": 0, "tomorrow": 1, "day after tomorrow": 2}
        if p in offsets:
            return today + timedelta(days=offsets[p])
        if p in WEEKDAYS:
            ahead = (WEEKDAYS.index(p) - today.weekday()) % 7
            return None if ahead == 0 else today + timedelta(days=ahead)  # "Thursday" on a Thursday: ask
        return None

    def spoken_date(self, phrase: str, now: datetime) -> str | None:
        """Say a concrete date before confirming, e.g. 'Monday 09 November'."""
        d = self.resolve_date(phrase, now)
        return d.strftime("%A %d %B") if d else None


__all__ = ["Schedule", "CallSession", "Visit", "ToolTimeout", "SlotTaken", "IdempotencyConflict",
           "REQUIRED_FIELDS"]
