"""Identify the operating system a SONiC host or container is built on.

Kept dependency-free (stdlib only) so unit tests can load it without Ansible.

Ubuntu-based SONiC reports its own codename (e.g. 'resolute') in os-release, so
checks written against Debian codenames must compare the Debian base instead.
Ubuntu records that base in /etc/debian_version (e.g. 'forky/sid').
"""
from dataclasses import dataclass
from typing import Optional

# Debian releases in order; a later entry is newer.
DEBIAN_CODENAMES = ("buster", "bullseye", "bookworm", "trixie", "forky", "duke")
_DEBIAN_MAJOR_TO_CODENAME = {"10": "buster", "11": "bullseye", "12": "bookworm", "13": "trixie", "14": "forky"}


@dataclass(frozen=True)
class OsProfile:
    id: str
    version_id: str
    codename: str
    debian_base: Optional[str]

    @property
    def is_debian(self):
        return self.id == "debian"

    def debian_base_at_least(self, name):
        """Return True if the Debian base is ``name`` or newer; False if the base is unknown."""
        if name not in DEBIAN_CODENAMES:
            raise ValueError("unknown Debian codename '{}'".format(name))
        if self.debian_base is None:
            return False
        return DEBIAN_CODENAMES.index(self.debian_base) >= DEBIAN_CODENAMES.index(name)


def _parse_os_release(text):
    fields = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key] = value.strip().strip('"').strip("'")
    return fields


def _debian_base_from_debian_version(text):
    value = text.strip()
    if "/" in value:
        name = value.split("/", 1)[0]
        return name if name in DEBIAN_CODENAMES else None
    return _DEBIAN_MAJOR_TO_CODENAME.get(value.split(".", 1)[0])


def parse_os_profile(os_release_text, debian_version_text):
    """Build an OsProfile from the contents of /etc/os-release and /etc/debian_version."""
    fields = _parse_os_release(os_release_text)
    os_id = fields.get("ID", "")
    codename = fields.get("VERSION_CODENAME", "")
    if os_id == "debian" and codename in DEBIAN_CODENAMES:
        debian_base = codename
    else:
        debian_base = _debian_base_from_debian_version(debian_version_text)
    return OsProfile(id=os_id, version_id=fields.get("VERSION_ID", ""), codename=codename,
                     debian_base=debian_base)


# Fails when os-release is unreadable; debian_version is optional (os-release alone identifies Debian)
_READ_SCRIPT = ('[ -r {os_release} ] || exit 1; printf "%s\\n" "$(<{os_release})"; echo @@debian_version@@; '
                'if [ -r {debian_version} ]; then printf "%s\\n" "$(<{debian_version})"; fi')


def os_profile_command(container=None, os_release_path="/etc/os-release", debian_version_path="/etc/debian_version"):
    """Return the shell command that prints the OS files of the host, or of ``container`` when given."""
    command = "bash -c '{}'".format(_READ_SCRIPT.format(os_release=os_release_path,
                                                        debian_version=debian_version_path))
    return "docker exec {} {}".format(container, command) if container else command


def parse_os_profile_output(stdout):
    """Build an OsProfile from the output of os_profile_command()."""
    os_release, _, debian_version = stdout.partition("@@debian_version@@")
    return parse_os_profile(os_release, debian_version)
