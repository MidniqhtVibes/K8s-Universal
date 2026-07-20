import subprocess
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined


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


def test_registry_role_runs_between_containerd_and_kubernetes_packages():
    project = Path(__file__).parents[1]
    site = yaml.safe_load((project / "ansible/site.yml").read_text(encoding="utf-8"))
    imports = [entry["import_playbook"] for entry in site]

    registry_index = imports.index("playbooks/03-container-registry.yml")
    assert imports[registry_index - 1] == "playbooks/03-containerd.yml"
    assert imports[registry_index + 1] == "playbooks/04-kubernetes-packages.yml"

    registry_play = yaml.safe_load(
        (project / "ansible/playbooks/03-container-registry.yml").read_text(encoding="utf-8")
    )[0]
    assert registry_play["hosts"] == "k8s_cluster"
    assert registry_play["roles"] == [
        {"role": "container_registry", "when": "container_registry_enabled | bool"}
    ]


def test_registry_role_is_endpoint_scoped_and_idempotent():
    project = Path(__file__).parents[1]
    role = project / "ansible/roles/container_registry"
    defaults = yaml.safe_load((role / "defaults/main.yml").read_text(encoding="utf-8"))
    tasks_text = (role / "tasks/main.yml").read_text(encoding="utf-8")
    handlers_text = (role / "handlers/main.yml").read_text(encoding="utf-8")

    assert defaults == {
        "container_registry_enabled": False,
        "container_registry_endpoint": "",
        "container_registry_use_http": False,
        "containerd_registry_config_path": "/etc/containerd/certs.d",
    }
    assert 'path: "{{ containerd_registry_config_path }}/{{ container_registry_endpoint }}"' in tasks_text
    assert 'dest: "{{ containerd_registry_config_path }}/{{ container_registry_endpoint }}/hosts.toml"' in tasks_text
    assert "ansible.builtin.lineinfile" in tasks_text
    assert "config_path" in tasks_text
    assert "ansible.builtin.uri" in tasks_text
    assert "status_code:" in tasks_text and "- 200" in tasks_text and "- 401" in tasks_text
    assert "inventory_hostname" in tasks_text
    assert "notify: Restart containerd" in tasks_text
    assert "ansible.builtin.systemd" in handlers_text
    assert "state: restarted" in handlers_text
    assert "insecure_skip_verify" not in tasks_text


def test_registry_hosts_template_renders_http_and_https():
    template_dir = Path(__file__).parents[1] / "ansible/roles/container_registry/templates"
    template = Environment(
        loader=FileSystemLoader(template_dir),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    ).get_template("hosts.toml.j2")

    http = template.render(
        container_registry_endpoint="10.200.50.240:5000",
        container_registry_use_http=True,
    )
    assert 'server = "http://10.200.50.240:5000"' in http
    assert '[host."http://10.200.50.240:5000"]' in http
    assert 'capabilities = ["pull", "resolve"]' in http

    https = template.render(
        container_registry_endpoint="registry.example.internal:443",
        container_registry_use_http=False,
    )
    assert 'server = "https://registry.example.internal:443"' in https
    assert '[host."https://registry.example.internal:443"]' in https
