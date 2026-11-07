"""Mutation check: plant known bugs in booking_core.py one at a time and confirm the test suites catch each.

Usage: python tools/mutation_check.py   (about a minute; exits non-zero if any planted bug survives)
"""
import shutil, subprocess, sys, tempfile, pathlib
REPO = pathlib.Path(__file__).resolve().parent.parent
PY = sys.executable
M = [
 ("book without current consent", "booking_core.py", "if self.confirmed != (self.version, slot):", "if self.confirmed is None:"),
 ("false confirmation on timeout", "booking_core.py", "found = self.schedule.lookup(key)\n        if found:", "found = self.schedule.lookup(key) or 'V1'\n        if found:"),
 ("no idempotency (blind retry writes twice)", "booking_core.py", "if key in self.keys:  # retry", "if False and key in self.keys:  # retry"),
 ("no slot-race check", "booking_core.py", "if any(v.org_id == org_id and v.slot == slot and v.status == \"booked\" for v in self.visits.values()):", "if False:"),
 ("says rescheduled after failed cancel", "booking_core.py", "self.status, self.pending_announcement = \"needs_human\", \"handoff\"\n            self._event(\"cancel_failed\"", "self.status, self.pending_announcement = \"rescheduled\", \"rescheduled\"\n            self._event(\"cancel_failed\""),
 ("tool leaks other callers in the org", "booking_core.py", "for v in self.schedule.visits_for(self.org_id, self.phone)]}", "for v in self.schedule.visits.values() if v.org_id == self.org_id]}"),
 ("tool accepts org_id from the conversation", "booking_core.py", "bad = [a for a in args if a in TENANT_ARGS]", "bad = []"),
 ("correction keeps old consent", "booking_core.py", "self.version += 1\n        self.confirmed = None", "self.version += 1"),
 ("recovery forgets the second write", "booking_core.py", "return self._finish_reschedule() if self.replacing else \"booked\"", "return \"booked\""),
 ("restore ignores reschedule state", "booking_core.py", "            elif k == \"reschedule\":\n                s.replacing = e[\"old_visit_id\"]\n", ""),
 ("late slots accepted", "booking_core.py", "if for_version != self.version:", "if False:"),
 ("barge-in counts as heard", "booking_core.py", "cleared = bool(announces) and not interrupted and", "cleared = bool(announces) and"),
]
caught = 0
for name, f, old, new in M:
    d = pathlib.Path(tempfile.mkdtemp())
    shutil.copytree(REPO, d / "r", ignore=shutil.ignore_patterns(".git", "__pycache__"))
    p = d / "r" / f
    src = p.read_text(encoding="utf-8")
    assert old in src, f"mutant anchor not found: {name}"
    p.write_text(src.replace(old, new, 1), encoding="utf-8")
    by = []
    for suite in ("tests/test_acceptance.py", "tests/test_invariants.py"):
        r = subprocess.run([PY, "-m", "pytest", "-q", "-x", suite], cwd=d / "r", capture_output=True, text=True)
        if r.returncode != 0:
            by.append(suite.split("_")[-1].replace(".py", ""))
    caught += bool(by)
    print(f"{'CAUGHT' if by else 'MISSED'}  {name:45s} by {', '.join(by) or '-'}")
    shutil.rmtree(d, ignore_errors=True)
print(f"{caught}/{len(M)} planted bugs caught")
sys.exit(0 if caught == len(M) else 1)
