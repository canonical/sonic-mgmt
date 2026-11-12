"""Mark conditions that name the x86 vs platform must name the arm64 vs platform too.

The arm64 vs image reports arm64-qemu_armv8a-r0. Conditions that apply only to VPP (x86 only) are exempt, as is
tests_mark_conditions_sonic_vpp.yaml.

Run from the repo root with::

    python3 -m pytest --noconftest --confcutdir=tests/common/unit_tests \
        tests/common/unit_tests/unit_test_vs_platform_marks.py -v
"""
from pathlib import Path

import pytest
import yaml

MARKS = Path(__file__).resolve().parents[1] / "plugins" / "conditional_mark"
FILES = sorted(p for p in MARKS.glob("tests_mark_conditions*.yaml") if p.name != "tests_mark_conditions_sonic_vpp.yaml")


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_conditions_name_both_vs_platforms(path):
    conditions = [s for s in _strings(yaml.safe_load(path.read_text()))
                  if "x86_64-kvm_x86_64-r0" in s and "vpp" not in s]
    missing = [s for s in conditions if "arm64-qemu_armv8a-r0" not in s]
    assert not missing, "\n".join(missing)
