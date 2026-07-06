import hashlib
import json
import shutil
from pathlib import Path

import yaml

from .schemas import ClusterConfig


def config_hash(config: dict) -> str:
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def render_cluster(config: ClusterConfig, destination: Path, source_root: Path) -> Path:
    generated = destination / "generated"
    terraform_dir = destination / "terraform"
    ansible_dir = destination / "ansible"
    generated.mkdir(parents=True, exist_ok=True)
    destination.mkdir(parents=True, exist_ok=True)

    public = config.public_dict()
    (destination / "cluster.yaml").write_text(yaml.safe_dump(public, sort_keys=False), encoding="utf-8")

    tfvars = {
        "proxmox_endpoint": config.proxmox.endpoint,
        "proxmox_insecure": not config.proxmox.verify_tls,
        "proxmox_node": config.proxmox.node,
        "template_vm_id": config.proxmox.template_vm_id,
        "datastore_id": config.proxmox.datastore,
        "network_bridge": config.proxmox.bridge,
        "vlan_id": config.proxmox.vlan_id,
        "gateway": str(config.network.gateway),
        "subnet_prefix": config.network.cidr.prefixlen,
        "dns_servers": [str(item) for item in config.network.dns_servers],
        "ssh_user": config.ssh.user,
        "ssh_public_key": config.ssh.public_key,
        "nodes": {node.name: node.model_dump(mode="json") for node in config.nodes},
    }
    (generated / "terraform.auto.tfvars.json").write_text(json.dumps(tfvars, indent=2), encoding="utf-8")

    groups: dict[str, dict] = {}
    role_map = {"loadbalancer": "loadbalancer", "control_plane": "control_plane", "worker": "worker"}
    for role, group in role_map.items():
        groups[group] = {
            "hosts": {
                node.name: {"ansible_host": str(node.ip), "node_vm_id": node.vm_id}
                for node in config.nodes
                if node.role == role
            }
        }
    groups["k8s_cluster"] = {"children": {"control_plane": {}, "worker": {}}}
    inventory = {
        "all": {
            "vars": {
                "ansible_user": config.ssh.user,
                "ansible_port": config.ssh.port,
                "ansible_ssh_private_key_file": "{{ lookup('env', 'CLUSTER_SSH_KEY_PATH') }}",
                "ansible_become": True,
                "ansible_ssh_common_args": "-o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new",
            },
            "children": groups,
        }
    }
    (generated / "ansible-inventory.yml").write_text(yaml.safe_dump(inventory, sort_keys=False), encoding="utf-8")

    control_planes = [node for node in config.nodes if node.role == "control_plane"]
    workers = [node for node in config.nodes if node.role == "worker"]
    loadbalancers = [node for node in config.nodes if node.role == "loadbalancer"]
    variables = {
        "kubernetes_minor": config.kubernetes.version,
        "api_vip": str(config.network.api_vip),
        "api_port": config.kubernetes.api_port,
        "api_prefix": config.network.cidr.prefixlen,
        "pod_cidr": str(config.kubernetes.pod_cidr),
        "service_cidr": str(config.kubernetes.service_cidr),
        "control_plane_endpoint": f"{config.network.api_vip}:{config.kubernetes.api_port}",
        "first_control_plane": control_planes[0].name,
        "first_control_plane_group": control_planes[0].name,
        "kube_user": config.ssh.user,
        "keepalived_interface": "{{ ansible_default_ipv4.interface }}",
        "keepalived_master": loadbalancers[0].name,
        "haproxy_backends": [{"name": node.name, "ip": str(node.ip)} for node in control_planes],
        "haproxy_workers": [{"name": node.name, "ip": str(node.ip)} for node in workers],
        "calico_version": config.addons.cni.version,
        "ingress_enabled": config.addons.ingress.enabled,
        "ingress_http_node_port": config.addons.ingress.http_node_port,
        "ingress_https_node_port": config.addons.ingress.https_node_port,
    }
    (generated / "ansible-vars.yml").write_text(yaml.safe_dump(variables, sort_keys=False), encoding="utf-8")

    traefik = {
        "deployment": {"replicas": config.addons.ingress.replicas},
        "ingressClass": {"enabled": True, "isDefaultClass": True, "name": "traefik"},
        "providers": {"kubernetesIngress": {"enabled": True}},
        "service": {"type": "NodePort", "spec": {"externalTrafficPolicy": "Cluster"}},
        "ports": {
            "web": {"port": 80, "expose": {"default": True}, "exposedPort": 80, "nodePort": config.addons.ingress.http_node_port, "protocol": "TCP"},
            "websecure": {"port": 443, "expose": {"default": True}, "exposedPort": 443, "nodePort": config.addons.ingress.https_node_port, "protocol": "TCP"},
        },
    }
    (generated / "traefik-values.yaml").write_text(yaml.safe_dump(traefik, sort_keys=False), encoding="utf-8")

    _refresh_tree(source_root / "terraform", terraform_dir)
    _refresh_tree(source_root / "ansible", ansible_dir)
    shutil.copy2(generated / "terraform.auto.tfvars.json", terraform_dir / "terraform.auto.tfvars.json")
    shutil.copy2(generated / "ansible-inventory.yml", ansible_dir / "inventory.generated.yml")
    shutil.copy2(generated / "ansible-vars.yml", ansible_dir / "group_vars" / "all.yml")
    return destination


def _refresh_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".terraform", "*.tfstate*", "*.tfplan", "terraform.auto.tfvars.json", "inventory.generated.yml", "generated.yml"),
    )
