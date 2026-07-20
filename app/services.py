import ipaddress
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .generator import config_hash, render_cluster
from .allocations import validate_cluster_allocations
from .models import AuditEvent, Cluster, ClusterStatus, Credential, CredentialKind, Job, JobKind, JobStatus, User
from .proxmox import ProxmoxClient
from .schemas import ClusterConfig, ClusterType
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
    normalized_name = name.strip()
    if not normalized_name:
        raise ValueError("Credential-Name darf nicht leer sein")
    if db.scalar(select(Credential).where(Credential.name == normalized_name, Credential.kind == kind)):
        raise ValueError("Ein Credential mit diesem Namen und Typ existiert bereits")
    credential = Credential(
        name=normalized_name,
        kind=kind,
        encrypted_payload=encrypt_payload(secret_payload),
        public_data=public_data,
    )
    db.add(credential)
    try:
        db.flush()
        db.add(AuditEvent(action="create_credential", object_type="credential", object_id=credential.id, details={"name": name, "kind": kind.value}))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("Ein Credential mit diesem Namen und Typ existiert bereits") from exc
    return credential


def credential_payload(db: Session, reference: str, expected: CredentialKind) -> dict:
    credential_id = reference.removeprefix("credential://")
    credential = db.get(Credential, credential_id)
    if credential is None or credential.kind != expected:
        raise ValueError(f"Credential {reference} ist nicht verfügbar")
    return decrypt_payload(credential.encrypted_payload)


def bind_proxmox_credential(db: Session, form: dict[str, str]) -> dict[str, str]:
    """Return form values with trusted connection data from the selected credential.

    Endpoint and TLS policy used to be independently editable in the cluster
    wizard. Discovery then used the credential values while Terraform used the
    form values, so the two phases could contact different endpoints or use
    different certificate verification. The credential is now the single
    source of truth for both values.
    """
    reference = form.get("proxmox_credential", "")
    if not reference.startswith("credential://"):
        raise ValueError("Proxmox-Credential nicht gefunden")
    credential = db.get(Credential, reference.removeprefix("credential://"))
    if not credential or credential.kind != CredentialKind.PROXMOX:
        raise ValueError("Proxmox-Credential nicht gefunden")
    credential_payload(db, reference, CredentialKind.PROXMOX)
    endpoint = str(credential.public_data.get("endpoint", "")).strip()
    if not endpoint:
        raise ValueError("Proxmox-Credential enthält keinen Endpoint")
    trusted = dict(form)
    trusted["proxmox_endpoint"] = endpoint
    if bool(credential.public_data.get("verify_tls", True)):
        trusted["verify_tls"] = "on"
    else:
        trusted.pop("verify_tls", None)
    return trusted


def validate_template_disk_size(config: ClusterConfig, discovery: dict) -> int:
    """Validate every requested VM disk against the selected live template."""
    template = None
    resources = discovery.get("vms", []) if isinstance(discovery, dict) else []
    for item in resources if isinstance(resources, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            vm_id = int(item.get("vmid"))
        except (TypeError, ValueError):
            continue
        if (
            item.get("template") in (1, True, "1")
            and item.get("type") == "qemu"
            and item.get("node") == config.proxmox.node
            and vm_id == config.proxmox.template_vm_id
        ):
            template = item
            break
    if template is None:
        raise ValueError(
            f"QEMU-Template {config.proxmox.template_vm_id} wurde auf Node "
            f"{config.proxmox.node} nicht gefunden."
        )
    template_disk_gb = template.get("template_disk_gb")
    if isinstance(template_disk_gb, bool) or not isinstance(template_disk_gb, int) or template_disk_gb < 1:
        raise ValueError(
            f"Die Disk-Größe des Proxmox-Templates {config.proxmox.template_vm_id} "
            "konnte nicht ermittelt werden. Der Vorgang wurde aus Sicherheitsgründen gestoppt."
        )
    role_labels = {
        "loadbalancer": "Load-Balancer",
        "control_plane": "Control-Plane",
        "worker": "Worker",
    }
    errors = []
    for role, label in role_labels.items():
        role_disks = [node.disk_gb for node in config.nodes if node.role == role]
        configured_disk = min(role_disks) if role_disks else template_disk_gb
        if configured_disk < template_disk_gb:
            errors.append(
                f"Die konfigurierte {label}-Disk mit {configured_disk} GB ist kleiner als "
                f"die Disk des ausgewählten Proxmox-Templates mit {template_disk_gb} GB."
            )
    if errors:
        raise ValueError(" ".join(errors))
    return template_disk_gb


def validate_template_disk_sizes(config: ClusterConfig, discovery: dict) -> dict[int, int]:
    """Validate role disks against the live template selected for each role.

    The legacy validator remains untouched for kubeadm. Talos validates its
    control-plane/workers against the Talos template and its load balancers
    independently against the Ubuntu template.
    """
    if config.cluster_type == ClusterType.KUBEADM:
        disk = validate_template_disk_size(config, discovery)
        return {config.proxmox.template_vm_id: disk}

    role_configs: list[tuple[str, ClusterConfig, int]] = []
    talos_nodes = config.model_copy(deep=True)
    talos_nodes.nodes = [node for node in talos_nodes.nodes if node.role != "loadbalancer"]
    role_configs.append(("Talos-Template", talos_nodes, config.proxmox.template_vm_id))

    load_balancers = config.model_copy(deep=True)
    load_balancers.proxmox.template_vm_id = config.effective_load_balancer_template_vm_id
    load_balancers.nodes = [node for node in load_balancers.nodes if node.role == "loadbalancer"]
    role_configs.append(("Ubuntu-LB-Template", load_balancers, config.effective_load_balancer_template_vm_id))

    disks: dict[int, int] = {}
    errors: list[str] = []
    for label, role_config, template_id in role_configs:
        try:
            disks[template_id] = validate_template_disk_size(role_config, discovery)
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
    if errors:
        raise ValueError(" ".join(errors))
    return disks


def validate_current_template_disk(db: Session, config: ClusterConfig) -> int:
    """Fetch trusted Proxmox metadata and validate before persisting IaC input."""
    payload = credential_payload(db, config.proxmox.credential_ref, CredentialKind.PROXMOX)
    discovery = ProxmoxClient(
        config.proxmox.endpoint,
        payload["api_token"],
        config.proxmox.verify_tls,
    ).discover()
    return validate_template_disk_sizes(config, discovery)[config.proxmox.template_vm_id]


def build_cluster_from_form(form: dict[str, str], cluster_id: str | None = None) -> ClusterConfig:
    cluster_id = cluster_id or str(uuid.uuid4())
    cluster_type = ClusterType(form.get("cluster_type", ClusterType.KUBEADM.value))
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
    ssh = None
    load_balancer_ssh = None
    talos = None
    if cluster_type == ClusterType.KUBEADM:
        ssh = {
            "user": form["ssh_user"].strip(),
            "port": 22,
            "public_key": form["ssh_public_key"].strip(),
            "credential_ref": form["ssh_credential"],
        }
    else:
        load_balancer_ssh = {
            "user": form["lb_ssh_user"].strip(),
            "port": 22,
            "public_key": form["lb_ssh_public_key"].strip(),
            "credential_ref": form["lb_ssh_credential"],
        }
        talos = {
            "version": form["talos_version"].strip(),
            "install_disk": form["talos_install_disk"].strip(),
            "network_interface": form.get("talos_network_interface", "eth0").strip(),
            "template_platform": "nocloud",
        }
    return ClusterConfig.model_validate({
        "schema_version": 1,
        "id": cluster_id,
        "name": form["name"].strip(),
        "cluster_type": cluster_type,
        "proxmox": {
            "endpoint": form["proxmox_endpoint"].strip(),
            "node": form["proxmox_node"].strip(),
            "datastore": form["datastore"].strip(),
            "template_vm_id": int(form["template_vm_id"]),
            "load_balancer_template_vm_id": (
                int(form["load_balancer_template_vm_id"])
                if cluster_type == ClusterType.TALOS
                else None
            ),
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
        "ssh": ssh,
        "load_balancer_ssh": load_balancer_ssh,
        "talos": talos,
        "kubernetes": {
            "version": form["kubernetes_version"].strip(),
            "api_port": 6443,
            "pod_cidr": form["pod_cidr"],
            "service_cidr": form["service_cidr"],
        },
        "registry_enabled": form.get("registry_enabled") == "on",
        "registry_endpoint": form.get("registry_endpoint"),
        "registry_use_http": form.get("registry_use_http") == "on",
        "nodes": nodes,
        "addons": {
            "cni": {"provider": "calico", "version": form["calico_version"].strip()},
            "ingress": {
                "enabled": form.get("ingress_enabled") == "on",
                "provider": "traefik",
                "replicas": int(form["traefik_replicas"]),
                "http_node_port": int(form["http_node_port"]),
                "https_node_port": int(form["https_node_port"]),
                "chart_version": "40.2.0",
            },
        },
    })


def save_cluster(db: Session, config: ClusterConfig, data_root: Path, source_root: Path) -> Cluster:
    validate_cluster_allocations(db, config)
    public = config.public_dict()
    digest = config_hash(public)
    cluster = db.get(Cluster, config.id)
    configuration_changed = False
    duplicate = db.scalar(select(Cluster).where(Cluster.name == config.name, Cluster.id != config.id))
    if duplicate:
        raise ValueError("Ein Cluster mit diesem Namen existiert bereits")
    if cluster is None:
        cluster = Cluster(id=config.id, name=config.name, config=public, config_hash=digest)
        db.add(cluster)
    else:
        # Defaults added in newer releases must not turn a semantically
        # unchanged legacy JSON document into a runtime-invalidating edit.
        # Keep the original JSON and hash until the user changes a real value.
        existing_public = ClusterConfig.model_validate(cluster.config).public_dict()
        configuration_changed = existing_public != public
        if not configuration_changed:
            digest = cluster.config_hash
        if configuration_changed and cluster.applied_hash and not cluster.applied_vm_ids:
            cluster.applied_vm_ids = [int(node["vm_id"]) for node in cluster.config.get("nodes", [])]
        cluster.name = config.name
        if configuration_changed:
            cluster.config = public
            cluster.config_hash = digest
            cluster.status = ClusterStatus.DRAFT
            cluster.planned_hash = None
            cluster.destroy_planned_hash = None
    render_cluster(config, data_root / "clusters" / config.id, source_root)
    db.add(AuditEvent(action="save_cluster", object_type="cluster", object_id=config.id, details={"config_hash": digest}))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("Ein Cluster mit diesem Namen existiert bereits") from exc
    if configuration_changed:
        # An old kubeconfig must never look valid for newly edited addresses.
        (data_root / "clusters" / config.id / "kubeconfig").unlink(missing_ok=True)
    return cluster


def cleanup_cluster_job_history(db: Session, cluster_id: str, retention_keep: int) -> dict[str, int]:
    """Delete only completed jobs beyond the configured retention window."""
    terminal_statuses = (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)
    finished = db.scalars(
        select(Job)
        .where(Job.cluster_id == cluster_id, Job.status.in_(terminal_statuses))
        .order_by(Job.created_at.desc(), Job.id.desc())
    ).all()
    active_ignored = len(db.scalars(
        select(Job.id).where(
            Job.cluster_id == cluster_id,
            Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
        )
    ).all())
    kept = finished[:retention_keep]
    for job in finished[retention_keep:]:
        db.delete(job)
    return {
        "deleted": max(0, len(finished) - len(kept)),
        "kept": len(kept),
        "active_ignored": active_ignored,
        "retention_limit": retention_keep,
    }


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
    runtime_jobs = {
        JobKind.ANSIBLE,
        JobKind.VERIFY,
        JobKind.MANIFEST_VALIDATE,
        JobKind.MANIFEST_DIFF,
        JobKind.MANIFEST_APPLY,
        JobKind.MANIFEST_DELETE,
    }
    if kind in runtime_jobs and cluster.applied_hash != cluster.config_hash:
        raise ValueError("Die gespeicherte Konfiguration wurde noch nicht erfolgreich mit Terraform angewendet")
    job = Job(cluster_id=cluster.id, kind=kind, requested_config_hash=cluster.config_hash, payload=payload or {})
    db.add(job)
    db.add(AuditEvent(action=f"queue_{kind.value}", object_type="cluster", object_id=cluster.id))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("Ein Cluster mit diesem Namen existiert bereits") from exc
    return job
