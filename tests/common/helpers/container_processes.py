"""Read and control the programs of a SONiC container.

Kept free of Ansible imports so unit tests can load it directly.
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional

CONTROL_ACTIONS = ("start", "stop", "restart")
SUPERVISORD_STATES = ("RUNNING", "EXITED", "STOPPED", "FATAL", "BACKOFF", "STARTING")
# Checked with fullmatch: "$" would also accept a trailing newline
_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:@-]*")
_SIGNAL_RE = re.compile(r"(SIG)?[A-Z][A-Z0-9]*")

# No single quotes (wrapped in bash -c '...').
PROBE_SCRIPT = r"""
if command -v supervisorctl >/dev/null 2>&1; then b=supervisord
else b=unknown; fi
echo @@backend@@
echo "$b"
if [ "$b" = supervisord ]; then
  echo @@status@@
  supervisorctl status
fi
echo @@end@@
"""


def build_probe_command(container):
    """Return the shell command that dumps the process state of ``container``."""
    return "docker exec {} bash -c '{}'".format(container, PROBE_SCRIPT)


def build_control_command(container, action, names):
    """Return the shell command that runs ``action`` on ``names``.

    Names may carry a supervisord group prefix ('group:program').
    """
    if action not in CONTROL_ACTIONS:
        raise ValueError("unsupported action '{}'".format(action))
    return _backend_command(container, action, names)


def build_signal_command(container, signal, names):
    """Return the shell command that sends ``signal`` ('SIGHUP' or 'HUP') to ``names``."""
    if not _SIGNAL_RE.fullmatch(signal):
        raise ValueError("unsupported signal '{}'".format(signal))
    return _backend_command(container, "signal " + signal, names)


def _backend_command(container, action, names):
    if not names:
        raise ValueError("no process names given")
    for name in names:
        if not _NAME_RE.fullmatch(name):
            raise ValueError("unsupported process name '{}'".format(name))
    return "docker exec {} supervisorctl {} {}".format(container, action, " ".join(names))


@dataclass
class ProcessInfo:
    name: str
    group: Optional[str]
    state: str
    pid: int = -1

    @property
    def full_name(self):
        return "{}:{}".format(self.group, self.name) if self.group else self.name


@dataclass
class ContainerProcesses:
    container: str
    backend: Optional[str] = None
    running: bool = False
    processes: List[ProcessInfo] = field(default_factory=list)
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
        result.processes.append(ProcessInfo(name=name, group=group or None, state=fields[1], pid=pid))


def parse_probe_output(container, rc, stdout):
    """Parse the output of build_probe_command(container), run with return code ``rc``."""
    result = ContainerProcesses(container=container)
    if rc != 0:
        result._add_error("container not running or docker exec failed (rc={})".format(rc))
        return result
    result.running = True
    sections = _split_sections(stdout or "")
    if "end" not in sections:
        result._add_error("probe output truncated")
    backend = (sections.get("backend") or [""])[0].strip()
    if backend != "supervisord":
        result._add_error("no supervisord in the container")
        return result
    result.backend = backend
    _parse_supervisorctl_status(result, sections.get("status", []))
    return result
