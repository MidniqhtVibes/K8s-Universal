from types import SimpleNamespace

import pytest

from app.proxmox import ProxmoxClient, template_disk_gb_from_bytes
from app.services import validate_current_template_disk, validate_template_disk_size

from .helpers import valid_config


GIB = 1024 ** 3


def discovery_for(config, disk_gb=20):
    template = {
        "vmid": config.proxmox.template_vm_id,
        "template": 1,
        "type": "qemu",
        "node": config.proxmox.node,
    }
    if disk_gb is not None:
        template["template_disk_gb"] = disk_gb
    return {"vms": [template]}


def config_with_disks(*, loadbalancer=20, control_plane=20, worker=20):
    config = valid_config()
    payload = config.model_dump(mode="json")
    sizes = {
        "loadbalancer": loadbalancer,
        "control_plane": control_plane,
        "worker": worker,
    }
    for node in payload["nodes"]:
        node["disk_gb"] = sizes[node["role"]]
    return type(config).model_validate(payload)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (20 * GIB, 20),
        (20 * GIB + 1, 21),
        (str(40 * GIB), 40),
        (0, None),
        (-1, None),
        (True, None),
        ("20 GB", None),
        (None, None),
    ],
)
def test_template_disk_bytes_are_normalized_fail_closed(raw, expected):
    assert template_disk_gb_from_bytes(raw) == expected


def test_proxmox_discovery_exposes_normalized_template_disk():
    client = object.__new__(ProxmoxClient)

    def fake_get(path, **_params):
        if path == "nodes":
            return [{"node": "pve"}]
        if path == "cluster/resources":
            return [
                {"vmid": 9000, "template": 1, "type": "qemu", "node": "pve", "maxdisk": 20 * GIB + 1},
                {"vmid": 301, "template": 0, "type": "qemu", "node": "pve", "maxdisk": 50 * GIB},
            ]
        return []

    client.get = fake_get

    discovery = client.discover()

    assert discovery["vms"][0]["template_disk_gb"] == 21
    assert "template_disk_gb" not in discovery["vms"][1]


@pytest.mark.parametrize("disk_gb", [20, 40])
def test_template_disk_accepts_equal_and_larger_role_disks(disk_gb):
    config = config_with_disks(
        loadbalancer=disk_gb,
        control_plane=disk_gb,
        worker=disk_gb,
    )

    assert validate_template_disk_size(config, discovery_for(config, 20)) == 20


@pytest.mark.parametrize(
    ("role", "label"),
    [
        ("loadbalancer", "Load-Balancer"),
        ("control_plane", "Control-Plane"),
        ("worker", "Worker"),
    ],
)
def test_template_disk_rejects_each_undersized_node_role(role, label):
    sizes = {"loadbalancer": 20, "control_plane": 20, "worker": 20}
    sizes[role] = 15
    config = config_with_disks(**sizes)

    with pytest.raises(ValueError, match=rf"{label}-Disk mit 15 GB.*20 GB"):
        validate_template_disk_size(config, discovery_for(config, 20))


def test_template_disk_reports_all_undersized_roles_together():
    config = config_with_disks(loadbalancer=15, control_plane=16, worker=17)

    with pytest.raises(ValueError) as error:
        validate_template_disk_size(config, discovery_for(config, 20))

    message = str(error.value)
    assert "Load-Balancer-Disk mit 15 GB" in message
    assert "Control-Plane-Disk mit 16 GB" in message
    assert "Worker-Disk mit 17 GB" in message


def test_template_disk_missing_metadata_stops_validation():
    config = config_with_disks(loadbalancer=40, control_plane=40, worker=40)

    with pytest.raises(ValueError, match="konnte nicht ermittelt werden"):
        validate_template_disk_size(config, discovery_for(config, None))


def test_server_validation_fetches_current_template_metadata(monkeypatch):
    config = config_with_disks(loadbalancer=20, control_plane=20, worker=20)
    calls = []

    class FakeProxmoxClient:
        def __init__(self, endpoint, token, verify_tls):
            calls.append((endpoint, token, verify_tls))

        def discover(self):
            calls.append("discover")
            return discovery_for(config, 20)

    monkeypatch.setattr("app.services.credential_payload", lambda *_args: {"api_token": "secret"})
    monkeypatch.setattr("app.services.ProxmoxClient", FakeProxmoxClient)

    assert validate_current_template_disk(SimpleNamespace(), config) == 20
    assert calls == [
        (config.proxmox.endpoint, "secret", config.proxmox.verify_tls),
        "discover",
    ]
