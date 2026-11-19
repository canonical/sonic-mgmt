"""SONIC_SSH_REGEX must match the sshd banner of Debian- and Ubuntu-based SONiC.

Run from the repo root with::

    python3 -m pytest --noconftest --confcutdir=tests/common/unit_tests \
        tests/common/unit_tests/unit_test_sonic_ssh_regex.py -v
"""
import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

# Banners sent by sshd on SONiC 202605.1174613 (Debian 13) and 202605_resolute (Ubuntu 26.04)
BANNERS = ["SSH-2.0-OpenSSH_10.0p2 Debian-7+fips", "SSH-2.0-OpenSSH_10.2p1 Ubuntu-2ubuntu3"]


def _constant(path, name):
    tree = ast.parse((REPO / path).read_text())
    return next(node.value.value for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == name for t in node.targets))


@pytest.mark.parametrize("banner", BANNERS)
def test_reboot_regex_matches_banner(banner):
    assert re.search(_constant("tests/common/reboot.py", "SONIC_SSH_REGEX"), banner)


def test_no_debian_only_banner_regex_left():
    debian_only = re.compile(r"OpenSSH_\[\\+w\\+\.\]\+ Debian['\"]")
    offenders = [str(p.relative_to(REPO)) for top in ("tests", "ansible")
                 for p in (REPO / top).rglob("*") if p.suffix in (".py", ".yml", ".yaml") and p.is_file()
                 and "unit_tests" not in p.parts and debian_only.search(p.read_text(errors="ignore"))]
    assert offenders == []
