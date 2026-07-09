import ipaddress
import json
import re
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .generator import config_hash, render_cluster
from .allocations import validate_cluster_allocations
from .models import AuditEvent, Cluster, Credential, CredentialKind, Job, JobKind, JobStatus, User
from .schemas import ClusterConfig
from .security import decrypt_payload, encrypt_payload, hash_password


def bootstrap_database(db: Session, admin_password: str) -> None:
    if db.scalar(select(User).where(User.username == "admin")) is None:
        db.add(User(username="admin", password_hash=hash_password(admin_password)))
        db.add(AuditEvent(action="bootstrap_admin", object_type="user"))
        db.commit()


def store_credential(
    db: Session,
    *,
    name: str,
    kind: CredentialKind,
    secret_payload: dict,
    public_data: dict,
) -> Credential:
    credential = Credential(
        name=name.strip(),
        kind=kind,
        encrypted_payload=encrypt_payload(secret_payload),
        public_data=public_data,
    )
    db.add(credential)
    db.flush()
    db.add(AuditEvent(action="create_credential", object_type="credential", object_id=credential.id, details={"name": name, "kind": kind.value}))
    db.commit()
    return credential


def credential_payload(db: Session, reference: str, expected: CredentialKind) -> dict:
    credential_id = reference.removeprefix("credential://")
    credential = db.get(Credential, credential_id)
    if credential is None or credential.kind != expected:
        raise ValueError(f"Credential {reference} ist nicht verfügbar")
    return decrypt_payload(credential.encrypted_payload)


def build_cluster_from_form(form: dict[str, str], cluster_id: str | None = None) -> ClusterConfig:
    cluster_id = cluster_id or str(uuid.uuid4())
    network = ipaddress.ip_network(form["network_cidr"], strict=True)
    role_specs = [
        ("loadbalancer", "lb", int(form["lb_count"]), form["lb_ip_start"], int(form["lb_vm_id_start"]), int(form["lb_cores"]), int(form["lb_memory"]), int(form["lb_disk"])),
        ("control_plane", "control", int(form["cp_count"]), form["cp_ip_start"], int(form["cp_vm_id_start"]), int(form["cp_cores"]), int(form["cp_memory"]), int(form["cp_disk"])),
        ("worker", "worker", int(form["worker_count"]), form["worker_ip_start"], int(form["worker_vm_id_start"]), int(form["worker_cores"]), int(form["worker_memory"]), int(form["worker_disk"])),
    ]
    nodes = []
    for role, prefix, count, ip_start, vm_start, cores, memory, disk in role_specs:
        first_ip = ipaddress.ip_address(ip_start)
        for offset in range(count):
            nodes.append({
                "name": f"{prefix}-{offset + 1:02d}",
                "role": role,
                "vm_id": vm_start + offset,
                "ip": str(first_ip + offset),
                "cores": cores,
                "memory_mb": memory,
                "disk_gb": disk,
            })
    public_key = form["ssh_public_key"].strip()
    return ClusterConfig.model_validate({
        "schema_version": 1,
        "id": cluster_id,
        "name": form["name"].strip(),
        "proxmox": {
            "endpoint": form["proxmox_endpoint"].strip(),
            "node": form["proxmox_node"].strip(),
            "datastore": form["datastore"].strip(),
            "template_vm_id": int(form["template_vm_id"]),
            "bridge": form["bridge"].strip(),
            "vlan_id": int(form["vlan_id"]) if form.get("vlan_id", "").strip() else None,
            "verify_tls": form.get("verify_tls") == "on",
            "vm_name_include_cluster": form.get("vm_name_include_cluster") == "on",
            "credential_ref": form["proxmox_credential"],
        },
        "network": {
            "cidr": str(network),
            "gateway": form["gateway"],
            "dns_servers": [item.strip() for item in form["dns_servers"].split(",") if item.strip()],
            "api_vip": form["api_vip"],
        },
        "ssh": {
            "user": form["ssh_user"].strip(),
            "port": int(form.get("ssh_port", 22)),
            "public_key": public_key,
            "credential_ref": form["ssh_credential"],
        },
        "kubernetes": {
            "version": form["kubernetes_version"].strip(),
            "api_port": int(form["api_port"]),
            "pod_cidr": form["pod_cidr"],
            "service_cidr": form["service_cidr"],
        },
        "nodes": nodes,
        "addons": {
            "cni": {"provider": "calico", "version": form["calico_version"].strip()},
            "ingress": {
                "enabled": form.get("ingress_enabled") == "on",
                "provider": "traefik",
                "replicas": int(form["traefik_replicas"]),
                "http_node_port": int(form["http_node_port"]),
                "https_node_port": int(form["https_node_port"]),
            },
        },
    })


def save_cluster(db: Session, config: ClusterConfig, data_root: Path, source_root: Path) -> Cluster:
    validate_cluster_allocations(db, config)
    public = config.public_dict()
    digest = config_hash(public)
    cluster = db.get(Cluster, config.id)
    if cluster is None:
        cluster = Cluster(id=config.id, name=config.name, config=public, config_hash=digest)
        db.add(cluster)
    else:
        cluster.name = config.name
        cluster.config = public
        cluster.config_hash = digest
        cluster.planned_hash = None
        cluster.destroy_planned_hash = None
    render_cluster(config, data_root / "clusters" / config.id, source_root)
    db.add(AuditEvent(action="save_cluster", object_type="cluster", object_id=config.id, details={"config_hash": digest}))
    db.commit()
    return cluster


def queue_job(db: Session, cluster: Cluster, kind: JobKind, payload: dict | None = None) -> Job:
    cluster = db.scalar(select(Cluster).where(Cluster.id == cluster.id).with_for_update())
    if cluster is None:
        raise ValueError("Cluster nicht gefunden")
    mutating = [JobStatus.QUEUED, JobStatus.RUNNING]
    existing = db.scalar(select(Job).where(Job.cluster_id == cluster.id, Job.status.in_(mutating)))
    if existing:
        raise ValueError("Für diesen Cluster läuft bereits ein Job")
    if kind == JobKind.APPLY and cluster.planned_hash != cluster.config_hash:
        raise ValueError("Konfiguration wurde seit dem Terraform-Plan geändert")
    if kind == JobKind.DESTROY and cluster.destroy_planned_hash != cluster.config_hash:
        raise ValueError("Es liegt kein aktueller bestätigbarer Destroy-Plan vor")
    job = Job(cluster_id=cluster.id, kind=kind, requested_config_hash=cluster.config_hash, payload=payload or {})
    db.add(job)
    db.add(AuditEvent(action=f"queue_{kind.value}", object_type="cluster", object_id=cluster.id))
    db.commit()
    return job


def safe_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", value)
