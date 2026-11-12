"""Read and control the processes of a SONiC container under supervisord or pebble.

Kept free of Ansible imports so unit tests can load it directly.

Dockerfile-built containers run their programs under supervisord; rock-built
containers run them under pebble and ship no supervisorctl. Both backends can
run on the same switch, so the backend is detected per container, inside the
container, every time it is queried.
"""
import datetime
import json
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import List, Optional

import yaml

CONTROL_ACTIONS = ("start", "stop", "restart")
SUPERVISORD_STATES = ("RUNNING", "EXITED", "STOPPED", "FATAL", "BACKOFF", "STARTING")
# pebble 'current' -> supervisord state word
PEBBLE_STATES = {"active": "RUNNING", "backoff": "BACKOFF", "error": "EXITED", "inactive": "STOPPED"}
# Checked with fullmatch: "$" would also accept a trailing newline
_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:@-]*")
_SIGNAL_RE = re.compile(r"(SIG)?[A-Z][A-Z0-9]*")
_USER_HZ = 100
# Max deviation of a service's current-since minus its process start from the container's usual gap
_START_TOLERANCE = 0.1

# Bash builtins only: rocks differ in which tools they stage. No single quotes (wrapped in bash -c '...').
PROBE_SCRIPT = r"""
if pebble services >/dev/null 2>&1; then b=pebble
elif command -v supervisorctl >/dev/null 2>&1; then b=supervisord
else b=unknown; fi
echo @@backend@@
echo "$b"
f=/etc/supervisor/critical_processes
if [ -f "$f" ]; then
  echo @@critical@@; c=$(<"$f"); [ -n "$c" ] && printf "%s\n" "$c"
else echo @@critical-missing@@; fi
if [ "$b" = supervisord ]; then
  echo @@status@@
  supervisorctl status
elif [ "$b" = pebble ]; then
  echo @@services@@
  pebble services --format json
  echo
  echo @@plan@@
  pebble plan
  echo @@btime@@
  while read -r k v; do [ "$k" = btime ] && echo "$v"; done < /proc/stat
  echo @@procs@@
  printf -v us "\037"; printf -v rs "\002"; printf -v nl "\n"
  # PID 1 is not pebble when the container shares the host PID namespace: keep our own root only
  { mapfile -d "" -t p1 < /proc/1/cmdline; } 2>/dev/null
  [ "${p1[0]##*/}" = pebble ] && own=1 || own=0
  for d in /proc/[0-9]*; do
    [ "${d#/proc/}" = "$$" ] && continue
    [ "$own" = 1 ] || [ "$d/root/" -ef / ] || continue
    { s=$(<"$d/stat"); } 2>/dev/null || continue
    read -r -a f <<< "${s##*) }"
    [ "${f[1]}" = "$$" ] && continue
    { mapfile -d "" -t a < "$d/cmdline"; } 2>/dev/null || continue
    [ "${#a[@]}" -gt 0 ] || continue
    IFS=$us; j="${a[*]}"; unset IFS
    echo "${d#/proc/} ${f[1]} ${f[2]} ${f[19]} ${j//$nl/$rs}"
  done
  echo @@groups@@
  for g in /etc/supervisor/conf.d/*.conf; do [ -f "$g" ] && printf "%s\n" "$(<"$g")"; done
fi
echo @@end@@
"""

_CONTROL_SCRIPT = """
if pebble services >/dev/null 2>&1; then exec pebble {action} {bare}
else exec supervisorctl {action} {full}; fi
"""


@dataclass(frozen=True)
class ProcEntry:
    pid: int
    ppid: int
    pgid: int
    start_ticks: int
    argv: List[str]


def build_probe_command(container, timeout=None):
    """Return the shell command that dumps the process state of ``container``, killed after ``timeout`` s if given."""
    command = "docker exec {} bash -c '{}'".format(container, PROBE_SCRIPT)
    return "timeout {} {}".format(int(timeout), command) if timeout else command


def build_control_command(container, action, names):
    """Return the shell command that runs ``action`` on ``names`` under the container's backend.

    Names may carry a supervisord group prefix ('group:program'); pebble gets the bare program name.
    Unlike supervisorctl, `pebble stop` also stops the services that require the named ones.
    """
    if action not in CONTROL_ACTIONS:
        raise ValueError("unsupported action '{}'".format(action))
    return _backend_command(container, action, names)


def build_signal_command(container, signal, names):
    """Return the shell command that sends ``signal`` ('SIGHUP' or 'HUP') to ``names`` under the container's backend."""
    if not _SIGNAL_RE.fullmatch(signal):
        raise ValueError("unsupported signal '{}'".format(signal))
    return _backend_command(container, "signal " + signal, names)


def _backend_command(container, action, names):
    if not names:
        raise ValueError("no process names given")
    for name in names:
        if not _NAME_RE.fullmatch(name):
            raise ValueError("unsupported process name '{}'".format(name))
    script = _CONTROL_SCRIPT.format(action=action,
                                    bare=" ".join(name.split(":", 1)[-1] for name in names),
                                    full=" ".join(names))
    return "docker exec {} bash -c '{}'".format(container, script)


def parse_proc_line(line):
    """Parse one 'procs' line: '<pid> <ppid> <pgid> <start ticks> <argv joined by \\x1f, newlines as \\x02>'.

    Return None if the line is malformed.
    """
    fields = line.split(" ", 4)
    if len(fields) != 5:
        return None
    try:
        pid, ppid, pgid, start_ticks = (int(value) for value in fields[:4])
    except ValueError:
        return None
    argv = [arg.replace("\x02", "\n") for arg in fields[4].split("\x1f")]
    # programs that rewrite their title (redis) leave empty arguments behind
    while len(argv) > 1 and argv[-1] == "":
        argv.pop()
    return ProcEntry(pid=pid, ppid=ppid, pgid=pgid, start_ticks=start_ticks, argv=argv)


@dataclass
class ProcessInfo:
    name: str
    group: Optional[str]
    state: str
    pid: int = -1
    raw_state: str = ""
    pid_note: str = ""

    @property
    def full_name(self):
        return "{}:{}".format(self.group, self.name) if self.group else self.name


@dataclass
class ContainerProcesses:
    container: str
    backend: Optional[str] = None
    running: bool = False
    processes: List[ProcessInfo] = field(default_factory=list)
    critical_groups: List[str] = field(default_factory=list)
    critical_programs: List[str] = field(default_factory=list)
    critical_parse_ok: bool = True
    error: Optional[str] = None

    def get(self, name, bare=True):
        """Find a process by 'group:program', or also by bare program name when ``bare``.

        supervisorctl itself knows a grouped program only as 'group:program' (bare=False).
        """
        for process in self.processes:
            if process.full_name == name:
                return process
        for process in self.processes if bare else []:
            if process.name == name:
                return process
        return None

    def _add_error(self, message):
        self.error = message if self.error is None else "{}; {}".format(self.error, message)


def _split_sections(stdout):
    sections, current = {}, None
    for line in stdout.split("\n"):
        if line.startswith("@@") and line.endswith("@@") and len(line) > 4:
            current = line[2:-2]
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return sections


def _parse_critical(result, lines, allow_bare):
    """Collect the valid entries; any other line, blank ones included, clears critical_parse_ok."""
    lines = list(lines)
    # $(<file) drops trailing newlines, so a trailing blank line is the probe's own (an empty file)
    while lines and not lines[-1].strip():
        lines.pop()
    for line in lines:
        fields = line.strip().split(":")
        if len(fields) == 1 and allow_bare and fields[0].strip():
            result.critical_programs.append(fields[0].strip())
        elif len(fields) == 2 and fields[0].strip() == "group" and fields[1].strip():
            result.critical_groups.append(fields[1].strip())
        elif len(fields) == 2 and fields[0].strip() == "program" and fields[1].strip():
            result.critical_programs.append(fields[1].strip())
        else:
            result.critical_parse_ok = False


def _parse_supervisorctl_status(result, lines):
    for line in lines:
        if not line.strip():
            continue
        fields = re.split(r"\s+", line.strip(), maxsplit=2)
        if len(fields) < 2 or fields[1] not in SUPERVISORD_STATES:
            result._add_error("unrecognised supervisorctl status line: {}".format(line.strip()))
            continue
        group, _, name = fields[0].rpartition(":")
        pid = -1
        if fields[1] == "RUNNING":
            match = re.search(r"pid (\d+),", fields[2] if len(fields) > 2 else "")
            pid = int(match.group(1)) if match else -1
        result.processes.append(ProcessInfo(name=name, group=group or None, state=fields[1], pid=pid,
                                            raw_state=fields[1]))


def _parse_group_definitions(lines):
    """Map program -> group from the [group:X] sections of supervisord conf text."""
    groups, current, programs = {}, None, None
    for line in lines + ["[end]"]:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if current and programs is not None:
                for program in programs.split(","):
                    if program.strip():
                        groups[program.strip()] = current
            current = stripped[len("[group:"):-1] if stripped.startswith("[group:") else None
            programs = None
        elif current and stripped.startswith("programs") and "=" in stripped:
            programs = stripped.split("=", 1)[1]
        elif current and programs is not None and line[:1].isspace() and stripped:
            programs += stripped
    return groups


def _parse_since(value):
    if not isinstance(value, str):
        return None
    match = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)$", value or "")
    if not match:
        return None
    zone = "+00:00" if match.group(3) == "Z" else match.group(3)
    moment = datetime.datetime.fromisoformat(match.group(1) + zone).timestamp()
    return moment + float("0" + match.group(2)) if match.group(2) else moment


def _command_argv(command):
    if not isinstance(command, str):
        return []
    try:
        return [arg for arg in shlex.split(command) if arg not in ("[", "]")]
    except ValueError:
        return []


def _process_name(argv):
    return os.path.basename(argv[0]).split(":", 1)[0].split(" ", 1)[0] if argv and argv[0] else ""


def _usual_gap(gaps):
    """Median of the largest group of gaps within the tolerance of each other; None when no group is largest."""
    gaps = sorted(gaps)
    groups = [[g for g in gaps if abs(g - center) <= _START_TOLERANCE] for center in gaps]
    if not groups:
        return None
    best = max(len(group) for group in groups)
    centers = [gaps[i] for i, group in enumerate(groups) if len(group) == best]
    if max(centers) - min(centers) > _START_TOLERANCE:
        return None
    group = groups[gaps.index(centers[0])]
    return group[len(group) // 2]


def _resolve_pebble_pids(services, commands, procs, btime):
    """Map each active pebble service to the pid of the pebble child running it.

    Under each rule a service takes a child only if it is the service's only candidate and no other unresolved
    service lists it. A match by name is dropped when its start time disagrees with the container's usual gap,
    or when another unclaimed child fits that start time too; the start time then decides. Anything left is
    not guessed.
    """
    daemons = [p for p in procs if _process_name(p.argv) == "pebble" and p.argv[1:2] in (["run"], ["enter"])]
    if len(daemons) != 1:
        return {}, "pebble daemon not found in the process list" if not daemons else "several pebble daemons"
    children = {p.pid: p for p in procs if p.ppid == daemons[0].pid}
    active = sorted(name for name, service in services.items() if service.get("current") == "active")

    def start_gap(name, child):
        since = _parse_since(services[name].get("current-since"))
        return None if since is None or btime is None else since - (btime + child.start_ticks / _USER_HZ)

    def by_argv(name, child):
        return child.argv == _command_argv(commands.get(name))

    def by_shebang(name, child):
        command = _command_argv(commands.get(name))
        return bool(command) and len(child.argv) - len(command) in (1, 2) and child.argv[-len(command):] == command

    def by_name(name, child):
        return _process_name(child.argv) == name

    def assign(rule, resolved):
        free = [pid for pid in children if pid not in resolved.values()]
        candidates = {name: [pid for pid in free if rule(name, children[pid])]
                      for name in active if name not in resolved}
        matched = {}
        for name, pids in candidates.items():
            if len(pids) == 1 and not any(pids[0] in others for other, others in candidates.items() if other != name):
                matched[name] = pids[0]
        resolved.update(matched)
        return matched

    resolved = {}
    by_argv_match = assign(by_argv, resolved)
    assign(by_shebang, resolved)
    by_name_match = assign(by_name, resolved)
    # current-since is the process start, but btime lacks its fraction of a second and a clock step moves the
    # gap of whatever started before it, so the usual gap comes from the exact argv matches that agree
    usual_gap = _usual_gap(g for g in (start_gap(n, children[pid]) for n, pid in by_argv_match.items())
                           if g is not None)
    if usual_gap is not None:
        def by_start_time(name, child):
            gap = start_gap(name, child)
            return gap is not None and abs(gap - usual_gap) <= _START_TOLERANCE

        free = [child for pid, child in children.items() if pid not in resolved.values()]
        for name, pid in by_name_match.items():
            if not by_start_time(name, children[pid]) or any(by_start_time(name, child) for child in free):
                del resolved[name]
        assign(by_start_time, resolved)
    return resolved, "no unique pebble child matched by argv, shebang, name or start time"


def _parse_pebble(result, sections):
    try:
        _parse_pebble_sections(result, sections)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        result._add_error("unexpected pebble output: {!r}".format(error))
    result.processes.sort(key=lambda process: process.full_name)


def _parse_pebble_sections(result, sections):
    try:
        services = json.loads("\n".join(sections.get("services", [])))["services"]
    except (ValueError, KeyError, TypeError):
        result._add_error("cannot parse pebble services JSON")
        return
    try:
        plan = yaml.safe_load("\n".join(sections.get("plan", []))) or {}
        commands = {name: (spec or {}).get("command", "") for name, spec in (plan.get("services") or {}).items()}
    except (yaml.YAMLError, AttributeError):
        result._add_error("cannot parse pebble plan")
        commands = {}
    btime_lines = [line for line in sections.get("btime", []) if line.strip()]
    btime = int(btime_lines[0]) if btime_lines and btime_lines[0].strip().isdigit() else None
    procs = [p for p in map(parse_proc_line, sections.get("procs", [])) if p is not None]
    group_lines = sections.get("groups", [])
    groups = _parse_group_definitions(group_lines)
    if result.critical_groups and not any(line.strip() for line in group_lines):
        result._add_error("members of critical group(s) {} unknown: no supervisord conf rendered in the container"
                          .format(", ".join(result.critical_groups)))
    pids, note = _resolve_pebble_pids(services, commands, procs, btime)
    for name in sorted(services):
        raw_state = services[name].get("current", "")
        state = PEBBLE_STATES.get(raw_state)
        if state is None:
            result._add_error("unknown pebble state '{}' for service {}".format(raw_state, name))
            continue
        process = ProcessInfo(name=name, group=groups.get(name), state=state, raw_state=raw_state)
        if state == "RUNNING":
            process.pid = pids.get(name, -1)
            process.pid_note = "" if process.pid > 0 else note
        result.processes.append(process)
    # supervisord lists every configured program; a critical program pebble does not define has not started
    for name in result.critical_programs:
        if name not in services:
            result.processes.append(ProcessInfo(name=name, group=None, state="STOPPED", raw_state="undefined"))


def parse_probe_output(container, rc, stdout, allow_bare_critical=False):
    """Parse the output of build_probe_command(container), run with return code ``rc``.

    allow_bare_critical accepts critical_processes lines without a 'program:' prefix (201811 images).
    """
    result = ContainerProcesses(container=container)
    if rc != 0:
        result._add_error("container not running or docker exec failed (rc={})".format(rc))
        return result
    result.running = True
    sections = _split_sections(stdout or "")
    if "end" not in sections:
        result._add_error("probe output truncated")
    backend = (sections.get("backend") or [""])[0].strip()
    if backend not in ("supervisord", "pebble"):
        result._add_error("no supervisord or pebble in the container")
        return result
    result.backend = backend
    _parse_critical(result, sections.get("critical", []), allow_bare_critical)
    if backend == "supervisord":
        _parse_supervisorctl_status(result, sections.get("status", []))
    else:
        _parse_pebble(result, sections)
    return result


def critical_process_status(result, groups=None, programs=None, strict=True):
    """Summarise critical processes the way SonicHost.critical_process_status() reports them.

    groups/programs replace the container's own critical lists when given. With strict=False a malformed
    critical_processes line is skipped instead of failing the container, as all_critical_process_status() did.
    """
    groups = result.critical_groups if groups is None else groups
    programs = result.critical_programs if programs is None else programs
    status = {"status": True, "exited_critical_process": [], "running_critical_process": []}
    if strict and not result.critical_parse_ok:
        status["status"] = False
        return status
    if not result.running or result.error or not result.processes:
        status["status"] = False
    for process in result.processes:
        critical = process.full_name in programs or (process.group is not None and process.group in groups)
        if not critical:
            continue
        if process.state == "RUNNING":
            status["running_critical_process"].append(process.full_name)
        else:
            status["exited_critical_process"].append(process.full_name)
            status["status"] = False
    return status
