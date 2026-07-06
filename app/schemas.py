from ipaddress import IPv4Address, IPv4Network
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ProxmoxConfig(BaseModel):
    endpoint: str
    node: str
    datastore: str
    template_vm_id: int = Field(ge=100, le=999999999)
    bridge: str = "vmbr0"
    vlan_id: int | None = Field(default=None, ge=1, le=4094)
    verify_tls: bool = True
    vm_name_include_cluster: bool = False
    credential_ref: str


class NetworkConfig(BaseModel):
    cidr: IPv4Network
    gateway: IPv4Address
    dns_servers: list[IPv4Address]
    api_vip: IPv4Address

    @field_validator("cidr", mode="before")
    @classmethod
    def parse_network(cls, value: object) -> IPv4Network:
        return IPv4Network(str(value), strict=True)


class SSHConfig(BaseModel):
    user: str = "ubuntu"
    port: int = Field(default=22, ge=1, le=65535)
    public_key: str
    credential_ref: str

    @field_validator("public_key")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        if not value.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
            raise ValueError("Nicht unterstütztes SSH-Public-Key-Format")
        return value.strip()


class KubernetesConfig(BaseModel):
    version: str = "v1.36"
    api_port: int = Field(default=6443, ge=1, le=65535)
    pod_cidr: IPv4Network
    service_cidr: IPv4Network

    @field_validator("pod_cidr", "service_cidr", mode="before")
    @classmethod
    def parse_network(cls, value: object) -> IPv4Network:
        return IPv4Network(str(value), strict=True)


class NodeConfig(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    role: Literal["loadbalancer", "control_plane", "worker"]
    vm_id: int = Field(ge=100, le=999999999)
    ip: IPv4Address
    cores: int = Field(ge=1, le=256)
    memory_mb: int = Field(ge=512)
    disk_gb: int = Field(ge=8)


class CNIConfig(BaseModel):
    provider: Literal["calico"] = "calico"
    version: str = "v3.32.0"


class IngressConfig(BaseModel):
    enabled: bool = True
    provider: Literal["traefik"] = "traefik"
    replicas: int = Field(default=2, ge=1)
    http_node_port: int = Field(default=30080, ge=30000, le=32767)
    https_node_port: int = Field(default=30443, ge=30000, le=32767)


class AddonsConfig(BaseModel):
    cni: CNIConfig = Field(default_factory=CNIConfig)
    ingress: IngressConfig = Field(default_factory=IngressConfig)


class ClusterConfig(BaseModel):
    model_config = ConfigDict(json_encoders={IPv4Address: str, IPv4Network: str})

    schema_version: Literal[1] = 1
    id: str
    name: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    proxmox: ProxmoxConfig
    network: NetworkConfig
    ssh: SSHConfig
    kubernetes: KubernetesConfig
    nodes: list[NodeConfig]
    addons: AddonsConfig = Field(default_factory=AddonsConfig)

    @model_validator(mode="after")
    def validate_cluster(self) -> "ClusterConfig":
        names = [node.name for node in self.nodes]
        ips = [node.ip for node in self.nodes]
        vm_ids = [node.vm_id for node in self.nodes]
        if len(names) != len(set(names)):
            raise ValueError("Node-Namen müssen eindeutig sein")
        if len(ips) != len(set(ips)):
            raise ValueError("Node-IP-Adressen müssen eindeutig sein")
        if len(vm_ids) != len(set(vm_ids)):
            raise ValueError("VM-IDs müssen eindeutig sein")
        for address in [self.network.gateway, self.network.api_vip, *ips]:
            if address not in self.network.cidr:
                raise ValueError(f"{address} liegt nicht im VM-Netz")
            if address in (self.network.cidr.network_address, self.network.cidr.broadcast_address):
                raise ValueError(f"{address} ist eine reservierte Netzadresse")
        if self.network.api_vip in ips:
            raise ValueError("API-VIP darf keinem Node gehören")
        networks = [self.network.cidr, self.kubernetes.pod_cidr, self.kubernetes.service_cidr]
        for index, left in enumerate(networks):
            for right in networks[index + 1 :]:
                if left.overlaps(right):
                    raise ValueError(f"Netze {left} und {right} überschneiden sich")
        roles = [node.role for node in self.nodes]
        if roles.count("loadbalancer") < 2:
            raise ValueError("HA benötigt mindestens zwei Load Balancer")
        if roles.count("control_plane") not in (3, 5, 7):
            raise ValueError("Control Plane benötigt eine ungerade Anzahl von 3, 5 oder 7 Nodes")
        if roles.count("worker") < 1:
            raise ValueError("Mindestens ein Worker ist erforderlich")
        if self.addons.ingress.http_node_port == self.addons.ingress.https_node_port:
            raise ValueError("HTTP- und HTTPS-NodePort müssen verschieden sein")
        return self

    def public_dict(self) -> dict:
        return self.model_dump(mode="json")
