"""Monit service status rules of the sanity check, loaded with ast to avoid the testbed imports.

Run from the repo root with::

    python3 -m pytest --noconftest --confcutdir=tests/common/unit_tests \
        tests/common/unit_tests/unit_test_monit_status.py -v
"""
import ast
from pathlib import Path

import pytest

CHECKS = Path(__file__).resolve().parents[1] / "plugins" / "sanity_check" / "checks.py"


@pytest.fixture(scope="module")
def check_status():
    tree = ast.parse(CHECKS.read_text())
    func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_check_monit_services_status")
    namespace = {}
    exec(compile(ast.Module(body=[func], type_ignores=[]), str(CHECKS), "exec"), namespace)
    return namespace["_check_monit_services_status"]


def _run(check_status, status, service_type="Program"):
    services = {"routeCheck": {"service_status": "OK", "service_type": "Program"},
                "arp_update_checker": {"service_status": status, "service_type": service_type}}
    return check_status({"failed": False}, services)["failed"]


@pytest.mark.parametrize("status", ["OK", "Status ok", "Not monitored", "Waiting"])
def test_program_without_a_failure_passes(check_status, status):
    # monit 5.35 (Ubuntu 26.04) shows a program checked 'every N cycles' as Waiting until its first result
    assert _run(check_status, status) is False


@pytest.mark.parametrize("status", ["Status failed", "Execution failed"])
def test_failed_program_fails(check_status, status):
    assert _run(check_status, status) is True


def test_process_not_running_fails(check_status):
    assert _run(check_status, "Does not exist", service_type="Process") is True
