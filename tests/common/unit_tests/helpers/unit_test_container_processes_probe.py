"""Run the container probe script locally against stub pebble/supervisorctl commands.

Run from the repo root with::

    python3 -m pytest --noconftest --confcutdir=tests/common/unit_tests \
        tests/common/unit_tests/helpers/unit_test_container_processes_probe.py -v
"""
import importlib.util
import os
import shlex
import subprocess
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "helpers" / "container_processes.py"
_spec = importlib.util.spec_from_file_location("container_processes", _MODULE_PATH)
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)

PEBBLE_STUB = """#!/bin/sh
case "$*" in
  "services") echo "Service Startup Current Since Notes";;
  "services --format json") echo '{"services":{"svc":{"name":"svc","startup":"enabled","current":"active"}}}';;
  "plan") printf 'services:\\n    svc:\\n        command: sleep 1000\\n';;
  start*|stop*|restart*|signal*) echo "pebble $*" >> "$STUB_LOG";;
  *) exit 1;;
esac
"""

SUPERVISORCTL_STUB = """#!/bin/sh
case "$1" in
  status) echo "svc                              RUNNING   pid 7, uptime 0:00:01";;
  *) echo "supervisorctl $*" >> "$STUB_LOG";;
esac
"""


def _stub_dir(tmp_path, pebble=False, supervisorctl=False):
    stub = tmp_path / "bin"
    stub.mkdir()
    for name, body, wanted in (("pebble", PEBBLE_STUB, pebble), ("supervisorctl", SUPERVISORCTL_STUB, supervisorctl)):
        if wanted:
            path = stub / name
            path.write_text(body)
            path.chmod(0o755)
    return stub


def _run_inner_script(command, stub, tmp_path):
    """Strip 'docker exec <ctr>' and run the rest locally with the stubs first on PATH."""
    argv = shlex.split(command)
    assert argv[:3] == ["docker", "exec", "swss"]
    env = dict(os.environ, PATH="{}:/usr/bin:/bin".format(stub), STUB_LOG=str(tmp_path / "stub.log"))
    return subprocess.run(argv[3:], env=env, capture_output=True, text=True, timeout=30)


def _sections(stdout):
    # whole-line markers only, as the parser reads them: a host process's argv may contain marker text
    return list(cp._split_sections(stdout))


def test_probe_keeps_docker_exec_prefix():
    assert shlex.split(cp.build_probe_command("swss"))[:3] == ["docker", "exec", "swss"]


def test_probe_selects_pebble_and_prints_pebble_sections(tmp_path):
    result = _run_inner_script(cp.build_probe_command("swss"), _stub_dir(tmp_path, pebble=True), tmp_path)
    assert result.returncode == 0, result.stderr
    assert _sections(result.stdout) == ["backend", "critical-missing", "services", "plan", "btime", "procs",
                                        "groups", "end"]
    assert result.stdout.splitlines()[1] == "pebble"


def test_probe_selects_supervisord_when_pebble_absent(tmp_path):
    result = _run_inner_script(cp.build_probe_command("swss"), _stub_dir(tmp_path, supervisorctl=True), tmp_path)
    assert result.returncode == 0, result.stderr
    assert _sections(result.stdout) == ["backend", "critical-missing", "status", "end"]
    assert result.stdout.splitlines()[1] == "supervisord"


def test_probe_reports_unknown_backend(tmp_path):
    result = _run_inner_script(cp.build_probe_command("swss"), _stub_dir(tmp_path), tmp_path)
    assert _sections(result.stdout) == ["backend", "critical-missing", "end"]
    assert result.stdout.splitlines()[1] == "unknown"


def test_probe_procs_lines_carry_pid_ppid_pgid_and_argv(tmp_path):
    result = _run_inner_script(cp.build_probe_command("swss"), _stub_dir(tmp_path, pebble=True), tmp_path)
    procs = cp._split_sections(result.stdout)["procs"]
    parsed = [cp.parse_proc_line(line) for line in procs]
    own = [p for p in parsed if p is not None and p.pid == os.getpid()]
    assert len(own) == 1
    assert own[0].ppid == os.getppid()
    assert own[0].pgid == os.getpgid(0)
    assert own[0].start_ticks == int(Path("/proc/self/stat").read_text().rsplit(") ", 1)[1].split()[19])
    assert own[0].argv == [a.decode() for a in Path("/proc/self/cmdline").read_bytes().split(b"\0")[:-1]]


def test_probe_procs_lines_all_parse_even_with_multiline_argv(tmp_path):
    sleeper = subprocess.Popen(["bash", "-c", "sleep 30; true", "line one\nline two"])
    try:
        result = _run_inner_script(cp.build_probe_command("swss"), _stub_dir(tmp_path, pebble=True), tmp_path)
    finally:
        sleeper.kill()
    # splitlines() as Ansible's stdout_lines does: it also splits on \x1c-\x1e
    procs = "\n".join(cp._split_sections(result.stdout)["procs"]).splitlines()
    assert [line for line in procs if cp.parse_proc_line(line) is None] == []
    entry = [cp.parse_proc_line(line) for line in procs if line.startswith("{} ".format(sleeper.pid))]
    assert entry[0].argv == ["bash", "-c", "sleep 30; true", "line one\nline two"]


def test_probe_reports_boot_time(tmp_path):
    result = _run_inner_script(cp.build_probe_command("swss"), _stub_dir(tmp_path, pebble=True), tmp_path)
    btime = result.stdout.split("@@btime@@\n", 1)[1].split("\n", 1)[0]
    expected = [line.split()[1] for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime ")]
    assert [btime] == expected


def test_probe_procs_leave_out_the_probe_itself(tmp_path):
    result = _run_inner_script(cp.build_probe_command("swss"), _stub_dir(tmp_path, pebble=True), tmp_path)
    procs = [cp.parse_proc_line(line) for line in cp._split_sections(result.stdout)["procs"]]
    # match the exact probe argv: other host processes (the shell running pytest) may contain marker text
    assert [p.pid for p in procs if p is not None and p.argv[:3] == ["bash", "-c", cp.PROBE_SCRIPT]] == []


@pytest.mark.parametrize("pebble, supervisorctl, expected", [
    (True, False, "pebble stop svc other\n"),
    (False, True, "supervisorctl stop grp:svc other\n"),
])
def test_control_strips_group_only_under_pebble(tmp_path, pebble, supervisorctl, expected):
    command = cp.build_control_command("swss", "stop", ["grp:svc", "other"])
    result = _run_inner_script(command, _stub_dir(tmp_path, pebble=pebble, supervisorctl=supervisorctl), tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "stub.log").read_text() == expected


@pytest.mark.parametrize("pebble, supervisorctl, expected", [
    (True, False, "pebble signal SIGHUP svc\n"),
    (False, True, "supervisorctl signal SIGHUP grp:svc\n"),
])
def test_signal_strips_group_only_under_pebble(tmp_path, pebble, supervisorctl, expected):
    command = cp.build_signal_command("swss", "SIGHUP", ["grp:svc"])
    result = _run_inner_script(command, _stub_dir(tmp_path, pebble=pebble, supervisorctl=supervisorctl), tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "stub.log").read_text() == expected


@pytest.mark.parametrize("signal", ["", "hup", "-9", "9", "HUP; reboot", "SIGHUP radvd", "HUP\n"])
def test_signal_rejects_what_is_not_a_signal_name(signal):
    with pytest.raises(ValueError):
        cp.build_signal_command("swss", signal, ["svc"])


def test_control_rejects_unknown_action():
    with pytest.raises(ValueError):
        cp.build_control_command("swss", "reread", ["svc"])


def test_probe_with_timeout_is_bounded():
    command = cp.build_probe_command("swss", timeout=60)
    assert shlex.split(command)[:5] == ["timeout", "60", "docker", "exec", "swss"]


@pytest.mark.parametrize("names", [[], ["-9"], ["--all"], ["svc\n"], ["svc\nreboot"]])
def test_control_rejects_names_that_are_not_programs(names):
    with pytest.raises(ValueError):
        cp.build_control_command("swss", "stop", names)
