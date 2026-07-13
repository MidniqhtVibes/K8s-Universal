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


def test_calico_manifests_are_downloaded_before_apply():
    project = Path(__file__).parents[1]
    playbook = (project / "ansible/playbooks/08-install-cni.yml").read_text(encoding="utf-8")
    assert "ansible.builtin.get_url" in playbook
    assert "until: calico_crds_download is succeeded" in playbook
    assert "until: tigera_operator_download is succeeded" in playbook
    assert '"/root/calico-{{ calico_version }}-crds.yaml"' in playbook
    assert '"/root/calico-{{ calico_version }}-tigera-operator.yaml"' in playbook
    assert '- "https://raw.githubusercontent.com' not in playbook


def test_package_install_waits_for_cloud_init_and_apt_locks():
    project = Path(__file__).parents[1]
    site = (project / "ansible/site.yml").read_text(encoding="utf-8")
    wait = (project / "ansible/playbooks/00-wait-for-hosts.yml").read_text(encoding="utf-8")
    bootstrap = (project / "ansible/playbooks/01-bootstrap-os.yml").read_text(encoding="utf-8")
    assert "00-wait-for-hosts.yml" in site
    assert "wait_for_connection" in wait
    assert "any_errors_fatal: true" in wait
    assert "timeout 900 cloud-init status --wait" in wait
    assert "timeout 900 cloud-init status --wait" not in bootstrap
    assert "async:" not in wait
    assert "async:" not in bootstrap
    assert "cloud_init_status.rc not in [0, 2]" in wait
    assert "'errors: []' not in cloud_init_status.stdout" in wait
    assert "lock_timeout: 600" in bootstrap
    assert "dpkg --configure -a" in bootstrap


def test_kubernetes_apt_keyring_is_rebuilt_safely():
    project = Path(__file__).parents[1]
    packages = (project / "ansible/playbooks/04-kubernetes-packages.yml").read_text(encoding="utf-8")
    assert "ansible.builtin.get_url" in packages
    assert "until: kubernetes_apt_key_download is succeeded" in packages
    assert "gpg --batch --dearmor" in packages
    assert "gpg --batch --quiet --show-keys" in packages
    assert "mv \"${final_tmp}\" \"${final_keyring}\"" in packages
    assert "creates: /etc/apt/keyrings/kubernetes-apt-keyring.gpg" not in packages
    assert "update_cache_retries: 5" in packages


def test_load_balancer_uses_cluster_variables_and_hides_disabled_ingress():
    playbook = (Path(__file__).parents[1] / "ansible/playbooks/02-loadbalancer.yml").read_text(encoding="utf-8")
    assert "virtual_router_id {{ keepalived_virtual_router_id }}" in playbook
    assert "virtual_router_id 51" not in playbook
    assert "{% if ingress_enabled %}" in playbook
    assert "150 - groups['loadbalancer'].index(inventory_hostname)" in playbook
    assert "254 - groups['loadbalancer'].index(inventory_hostname)" not in playbook
    assert "weight 2" in playbook


def test_calico_block_size_is_generated_per_pod_network():
    playbook = (Path(__file__).parents[1] / "ansible/playbooks/08-install-cni.yml").read_text(encoding="utf-8")
    assert "blockSize: {{ calico_block_size }}" in playbook
