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
    bootstrap = (project / "ansible/playbooks/01-bootstrap-os.yml").read_text(encoding="utf-8")
    assert "cloud-init status --wait" in bootstrap
    assert "timeout 600 cloud-init status --wait --long" in bootstrap
    assert "async:" not in bootstrap
    assert "poll:" not in bootstrap
    assert "cloud_init_status.rc not in [0, 2]" in bootstrap
    assert "'errors: []' not in cloud_init_status.stdout" in bootstrap
    assert "lock_timeout: 600" in bootstrap
    assert "dpkg --configure -a" in bootstrap


def test_kubeadm_bootstrap_uses_local_api_until_vip_is_ready():
    project = Path(__file__).parents[1]
    init_playbook = (project / "ansible/playbooks/05-init-control-plane.yml").read_text(encoding="utf-8")
    cni_playbook = (project / "ansible/playbooks/08-install-cni.yml").read_text(encoding="utf-8")
    assert "/root/admin-local.conf" in init_playbook
    assert "server: https://127.0.0.1:{{ api_port }}" in init_playbook
    assert "kubeadm\n          - token\n          - create\n          - --kubeconfig\n          - /root/admin-local.conf" in init_playbook
    assert "Normalize join command endpoint to VIP" in init_playbook
    assert "regex_replace('^(kubeadm join )[^ ]+'" in init_playbook
    assert "Wait for Kubernetes API through VIP" in init_playbook
    assert "/etc/kubernetes/admin.conf" not in cni_playbook
    assert "/root/admin-local.conf" in cni_playbook
