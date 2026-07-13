import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT = Path(__file__).parents[1]
SCRIPT = PROJECT / "proxmox/create-template.sh"
# Windows kann einen WindowsApps-Alias namens bash.exe anbieten, obwohl Bash
# nur ueber eine separat zu startende WSL-VM erreichbar ist. Der eigentliche
# Laufzeittest gehoert deshalb in die Linux-/Docker-CI; die statischen
# Sicherheitspruefungen unten laufen weiterhin auf allen Plattformen.
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


def test_template_script_syntax_and_help():
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
    for option in ("--vm-id", "--storage", "--bridge", "--ubuntu-release", "--yes"):
        assert option in output


def test_template_script_rejects_invalid_vm_ids_before_host_actions():
    for args in (("--yes",), ("--vm-id", "not-a-number", "--yes")):
        result = run_script(*args)
        assert result.returncode != 0
        assert "VM-ID" in result.stdout + result.stderr


def test_template_script_keeps_core_safety_guards():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "set -Eeuo pipefail" in source
    assert "--proto '=https'" in source
    assert "sha256sum" in source
    assert "/cluster/resources" in source
    assert re.search(r'(?m)^\s*qm template "\$VM_ID"\s*$', source)
    assert not re.search(r"(?m)^\s*qm destroy(?:\s|$)", source)
    assert not re.search(r'(?m)^\s*VM_ID=["\']?\d+', source)


def test_template_script_accepts_both_proxmox_agent_serializations():
    source = SCRIPT.read_text(encoding="utf-8")
    declaration = re.search(r"(?m)^readonly AGENT_CONFIG_PATTERN='([^']+)'$", source)
    assert declaration, "Agent-Konfigurationsmuster fehlt"

    pattern = declaration.group(1)
    assert re.search(pattern, "agent: 1,fstrim_cloned_disks=1")
    assert re.search(pattern, "agent: enabled=1,fstrim_cloned_disks=1")
    assert not re.search(pattern, "agent: 0,fstrim_cloned_disks=1")
    assert not re.search(pattern, "agent: enabled=0,fstrim_cloned_disks=1")


def test_template_script_is_documented_as_proxmox_host_tool():
    for name in ("README.md", "BUILDER.md"):
        content = (PROJECT / name).read_text(encoding="utf-8")
        assert "proxmox/create-template.sh" in content
        assert "--vm-id" in content
        assert "Proxmox-Host" in content

    dockerfile = (PROJECT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY proxmox proxmox" in dockerfile

    attributes = (PROJECT / ".gitattributes").read_text(encoding="utf-8")
    assert "proxmox/*.sh text eol=lf" in attributes
