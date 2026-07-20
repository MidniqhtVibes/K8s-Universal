import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError

from app.generator import render_cluster
from app.main import cluster_form_values
from app.schemas import ClusterConfig
from app.services import build_cluster_from_form
from app.worker import run_ansible_stack

from .helpers import valid_config


def _payload(**registry_values) -> dict:
    payload = valid_config().model_dump(mode="json")
    payload.update(registry_values)
    return payload


def _form(**registry_values) -> dict[str, str]:
    values = {
        "name": "test-cluster",
        "proxmox_endpoint": "https://pve.test:8006/",
        "proxmox_node": "pve",
        "datastore": "local-lvm",
        "template_vm_id": "9876",
        "bridge": "vmbr0",
        "vlan_id": "",
        "proxmox_credential": "credential://proxmox",
        "network_cidr": "10.10.10.0/24",
        "gateway": "10.10.10.1",
        "dns_servers": "10.10.10.1",
        "api_vip": "10.10.10.10",
        "ssh_user": "ubuntu",
        "ssh_public_key": "ssh-ed25519 AAAATEST test",
        "ssh_credential": "credential://ssh",
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
    values.update(registry_values)
    return values


def test_legacy_and_disabled_registry_configs_use_safe_defaults():
    payload = _payload()
    for field in ("registry_enabled", "registry_endpoint", "registry_use_http"):
        payload.pop(field)

    legacy = ClusterConfig.model_validate(payload)
    assert legacy.registry_enabled is False
    assert legacy.registry_endpoint is None
    assert legacy.registry_use_http is False

    disabled = ClusterConfig.model_validate(_payload(
        registry_enabled=False,
        registry_endpoint=" stale.registry.test:5000 ",
        registry_use_http=True,
    ))
    assert disabled.registry_endpoint is None
    assert disabled.registry_use_http is False


@pytest.mark.parametrize("endpoint", ["10.200.50.240:5000", "registry.lab.local:443"])
def test_enabled_registry_accepts_and_trims_valid_host_and_port(endpoint):
    config = ClusterConfig.model_validate(_payload(
        registry_enabled=True,
        registry_endpoint=f"  {endpoint}  ",
        registry_use_http=True,
    ))

    assert config.registry_endpoint == endpoint
    assert config.registry_use_http is True


@pytest.mark.parametrize(
    "endpoint",
    [
        None,
        "",
        "http://10.200.50.240:5000",
        "https://registry.lab.local:443",
        "10.200.50.240",
        "10.200.50.240:",
        "10.200.50.240:0",
        "10.200.50.240:65536",
        "10.200.50.240:not-a-port",
        "registry.lab.local:5000/path",
        "registry_lab.local:5000",
        "registry.lab.local:5000;touch-pwned",
        "999.999.999.999:5000",
    ],
)
def test_enabled_registry_rejects_invalid_or_unsafe_endpoints(endpoint):
    with pytest.raises(ValidationError, match="host:port"):
        ClusterConfig.model_validate(_payload(
            registry_enabled=True,
            registry_endpoint=endpoint,
            registry_use_http=True,
        ))


def test_registry_form_values_round_trip_and_legacy_edit_defaults():
    config = build_cluster_from_form(_form(
        registry_enabled="on",
        registry_endpoint=" 10.200.50.240:5000 ",
        registry_use_http="on",
    ))
    assert config.registry_enabled is True
    assert config.registry_endpoint == "10.200.50.240:5000"
    assert config.registry_use_http is True

    edit_values = cluster_form_values(SimpleNamespace(config=config.public_dict()))
    assert edit_values["registry_enabled"] == "on"
    assert edit_values["registry_endpoint"] == "10.200.50.240:5000"
    assert edit_values["registry_use_http"] == "on"

    legacy = config.public_dict()
    for field in ("registry_enabled", "registry_endpoint", "registry_use_http"):
        legacy.pop(field)
    legacy_values = cluster_form_values(SimpleNamespace(config=legacy))
    assert legacy_values["registry_enabled"] == ""
    assert legacy_values["registry_endpoint"] == ""
    assert legacy_values["registry_use_http"] == ""


@pytest.mark.parametrize(
    ("enabled", "endpoint", "use_http", "expected_endpoint"),
    [
        (False, None, False, ""),
        (True, "10.200.50.240:5000", True, "10.200.50.240:5000"),
        (True, "registry.lab.local:443", False, "registry.lab.local:443"),
    ],
)
def test_generator_passes_registry_settings_to_ansible(
    tmp_path, enabled, endpoint, use_http, expected_endpoint
):
    config = ClusterConfig.model_validate(_payload(
        registry_enabled=enabled,
        registry_endpoint=endpoint,
        registry_use_http=use_http,
    ))
    source = tmp_path / "source"
    (source / "terraform").mkdir(parents=True)
    (source / "ansible" / "group_vars").mkdir(parents=True)
    (source / "ansible" / "site.yml").write_text("---\n", encoding="utf-8")

    render_cluster(config, tmp_path / "cluster", source)

    variables = yaml.safe_load(
        (tmp_path / "cluster/generated/ansible-vars.yml").read_text(encoding="utf-8")
    )
    assert variables["container_registry_enabled"] is enabled
    assert variables["container_registry_endpoint"] == expected_endpoint
    assert variables["container_registry_use_http"] is use_http


def test_worker_logs_registry_flow_only_when_enabled(monkeypatch, tmp_path):
    log: list[str] = []
    commands: list[list[str]] = []
    monkeypatch.setattr("app.worker.append_log", lambda _job_id, text: log.append(text))
    monkeypatch.setattr("app.worker.wait_for_ssh", lambda *_args: None)
    monkeypatch.setattr(
        "app.worker.run_command",
        lambda _job, command, *_args, **_kwargs: commands.append(command) or 0,
    )
    monkeypatch.setattr("app.worker.verify_cluster", lambda *_args: None)
    config = ClusterConfig.model_validate(_payload(
        registry_enabled=True,
        registry_endpoint="10.200.50.240:5000",
        registry_use_http=True,
    ))
    config.addons.ingress.enabled = False

    run_ansible_stack(
        SimpleNamespace(id="job-1"), config, tmp_path, tmp_path, tmp_path / "kubeconfig", {}, []
    )

    output = "".join(log)
    assert "[Registry] Private Registry aktiviert: 10.200.50.240:5000" in output
    assert "[Registry] Protokoll: HTTP" in output
    assert "Control Plane Nodes" in output and "Worker Nodes" in output
    assert "[Registry] Registry erreichbar." in output
    assert commands == [["ansible-playbook", "-i", "inventory.generated.yml", "site.yml"]]

    log.clear()
    disabled = valid_config()
    disabled.addons.ingress.enabled = False
    run_ansible_stack(
        SimpleNamespace(id="job-2"), disabled, tmp_path, tmp_path, tmp_path / "kubeconfig", {}, []
    )
    assert "[Registry]" not in "".join(log)


def test_registry_migration_preserves_existing_json_and_hashes():
    migration = importlib.import_module("migrations.versions.0007_container_registry_config")

    assert migration.down_revision == "0006_applied_cluster_state"
    assert migration.upgrade() is None
    assert migration.downgrade() is None
    source = Path(migration.__file__).read_text(encoding="utf-8")
    assert "no physical database change or JSON rewrite" in source
    assert "clusters.config`` and its authorization/state hashes" in source
