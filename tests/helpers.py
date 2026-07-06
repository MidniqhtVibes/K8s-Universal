from app.schemas import ClusterConfig


def valid_config() -> ClusterConfig:
    return ClusterConfig.model_validate({
        "schema_version": 1,
        "id": "00000000-0000-0000-0000-000000000001",
        "name": "test-cluster",
        "proxmox": {"endpoint": "https://pve.test:8006/", "node": "pve", "datastore": "local-lvm", "template_vm_id": 9000, "bridge": "vmbr0", "verify_tls": False, "vm_name_include_cluster": True, "credential_ref": "credential://proxmox"},
        "network": {"cidr": "10.10.10.0/24", "gateway": "10.10.10.1", "dns_servers": ["10.10.10.1"], "api_vip": "10.10.10.10"},
        "ssh": {"user": "ubuntu", "public_key": "ssh-ed25519 AAAATEST test", "credential_ref": "credential://ssh"},
        "kubernetes": {"version": "v1.33", "api_port": 6443, "pod_cidr": "192.168.0.0/16", "service_cidr": "10.96.0.0/12"},
        "nodes": [
            {"name": "lb-01", "role": "loadbalancer", "vm_id": 301, "ip": "10.10.10.11", "cores": 1, "memory_mb": 1024, "disk_gb": 20},
            {"name": "lb-02", "role": "loadbalancer", "vm_id": 302, "ip": "10.10.10.12", "cores": 1, "memory_mb": 1024, "disk_gb": 20},
            {"name": "control-01", "role": "control_plane", "vm_id": 311, "ip": "10.10.10.21", "cores": 2, "memory_mb": 4096, "disk_gb": 40},
            {"name": "control-02", "role": "control_plane", "vm_id": 312, "ip": "10.10.10.22", "cores": 2, "memory_mb": 4096, "disk_gb": 40},
            {"name": "control-03", "role": "control_plane", "vm_id": 313, "ip": "10.10.10.23", "cores": 2, "memory_mb": 4096, "disk_gb": 40},
            {"name": "worker-01", "role": "worker", "vm_id": 321, "ip": "10.10.10.31", "cores": 2, "memory_mb": 4096, "disk_gb": 50},
        ],
    })
