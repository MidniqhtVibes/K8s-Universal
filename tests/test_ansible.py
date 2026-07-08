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
    assert "timeout 1200 cloud-init status --wait --long" in bootstrap
    assert "async:" not in bootstrap
    assert "poll:" not in bootstrap
    assert "failed_when: false" in bootstrap
    assert "Collect cloud-init output log on failure" in bootstrap
    assert "Collect cloud-init debug log on failure" in bootstrap
    assert "Fail if cloud-init did not finish cleanly" in bootstrap
    assert "cloud_init_status.rc not in [0, 2]" in bootstrap
    assert "'errors: []' not in cloud_init_status.stdout" in bootstrap
    assert "lock_timeout: 600" in bootstrap
    assert "dpkg --configure -a" in bootstrap


def test_kubeadm_bootstrap_uses_local_api_until_vip_is_ready():
    project = Path(__file__).parents[1]
    init_playbook = (project / "ansible/playbooks/05-init-control-plane.yml").read_text(encoding="utf-8")
    cni_playbook = (project / "ansible/playbooks/08-install-cni.yml").read_text(encoding="utf-8")
    assert "/root/admin-local.conf" in init_playbook
    assert "server: https://{{ ansible_host }}:{{ api_port }}" in init_playbook
    assert "kubeadm\n          - token\n          - create\n          - --kubeconfig\n          - /root/admin-local.conf" in init_playbook
    assert "Normalize join command endpoint to VIP" in init_playbook
    assert "regex_replace('^(kubeadm join )[^ ]+'" in init_playbook
    assert "Wait for Kubernetes API through VIP" in init_playbook
    assert "/etc/kubernetes/admin.conf" not in cni_playbook
    assert "/root/admin-local.conf" in cni_playbook


def test_loadbalancer_waits_for_vip_before_control_plane_bootstrap():
    project = Path(__file__).parents[1]
    loadbalancer_playbook = (project / "ansible/playbooks/02-loadbalancer.yml").read_text(encoding="utf-8")
    init_playbook = (project / "ansible/playbooks/05-init-control-plane.yml").read_text(encoding="utf-8")
    assert "update_cache_retries: 5" in loadbalancer_playbook
    assert "cache_valid_time: 3600" in loadbalancer_playbook
    assert "Apply pending load balancer service restarts" in loadbalancer_playbook
    assert "Wait for Keepalived master to own the API VIP" in loadbalancer_playbook
    assert "keepalived_master_vip_check.stdout | trim | length > 0" in loadbalancer_playbook
    assert "select('defined') | map(attribute='stdout') | map('trim') | reject('equalto', '')" in loadbalancer_playbook
    assert "Wait for Kubernetes API TCP port through VIP" in init_playbook


def test_infrastructure_playbooks_stop_globally_on_host_failure():
    project = Path(__file__).parents[1]
    for playbook in (project / "ansible/playbooks").glob("*.yml"):
        if playbook.name == "site.yml":
            continue
        content = playbook.read_text(encoding="utf-8")
        if "hosts: localhost" in content and "hosts: control_plane[0]" not in content:
            continue
        assert "any_errors_fatal: true" in content, f"{playbook.name} must stop globally on host failures"
