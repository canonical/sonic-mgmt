"""Process and monit helpers defined inside test modules, loaded with ast and run against a fake DUT.

Run from the repo root with::

    python3 -m pytest --noconftest --confcutdir=tests/common/unit_tests \
        tests/common/unit_tests/unit_test_test_local_process_helpers.py -v
"""
import ast
import importlib.util
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[2]
ROCK_T0 = Path(__file__).resolve().parent / "helpers" / "data" / "container_processes" / "rock-7a081af43c-t0"

_spec = importlib.util.spec_from_file_location("container_processes",
                                               TESTS / "common" / "helpers" / "container_processes.py")
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)


def _load(path, name):
    tree = ast.parse(path.read_text())
    func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = {}
    exec(compile(ast.Module(body=[func], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


class FakeDut:
    def __init__(self, monit=None):
        self.monit = monit or {}

    def get_process_table(self, container):
        return cp.parse_probe_output(container, 0, (ROCK_T0 / "{}.txt".format(container)).read_text())

    def is_service_running(self, service_name, docker_name):
        # bare=False as in SonicHost.is_service_running: supervisorctl knows a grouped program only by its full name
        service = self.get_process_table(docker_name).get(service_name, bare=False)
        return service is not None and service.state == "RUNNING"

    def get_monit_services_status(self):
        return self.monit


@pytest.mark.parametrize("program, expected", [("orchagent", True), ("start", False), ("no-such-program", False)])
def test_autorestart_program_running_on_pebble(program, expected):
    is_running = _load(TESTS / "autorestart" / "test_container_autorestart.py", "is_supervisor_program_running")
    assert is_running(FakeDut(), "swss", program) is expected


@pytest.mark.parametrize("status, expected", [("Status ok", True), ("OK", True), ("Waiting", True),
                                              ("Status failed", False)])
def test_memory_checker_monit_running(status, expected):
    # monit 5.35 (Ubuntu 26.04) shows a program checked 'every N cycles' as Waiting until its first result
    check = _load(TESTS / "memory_checker" / "test_memory_checker.py", "check_monit_running")
    monit = {"root-overlay": {"service_type": "Filesystem", "service_status": "Accessible"},
             "arp_update_checker": {"service_type": "Program", "service_status": status}}
    assert check(FakeDut(monit)) is expected
