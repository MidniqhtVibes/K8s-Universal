import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT = Path(__file__).parents[1]
SCRIPT = PROJECT / "proxmox/create-orchestrator-vm.sh"
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


def test_orchestrator_vm_script_syntax_and_help():
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
        "--ubuntu-release",
        "--repository-ref",
        "--ssh-key-file",
        "--readiness-timeout",
        "--install-dependencies",
        "--yes",
    ):
        assert option in output


def test_orchestrator_vm_script_rejects_bad_inputs_before_host_actions():
    cases = (
        (("--yes",), "VM-ID"),
        (("--vm-id", "not-a-number", "--yes"), "VM-ID"),
        (("--vm-id", "99", "--yes"), "VM-ID"),
        (
            (
                "--vm-id",
                "9300",
                "--repository-ref",
                "../main",
                "--yes",
            ),
            "--repository-ref",
        ),
        (
            (
                "--vm-id",
                "9300",
                "--image-url",
                "https://images.example.test/ubuntu.img",
                "--yes",
            ),
            "--image-sha256",
        ),
        (
            ("--vm-id", "9300", "--readiness-timeout", "10", "--yes"),
            "--readiness-timeout",
        ),
    )

    for args, expected in cases:
        result = run_script(*args)
        assert result.returncode != 0
        assert expected in result.stdout + result.stderr


def test_orchestrator_vm_script_keeps_core_safety_guards():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "set -Eeuo pipefail" in source
    assert "umask 077" in source
    assert "--proto '=https'" in source
    assert "sha256sum" in source
    assert "SHA256SUMS.gpg" in source
    assert "D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81" in source
    assert re.search(r"gpg\b.*?--verify", source, re.DOTALL)
    assert "/cluster/resources" in source
    assert source.count("assert_vm_id_is_free") >= 3
    assert not re.search(r"(?m)^\s*qm destroy(?:\s|$)", source)
    assert not re.search(r"(?m)^\s*qm template(?:\s|$)", source)
    assert not re.search(r'(?m)^\s*VM_ID=["\']?\d+', source)
    assert not re.search(r"curl[^\n|]*\|\s*(?:ba)?sh\b", source)
    assert "PRIVATE KEY" in source
    assert "/root/.ssh/authorized_keys" not in source


def test_orchestrator_vm_script_installs_docker_with_compose_v2():
    source = SCRIPT.read_text(encoding="utf-8")

    for fragment in (
        "https://download.docker.com/linux/ubuntu",
        "docker-ce",
        "docker-ce-cli",
        "containerd.io",
        "docker-buildx-plugin",
        "docker-compose-plugin",
        "docker compose version",
    ):
        assert fragment in source

    assert re.search(r"systemctl\s+enable\b[^\n]*\bdocker(?:\.service)?\b", source)
    assert not re.search(r"(?m)^\s*docker-compose(?:\s|$)", source)


def test_orchestrator_vm_script_downloads_both_files_from_one_resolved_commit():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "https://raw.githubusercontent.com/MidniqhtVibes/K8S-Universal" in source
    assert "https://api.github.com/repos/MidniqhtVibes/K8S-Universal" in source
    assert re.search(r"RESOLVED_REPOSITORY_COMMIT=.*REPOSITORY_REF", source)
    compose_download = re.search(
        r'\$\{RESOLVED_REPOSITORY_COMMIT\}/compose\.yaml(?:["\']|$)',
        source,
    )
    env_example_download = re.search(
        r'\$\{RESOLVED_REPOSITORY_COMMIT\}/\.env\.example(?:["\']|$)',
        source,
    )
    assert compose_download, "compose.yaml muss die aufgeloeste Commit-SHA verwenden"
    assert env_example_download, ".env.example muss dieselbe Commit-SHA verwenden"


def test_orchestrator_vm_script_prepares_user_owned_application_directory():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "/home/ubuntu/k8s-universal" in source
    assert re.search(r"--install\s+[^\n]*\bnano\b", source)
    assert "compose.yaml" in source
    assert ".env.example" in source
    assert re.search(r"chown\s+(?:-R\s+)?ubuntu:ubuntu\b", source)
    assert not re.search(
        r"(?m)^(?:cp|install)\b[^\n]*\.env\.example[^\n]*[ /]\.env(?:\s|$)",
        source,
    )
    assert "cp .env.example .env" in source


def test_orchestrator_vm_script_starts_a_normal_vm_instead_of_a_template():
    source = SCRIPT.read_text(encoding="utf-8")

    assert re.search(r'(?m)^\s*qm start "\$VM_ID"\s*$', source)
    create_index = source.index('qm create "$VM_ID"')
    create_end = source.index("VM_CREATED=true", create_index)
    start_index = source.index('qm start "$VM_ID"')
    assert "--onboot 0" in source[create_index:create_end]
    assert 'qm set "$VM_ID" --onboot 1' in source[create_end:start_index]
    assert not re.search(r"(?m)^\s*qm template(?:\s|$)", source)


def test_orchestrator_vm_script_verifies_guest_readiness_before_success():
    source = SCRIPT.read_text(encoding="utf-8")

    for fragment in (
        'qm guest cmd "$VM_ID" ping',
        'qm guest exec "$VM_ID"',
        "cloud-init status --wait",
        "systemctl is-active --quiet docker.service",
        "K8S_UNIVERSAL_GUEST_READY",
    ):
        assert fragment in source

    assert source.index('qm start "$VM_ID"') < source.index("FINISHED=true")
    assert source.index("K8S_UNIVERSAL_GUEST_READY") < source.index("FINISHED=true")


def test_orchestrator_vm_script_is_documented_and_packaged():
    for name in ("README.md", "BUILDER.md"):
        content = (PROJECT / name).read_text(encoding="utf-8")
        assert "proxmox/create-orchestrator-vm.sh" in content
        assert "wget" in content
        assert "docker compose up -d" in content

    dockerfile = (PROJECT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY proxmox proxmox" in dockerfile

    attributes = (PROJECT / ".gitattributes").read_text(encoding="utf-8")
    assert "proxmox/*.sh text eol=lf" in attributes
