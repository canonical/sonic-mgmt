"""Unit tests for tests/common/helpers/os_profile.py.

Run from the repo root with::

    python3 -m pytest --noconftest --confcutdir=tests/common/unit_tests \
        tests/common/unit_tests/helpers/unit_test_os_profile.py -v
"""
import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "helpers" / "os_profile.py"
_spec = importlib.util.spec_from_file_location("os_profile", _MODULE_PATH)
os_profile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(os_profile)

# /usr/lib/os-release of SONiC 202605.1174613 (Debian trixie)
DEBIAN_TRIXIE_OS_RELEASE = """\
PRETTY_NAME="Debian GNU/Linux 13 (trixie)"
NAME="Debian GNU/Linux"
VERSION_ID="13"
VERSION="13 (trixie)"
VERSION_CODENAME=trixie
DEBIAN_VERSION_FULL=13.5
ID=debian
HOME_URL="https://www.debian.org/"
SUPPORT_URL="https://www.debian.org/support"
BUG_REPORT_URL="https://bugs.debian.org/"
"""

# /usr/lib/os-release of SONiC 202605_resolute (Ubuntu 26.04)
UBUNTU_RESOLUTE_OS_RELEASE = """\
PRETTY_NAME="Ubuntu 26.04.1 LTS"
NAME="Ubuntu"
VERSION_ID="26.04"
VERSION="26.04.1 LTS (Resolute Raccoon)"
VERSION_CODENAME=resolute
ID=ubuntu
ID_LIKE=debian
HOME_URL="https://www.ubuntu.com/"
SUPPORT_URL="https://help.ubuntu.com/"
BUG_REPORT_URL="https://bugs.launchpad.net/ubuntu/"
PRIVACY_POLICY_URL="https://www.ubuntu.com/legal/terms-and-policies/privacy-policy"
UBUNTU_CODENAME=resolute
LOGO=ubuntu-logo
"""


def test_debian_base_is_own_codename():
    profile = os_profile.parse_os_profile(DEBIAN_TRIXIE_OS_RELEASE, "13.5\n")
    assert (profile.id, profile.version_id, profile.codename, profile.debian_base) == \
        ("debian", "13", "trixie", "trixie")


def test_ubuntu_debian_base_comes_from_debian_version():
    profile = os_profile.parse_os_profile(UBUNTU_RESOLUTE_OS_RELEASE, "forky/sid\n")
    assert (profile.id, profile.version_id, profile.codename, profile.debian_base) == \
        ("ubuntu", "26.04", "resolute", "forky")


@pytest.mark.parametrize("debian_version, base", [
    ("10.13", "buster"),
    ("11.11", "bullseye"),
    ("12.12", "bookworm"),
    ("13.5", "trixie"),
    ("14.1", "forky"),
    ("bookworm/sid", "bookworm"),
])
def test_ubuntu_debian_version_forms(debian_version, base):
    profile = os_profile.parse_os_profile(UBUNTU_RESOLUTE_OS_RELEASE, debian_version)
    assert profile.debian_base == base


def test_debian_without_codename_falls_back_to_debian_version():
    os_release = DEBIAN_TRIXIE_OS_RELEASE.replace("VERSION_CODENAME=trixie\n", "")
    assert os_profile.parse_os_profile(os_release, "12.12").debian_base == "bookworm"


@pytest.mark.parametrize("debian_version", ["", "unknown", "9.13", "sid"])
def test_unrecognised_debian_version_has_no_base(debian_version):
    profile = os_profile.parse_os_profile(UBUNTU_RESOLUTE_OS_RELEASE, debian_version)
    assert profile.debian_base is None
    assert profile.debian_base_at_least("buster") is False


@pytest.mark.parametrize("os_release, debian_version, name, expected", [
    (UBUNTU_RESOLUTE_OS_RELEASE, "forky/sid", "bookworm", True),
    (UBUNTU_RESOLUTE_OS_RELEASE, "forky/sid", "trixie", True),
    (UBUNTU_RESOLUTE_OS_RELEASE, "forky/sid", "duke", False),
    (DEBIAN_TRIXIE_OS_RELEASE, "13.5", "trixie", True),
    (DEBIAN_TRIXIE_OS_RELEASE, "13.5", "forky", False),
    (DEBIAN_TRIXIE_OS_RELEASE.replace("trixie", "bookworm"), "12.12", "trixie", False),
])
def test_debian_base_at_least(os_release, debian_version, name, expected):
    profile = os_profile.parse_os_profile(os_release, debian_version)
    assert profile.debian_base_at_least(name) is expected


def test_debian_base_at_least_rejects_unknown_name():
    profile = os_profile.parse_os_profile(DEBIAN_TRIXIE_OS_RELEASE, "13.5")
    with pytest.raises(ValueError):
        profile.debian_base_at_least("trusty")


def test_is_debian():
    assert os_profile.parse_os_profile(DEBIAN_TRIXIE_OS_RELEASE, "13.5").is_debian is True
    assert os_profile.parse_os_profile(UBUNTU_RESOLUTE_OS_RELEASE, "forky/sid").is_debian is False


def _run_read_script(tmp_path, os_release=None, debian_version=None):
    """Run the script os_profile_command() builds, against files in tmp_path."""
    import shlex
    import subprocess
    paths = {"os_release": tmp_path / "os-release", "debian_version": tmp_path / "debian_version"}
    for key, text in (("os_release", os_release), ("debian_version", debian_version)):
        if text is not None:
            paths[key].write_text(text)
    argv = shlex.split(os_profile.os_profile_command(os_release_path=str(paths["os_release"]),
                                                     debian_version_path=str(paths["debian_version"])))
    return subprocess.run(argv, capture_output=True, text=True)


def test_read_script_output_parses(tmp_path):
    result = _run_read_script(tmp_path, UBUNTU_RESOLUTE_OS_RELEASE, "forky/sid\n")
    assert result.returncode == 0
    assert os_profile.parse_os_profile_output(result.stdout).debian_base == "forky"


def test_read_script_needs_only_os_release(tmp_path):
    result = _run_read_script(tmp_path, DEBIAN_TRIXIE_OS_RELEASE)
    assert result.returncode == 0
    assert os_profile.parse_os_profile_output(result.stdout).debian_base == "trixie"


def test_read_script_fails_without_os_release(tmp_path):
    assert _run_read_script(tmp_path, debian_version="13.5\n").returncode != 0


def test_read_script_in_a_container():
    assert os_profile.os_profile_command("syncd").startswith("docker exec syncd bash -c ")
