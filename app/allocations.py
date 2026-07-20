import ipaddress
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Cluster, ClusterStatus, Preference


DEFAULT_PREFERENCES: dict[str, Any] = {
    "auto_suggest": True,
    "vm_name_include_cluster": True,
    "network_cidr": "10.200.50.0/24",
    "gateway": "10.200.50.1",
    "dns_servers": "10.200.50.1, 1.1.1.1",
    "pod_cidr": "192.168.0.0/16",
    "service_cidr": "10.96.0.0/12",
    "vip_pool_start": "10.200.50.140",
    "vip_pool_end": "10.200.50.144",
    "lb_ip_start": "10.200.50.145",
    "lb_ip_end": "10.200.50.149",
    "cp_ip_start": "10.200.50.151",
    "cp_ip_end": "10.200.50.159",
    "worker_ip_start": "10.200.50.161",
    "worker_ip_end": "10.200.50.199",
    "lb_vm_id_start": 301,
    "lb_vm_id_end": 309,
    "cp_vm_id_start": 311,
    "cp_vm_id_end": 319,
    "worker_vm_id_start": 321,
    "worker_vm_id_end": 399,
    "lb_count": 2,
    "cp_count": 3,
    "worker_count": 2,
    "proxmox_node": "pve",
    "datastore": "local-lvm",
    "bridge": "vmbr0",
    "vlan_id": "",
    "ssh_user": "ubuntu",
    "kubernetes_version": "v1.36",
    "calico_version": "v3.32.0",
    "ingress_enabled": True,
    "traefik_replicas": 2,
    "http_node_port": 30080,
    "https_node_port": 30443,
    "lb_cores": 1,
    "lb_memory": 2048,
    "lb_disk": 30,
    "cp_cores": 2,
    "cp_memory": 4096,
    "cp_disk": 40,
    "worker_cores": 2,
    "worker_memory": 4096,
    "worker_disk": 50,
    "reserved_ips": "",
}


def get_preferences(db: Session) -> Preference | None:
    """Return only explicitly persisted defaults without writing on reads."""
    return db.get(Preference, 1)


def effective_preferences(db: Session) -> dict[str, Any]:
    """Merge persisted user defaults over non-persistent system fallbacks."""
    preference = get_preferences(db)
    merged = dict(DEFAULT_PREFERENCES)
    if preference is not None:
        merged.update({
            key: value
            for key, value in (preference.config or {}).items()
            if key in DEFAULT_PREFERENCES
        })
    return merged


def wizard_default_values(preferences: dict[str, Any]) -> dict[str, str]:
    """Map only reusable, non-secret defaults into a new-cluster form."""
    fields = (
        "network_cidr", "gateway", "dns_servers", "pod_cidr", "service_cidr",
        "lb_count", "cp_count", "worker_count", "proxmox_node", "datastore",
        "bridge", "vlan_id", "ssh_user", "kubernetes_version", "calico_version",
        "traefik_replicas", "http_node_port", "https_node_port", "lb_cores",
        "lb_memory", "lb_disk", "cp_cores", "cp_memory", "cp_disk",
        "worker_cores", "worker_memory", "worker_disk",
    )
    values = {field: str(preferences[field]) for field in fields}
    values["api_vip"] = str(preferences["vip_pool_start"])
    for role in ("lb", "cp", "worker"):
        values[f"{role}_ip_start"] = str(preferences[f"{role}_ip_start"])
        values[f"{role}_vm_id_start"] = str(preferences[f"{role}_vm_id_start"])
    values["vm_name_include_cluster"] = "on" if preferences["vm_name_include_cluster"] else ""
    values["ingress_enabled"] = "on" if preferences["ingress_enabled"] else ""
    return values


def parse_reserved_ips(value: str) -> set[ipaddress.IPv4Address]:
    result: set[ipaddress.IPv4Address] = set()
    for token in (item.strip() for item in value.split(",")):
        if not token:
            continue
        if "/" in token:
            network = ipaddress.ip_network(token, strict=False)
            if not isinstance(network, ipaddress.IPv4Network):
                raise ValueError("Nur IPv4-Reservierungen werden unterstützt")
            if network.num_addresses > 4096:
                raise ValueError(f"Reservierter Bereich {network} ist zu groß (maximal 4096 Adressen)")
            result.update(network.hosts())
        else:
            result.add(ipaddress.IPv4Address(token))
    return result


def used_allocations(db: Session, exclude_cluster_id: str | None = None) -> tuple[set[ipaddress.IPv4Address], set[int]]:
    clusters = db.scalars(select(Cluster).where(Cluster.status != ClusterStatus.DESTROYED)).all()
    used_ips: set[ipaddress.IPv4Address] = set()
    used_vm_ids: set[int] = set()
    for cluster in clusters:
        if cluster.id == exclude_cluster_id:
            continue
        network = cluster.config.get("network", {})
        if network.get("api_vip"):
            used_ips.add(ipaddress.IPv4Address(network["api_vip"]))
        for node in cluster.config.get("nodes", []):
            used_ips.add(ipaddress.IPv4Address(node["ip"]))
            used_vm_ids.add(int(node["vm_id"]))
    return used_ips, used_vm_ids


def _first_ip_block(start: str, end: str, count: int, used: set[ipaddress.IPv4Address]) -> str:
    first = ipaddress.IPv4Address(start)
    last = ipaddress.IPv4Address(end)
    for candidate_int in range(int(first), int(last) - count + 2):
        block = {ipaddress.IPv4Address(candidate_int + offset) for offset in range(count)}
        if not block & used:
            return str(ipaddress.IPv4Address(candidate_int))
    raise ValueError(f"Kein zusammenhängender Block mit {count} freien IPs in {first}–{last}")


def _first_id_block(start: int, end: int, count: int, used: set[int]) -> int:
    for candidate in range(start, end - count + 2):
        if not set(range(candidate, candidate + count)) & used:
            return candidate
    raise ValueError(f"Kein zusammenhängender Block mit {count} freien VM-IDs in {start}–{end}")


def suggest_allocations(
    db: Session,
    lb_count: int | None = None,
    cp_count: int | None = None,
    worker_count: int | None = None,
    exclude_cluster_id: str | None = None,
    extra_used_vm_ids: set[int] | None = None,
) -> dict[str, Any]:
    preferences = effective_preferences(db)
    counts = {
        "lb": lb_count or int(preferences["lb_count"]),
        "cp": cp_count or int(preferences["cp_count"]),
        "worker": worker_count or int(preferences["worker_count"]),
    }
    used_ips, used_ids = used_allocations(db, exclude_cluster_id)
    used_ids |= extra_used_vm_ids or set()
    used_ips |= parse_reserved_ips(str(preferences.get("reserved_ips", "")))
    network = ipaddress.IPv4Network(str(preferences["network_cidr"]), strict=True)
    used_ips |= {
        network.network_address,
        network.broadcast_address,
        ipaddress.IPv4Address(str(preferences["gateway"])),
    }
    vip = _first_ip_block(preferences["vip_pool_start"], preferences["vip_pool_end"], 1, used_ips)
    used_ips.add(ipaddress.IPv4Address(vip))
    result: dict[str, Any] = {"api_vip": vip, **{f"{key}_count": value for key, value in counts.items()}}
    for role in ("lb", "cp", "worker"):
        result[f"{role}_ip_start"] = _first_ip_block(preferences[f"{role}_ip_start"], preferences[f"{role}_ip_end"], counts[role], used_ips)
        first_ip = ipaddress.IPv4Address(result[f"{role}_ip_start"])
        used_ips |= {first_ip + offset for offset in range(counts[role])}
        result[f"{role}_vm_id_start"] = _first_id_block(int(preferences[f"{role}_vm_id_start"]), int(preferences[f"{role}_vm_id_end"]), counts[role], used_ids)
        used_ids |= set(range(result[f"{role}_vm_id_start"], result[f"{role}_vm_id_start"] + counts[role]))
    return result


def validate_preference_config(config: dict[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULT_PREFERENCES)
    merged.update({key: value for key, value in config.items() if key in DEFAULT_PREFERENCES})
    network = ipaddress.ip_network(str(merged["network_cidr"]), strict=True)
    if not isinstance(network, ipaddress.IPv4Network):
        raise ValueError("Nur IPv4-Netze werden unterstützt")
    gateway = ipaddress.IPv4Address(str(merged["gateway"]))
    if gateway not in network or gateway in (network.network_address, network.broadcast_address):
        raise ValueError("Gateway muss eine nutzbare Host-Adresse im VM-Netz sein")
    for dns_server in str(merged["dns_servers"]).split(","):
        ipaddress.IPv4Address(dns_server.strip())
    pod_network = ipaddress.ip_network(str(merged["pod_cidr"]), strict=True)
    service_network = ipaddress.ip_network(str(merged["service_cidr"]), strict=True)
    if network.overlaps(pod_network) or network.overlaps(service_network) or pod_network.overlaps(service_network):
        raise ValueError("VM-, Pod- und Service-Netze dürfen sich nicht überschneiden")
    parse_reserved_ips(str(merged.get("reserved_ips", "")))
    ip_ranges: dict[str, tuple[ipaddress.IPv4Address, ipaddress.IPv4Address]] = {}
    for role in ("vip_pool", "lb_ip", "cp_ip", "worker_ip"):
        start = ipaddress.IPv4Address(str(merged[f"{role}_start"]))
        end = ipaddress.IPv4Address(str(merged[f"{role}_end"]))
        if start not in network or end not in network or int(start) > int(end):
            raise ValueError(f"Ungültiger IP-Pool: {role}")
        if start in (network.network_address, network.broadcast_address) or end in (network.network_address, network.broadcast_address):
            raise ValueError(f"IP-Pool {role} darf Netz- und Broadcast-Adresse nicht enthalten")
        if int(start) <= int(gateway) <= int(end):
            raise ValueError(f"IP-Pool {role} darf das Gateway nicht enthalten")
        ip_ranges[role] = (start, end)
    range_names = list(ip_ranges)
    for index, left_name in enumerate(range_names):
        left_start, left_end = ip_ranges[left_name]
        for right_name in range_names[index + 1 :]:
            right_start, right_end = ip_ranges[right_name]
            if int(left_start) <= int(right_end) and int(right_start) <= int(left_end):
                raise ValueError(f"IP-Pools {left_name} und {right_name} überschneiden sich")
    id_ranges: dict[str, tuple[int, int]] = {}
    for role in ("lb", "cp", "worker"):
        start = int(merged[f"{role}_vm_id_start"])
        end = int(merged[f"{role}_vm_id_end"])
        if start < 100 or start > end:
            raise ValueError(f"Ungültiger VM-ID-Pool: {role}")
        id_ranges[role] = (start, end)
    id_roles = list(id_ranges)
    for index, left_role in enumerate(id_roles):
        left_start, left_end = id_ranges[left_role]
        for right_role in id_roles[index + 1 :]:
            right_start, right_end = id_ranges[right_role]
            if left_start <= right_end and right_start <= left_end:
                raise ValueError(f"VM-ID-Pools {left_role} und {right_role} überschneiden sich")
    if int(merged["cp_count"]) not in (3, 5, 7):
        raise ValueError("Control-Plane-Standardanzahl muss 3, 5 oder 7 sein")
    for role in ("lb", "cp", "worker"):
        count = int(merged[f"{role}_count"])
        if count < (2 if role == "lb" else 1):
            raise ValueError(f"Ungültige Standardanzahl: {role}")
        if role == "lb" and count > 10:
            raise ValueError("Höchstens zehn Load Balancer werden unterstützt")
        ip_capacity = int(ipaddress.IPv4Address(merged[f"{role}_ip_end"])) - int(ipaddress.IPv4Address(merged[f"{role}_ip_start"])) + 1
        id_capacity = int(merged[f"{role}_vm_id_end"]) - int(merged[f"{role}_vm_id_start"]) + 1
        if count > ip_capacity or count > id_capacity:
            raise ValueError(f"Der Pool für {role} ist kleiner als die Standardanzahl")
    for field in ("proxmox_node", "datastore", "bridge", "ssh_user"):
        value = str(merged[field]).strip()
        if not value or len(value) > 120:
            raise ValueError(f"Ungültiger Standardwert: {field}")
        merged[field] = value
    vlan_id = str(merged.get("vlan_id", "")).strip()
    if vlan_id:
        vlan_number = int(vlan_id)
        if not 1 <= vlan_number <= 4094:
            raise ValueError("VLAN-ID muss zwischen 1 und 4094 liegen")
        merged["vlan_id"] = vlan_number
    else:
        merged["vlan_id"] = ""
    if merged["kubernetes_version"] != "v1.36":
        raise ValueError("Derzeit wird nur Kubernetes v1.36 unterstützt")
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", str(merged["calico_version"])):
        raise ValueError("Ungültige Calico-Version")
    for role in ("lb", "cp", "worker"):
        cores = int(merged[f"{role}_cores"])
        memory = int(merged[f"{role}_memory"])
        disk = int(merged[f"{role}_disk"])
        if not 1 <= cores <= 256:
            raise ValueError(f"Ungültige Standard-CPU-Anzahl: {role}")
        if memory < 512:
            raise ValueError(f"Standard-RAM für {role} muss mindestens 512 MB betragen")
        if disk < 8:
            raise ValueError(f"Standard-Disk für {role} muss mindestens 8 GB betragen")
        merged[f"{role}_cores"] = cores
        merged[f"{role}_memory"] = memory
        merged[f"{role}_disk"] = disk
    replicas = int(merged["traefik_replicas"])
    http_port = int(merged["http_node_port"])
    https_port = int(merged["https_node_port"])
    if replicas < 1:
        raise ValueError("Traefik benötigt mindestens eine Replik")
    if not 30000 <= http_port <= 32767 or not 30000 <= https_port <= 32767:
        raise ValueError("Traefik-NodePorts müssen zwischen 30000 und 32767 liegen")
    if http_port == https_port:
        raise ValueError("HTTP- und HTTPS-NodePort müssen verschieden sein")
    merged["traefik_replicas"] = replicas
    merged["http_node_port"] = http_port
    merged["https_node_port"] = https_port
    merged["auto_suggest"] = bool(merged["auto_suggest"])
    merged["vm_name_include_cluster"] = bool(merged["vm_name_include_cluster"])
    merged["ingress_enabled"] = bool(merged["ingress_enabled"])
    return merged


def validate_cluster_allocations(db: Session, config: Any) -> None:
    """Reject allocations owned by another active builder cluster or reserved in settings."""
    preferences = effective_preferences(db)
    used_ips, used_ids = used_allocations(db, exclude_cluster_id=str(config.id))
    reserved_ips = parse_reserved_ips(str(preferences.get("reserved_ips", "")))
    existing = db.get(Cluster, str(config.id))
    existing_ips: set[ipaddress.IPv4Address] = set()
    if existing:
        if existing.config.get("network", {}).get("api_vip"):
            existing_ips.add(ipaddress.IPv4Address(existing.config["network"]["api_vip"]))
        existing_ips |= {ipaddress.IPv4Address(node["ip"]) for node in existing.config.get("nodes", [])}
    requested_ips = {config.network.api_vip, *(node.ip for node in config.nodes)}
    requested_ids = {node.vm_id for node in config.nodes}
    ip_conflicts = sorted(str(item) for item in requested_ips & (used_ips | (reserved_ips - existing_ips)))
    id_conflicts = sorted(requested_ids & used_ids)
    messages = []
    if ip_conflicts:
        messages.append("IPs bereits verwendet oder reserviert: " + ", ".join(ip_conflicts))
    if id_conflicts:
        messages.append("VM-IDs bereits von einem Cluster verwendet: " + ", ".join(map(str, id_conflicts)))
    if messages:
        raise ValueError("; ".join(messages))
