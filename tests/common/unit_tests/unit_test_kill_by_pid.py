"""The kill-by-pid helpers must never run `kill` with a pid that is not a process.

Under pebble a running service whose pid cannot be resolved has pid -1, and `kill -SIGKILL -1` in a container
that shares the host PID namespace (gnmi) kills every process on the switch.

Run from the repo root with::

    python3 -m pytest --noconftest --confcutdir=tests/common/unit_tests \
        tests/common/unit_tests/unit_test_kill_by_pid.py -v
"""
import ast
import logging
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[2]
SITES = [TESTS / "common" / "utilities.py", TESTS / "common" / "helpers" / "dut_utils.py"]


def _pytest_assert(condition, message=None):
    assert condition, message


def _load(path, name):
    tree = ast.parse(path.read_text())
    func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = {"pytest_assert": _pytest_assert, "logger": logging.getLogger("unit")}
    exec(compile(ast.Module(body=[func], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


class FakeDut:
    os_version = "202605_resolute.0-7a081af43"

    def __init__(self):
        self.commands = []

    def shell(self, cmd, **kwargs):
        self.commands.append(cmd)
        return {"rc": 0}


@pytest.mark.parametrize("path", SITES, ids=lambda p: p.name)
@pytest.mark.parametrize("pid", [-1, 0, None])
def test_kill_refuses_a_pid_that_is_not_a_process(path, pid):
    dut = FakeDut()
    with pytest.raises(AssertionError):
        _load(path, "kill_process_by_pid")(dut, "gnmi", "gnmi-native", pid)
    assert dut.commands == []


@pytest.mark.parametrize("path", SITES, ids=lambda p: p.name)
def test_kill_by_pid(path):
    dut = FakeDut()
    _load(path, "kill_process_by_pid")(dut, "swss", "orchagent", 101)
    assert dut.commands == ["docker exec swss kill -SIGKILL 101"]


def test_autorestart_uses_the_guarded_helper():
    tree = ast.parse((TESTS / "autorestart" / "test_container_autorestart.py").read_text())
    assert "kill_process_by_pid" not in [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
