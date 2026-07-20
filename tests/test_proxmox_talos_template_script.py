import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.schemas import SUPPORTED_TALOS_VERSIONS, TalosConfig


PROJECT = Path(__file__).parents[1]
SCRIPT = PROJECT / "proxmox/create-talos-template.sh"
BASH = None if os.name == "nt" else shutil.which("bash")


def run_script(*args: str) -> subprocess.CompletedProcess[str]:
    if BASH is None:
        pytest.skip("bash ist fuer den Shellskript-Test nicht installiert")
    return subprocess.run(
        [BASH, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_talos_template_script_syntax_and_help():
    assert SCRIPT.is_file()
    if BASH is None:
        pytest.skip("bash ist fuer den Shellskript-Test nicht installiert")

    syntax = subprocess.run(
        [BASH, "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr

    help_result = run_script("--help")
    output = help_result.stdout + help_result.stderr
    assert help_result.returncode == 0
    for option in (
        "--vm-id",
        "--storage",
        "--bridge",
        "--talos-version",
        "--install-disk",
        "--image-sha256",
        "--yes",
    ):
        assert option in output


def test_talos_template_script_rejects_bad_inputs_before_host_actions():
    cases = (
        (("--yes",), "VM-ID"),
        (("--vm-id", "not-a-number", "--yes"), "VM-ID"),
        (("--vm-id", "9200", "--talos-version", "v1.13.5", "--yes"), "v1.13.6"),
        (("--vm-id", "9200", "--install-disk", "/dev/nvme0n1", "--yes"), "--install-disk"),
        (
            (
                "--vm-id",
                "9200",
                "--image-sha256",
                "0" * 64,
                "--yes",
            ),
            "--image-url",
        ),
        (
            (
                "--vm-id",
                "9200",
                "--image-url",
                "https://images.example.test/talos.raw.xz",
                "--yes",
            ),
            "--image-sha256",
        ),
    )
    for args, expected in cases:
        result = run_script(*args)
        assert result.returncode != 0
        assert expected in result.stdout + result.stderr


def test_talos_template_script_keeps_core_safety_guards():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "set -Eeuo pipefail" in source
    assert "umask 077" in source
    assert "--proto '=https'" in source
    assert "sha256sum" in source
    assert "pveversion qemu-img" in source
    assert "qemu-utils" not in source
    assert "/cluster/resources" in source
    assert source.count("assert_vm_id_is_free") >= 3
    assert re.search(r'(?m)^\s*qm template "\$VM_ID"\s*$', source)
    assert not re.search(r"(?m)^\s*qm destroy(?:\s|$)", source)
    assert not re.search(r'(?m)^\s*VM_ID=["\']?\d+', source)


def test_talos_template_script_pins_supported_nocloud_image():
    source = SCRIPT.read_text(encoding="utf-8")

    assert SUPPORTED_TALOS_VERSIONS == ("v1.13.6",)
    assert f'readonly SUPPORTED_TALOS_VERSION="{SUPPORTED_TALOS_VERSIONS[0]}"' in source
    assert TalosConfig().install_disk == "/dev/sda"
    assert (
        'readonly DEFAULT_SCHEMATIC_ID="376567988ad370138ad8b2698212367b8edcb69b5fd68c80be1f2ec7d603b4ba"'
        in source
    )
    assert (
        'readonly DEFAULT_IMAGE_SHA256="d46b9209f9aa9d96d8ee4439351687e2b4519c0d61df2fe974ee533a3ed9ef21"'
        in source
    )
    assert "nocloud-amd64.raw.xz" in source
    assert "xz --test" in source
    assert "virt-customize" not in source


def test_talos_template_script_builds_expected_proxmox_hardware():
    source = SCRIPT.read_text(encoding="utf-8")

    for fragment in (
        "--bios ovmf",
        "--machine q35",
        "--balloon 0",
        "--scsihw virtio-scsi-pci",
        "--agent 0",
        "--serial0 socket",
        "--vga serial0",
        'efitype=4m,pre-enrolled-keys=0',
        '--ide2 "${STORAGE}:cloudinit"',
    ):
        assert fragment in source

    assert not re.search(r"--scsihw\s+virtio-scsi-single", source)
    assert 'DISK_INTERFACE="scsi0"' in source
    assert 'DISK_INTERFACE="virtio0"' in source
    assert 'qm set "$VM_ID" "--${DISK_INTERFACE}"' in source
    assert 'qm set "$VM_ID" --boot "order=${DISK_INTERFACE}"' in source


def test_talos_template_script_is_documented_and_packaged():
    for name in ("README.md", "BUILDER.md"):
        content = (PROJECT / name).read_text(encoding="utf-8")
        assert "proxmox/create-talos-template.sh" in content
        assert "--install-disk /dev/sda" in content

    dockerfile = (PROJECT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY proxmox proxmox" in dockerfile

    attributes = (PROJECT / ".gitattributes").read_text(encoding="utf-8")
    assert "proxmox/*.sh text eol=lf" in attributes
