"""Keeps the catalog honest: every check names tests that exist, and the README lists every check."""
import json
from pathlib import Path

import test_acceptance

ROOT = Path(__file__).resolve().parent.parent
CHECKS = json.loads((ROOT / "checks.json").read_text(encoding="utf-8"))


def test_catalog_has_18_checks_in_order():
    assert [c["id"] for c in CHECKS] == list(range(1, 19))


def test_every_check_names_existing_tests():
    for c in CHECKS:
        assert c["tests"], f"check {c['id']} has no test"
        for name in c["tests"]:
            assert callable(getattr(test_acceptance, name, None)), f"check {c['id']}: {name} not found"


def test_readme_lists_every_check():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for c in CHECKS:
        assert f"| {c['id']} | {c['title']} |" in readme, f"README is missing check {c['id']}"
