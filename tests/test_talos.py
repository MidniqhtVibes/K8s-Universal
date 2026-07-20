import json
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError

from app.generator import render_cluster
from app.schemas import ClusterConfig, ClusterType
from app.services import build_cluster_from_form, validate_template_disk_sizes
from app.talos import (
    calico_custom_resources,
    config_generation_command,
    global_machine_patch,
    node_machine_patch,
    secrets_command,
    write_secure_yaml,
    write_secure_yaml_documents,
)

from .helpers import valid_config


def talos_config() -> ClusterConfig:
    payload = valid_config().model_dump(mode="json")
    payload.update({
        "cluster_type": "talos",
        "ssh": None,
        "load_balancer_ssh": {
            "user": "ubuntu",
            "public_key": "ssh-ed25519 AAAATEST test",
            "credential_ref": "credential://lb-ssh",
        },
        "talos": {
            "version": "v1.13.6",
            "install_disk": "/dev/sda",
            "network_interface": "eth0",
            "template_platform": "nocloud",
        },
    })
    payload["proxmox"]["load_balancer_template_vm_id"] = 9877
    return ClusterConfig.model_validate(payload)


def talos_form() -> dict[str, str]:
    return {
        "cluster_type": "talos",
        "name": "talos-cluster",
        "proxmox_endpoint": "https://pve.test:8006/",
        "proxmox_node": "pve",
        "datastore": "local-lvm",
        "template_vm_id": "9876",
        "load_balancer_template_vm_id": "9877",
        "bridge": "vmbr0",
        "vlan_id": "",
        "proxmox_credential": "credential://proxmox",
        "network_cidr": "10.10.10.0/24",
        "gateway": "10.10.10.1",
        "dns_servers": "10.10.10.1, 1.1.1.1",
        "api_vip": "10.10.10.10",
        "lb_ssh_user": "ubuntu",
        "lb_ssh_public_key": "ssh-ed25519 AAAATEST test",
        "lb_ssh_credential": "credential://lb-ssh",
        "talos_version": "v1.13.6",
        "talos_install_disk": "/dev/sda",
        "talos_network_interface": "eth0",
        "kubernetes_version": "v1.36",
        "pod_cidr": "192.168.0.0/16",
        "service_cidr": "10.96.0.0/12",
        "lb_count": "2",
        "lb_ip_start": "10.10.10.11",
        "lb_vm_id_start": "301",
        "lb_cores": "1",
        "lb_memory": "1024",
        "lb_disk": "20",
        "cp_count": "3",
        "cp_ip_start": "10.10.10.21",
        "cp_vm_id_start": "311",
        "cp_cores": "2",
        "cp_memory": "4096",
        "cp_disk": "40",
        "worker_count": "1",
        "worker_ip_start": "10.10.10.31",
        "worker_vm_id_start": "321",
        "worker_cores": "2",
        "worker_memory": "4096",
        "worker_disk": "50",
        "calico_version": "v3.32.0",
        "traefik_replicas": "2",
        "http_node_port": "30080",
        "https_node_port": "30443",
    }


def test_legacy_cluster_defaults_to_kubeadm_without_requiring_new_fields():
    payload = valid_config().model_dump(mode="json")
    for field in ("cluster_type", "load_balancer_ssh", "talos"):
        payload.pop(field, None)
    payload["proxmox"].pop("load_balancer_template_vm_id", None)

    config = ClusterConfig.model_validate(payload)

    assert config.cluster_type == ClusterType.KUBEADM
    assert config.ssh is not None
    assert config.talos is None
    assert config.effective_load_balancer_template_vm_id == config.proxmox.template_vm_id


def test_talos_form_keeps_generic_ssh_null_and_uses_lb_only_credential():
    config = build_cluster_from_form(talos_form())

    assert config.cluster_type == ClusterType.TALOS
    assert config.ssh is None
    assert config.load_balancer_ssh is not None
    assert config.load_balancer_ssh.credential_ref == "credential://lb-ssh"
    assert config.talos is not None and config.talos.version.value == "v1.13.6"
    assert config.kubernetes_patch_version == "1.36.2"


def test_talos_rejects_an_empty_load_balancer_ssh_user():
    form = talos_form()
    form["lb_ssh_user"] = "   "

    with pytest.raises(ValidationError, match="at least 1 character"):
        build_cluster_from_form(form)


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda payload: payload.update(load_balancer_ssh=None), "Load-Balancer"),
        (lambda payload: payload.update(talos=None), "Talos-Version"),
        (lambda payload: payload["proxmox"].update(load_balancer_template_vm_id=None), "Ubuntu-Template"),
        (lambda payload: payload["talos"].update(version="v1.12.0"), "Input should be"),
    ],
)
def test_talos_rejects_missing_or_unsupported_type_specific_configuration(mutation, match):
    payload = talos_config().model_dump(mode="json")
    mutation(payload)
    with pytest.raises(ValidationError, match=match):
        ClusterConfig.model_validate(payload)


def test_talos_validates_each_role_against_its_actual_template():
    config = talos_config()
    discovery = {
        "vms": [
            {"vmid": 9876, "template": 1, "type": "qemu", "node": "pve", "template_disk_gb": 35},
            {"vmid": 9877, "template": 1, "type": "qemu", "node": "pve", "template_disk_gb": 20},
        ]
    }
    assert validate_template_disk_sizes(config, discovery) == {9876: 35, 9877: 20}

    payload = config.model_dump(mode="json")
    next(node for node in payload["nodes"] if node["role"] == "loadbalancer")["disk_gb"] = 19
    undersized = ClusterConfig.model_validate(payload)
    with pytest.raises(ValueError, match="Load-Balancer-Disk mit 19 GB"):
        validate_template_disk_sizes(undersized, discovery)


def test_talos_reports_template_errors_for_both_role_groups():
    config = talos_config()
    discovery = {
        "vms": [
            {"vmid": 9876, "template": 1, "type": "qemu", "node": "pve", "template_disk_gb": 60},
            {"vmid": 9877, "template": 1, "type": "qemu", "node": "pve", "template_disk_gb": 30},
        ]
    }

    with pytest.raises(ValueError) as exc_info:
        validate_template_disk_sizes(config, discovery)

    message = str(exc_info.value)
    assert "Talos-Template" in message
    assert "Ubuntu-LB-Template" in message


def test_talos_generator_uses_role_templates_and_excludes_nodes_from_ssh_inventory(tmp_path):
    config = talos_config()
    source = tmp_path / "source"
    (source / "terraform").mkdir(parents=True)
    (source / "terraform/main.tf").write_text("# terraform", encoding="utf-8")
    (source / "ansible/group_vars").mkdir(parents=True)
    (source / "ansible/site.yml").write_text("---", encoding="utf-8")

    render_cluster(config, tmp_path / "cluster", source)

    tfvars = json.loads((tmp_path / "cluster/generated/terraform.auto.tfvars.json").read_text(encoding="utf-8"))
    inventory = yaml.safe_load((tmp_path / "cluster/generated/ansible-inventory.yml").read_text(encoding="utf-8"))
    assert tfvars["cluster_type"] == "talos"
    assert tfvars["template_vm_id"] == 9876
    assert tfvars["load_balancer_template_vm_id"] == 9877
    assert tfvars["talos_install_disk"] == "/dev/sda"
    assert set(inventory["all"]["children"]["loadbalancer"]["hosts"]) == {"lb-01", "lb-02"}
    assert inventory["all"]["children"]["control_plane"]["hosts"] == {}
    assert inventory["all"]["children"]["worker"]["hosts"] == {}


def test_talos_patches_disable_default_cni_and_pin_per_node_network():
    config = talos_config()
    global_documents = global_machine_patch(config)
    node_documents = node_machine_patch(config, next(node for node in config.nodes if node.name == "control-01"))

    assert global_documents[0]["cluster"]["network"]["cni"]["name"] == "none"
    assert global_documents[0]["cluster"]["network"]["podSubnets"] == ["192.168.0.0/16"]
    assert [document["kind"] for document in node_documents] == ["HostnameConfig", "LinkConfig", "ResolverConfig"]
    assert node_documents[0]["hostname"] == "control-01"
    assert node_documents[0]["auto"] == "off"
    assert node_documents[1]["addresses"] == [{"address": "10.10.10.21/24"}]
    assert node_documents[1]["routes"] == [{"gateway": "10.10.10.1"}]


def test_talos_registry_and_calico_use_talos_native_configuration():
    payload = talos_config().model_dump(mode="json")
    payload.update(registry_enabled=True, registry_endpoint="registry.lab:5000", registry_use_http=True)
    config = ClusterConfig.model_validate(payload)

    registry = global_machine_patch(config)[1]
    assert registry == {
        "apiVersion": "v1alpha1",
        "kind": "RegistryMirrorConfig",
        "name": "registry.lab:5000",
        "endpoints": [{"url": "http://registry.lab:5000"}],
        "skipFallback": True,
    }
    resources = calico_custom_resources(config)
    installation = next(item for item in resources if item["kind"] == "Installation")
    felix = next(item for item in resources if item["kind"] == "FelixConfiguration")
    assert installation["spec"]["kubeletVolumePluginPath"] == "None"
    assert installation["spec"]["calicoNetwork"]["linuxDataplane"] == "Nftables"
    assert felix["spec"]["cgroupV2Path"] == "/sys/fs/cgroup"


def test_talos_generation_command_is_pinned_and_never_writes_secrets_to_stdout(tmp_path):
    config = talos_config()
    command = config_generation_command(
        config,
        tmp_path / "talos",
        tmp_path / "talos/secrets.yaml",
        tmp_path / "talos/common.patch.yaml",
    )
    assert command[:4] == ["talosctl", "gen", "config", "test-cluster"]
    assert command[command.index("--talos-version") + 1] == "v1.13.6"
    assert command[command.index("--kubernetes-version") + 1] == "1.36.2"
    assert command[command.index("--install-image") + 1] == "ghcr.io/siderolabs/installer:v1.13.6"
    assert command[command.index("--output") + 1] != "-"


@pytest.mark.skipif(shutil.which("talosctl") is None, reason="talosctl is not installed")
def test_bundled_talosctl_accepts_generated_machine_configs(tmp_path):
    payload = talos_config().model_dump(mode="json")
    payload.update(
        registry_enabled=True,
        registry_endpoint="registry.lab:5000",
        registry_use_http=True,
    )
    config = ClusterConfig.model_validate(payload)
    talos_dir = tmp_path / "talos"
    talos_dir.mkdir()
    secrets_path = talos_dir / "secrets.yaml"
    patch_path = talos_dir / "common.patch.yaml"
    output_dir = talos_dir / "rendered"
    write_secure_yaml_documents(patch_path, global_machine_patch(config))

    subprocess.run(secrets_command(config, secrets_path), check=True, capture_output=True, text=True)
    subprocess.run(
        config_generation_command(config, output_dir, secrets_path, patch_path),
        check=True,
        capture_output=True,
        text=True,
    )

    for role, base_name in (("control_plane", "controlplane.yaml"), ("worker", "worker.yaml")):
        node = next(node for node in config.nodes if node.role == role)
        node_patch_path = talos_dir / f"{node.name}.patch.yaml"
        node_config_path = talos_dir / f"{node.name}.yaml"
        write_secure_yaml_documents(node_patch_path, node_machine_patch(config, node))
        subprocess.run(
            [
                "talosctl", "machineconfig", "patch", str(output_dir / base_name),
                "--patch", f"@{node_patch_path}", "--output", str(node_config_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["talosctl", "validate", "--config", str(node_config_path), "--mode", "cloud", "--strict"],
            check=True,
            capture_output=True,
            text=True,
        )


def test_secure_talos_yaml_is_owner_only(tmp_path):
    target = tmp_path / "talos" / "secret.yaml"
    write_secure_yaml(target, {"secret": "value"})
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_talos_server_version_parser_ignores_the_client_version():
    from app.worker import _parse_talos_server_version

    output = "Client:\n\tTalos v1.13.5\nServer:\n\tNODE: 10.10.10.21\n\tTalos v1.13.6\n"
    assert _parse_talos_server_version(output) == "v1.13.6"
    assert _parse_talos_server_version("Client:\n\tTalos v1.13.6\n") is None


def test_terraform_and_worker_keep_kubeadm_resource_address_and_isolate_talos_ssh():
    project = Path(__file__).parents[1]
    terraform = (project / "terraform/main.tf").read_text(encoding="utf-8")
    worker = (project / "app/worker.py").read_text(encoding="utf-8")
    dockerfile = (project / "Dockerfile").read_text(encoding="utf-8")

    assert 'resource "proxmox_virtual_environment_vm" "k8s"' in terraform
    assert "for_each = var.nodes" in terraform
    assert 'coalesce(var.load_balancer_template_vm_id, var.template_vm_id)' in terraform
    assert 'each.value.role != "loadbalancer" && var.talos_install_disk == "/dev/vda"' in terraform
    assert 'var.cluster_type == "kubeadm" || each.value.role == "loadbalancer"' in terraform
    assert "wait_for_load_balancer_ssh(job, config)" in worker
    assert '"--limit", "loadbalancer"' in worker
    assert "if config.cluster_type == ClusterType.KUBEADM:" in worker
    assert "states = classify_talos_nodes" in worker
    assert '"talosctl", "health", "--nodes", bootstrap_ip' in worker
    assert 'for marker in ("bootstrap.requested.yaml", "bootstrap.complete.yaml")' in worker
    assert worker.index("v1_crd_projectcalico_org.yaml") < worker.index("tigera-operator.yaml")
    assert worker.index("tigera-operator.yaml") < worker.index("crd/installations.operator.tigera.io")
    assert "TALOSCTL_VERSION=v1.13.6" in dockerfile


def test_talos_wizard_is_one_dynamic_form_with_locked_edit_support():
    project = Path(__file__).parents[1]
    wizard = (project / "app/templates/wizard.html").read_text(encoding="utf-8")
    javascript = (project / "app/static/wizard.js").read_text(encoding="utf-8")

    assert wizard.count('id="wizard"') == 1
    assert 'name="cluster_type" value="kubeadm"' in wizard
    assert 'name="cluster_type" value="talos"' in wizard
    assert 'name="talos_version"' in wizard
    assert 'name="load_balancer_template_vm_id"' in wizard
    assert 'name="lb_ssh_credential"' in wizard
    assert "cluster_type_locked" in wizard
    assert "updateClusterTypeFields" in javascript
    assert "wrapper.hidden = talos" in javascript
    assert "section.hidden = !talos" in javascript
    assert "if (field.type === 'hidden')" in javascript
