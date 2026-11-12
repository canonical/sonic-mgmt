"""critical_process_status() against the supervisorctl parser it replaced, on real probe output.

Run from the repo root with::

    python3 -m pytest --noconftest --confcutdir=tests/common/unit_tests \
        tests/common/unit_tests/helpers/unit_test_container_processes_parity.py -v

data/container_processes/dockerfile-7a081af43c-t0 and rock-7a081af43c-t0 hold the probe output of every container
of the Dockerfile and the rock build of sonic-buildimage 202605_resolute 7a081af43c, both after deploy-mg of
vms-kvm-t0. SonicHost.parse_service_status_and_critical_process() is the supervisorctl parser from before the
change, kept unchanged.
"""
import ast
import importlib.util
import re
from pathlib import Path

import pytest

# the old parser passes re.split's maxsplit positionally
pytestmark = pytest.mark.filterwarnings("ignore:'maxsplit' is passed as positional argument:DeprecationWarning")

COMMON = Path(__file__).resolve().parents[2]
DATA = Path(__file__).resolve().parent / "data" / "container_processes"
DOCKERFILE_T0 = DATA / "dockerfile-7a081af43c-t0"
ROCK_T0 = DATA / "rock-7a081af43c-t0"

_spec = importlib.util.spec_from_file_location("container_processes", COMMON / "helpers" / "container_processes.py")
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)


def _old_parser():
    tree = ast.parse((COMMON / "devices" / "sonic.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SonicHost")
    func = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                and n.name == "parse_service_status_and_critical_process")
    namespace = {"re": re}
    exec(compile(ast.Module(body=[func], type_ignores=[]), "sonic.py", "exec"), namespace)
    return namespace["parse_service_status_and_critical_process"]


OLD = _old_parser()
CONTAINERS = sorted(p.stem for p in DOCKERFILE_T0.glob("*.txt"))


def _old_status(text):
    table = cp.parse_probe_output("ctr", 0, text)
    status_lines = [line for line in cp._split_sections(text)["status"] if line]
    return OLD(None, service_result={"stdout_lines": status_lines},
               critical_group_list=table.critical_groups, critical_process_list=table.critical_programs)


@pytest.mark.parametrize("container", CONTAINERS)
def test_supervisord_status_matches_the_old_parser(container):
    text = (DOCKERFILE_T0 / "{}.txt".format(container)).read_text()
    assert cp.critical_process_status(cp.parse_probe_output(container, 0, text)) == _old_status(text)


@pytest.mark.parametrize("container", CONTAINERS)
def test_supervisord_status_matches_the_old_parser_with_a_critical_program_exited(container):
    text = (DOCKERFILE_T0 / "{}.txt".format(container)).read_text()
    table = cp.parse_probe_output(container, 0, text)
    critical = [p for p in table.processes if p.state == "RUNNING" and cp.critical_process_status(table)
                ["running_critical_process"].count(p.full_name)]
    if not critical:
        pytest.skip("no running critical program")
    name = critical[0].full_name
    text = re.sub(r"^({}\s+)RUNNING\s+pid \d+, uptime \S+".format(re.escape(name)), r"\1EXITED    Nov 10 09:07 PM",
                  text, count=1, flags=re.M)
    new = cp.critical_process_status(cp.parse_probe_output(container, 0, text))
    assert new == _old_status(text)
    assert name in new["exited_critical_process"]


@pytest.mark.parametrize("container", CONTAINERS)
def test_both_backends_report_the_same_critical_processes(container):
    dockerfile = cp.critical_process_status(
        cp.parse_probe_output(container, 0, (DOCKERFILE_T0 / "{}.txt".format(container)).read_text()))
    rock = cp.critical_process_status(
        cp.parse_probe_output(container, 0, (ROCK_T0 / "{}.txt".format(container)).read_text()))
    for status in (dockerfile, rock):
        status["running_critical_process"].sort()
        status["exited_critical_process"].sort()
    assert rock == dockerfile


def test_supervisord_groups_on_the_t0_testbed():
    table = cp.parse_probe_output("dhcp_relay", 0, (DOCKERFILE_T0 / "dhcp_relay.txt").read_text())
    assert sorted(p.full_name for p in table.processes if p.group) == [
        "dhcp-relay:dhcp6relay", "dhcp-relay:dhcprelayd", "dhcpmon:dhcpmon-Vlan1000"]


# The critical_processes parsing of 36c29fc, which the change replaced: the per-service path
# (get_critical_group_and_process_lists) fails on a bad line, the batch path (critical_group_process) skips it.
def _old_critical_per_service(lines):
    groups, programs = [], []
    for line in lines:
        line_info = line.strip().split(':')
        if len(line_info) != 2:
            return groups, programs, False
        key, value = line_info[0].strip(), line_info[1].strip()
        if key == "group" and value:
            groups.append(value)
        elif key == "program" and value:
            programs.append(value)
        else:
            return groups, programs, False
    return groups, programs, True


def _old_critical_batch(lines):
    groups, programs = [], []
    for line in lines:
        line_info = line.strip().split(':')
        if len(line_info) == 2:
            key, value = line_info[0].strip(), line_info[1].strip()
            if key == "group" and value:
                groups.append(value)
            elif key == "program" and value:
                programs.append(value)
    return groups, programs


CAPTURED_CRITICAL = sorted({tuple(cp._split_sections(p.read_text()).get("critical", []))
                            for d in (DATA / "rock-7a081af43c", ROCK_T0, DOCKERFILE_T0) for p in d.glob("*.txt")})
CRAFTED_CRITICAL = [("program:a", "bogus", "program:b"), ("# comment", "program:a"), ("foo:bar", "program:a"),
                    ("program:", "program:a"), ("program:a", "", "program:b"), ("group:g:x",), ()]


@pytest.mark.parametrize("lines", [list(c) for c in CAPTURED_CRITICAL + CRAFTED_CRITICAL])
def test_critical_processes_parsing_matches_36c29fc(lines):
    # the probe prints the file the way `cat` did, minus trailing blank lines, which stdout_lines also lost
    while lines and not lines[-1].strip():
        lines.pop()
    text = "@@backend@@\nsupervisord\n@@critical@@\n{}@@status@@\nx RUNNING pid 1, uptime 0:00:01\n@@end@@\n".format(
        "".join(line + "\n" for line in lines))
    table = cp.parse_probe_output("ctr", 0, text)
    groups, programs, succeeded = _old_critical_per_service(lines)
    assert table.critical_parse_ok is succeeded
    if succeeded:
        assert (table.critical_groups, table.critical_programs) == (groups, programs)
    assert (table.critical_groups, table.critical_programs) == _old_critical_batch(lines)
