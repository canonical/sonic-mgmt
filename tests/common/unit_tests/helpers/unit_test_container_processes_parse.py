"""Parse container probe output captured from SONiC vs images.

Run from the repo root with::

    python3 -m pytest --noconftest --confcutdir=tests/common/unit_tests \
        tests/common/unit_tests/helpers/unit_test_container_processes_parse.py -v

data/container_processes/rock-7a081af43c holds the probe output of every running container on the rock
build of sonic-buildimage 202605_resolute 7a081af43c, captured with build_probe_command() on a VM without
configuration; rock-7a081af43c-t0 holds the same after deploy-mg of vms-kvm-t0.
"""
import datetime
import importlib.util
import json
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "helpers" / "container_processes.py"
_spec = importlib.util.spec_from_file_location("container_processes", _MODULE_PATH)
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)

ROCK = Path(__file__).resolve().parent / "data" / "container_processes" / "rock-7a081af43c"
ROCK_T0 = ROCK.parent / "rock-7a081af43c-t0"
ROCK_PEBBLE = sorted(p.stem for p in ROCK.glob("*.txt") if "@@backend@@\npebble\n" in p.read_text())
ALL_CAPTURES = [(d, p.stem) for d in (ROCK, ROCK_T0) for p in sorted(d.glob("*.txt"))]


def _probe(name, directory=ROCK):
    return (directory / "{}.txt".format(name)).read_text()


def _parse(name, rc=0, text=None):
    return cp.parse_probe_output(name, rc, _probe(name) if text is None else text)


def _pid_of(text, argv0):
    procs = text.split("\n@@procs@@\n", 1)[1].split("\n@@groups@@", 1)[0]
    pids = [entry.pid for entry in map(cp.parse_proc_line, procs.split("\n"))
            if entry is not None and entry.argv[0].strip() == argv0]
    assert len(pids) == 1, pids
    return pids[0]


def test_supervisord_container():
    result = _parse("syncd")
    assert (result.backend, result.running, result.error) == ("supervisord", True, None)
    assert result.critical_programs == ["syncd"] and result.critical_groups == []
    assert result.get("syncd").state == "RUNNING" and result.get("syncd").pid == 25
    assert result.get("start").state == "EXITED" and result.get("start").pid == -1


def test_pebble_states_map_to_supervisord_words():
    result = _parse("swss")
    assert (result.backend, result.running, result.error) == ("pebble", True, None)
    assert result.get("orchagent").state == "RUNNING"
    assert result.get("orchagent").raw_state == "active"
    assert result.get("start").state == "STOPPED"
    assert result.get("start").raw_state == "inactive"


@pytest.mark.parametrize("container, service, argv0", [
    ("swss", "orchagent", "/usr/bin/orchagent"),          # orchagent.sh execs orchagent
    ("swss", "portsyncd", "/usr/bin/portsyncd"),          # argv equals the command
    ("bgp", "bgpcfgd", "/usr/bin/python3"),               # script run through its shebang
    ("lldp", "lldpd", "lldpd: monitor."),                 # process retitles itself
    ("gnmi", "gnmi-native", "/usr/sbin/telemetry"),       # wrapper execs a differently named binary
    ("gnmi", "dialout", "/usr/sbin/dialout_client_cli"),
    ("mgmt-framework", "rest-server", "/usr/sbin/rest_server"),
])
def test_pebble_pid_resolution(container, service, argv0):
    text = _probe(container)
    if service == "bgpcfgd":
        procs = text.split("\n@@procs@@\n", 1)[1].split("\n@@groups@@", 1)[0].split("\n")
        expected = [e.pid for e in map(cp.parse_proc_line, procs) if e and e.argv[1:] == ["/usr/bin/bgpcfgd"]][0]
    else:
        expected = _pid_of(text, argv0)
    assert _parse(container).get(service).pid == expected


def test_pebble_pid_resolution_tells_redis_instances_apart():
    text, result = _probe("database"), _parse("database")
    procs = text.split("\n@@procs@@\n", 1)[1].split("\n@@groups@@", 1)[0].split("\n")
    by_port = {e.argv[0].rsplit(":", 1)[1]: e.pid for e in map(cp.parse_proc_line, procs)
               if e and e.argv[0].startswith("/usr/bin/redis-server")}
    assert result.get("redis").pid == by_port["6379"]
    assert result.get("redis_bmp").pid == by_port["6400"]


@pytest.mark.parametrize("directory, container", ALL_CAPTURES)
def test_every_running_pebble_service_gets_its_own_pid(directory, container):
    result = cp.parse_probe_output(container, 0, _probe(container, directory))
    running = [p for p in result.processes if p.state == "RUNNING"]
    unresolved = [(p.name, p.pid_note) for p in running if p.pid <= 0]
    assert unresolved == []
    pids = [p.pid for p in running]
    assert len(pids) == len(set(pids))


# The capture VM ran without a minigraph: no VLAN, so radvd is not started; no gearbox, so gbsyncdmgrd exits.
NOT_RUNNING_WITHOUT_CONFIG = {"radv": ["radvd"], "gbsyncd": ["gbsyncdmgrd"]}


@pytest.mark.parametrize("container", sorted(p.stem for p in ROCK.glob("*.txt")
                                             if p.stem not in NOT_RUNNING_WITHOUT_CONFIG))
def test_every_critical_entry_resolves_on_a_healthy_switch(container):
    result = _parse(container)
    status = cp.critical_process_status(result)
    assert status["status"] is True, (status, result.error)
    assert status["exited_critical_process"] == []
    for name in result.critical_programs:
        assert result.get(name) is not None, name


@pytest.mark.parametrize("container", sorted(NOT_RUNNING_WITHOUT_CONFIG))
def test_critical_program_not_started_is_reported(container):
    status = cp.critical_process_status(_parse(container))
    assert (status["status"], status["exited_critical_process"]) == (False, NOT_RUNNING_WITHOUT_CONFIG[container])


def test_pebble_critical_program_without_a_service_counts_as_stopped():
    process = _parse("radv").get("radvd")
    assert (process.state, process.raw_state, process.pid) == ("STOPPED", "undefined", -1)


def test_pebble_groups_come_from_rendered_supervisord_conf():
    text = _probe("dhcp_relay").replace(
        "[program:dhcprelayd]", "[group:dhcp-relay]\nprograms=dhcprelayd\n\n[program:dhcprelayd]")
    result = _parse("dhcp_relay", text=text)
    process = result.get("dhcp-relay:dhcprelayd")
    assert (process.name, process.group, process.full_name) == ("dhcprelayd", "dhcp-relay", "dhcp-relay:dhcprelayd")
    assert result.get("dhcprelayd") is process
    assert cp.critical_process_status(result)["running_critical_process"] == ["dhcp-relay:dhcprelayd"]


@pytest.mark.parametrize("container", sorted(p.stem for p in ROCK_T0.glob("*.txt")))
def test_every_critical_entry_resolves_on_the_t0_testbed(container):
    status = cp.critical_process_status(cp.parse_probe_output(container, 0, _probe(container, ROCK_T0)))
    if container == "gbsyncd":  # vs has no gearbox
        assert status["exited_critical_process"] == ["gbsyncdmgrd"]
    else:
        assert (status["status"], status["exited_critical_process"]) == (True, [])


def test_pebble_groups_on_the_t0_testbed():
    result = cp.parse_probe_output("dhcp_relay", 0, _probe("dhcp_relay", ROCK_T0))
    grouped = sorted(p.full_name for p in result.processes if p.group)
    assert grouped == ["dhcp-relay:dhcp6relay", "dhcp-relay:dhcprelayd", "dhcpmon:dhcpmon-Vlan1000"]
    assert cp.critical_process_status(result)["running_critical_process"] == ["dhcp-relay:dhcp6relay",
                                                                              "dhcp-relay:dhcprelayd"]


def test_pebble_group_absent_from_rendered_conf_has_no_members():
    result = _parse("dhcp_relay")
    assert result.critical_groups == ["dhcp-relay"]
    assert result.get("dhcprelayd").group is None
    assert cp.critical_process_status(result)["status"] is True


def test_pebble_group_without_any_rendered_conf_fails():
    text = _probe("dhcp_relay")
    head, tail = text.split("\n@@groups@@\n", 1)
    result = _parse("dhcp_relay", text=head + "\n@@groups@@\n@@end@@\n")
    status = cp.critical_process_status(result)
    assert status["status"] is False
    assert "dhcp-relay" in result.error


def test_critical_process_not_running_is_reported():
    text = _probe("syncd").replace("syncd                            RUNNING   pid 25, uptime",
                                   "syncd                            EXITED    Nov 10 08:50 AM")
    status = cp.critical_process_status(_parse("syncd", text=text))
    assert status == {"status": False, "exited_critical_process": ["syncd"], "running_critical_process": []}


def test_pebble_critical_service_in_error_is_reported():
    text = _probe("swss").replace('"orchagent":{"name":"orchagent","startup":"disabled","current":"active"',
                                  '"orchagent":{"name":"orchagent","startup":"disabled","current":"error"')
    status = cp.critical_process_status(_parse("swss", text=text))
    assert status["status"] is False
    assert status["exited_critical_process"] == ["orchagent"]


def test_unrecognised_supervisorctl_line_marks_container_unhealthy():
    text = _probe("syncd").replace("@@status@@\n", "@@status@@\nunix:///var/run/supervisor.sock no such file\n")
    result = _parse("syncd", text=text)
    assert result.error is not None
    assert cp.critical_process_status(result)["status"] is False


def test_missing_critical_file_means_no_critical_processes():
    text = _probe("syncd").replace("@@critical@@\nprogram:syncd\n", "@@critical-missing@@\n")
    result = _parse("syncd", text=text)
    assert (result.critical_programs, result.critical_groups, result.critical_parse_ok) == ([], [], True)
    assert cp.critical_process_status(result) == {"status": True, "exited_critical_process": [],
                                                  "running_critical_process": []}


def test_malformed_critical_line_fails_parse():
    text = _probe("syncd").replace("program:syncd\n", "syncd\n")
    assert _parse("syncd", text=text).critical_parse_ok is False


def test_bare_critical_names_are_accepted_when_allowed():
    text = _probe("syncd").replace("program:syncd\n", "syncd\n")
    result = cp.parse_probe_output("syncd", 0, text, allow_bare_critical=True)
    assert (result.critical_parse_ok, result.critical_programs) == (True, ["syncd"])


def test_container_not_running():
    result = cp.parse_probe_output("swss", 1, "")
    assert (result.running, result.backend, result.processes) == (False, None, [])
    assert cp.critical_process_status(result)["status"] is False


def test_truncated_output_is_an_error():
    result = _parse("swss", text=_probe("swss").replace("@@end@@\n", ""))
    assert result.error is not None
    assert cp.critical_process_status(result)["status"] is False


def test_unknown_backend_is_an_error():
    result = _parse("syncd", text="@@backend@@\nunknown\n@@critical-missing@@\n@@end@@\n")
    assert (result.backend, result.error is not None) == (None, True)


def test_malformed_pebble_json_is_an_error():
    text = _probe("swss")
    head, rest = text.split("\n@@services@@\n", 1)
    result = _parse("swss", text=head + "\n@@services@@\n{not json\n@@plan@@\n" + rest.split("\n@@plan@@\n", 1)[1])
    assert "services" in result.error


def test_critical_status_with_explicit_lists():
    status = cp.critical_process_status(_parse("swss"), groups=[], programs=["orchagent", "no-such-program"])
    assert status == {"status": True, "exited_critical_process": [], "running_critical_process": ["orchagent"]}


@pytest.mark.parametrize("shift", [-1, 1])
def test_start_time_rule_does_not_depend_on_btime_rounding(shift):
    # /proc/stat btime drops the fraction of a second, which differs from boot to boot
    text = _probe("database")
    btime = int(text.split("\n@@btime@@\n", 1)[1].split("\n", 1)[0])
    result = _parse("database", text=text.replace("\n@@btime@@\n{}\n".format(btime),
                                                  "\n@@btime@@\n{}\n".format(btime + shift)))
    assert (result.get("redis").pid, result.get("redis_bmp").pid) == \
        (_parse("database").get("redis").pid, _parse("database").get("redis_bmp").pid)
    assert result.get("redis").pid > 0


def _synthetic(services, procs, btime=1000):
    """Probe output for a pebble container: services {name: (command, since)}, procs [(pid, argv, start)].

    Times are seconds after btime; pebble is pid 1 and every proc is its direct child.
    """
    def since(t):
        return datetime.datetime.fromtimestamp(btime + t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    svc = {n: {"name": n, "startup": "enabled", "current": "active", "current-since": since(s)}
           for n, (_, s) in services.items()}
    plan = "services:\n" + "".join("    {}:\n        command: {}\n".format(n, c) for n, (c, _) in services.items())
    lines = ["1 0 1 0 /usr/bin/pebble\x1fenter"]
    lines += ["{} 1 {} {} {}".format(pid, pid, int(start * 100), "\x1f".join(argv)) for pid, argv, start in procs]
    return ("@@backend@@\npebble\n@@critical-missing@@\n@@services@@\n{}\n@@plan@@\n{}@@btime@@\n{}\n"
            "@@procs@@\n{}\n@@groups@@\n@@end@@\n").format(json.dumps({"services": svc}), plan, btime, "\n".join(lines))


ANCHORS = {"rsyslogd": ("/usr/sbin/rsyslogd -n", 1.0), "listener": ("/usr/bin/listener --pebble", 2.0)}
ANCHOR_PROCS = [(10, ["/usr/sbin/rsyslogd", "-n"], 1.0), (11, ["/usr/bin/listener", "--pebble"], 2.0)]


def _pids(text):
    return {p.name: p.pid for p in cp.parse_probe_output("ctr", 0, text).processes}


def test_name_match_that_contradicts_the_start_time_is_dropped():
    # a.sh execs a binary also named b; b's own process has retitled itself
    services = dict(ANCHORS, a=("/usr/bin/a.sh", 10.0), b=("/usr/bin/b --x", 20.0))
    procs = ANCHOR_PROCS + [(20, ["/usr/bin/b", "--y"], 10.0), (30, ["b-daemon"], 20.0)]
    assert _pids(_synthetic(services, procs)) == {"rsyslogd": 10, "listener": 11, "a": 20, "b": 30}


def test_same_named_orphan_does_not_take_the_service():
    # a reparented `b -r` child belongs to no service; b's own process has retitled itself
    services = dict(ANCHORS, b=("/usr/bin/b", 20.0))
    procs = ANCHOR_PROCS + [(30, ["worker"], 20.0), (40, ["/usr/bin/b", "-r"], 25.0)]
    assert _pids(_synthetic(services, procs))["b"] == 30


def test_child_that_is_also_another_services_candidate_is_not_assigned():
    services = dict(ANCHORS, x=("/usr/bin/x.sh", 10.0), y=("/usr/bin/y.sh", 10.05))
    procs = ANCHOR_PROCS + [(30, ["one"], 10.05), (40, ["two"], 9.93)]
    pids = _pids(_synthetic(services, procs))
    assert (pids["x"], pids["y"]) == (-1, -1)


def test_unresolved_running_service_has_no_pid_and_says_why():
    services = dict(ANCHORS, lost=("/usr/bin/lost.sh", 30.0))
    procs = ANCHOR_PROCS + [(50, ["something-else"], 40.0)]
    process = cp.parse_probe_output("ctr", 0, _synthetic(services, procs)).get("lost")
    assert (process.state, process.pid) == ("RUNNING", -1)
    assert process.pid_note


def test_batch_status_skips_malformed_critical_lines_like_before():
    # SonicHost.critical_group_process() skipped them; get_critical_group_and_process_lists() failed on them
    text = _probe("syncd").replace("@@critical@@\nprogram:syncd\n", "@@critical@@\nbogus line\nprogram:syncd\n")
    result = _parse("syncd", text=text)
    assert cp.critical_process_status(result)["status"] is False
    assert cp.critical_process_status(result, strict=False) == {
        "status": True, "exited_critical_process": [], "running_critical_process": ["syncd"]}


def test_blank_line_inside_critical_processes_is_malformed():
    text = _probe("syncd").replace("@@critical@@\nprogram:syncd\n", "@@critical@@\nprogram:syncd\n\nprogram:x\n")
    assert _parse("syncd", text=text).critical_parse_ok is False


def test_exact_lookup_needs_the_group_prefix():
    result = cp.parse_probe_output("dhcp_relay", 0, _probe("dhcp_relay", ROCK_T0))
    assert result.get("dhcp6relay", bare=False) is None
    assert result.get("dhcp-relay:dhcp6relay", bare=False).name == "dhcp6relay"


def test_pebble_processes_are_ordered_like_supervisorctl():
    names = [p.full_name for p in cp.parse_probe_output("dhcp_relay", 0, _probe("dhcp_relay", ROCK_T0)).processes]
    assert names == sorted(names)


@pytest.mark.parametrize("services", ['{"services": null}', '{"services": []}', '{"services": {"svc": "x"}}'])
def test_unexpected_pebble_json_shape_is_an_error(services):
    text = _probe("swss")
    head, rest = text.split("\n@@services@@\n", 1)
    plan_onwards = rest.split("\n@@plan@@\n", 1)[1]
    result = _parse("swss", text=head + "\n@@services@@\n" + services + "\n@@plan@@\n" + plan_onwards)
    assert result.error is not None


def test_unexpected_pebble_plan_shape_keeps_the_service():
    text = _probe("swss")
    head, rest = text.split("\n@@plan@@\n", 1)
    result = _parse("swss", text=head + "\n@@plan@@\nservices:\n    orchagent:\n        command: [a, b]\n@@btime@@\n"
                    + rest.split("\n@@btime@@\n", 1)[1])
    assert result.get("orchagent").state == "RUNNING"


def test_non_string_current_since_counts_as_missing():
    text = _probe("swss").replace('"current-since":"', '"current-since":5,"x":"', 1)
    result = _parse("swss", text=text)
    assert result.error is None and result.get("buffermgrd").state == "RUNNING"


def test_argv_match_keeps_its_pid_after_a_clock_step():
    # svc restarted on its own after the clock was stepped 2 s: its gap differs from the anchors' but argv is exact
    services = dict(ANCHORS, svc=("/usr/bin/svc --flag", 52.0))
    procs = ANCHOR_PROCS + [(60, ["/usr/bin/svc", "--flag"], 50.0)]
    assert _pids(_synthetic(services, procs))["svc"] == 60


def test_disagreeing_anchors_give_no_reference_gap():
    # rsyslogd restarted after a 2 s clock step, listener not: no usual gap, so the start-time rule stays out
    services = {"rsyslogd": ("/usr/sbin/rsyslogd -n", 52.0), "listener": ("/usr/bin/listener --pebble", 2.0),
                "native": ("/usr/bin/native.sh", 3.0)}
    procs = [(10, ["/usr/sbin/rsyslogd", "-n"], 50.0), (11, ["/usr/bin/listener", "--pebble"], 2.0),
             (12, ["/usr/sbin/telemetry"], 3.0)]
    assert _pids(_synthetic(services, procs)) == {"rsyslogd": 10, "listener": 11, "native": -1}


def test_name_match_with_another_child_at_the_same_start_time_is_dropped():
    # an orphan `b -r` started 40 ms after b, whose own process has retitled itself
    services = dict(ANCHORS, b=("/usr/bin/b", 20.0))
    procs = ANCHOR_PROCS + [(30, ["worker"], 20.0), (40, ["/usr/bin/b", "-r"], 20.04)]
    assert _pids(_synthetic(services, procs))["b"] == -1
