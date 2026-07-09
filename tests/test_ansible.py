import subprocess
from pathlib import Path


def test_all_ansible_playbooks_resolve_core_modules():
    project = Path(__file__).parents[1]
    result = subprocess.run(
        ["ansible-playbook", "--syntax-check", "-i", "inventory.ini", "site.yml"],
        cwd=project / "ansible",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_calico_crds_use_server_side_apply():
    project = Path(__file__).parents[1]
    playbook = (project / "ansible/playbooks/08-install-cni.yml").read_text(encoding="utf-8")
    assert "--server-side" in playbook
    assert "--force-conflicts" in playbook
    assert " create -f " not in playbook


def test_package_install_waits_for_cloud_init_and_apt_locks():
    project = Path(__file__).parents[1]
    site = (project / "ansible/site.yml").read_text(encoding="utf-8")
    wait = (project / "ansible/playbooks/00-wait-for-hosts.yml").read_text(encoding="utf-8")
    bootstrap = (project / "ansible/playbooks/01-bootstrap-os.yml").read_text(encoding="utf-8")
    assert "00-wait-for-hosts.yml" in site
    assert "wait_for_connection" in wait
    assert "any_errors_fatal: true" in wait
    assert "timeout 900 cloud-init status --wait" in wait
    assert "timeout 900 cloud-init status --wait" in bootstrap
    assert "async:" not in wait
    assert "async:" not in bootstrap
    assert "cloud_init_status.rc not in [0, 2]" in bootstrap
    assert "'errors: []' not in cloud_init_status.stdout" in bootstrap
    assert "lock_timeout: 600" in bootstrap
    assert "dpkg --configure -a" in bootstrap
