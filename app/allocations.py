import ipaddress
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
    "reserved_ips": "",
}


def get_preferences(db: Session) -> Preference:
    preference = db.get(Preference, 1)
    if preference is None:
        preference = Preference(id=1, config=dict(DEFAULT_PREFERENCES))
        db.add(preference)
        db.commit()
    else:
        merged = dict(DEFAULT_PREFERENCES)
        merged.update(preference.config or {})
        preference.config = merged
    return preference


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
                raise ValueError(f"Reservierter Bereich {network} ist zu groÃŸ (maximal 4096 Adressen)")
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
    preferences = get_preferences(db).config
    counts = {
        "lb": lb_count or int(preferences["lb_count"]),
        "cp": cp_count or int(preferences["cp_count"]),
        "worker": worker_count or int(preferences["worker_count"]),
    }
    used_ips, used_ids = used_allocations(db, exclude_cluster_id)
    used_ids |= extra_used_vm_ids or set()
    used_ips |= parse_reserved_ips(str(preferences.get("reserved_ips", "")))
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
    merged.update(config)
    network = ipaddress.ip_network(str(merged["network_cidr"]), strict=True)
    if not isinstance(network, ipaddress.IPv4Network):
        raise ValueError("Nur IPv4-Netze werden unterstützt")
    ipaddress.IPv4Address(str(merged["gateway"]))
    for dns_server in str(merged["dns_servers"]).split(","):
        ipaddress.IPv4Address(dns_server.strip())
    pod_network = ipaddress.ip_network(str(merged["pod_cidr"]), strict=True)
    service_network = ipaddress.ip_network(str(merged["service_cidr"]), strict=True)
    if network.overlaps(pod_network) or network.overlaps(service_network) or pod_network.overlaps(service_network):
        raise ValueError("VM-, Pod- und Service-Netze dÃ¼rfen sich nicht Ã¼berschneiden")
    parse_reserved_ips(str(merged.get("reserved_ips", "")))
    for role in ("vip_pool", "lb_ip", "cp_ip", "worker_ip"):
        start = ipaddress.IPv4Address(str(merged[f"{role}_start"]))
        end = ipaddress.IPv4Address(str(merged[f"{role}_end"]))
        if start not in network or end not in network or int(start) > int(end):
            raise ValueError(f"Ungültiger IP-Pool: {role}")
    for role in ("lb", "cp", "worker"):
        start = int(merged[f"{role}_vm_id_start"])
        end = int(merged[f"{role}_vm_id_end"])
        if start < 100 or start > end:
            raise ValueError(f"Ungültiger VM-ID-Pool: {role}")
    if int(merged["cp_count"]) not in (3, 5, 7):
        raise ValueError("Control-Plane-Standardanzahl muss 3, 5 oder 7 sein")
    for role in ("lb", "cp", "worker"):
        count = int(merged[f"{role}_count"])
        if count < (2 if role == "lb" else 1):
            raise ValueError(f"UngÃ¼ltige Standardanzahl: {role}")
        ip_capacity = int(ipaddress.IPv4Address(merged[f"{role}_ip_end"])) - int(ipaddress.IPv4Address(merged[f"{role}_ip_start"])) + 1
        id_capacity = int(merged[f"{role}_vm_id_end"]) - int(merged[f"{role}_vm_id_start"]) + 1
        if count > ip_capacity or count > id_capacity:
            raise ValueError(f"Der Pool fÃ¼r {role} ist kleiner als die Standardanzahl")
    return merged


def validate_cluster_allocations(db: Session, config: Any) -> None:
    """Reject allocations owned by another active builder cluster or reserved in settings."""
    preferences = get_preferences(db).config
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
