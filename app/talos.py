import os
import tempfile
from pathlib import Path

import yaml

from .schemas import ClusterConfig, NodeConfig


TALOS_API_PORT = 50000
TALOS_INSTALLER_REPOSITORY = "ghcr.io/siderolabs/installer"


def write_secure_yaml(path: Path, document: dict) -> None:
    """Atomically write a Talos-related YAML file with owner-only access."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            yaml.safe_dump(document, stream, sort_keys=False)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_secure_yaml_documents(path: Path, documents: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            yaml.safe_dump_all(documents, stream, sort_keys=False)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def secure_talos_workspace(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    for item in path.rglob("*"):
        if item.is_file():
            os.chmod(item, 0o600)
        elif item.is_dir():
            os.chmod(item, 0o700)


def global_machine_patch(config: ClusterConfig) -> list[dict]:
    """Disable Talos' default CNI and retain the wizard's Kubernetes CIDRs."""
    documents = [{
        "cluster": {
            "network": {
                "cni": {"name": "none"},
                "podSubnets": [str(config.kubernetes.pod_cidr)],
                "serviceSubnets": [str(config.kubernetes.service_cidr)],
            },
        },
        "machine": {
            "kubelet": {
                "nodeIP": {"validSubnets": [str(config.network.cidr)]},
            }
        },
    }]
    if config.registry_enabled and config.registry_endpoint:
        scheme = "http" if config.registry_use_http else "https"
        documents.append({
            "apiVersion": "v1alpha1",
            "kind": "RegistryMirrorConfig",
            "name": config.registry_endpoint,
            "endpoints": [{"url": f"{scheme}://{config.registry_endpoint}"}],
            "skipFallback": True,
        })
    return documents


def node_machine_patch(config: ClusterConfig, node: NodeConfig) -> list[dict]:
    if config.talos is None:
        raise ValueError("Talos-Konfiguration fehlt")
    return [
        {
            "apiVersion": "v1alpha1",
            "kind": "HostnameConfig",
            "hostname": node.name,
            # Generated configs default to ``auto: stable``. Talos requires
            # automatic generation to be explicitly disabled when a static
            # hostname is patched in.
            "auto": "off",
        },
        {
            "apiVersion": "v1alpha1",
            "kind": "LinkConfig",
            "name": config.talos.network_interface,
            "addresses": [
                {"address": f"{node.ip}/{config.network.cidr.prefixlen}"},
            ],
            "routes": [{"gateway": str(config.network.gateway)}],
        },
        {
            "apiVersion": "v1alpha1",
            "kind": "ResolverConfig",
            "nameservers": [
                {"address": str(server)} for server in config.network.dns_servers
            ],
        },
    ]


def calico_custom_resources(config: ClusterConfig) -> list[dict]:
    """Return the Talos-compatible Calico operator resources.

    Talos has an immutable host filesystem, so Calico's kubelet volume plugin
    path must be disabled. NFTables/VXLAN avoid host kernel-module management
    from privileged Kubernetes workloads.
    """
    return [
        {
            "apiVersion": "crd.projectcalico.org/v1",
            "kind": "FelixConfiguration",
            "metadata": {"name": "default"},
            "spec": {"cgroupV2Path": "/sys/fs/cgroup"},
        },
        {
            "apiVersion": "operator.tigera.io/v1",
            "kind": "Installation",
            "metadata": {"name": "default"},
            "spec": {
                "calicoNetwork": {
                    "bgp": "Disabled",
                    "linuxDataplane": "Nftables",
                    "ipPools": [
                        {
                            "name": "default-ipv4-ippool",
                            "blockSize": max(26, config.kubernetes.pod_cidr.prefixlen),
                            "cidr": str(config.kubernetes.pod_cidr),
                            "encapsulation": "VXLAN",
                            "natOutgoing": "Enabled",
                            "nodeSelector": "all()",
                        }
                    ],
                },
                "kubeletVolumePluginPath": "None",
            },
        },
        {
            "apiVersion": "operator.tigera.io/v1",
            "kind": "APIServer",
            "metadata": {"name": "default"},
            "spec": {},
        },
    ]


def secrets_command(config: ClusterConfig, secrets_path: Path) -> list[str]:
    if config.talos is None:
        raise ValueError("Talos-Konfiguration fehlt")
    return [
        "talosctl", "gen", "secrets",
        "--talos-version", config.talos.version.value,
        "--output-file", str(secrets_path),
    ]


def config_generation_command(
    config: ClusterConfig,
    talos_dir: Path,
    secrets_path: Path,
    global_patch_path: Path,
) -> list[str]:
    if config.talos is None:
        raise ValueError("Talos-Konfiguration fehlt")
    command = [
        "talosctl", "gen", "config",
        config.name,
        f"https://{config.network.api_vip}:{config.kubernetes.api_port}",
        "--force",
        "--output", str(talos_dir),
        "--with-secrets", str(secrets_path),
        "--config-patch", f"@{global_patch_path}",
        "--additional-sans", str(config.network.api_vip),
        "--talos-version", config.talos.version.value,
        "--kubernetes-version", config.kubernetes_patch_version,
        "--install-disk", config.talos.install_disk,
        "--install-image", f"{TALOS_INSTALLER_REPOSITORY}:{config.talos.version.value}",
        "--with-docs=false",
        "--with-examples=false",
    ]
    return command
