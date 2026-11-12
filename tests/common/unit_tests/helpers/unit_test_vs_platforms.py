"""The arm64 vs image reports ONIE's qemu platform name; vs checks must accept it as they accept the x86 name.

Run from the repo root with::

    python3 -m pytest --noconftest --confcutdir=tests/common/unit_tests \
        tests/common/unit_tests/helpers/unit_test_vs_platforms.py -v
"""
import ast
import importlib.util
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[3]
ARM64_VS = "arm64-qemu_armv8a-r0"

_spec = importlib.util.spec_from_file_location("constants", TESTS / "common" / "constants.py")
constants = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(constants)


def _load(path, name):
    tree = ast.parse(path.read_text())
    func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = {"KVM_PLATFORMS": constants.KVM_PLATFORMS}
    exec(compile(ast.Module(body=[func], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


class FakeDut:
    def __init__(self, platform):
        self.facts = {"platform": platform}


def test_kvm_platforms_lists_both_vs_names():
    assert constants.KVM_PLATFORMS == (constants.KVM_PLATFORM, ARM64_VS)


@pytest.mark.parametrize("platform, expected", [
    ("x86_64-kvm_x86_64-r0", True),
    (ARM64_VS, True),
    ("x86_64-mlnx_msn2700-r0", False),
    ("arm64-nvda_bf-9009d3b600cvaa", False),
])
def test_is_virtual_platform(platform, expected):
    is_virtual_platform = _load(TESTS / "common" / "helpers" / "dut_utils.py", "is_virtual_platform")
    assert is_virtual_platform(FakeDut(platform)) is expected


@pytest.mark.parametrize("platform, expected", [
    ("x86_64-kvm_x86_64-r0", True),
    ("x86_64-kvm_x86_64_4_asic-r0", True),
    (ARM64_VS, True),
    ("x86_64-arista_7050_qx32", False),
])
def test_macsec_virtual_switch_platform(platform, expected):
    is_virtual_switch_platform = _load(TESTS / "common" / "macsec" / "macsec_platform_helper.py",
                                       "is_virtual_switch_platform")
    assert is_virtual_switch_platform(platform) is expected
