"""SonicHost and dut_utils process helpers on top of container_processes, with a fake DUT.

Run from the repo root with::

    python3 -m pytest --noconftest --confcutdir=tests/common/unit_tests \
        tests/common/unit_tests/helpers/unit_test_sonichost_processes.py -v

The helpers are loaded with ast so the Ansible dependency tree is not imported.
"""
import ast
import importlib.util
import json
import logging
import os
import re
from collections import defaultdict
from pathlib import Path

import pytest

COMMON = Path(__file__).resolve().parents[2]
ROCK = Path(__file__).resolve().parent / "data" / "container_processes" / "rock-7a081af43c"


def _load_module(name):
    spec = importlib.util.spec_from_file_location(name, COMMON / "helpers" / "{}.py".format(name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cp = _load_module("container_processes")
osp = _load_module("os_profile")


class _FakeTime:
    @staticmethod
    def sleep(_):
        pass


def _pytest_assert(condition, message=None):
    assert condition, message


def _wait_until(timeout, interval, delay, condition, *args, **kwargs):
    return condition(*args, **kwargs)


def _namespace():
    return {"container_processes": cp, "os_profile_command": osp.os_profile_command,
            "parse_os_profile_output": osp.parse_os_profile_output, "logging": logging,
            "logger": logging.getLogger("unit"), "time": _FakeTime, "json": json, "os": os,
            "defaultdict": defaultdict, "pytest_assert": _pytest_assert, "wait_until": _wait_until}


def _load_functions(path, names, class_name=None):
    tree = ast.parse(path.read_text())
    body = tree.body
    if class_name:
        body = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name).body
    funcs = [n for n in body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert sorted(f.name for f in funcs) == sorted(names)
    namespace = _namespace()
    exec(compile(ast.Module(body=funcs, type_ignores=[]), str(path), "exec"), namespace)
    return {name: namespace[name] for name in names}


SONICHOST_METHODS = _load_functions(COMMON / "devices" / "sonic.py", [
    "get_process_table", "get_process_tables", "control_container_process", "signal_container_process",
    "os_profile", "_critical_lists",
    "get_critical_group_and_process_lists", "critical_process_status", "all_critical_process_status",
    "start_pmon_daemon", "stop_pmon_daemon_service", "get_pmon_daemon_status", "get_pmon_daemon_states",
    "kill_bgpd", "start_bgpd", "is_service_running"], class_name="SonicHost")
DUT_UTILS = _load_functions(COMMON / "helpers" / "dut_utils.py",
                            ["get_program_info", "get_group_program_info", "verify_orchagent_running_or_assert"])
MULTI_ASIC = _load_functions(COMMON / "devices" / "multi_asic.py", ["modify_syslog_rate_limit"],
                             class_name="MultiAsicSonicHost")


class FakeShellHost:
    """Answers shell/shell_cmds with probe output read from the capture directory."""

    hostname = "vlab-01"
    _os_version = ""
    sonic_release = "202605"
    facts = {"platform": "x86_64-kvm_x86_64-r0"}

    def __init__(self, texts=None):
        self.texts = texts or {}
        self.commands = []

    def _answer(self, cmd):
        self.commands.append(cmd)
        bare = re.sub(r"^timeout \d+ ", "", cmd)
        for container, text in self.texts.items():
            if bare.startswith("docker exec {} ".format(container)):
                return {"rc": 0, "stdout": text, "stderr": "", "cmd": cmd}
        return {"rc": 1, "stdout": "", "stderr": "no such file", "cmd": cmd}

    def shell(self, cmd, module_ignore_errors=False, **kwargs):
        result = self._answer(cmd)
        if result["rc"] != 0 and not module_ignore_errors:
            raise RuntimeError(cmd)
        return result

    def shell_cmds(self, cmds, **kwargs):
        return {"results": [dict(self._answer(cmd), cmd="reordered " + cmd) for cmd in cmds]}

    def _retry_if_oci_exec_race(self, cmd, result):
        return result


for _name, _func in SONICHOST_METHODS.items():
    setattr(FakeShellHost, _name, _func)


class FakeHost(FakeShellHost):
    """Serves process tables from the rock capture and records control calls."""

    def __init__(self, overrides=None):
        super().__init__()
        self.overrides = overrides or {}
        self.control_calls = []
        self.critical_services = ["swss", "syncd", "radv", "bgp", "database", "pmon"]

    def get_process_table(self, container):
        text = self.overrides.get(container) or (ROCK / "{}.txt".format(container)).read_text()
        return cp.parse_probe_output(container, 0, text)

    def get_process_tables(self, containers):
        return {container: self.get_process_table(container) for container in containers}

    def control_container_process(self, container, action, names, module_ignore_errors=False):
        self.control_calls.append((container, action, list(names)))
        return {"rc": 0, "stdout": "", "stderr": ""}

    def is_service_fully_started(self, service):
        return True

    def get_bgp_docker_names(self):
        return ["bgp"]

    def get_frr_mgmt_framework_config(self):
        return False


def _rock_pid(container, argv0):
    return next(p.pid for p in cp.parse_probe_output(container, 0, (ROCK / "{}.txt".format(container)).read_text())
                .processes if p.name == argv0)


def test_get_process_table_runs_the_probe():
    host = FakeShellHost({"swss": (ROCK / "swss.txt").read_text()})
    table = host.get_process_table("swss")
    assert host.commands == [cp.build_probe_command("swss")]
    assert (table.backend, table.get("orchagent").state) == ("pebble", "RUNNING")


def test_get_process_tables_matches_results_by_position():
    host = FakeShellHost({"swss": (ROCK / "swss.txt").read_text(), "syncd": (ROCK / "syncd.txt").read_text()})
    tables = host.get_process_tables(["syncd", "swss"])
    assert (tables["syncd"].backend, tables["swss"].backend) == ("supervisord", "pebble")


def test_get_process_table_of_stopped_container():
    table = FakeShellHost().get_process_table("swss")
    assert (table.running, table.processes) == (False, [])


def test_control_container_process_runs_the_control_command():
    host = FakeShellHost({"pmon": ""})
    host.control_container_process("pmon", "stop", ["xcvrd"])
    assert host.commands == [cp.build_control_command("pmon", "stop", ["xcvrd"])]


def test_signal_container_process_runs_the_signal_command():
    host = FakeShellHost({"radv": ""})
    host.signal_container_process("radv", "SIGHUP", ["radvd"])
    assert host.commands == [cp.build_signal_command("radv", "SIGHUP", ["radvd"])]


def test_os_profile_of_host_is_cached_and_of_container_is_not():
    os_release = 'ID=ubuntu\nVERSION_ID="26.04"\nVERSION_CODENAME=resolute\n'
    host = FakeShellHost()
    host.shell = lambda cmd, **kwargs: (host.commands.append(cmd) or
                                        {"rc": 0, "stdout": os_release + "\n@@debian_version@@\nforky/sid\n"})
    assert host.os_profile().debian_base == "forky"
    assert host.os_profile().debian_base == "forky"
    assert len(host.commands) == 1
    assert host.os_profile("syncd").codename == "resolute"
    assert host.os_profile("syncd").codename == "resolute"
    assert len(host.commands) == 3
    assert host.commands[1].startswith("docker exec syncd ")


def test_all_critical_process_status():
    status = FakeHost().all_critical_process_status()
    assert sorted(status) == ["bgp", "database", "pmon", "radv", "swss", "syncd"]
    assert status["swss"]["status"] is True and "orchagent" in status["swss"]["running_critical_process"]
    assert status["syncd"] == {"status": True, "exited_critical_process": [], "running_critical_process": ["syncd"]}
    assert status["radv"] == {"status": False, "exited_critical_process": ["radvd"], "running_critical_process": []}


def test_critical_process_status():
    status = FakeHost().critical_process_status("database")
    assert status == {"status": True, "exited_critical_process": [],
                      "running_critical_process": ["redis", "redis_bmp"]}


def test_critical_process_status_of_service_not_started():
    host = FakeHost()
    host.is_service_fully_started = lambda service: False
    assert host.critical_process_status("swss")["status"] is False


def test_get_critical_group_and_process_lists():
    groups, programs, succeeded = FakeHost().get_critical_group_and_process_lists("swss")
    assert (groups, succeeded) == ([], True)
    assert programs[:2] == ["orchagent", "portsyncd"]


def test_pmon_critical_lists_keep_only_running_programs():
    text = (ROCK / "pmon.txt").read_text().replace("@@critical@@\n",
                                                   "@@critical@@\nprogram:stormond\nprogram:xcvrd\n")
    assert FakeHost({"pmon": text}).get_critical_group_and_process_lists("pmon") == ([], ["stormond"], True)


@pytest.mark.parametrize("daemon, expected", [
    ("stormond", ("RUNNING", "pid")),
    ("chassis_db_init", ("EXITED", -1)),
    ("xcvrd", (None, -1)),
])
def test_get_pmon_daemon_status(daemon, expected):
    status, pid = FakeHost().get_pmon_daemon_status(daemon)
    assert (status, pid) == (expected[0], _rock_pid("pmon", "stormond") if expected[1] == "pid" else expected[1])


def test_get_pmon_daemon_states():
    assert FakeHost().get_pmon_daemon_states() == {"proc-exit-listener": "RUNNING", "stormond": "RUNNING"}


def test_pmon_daemon_start_and_stop():
    host = FakeHost()
    host.start_pmon_daemon("xcvrd")
    host.stop_pmon_daemon_service("xcvrd")
    assert host.control_calls == [("pmon", "start", ["xcvrd"]), ("pmon", "stop", ["xcvrd"])]


def test_bgpd_stop_and_start():
    host = FakeHost()
    assert host.kill_bgpd()["rc"] == 0
    assert host.start_bgpd()["rc"] == 0
    assert host.control_calls == [("bgp", "stop", ["bgpd"]), ("bgp", "start", ["bgpd"]),
                                  ("bgp", "restart", ["bgpcfgd"])]


def test_get_program_info():
    host = FakeHost()
    assert DUT_UTILS["get_program_info"](host, "swss", "orchagent") == ("RUNNING", _rock_pid("swss", "orchagent"))
    assert DUT_UTILS["get_program_info"](host, "swss", "start") == ("STOPPED", -1)
    assert DUT_UTILS["get_program_info"](host, "swss", "no-such-program") == (None, -1)


def test_get_group_program_info():
    text = (ROCK / "dhcp_relay.txt").read_text().replace(
        "[program:dhcprelayd]", "[group:dhcp-relay]\nprograms=dhcprelayd\n\n[program:dhcprelayd]")
    info = DUT_UTILS["get_group_program_info"](FakeHost({"dhcp_relay": text}), "dhcp_relay", "dhcp-relay")
    assert dict(info) == {"dhcprelayd": ["RUNNING", _rock_pid("dhcp_relay", "dhcprelayd")]}


def test_is_service_running():
    host = FakeHost()
    assert host.is_service_running("orchagent", "swss") is True
    assert host.is_service_running("start", "swss") is False
    assert host.is_service_running("no-such-program", "swss") is False


def test_verify_orchagent_running_or_assert():
    host = FakeHost()
    host.is_multi_asic = False
    DUT_UTILS["verify_orchagent_running_or_assert"](host)
    orchagent = '"orchagent":{"name":"orchagent","startup":"disabled","current":'
    text = (ROCK / "swss.txt").read_text().replace(orchagent + '"active"', orchagent + '"error"')
    stopped = FakeHost({"swss": text})
    stopped.is_multi_asic = False
    with pytest.raises(AssertionError):
        DUT_UTILS["verify_orchagent_running_or_assert"](stopped)


class FakeMultiAsicHost:
    def __init__(self):
        self.sonichost = FakeHost()
        self.sonichost.DEFAULT_ASIC_SERVICES = ["swss", "bgp"]
        self.sonichost.shell_calls = []
        self.sonichost.shell = lambda cmd, **kwargs: self.sonichost.shell_calls.append(cmd) or {"rc": 0}
        self.asics = []

    def get_facts(self):
        return {"asic_type": "vs"}


MultiAsic = type("MultiAsic", (FakeMultiAsicHost,), MULTI_ASIC)


@pytest.mark.parametrize("option, expression", [
    ("disable", r"'s/^\$SystemLogRateLimit/#\$SystemLogRateLimit/g'"),
    ("enable", r"'s/^#\$SystemLogRateLimit/\$SystemLogRateLimit/g'"),
])
def test_modify_syslog_rate_limit_needs_no_tools_in_the_container(option, expression):
    # the database rock has no sed: edit the file from the host through the container's root
    host = MultiAsic()
    host.modify_syslog_rate_limit("database", rl_option=option)
    assert host.sonichost.shell_calls == [
        r"sudo sed -i {} /proc/$(docker inspect -f \{{\{{.State.Pid\}}\}} database)/root/etc/rsyslog.conf"
        .format(expression)]
    assert host.sonichost.control_calls == [("database", "restart", ["rsyslogd"])]


def test_get_process_tables_bounds_each_probe():
    # shell_cmds drops its own timeout for commands with a single quote, and the probe has them
    host = FakeShellHost({"swss": (ROCK / "swss.txt").read_text()})
    host._answer = lambda cmd: host.commands.append(cmd) or {"rc": 0, "stdout": (ROCK / "swss.txt").read_text()}
    host.get_process_tables(["swss"])
    assert host.commands == [cp.build_probe_command("swss", timeout=60)]


def test_is_service_running_needs_the_group_prefix_like_supervisorctl():
    host = FakeHost({"dhcp_relay": (ROCK.parent / "rock-7a081af43c-t0" / "dhcp_relay.txt").read_text()})
    assert host.is_service_running("dhcp6relay", "dhcp_relay") is False
    assert host.is_service_running("dhcp-relay:dhcp6relay", "dhcp_relay") is True


def test_os_profile_failure_raises_and_is_not_cached():
    host = FakeShellHost()
    with pytest.raises(RuntimeError):
        host.os_profile()
    assert getattr(host, "_os_profile", None) is None
