import asyncio
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .allocations import get_preferences, suggest_allocations, used_allocations, validate_preference_config
from .db import Base, SessionLocal, engine, get_db
from .manifests import APPLICATION_TEMPLATES, create_revision, render_application_template, validate_manifest_content, validate_manifest_path
from .models import AuditEvent, ApplicationBundle, Cluster, ClusterStatus, Credential, CredentialKind, Job, JobKind, JobStatus, ManifestFile, ManifestRevision, User, utcnow
from .kubectl_terminal import audit_safe_command, parse_kubectl_command
from .proxmox import ProxmoxClient, ProxmoxError, split_token
from .security import validate_ssh_keypair, verify_password
from .services import bind_proxmox_credential, bootstrap_database, build_cluster_from_form, credential_payload, queue_job, save_cluster, store_credential
from .terraform_state import managed_vm_ids


settings = get_settings()


def cluster_runtime_is_current(cluster: Cluster) -> bool:
    return cluster.applied_hash is not None and cluster.applied_hash == cluster.config_hash


def sidebar_context(request: Request) -> dict:
    with SessionLocal() as db:
        clusters = db.scalars(select(Cluster).order_by(Cluster.name)).all()
        user_id = request.session.get("user_id")
        user = db.get(User, user_id) if user_id else None
        return {"sidebar_clusters": clusters, "sidebar_username": user.username if user else None}


templates = Jinja2Templates(
    directory=Path(__file__).parent / "templates",
    context_processors=[sidebar_context],
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        bootstrap_database(db, settings.initial_admin_password.get_secret_value())
    yield


app = FastAPI(title="Proxmox Kubernetes Cluster Builder", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret.get_secret_value(),
    https_only=settings.session_https_only,
    same_site="strict",
    max_age=8 * 60 * 60,
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id else None
    if not user or not user.enabled:
        raise HTTPException(status_code=401)
    return user


@app.exception_handler(401)
async def unauthenticated(_: Request, __: HTTPException):
    return RedirectResponse("/login", status_code=303)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@app.post("/login")
def login(request: Request, username: str = Form(), password: str = Form(), db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == username))
    if not user or not verify_password(user.password_hash, password):
        return templates.TemplateResponse(request, "login.html", {"error": "Anmeldung fehlgeschlagen"}, status_code=400)
    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _: User = Depends(current_user), db: Session = Depends(get_db)):
    clusters = db.scalars(select(Cluster).order_by(Cluster.created_at.desc())).all()
    return templates.TemplateResponse(request, "dashboard.html", {"clusters": clusters})


@app.get("/credentials", response_class=HTMLResponse)
def credentials_page(request: Request, _: User = Depends(current_user), db: Session = Depends(get_db)):
    credentials = db.scalars(select(Credential).order_by(Credential.created_at.desc())).all()
    active_clusters = db.scalars(select(Cluster).where(Cluster.status != ClusterStatus.DESTROYED)).all()
    used_credential_ids = credential_ids_used_by(active_clusters)
    return templates.TemplateResponse(request, "credentials.html", {"credentials": credentials, "used_credential_ids": used_credential_ids})


def credential_ids_used_by(clusters: list[Cluster]) -> set[str]:
    used: set[str] = set()
    for cluster in clusters:
        for section in ("proxmox", "ssh"):
            reference = cluster.config.get(section, {}).get("credential_ref")
            if isinstance(reference, str) and reference.startswith("credential://"):
                used.add(reference.removeprefix("credential://"))
    return used


@app.post("/credentials/proxmox")
def create_proxmox_credential(
    name: str = Form(), endpoint: str = Form(), api_token: str = Form(), verify_tls: bool = Form(False),
    _: User = Depends(current_user), db: Session = Depends(get_db),
):
    try:
        endpoint = endpoint.strip().rstrip("/") + "/"
        split_token(api_token)
        client = ProxmoxClient(endpoint, api_token, verify_tls)
        client.get("version")
        store_credential(db, name=name, kind=CredentialKind.PROXMOX, secret_payload={"api_token": api_token}, public_data={"endpoint": endpoint, "verify_tls": verify_tls})
    except (ValueError, ProxmoxError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse("/credentials", status_code=303)


@app.post("/credentials/ssh")
def create_ssh_credential(
    name: str = Form(), private_key: str = Form(), public_key: str = Form(),
    _: User = Depends(current_user), db: Session = Depends(get_db),
):
    try:
        validate_ssh_keypair(private_key, public_key)
        store_credential(db, name=name, kind=CredentialKind.SSH, secret_payload={"private_key": private_key}, public_data={"public_key": public_key.strip()})
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse("/credentials", status_code=303)


@app.post("/credentials/ssh/generate")
def generate_ssh_credential(name: str = Form(), _: User = Depends(current_user), db: Session = Depends(get_db)):
    key = Ed25519PrivateKey.generate()
    private_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()).decode()
    public_key = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode() + " cluster-builder"
    try:
        store_credential(db, name=name, kind=CredentialKind.SSH, secret_payload={"private_key": private_key}, public_data={"public_key": public_key})
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse("/credentials", status_code=303)


@app.post("/credentials/{credential_id}/delete")
def delete_credential(credential_id: str, _: User = Depends(current_user), db: Session = Depends(get_db)):
    credential = db.get(Credential, credential_id)
    if not credential:
        raise HTTPException(404)
    active_clusters = db.scalars(select(Cluster).where(Cluster.status != ClusterStatus.DESTROYED)).all()
    used_by = [
        cluster.name
        for cluster in active_clusters
        if credential.id in credential_ids_used_by([cluster])
    ]
    if used_by:
        raise HTTPException(409, "Credential wird noch von aktiven Clustern verwendet: " + ", ".join(sorted(used_by)))
    db.add(AuditEvent(action="delete_credential", object_type="credential", object_id=credential.id, details={"name": credential.name, "kind": credential.kind.value}))
    db.delete(credential)
    db.commit()
    return RedirectResponse("/credentials", status_code=303)


@app.get("/api/proxmox/{credential_id}/discover")
def discover_proxmox(credential_id: str, _: User = Depends(current_user), db: Session = Depends(get_db)):
    credential = db.get(Credential, credential_id)
    if not credential or credential.kind != CredentialKind.PROXMOX:
        raise HTTPException(404, "Credential nicht gefunden")
    payload = credential_payload(db, f"credential://{credential.id}", CredentialKind.PROXMOX)
    try:
        return ProxmoxClient(credential.public_data["endpoint"], payload["api_token"], credential.public_data.get("verify_tls", True)).discover()
    except ProxmoxError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/clusters/new", response_class=HTMLResponse)
def new_cluster(request: Request, _: User = Depends(current_user), db: Session = Depends(get_db)):
    preference = get_preferences(db).config
    values = {
        "network_cidr": preference["network_cidr"], "gateway": preference["gateway"],
        "dns_servers": preference["dns_servers"], "pod_cidr": preference["pod_cidr"],
        "service_cidr": preference["service_cidr"], "lb_count": str(preference["lb_count"]),
        "cp_count": str(preference["cp_count"]), "worker_count": str(preference["worker_count"]),
        "vm_name_include_cluster": "on" if preference["vm_name_include_cluster"] else "",
    }
    if preference.get("auto_suggest", True):
        try:
            values.update({key: str(value) for key, value in suggest_allocations(db).items()})
        except ValueError as exc:
            return render_wizard(request, db, str(exc), values, 400)
    return render_wizard(request, db, values=values)


def render_wizard(request: Request, db: Session, error: str | None = None, values: dict | None = None, status_code: int = 200):
    credentials = db.scalars(select(Credential).order_by(Credential.name)).all()
    used_ips, used_ids = used_allocations(db, request.path_params.get("cluster_id"))
    return templates.TemplateResponse(request, "wizard.html", {
        "credentials": credentials, "error": error, "values": values or {},
        "used_ips": sorted(map(str, used_ips)), "used_vm_ids": sorted(used_ids),
        "action": request.url.path if request.url.path.endswith("/edit") else "/clusters",
    }, status_code=status_code)


@app.post("/clusters")
async def create_cluster(request: Request, _: User = Depends(current_user), db: Session = Depends(get_db)):
    form_data = await request.form()
    values = {key: str(value) for key, value in form_data.items()}
    try:
        values = bind_proxmox_credential(db, values)
        config = build_cluster_from_form(values)
        ssh_id = config.ssh.credential_ref.removeprefix("credential://")
        ssh_credential = db.get(Credential, ssh_id)
        if not ssh_credential or ssh_credential.kind != CredentialKind.SSH:
            raise ValueError("SSH-Credential nicht gefunden")
        if config.ssh.public_key != ssh_credential.public_data.get("public_key"):
            raise ValueError("Public Key passt nicht zum ausgewählten SSH-Credential")
        cluster = save_cluster(db, config, settings.data_root, settings.source_root)
    except (ValidationError, ValueError, KeyError) as exc:
        return render_wizard(request, db, str(exc), values, 400)
    return RedirectResponse(f"/clusters/{cluster.id}", status_code=303)


def cluster_form_values(cluster: Cluster) -> dict[str, str]:
    config = cluster.config
    roles = {role: [node for node in config["nodes"] if node["role"] == role] for role in ("loadbalancer", "control_plane", "worker")}
    values = {
        "name": config["name"], "proxmox_credential": config["proxmox"]["credential_ref"], "proxmox_endpoint": config["proxmox"]["endpoint"],
        "proxmox_node": config["proxmox"]["node"], "datastore": config["proxmox"]["datastore"], "template_vm_id": str(config["proxmox"]["template_vm_id"]),
        "bridge": config["proxmox"]["bridge"], "vlan_id": str(config["proxmox"].get("vlan_id") or ""),
        "vm_name_include_cluster": "on" if config["proxmox"].get("vm_name_include_cluster", False) else "",
        "network_cidr": config["network"]["cidr"], "gateway": config["network"]["gateway"], "dns_servers": ", ".join(config["network"]["dns_servers"]), "api_vip": config["network"]["api_vip"],
        "pod_cidr": config["kubernetes"]["pod_cidr"], "service_cidr": config["kubernetes"]["service_cidr"], "kubernetes_version": config["kubernetes"]["version"],
        "ssh_credential": config["ssh"]["credential_ref"], "ssh_user": config["ssh"]["user"], "ssh_public_key": config["ssh"]["public_key"],
        "calico_version": config["addons"]["cni"]["version"], "ingress_enabled": "on" if config["addons"]["ingress"]["enabled"] else "", "traefik_replicas": str(config["addons"]["ingress"]["replicas"]),
        "http_node_port": str(config["addons"]["ingress"]["http_node_port"]), "https_node_port": str(config["addons"]["ingress"]["https_node_port"]),
    }
    mapping = {"loadbalancer": "lb", "control_plane": "cp", "worker": "worker"}
    for role, prefix in mapping.items():
        nodes = roles[role]
        first = nodes[0]
        values.update({f"{prefix}_count": str(len(nodes)), f"{prefix}_ip_start": first["ip"], f"{prefix}_vm_id_start": str(first["vm_id"]), f"{prefix}_cores": str(first["cores"]), f"{prefix}_memory": str(first["memory_mb"]), f"{prefix}_disk": str(first["disk_gb"])})
    return values


@app.get("/api/allocations/suggest")
def allocation_suggestion(
    lb_count: int = 2, cp_count: int = 3, worker_count: int = 2,
    exclude_cluster_id: str | None = None, credential_id: str | None = None,
    _: User = Depends(current_user), db: Session = Depends(get_db),
):
    try:
        if lb_count < 2 or cp_count not in (3, 5, 7) or worker_count < 1:
            raise ValueError("Ungültige Knotenzahl")
        external_ids: set[int] = set()
        if credential_id:
            credential = db.get(Credential, credential_id)
            if not credential or credential.kind != CredentialKind.PROXMOX:
                raise ValueError("Proxmox-Credential nicht gefunden")
            payload = credential_payload(db, f"credential://{credential.id}", CredentialKind.PROXMOX)
            discovery = ProxmoxClient(credential.public_data["endpoint"], payload["api_token"], credential.public_data.get("verify_tls", True)).discover()
            external_ids = {int(vm["vmid"]) for vm in discovery.get("vms", []) if vm.get("vmid") is not None}
        return suggest_allocations(db, lb_count, cp_count, worker_count, exclude_cluster_id, external_ids)
    except (ValueError, ProxmoxError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/settings", response_class=HTMLResponse)
def preferences_page(request: Request, _: User = Depends(current_user), db: Session = Depends(get_db)):
    used_ips, used_ids = used_allocations(db)
    return templates.TemplateResponse(request, "settings.html", {
        "preferences": get_preferences(db).config,
        "used_ips": sorted(map(str, used_ips)), "used_vm_ids": sorted(used_ids),
    })


@app.post("/settings")
async def update_preferences(request: Request, _: User = Depends(current_user), db: Session = Depends(get_db)):
    form = await request.form()
    integer_fields = {
        "lb_vm_id_start", "lb_vm_id_end", "cp_vm_id_start", "cp_vm_id_end",
        "worker_vm_id_start", "worker_vm_id_end", "lb_count", "cp_count", "worker_count",
    }
    try:
        config = {key: (int(value) if key in integer_fields else str(value).strip()) for key, value in form.items()}
        config["auto_suggest"] = "auto_suggest" in form
        config["vm_name_include_cluster"] = "vm_name_include_cluster" in form
        validated = validate_preference_config(config)
    except (ValueError, TypeError) as exc:
        used_ips, used_ids = used_allocations(db)
        return templates.TemplateResponse(request, "settings.html", {
            "preferences": {
                **get_preferences(db).config, **{key: str(value) for key, value in form.items()},
                "auto_suggest": "auto_suggest" in form,
                "vm_name_include_cluster": "vm_name_include_cluster" in form,
            }, "error": str(exc),
            "used_ips": sorted(map(str, used_ips)), "used_vm_ids": sorted(used_ids),
        }, status_code=400)
    preference = get_preferences(db)
    preference.config = validated
    db.add(AuditEvent(action="update_preferences", object_type="preference", object_id="1"))
    db.commit()
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.get("/clusters/{cluster_id}/edit", response_class=HTMLResponse)
def edit_cluster_page(cluster_id: str, request: Request, _: User = Depends(current_user), db: Session = Depends(get_db)):
    cluster = db.get(Cluster, cluster_id)
    if not cluster:
        raise HTTPException(404)
    return render_wizard(request, db, values=cluster_form_values(cluster))


@app.post("/clusters/{cluster_id}/edit")
async def update_cluster(cluster_id: str, request: Request, _: User = Depends(current_user), db: Session = Depends(get_db)):
    cluster = db.get(Cluster, cluster_id)
    if not cluster:
        raise HTTPException(404)
    active = db.scalar(select(Job).where(Job.cluster_id == cluster_id, Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])))
    if active:
        raise HTTPException(409, "Cluster kann während eines laufenden Jobs nicht bearbeitet werden")
    form_data = await request.form()
    values = {key: str(value) for key, value in form_data.items()}
    try:
        values = bind_proxmox_credential(db, values)
        config = build_cluster_from_form(values, cluster_id)
        ssh_credential = db.get(Credential, config.ssh.credential_ref.removeprefix("credential://"))
        if not ssh_credential or ssh_credential.kind != CredentialKind.SSH or config.ssh.public_key != ssh_credential.public_data.get("public_key"):
            raise ValueError("SSH-Credential und Public Key passen nicht zusammen")
        save_cluster(db, config, settings.data_root, settings.source_root)
    except (ValidationError, ValueError, KeyError) as exc:
        return render_wizard(request, db, str(exc), values, 400)
    return RedirectResponse(f"/clusters/{cluster_id}", status_code=303)


@app.get("/clusters/{cluster_id}", response_class=HTMLResponse)
def cluster_detail(cluster_id: str, request: Request, _: User = Depends(current_user), db: Session = Depends(get_db)):
    cluster = db.get(Cluster, cluster_id)
    if not cluster:
        raise HTTPException(404)
    jobs = db.scalars(select(Job).where(Job.cluster_id == cluster.id).order_by(Job.created_at.desc())).all()
    workspace = settings.data_root / "clusters" / cluster.id
    runtime_current = cluster_runtime_is_current(cluster)
    kubeconfig_available = runtime_current and (workspace / "kubeconfig").is_file()
    terraform_state_available = runtime_current and (workspace / "terraform" / "terraform.tfstate").is_file()
    return templates.TemplateResponse(request, "cluster.html", {
        "cluster": cluster,
        "jobs": jobs,
        "kubeconfig_available": kubeconfig_available,
        "terraform_state_available": terraform_state_available,
    })


@app.get("/clusters/{cluster_id}/terminal", response_class=HTMLResponse)
def cluster_terminal(cluster_id: str, request: Request, _: User = Depends(current_user), db: Session = Depends(get_db)):
    cluster = db.get(Cluster, cluster_id)
    if not cluster:
        raise HTTPException(404)
    kubeconfig_available = cluster_runtime_is_current(cluster) and (settings.data_root / "clusters" / cluster.id / "kubeconfig").is_file()
    return templates.TemplateResponse(request, "terminal.html", {"cluster": cluster, "kubeconfig_available": kubeconfig_available})


@app.get("/clusters/{cluster_id}/applications", response_class=HTMLResponse)
def applications_page(cluster_id: str, request: Request, _: User = Depends(current_user), db: Session = Depends(get_db)):
    cluster = db.get(Cluster, cluster_id)
    if not cluster:
        raise HTTPException(404)
    bundles = db.scalars(select(ApplicationBundle).where(ApplicationBundle.cluster_id == cluster_id).order_by(ApplicationBundle.name)).all()
    return templates.TemplateResponse(request, "applications.html", {
        "cluster": cluster,
        "bundles": bundles,
        "application_templates": APPLICATION_TEMPLATES,
    })


@app.post("/clusters/{cluster_id}/applications")
def create_application(
    cluster_id: str, name: str = Form(), description: str = Form(""), template_id: str = Form("blank"),
    _: User = Depends(current_user), db: Session = Depends(get_db),
):
    cluster = db.get(Cluster, cluster_id)
    normalized = name.strip().lower()
    if not cluster:
        raise HTTPException(404)
    if (
        not normalized
        or len(normalized) > 63
        or normalized[0] == "-"
        or normalized[-1] == "-"
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in normalized)
    ):
        raise HTTPException(400, "Anwendungsname darf nur Kleinbuchstaben, Zahlen und Bindestriche enthalten")
    if db.scalar(select(ApplicationBundle).where(ApplicationBundle.cluster_id == cluster_id, ApplicationBundle.name == normalized)):
        raise HTTPException(409, "Eine Anwendung mit diesem Namen existiert bereits")
    try:
        template_files = render_application_template(template_id, normalized)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    bundle = ApplicationBundle(cluster_id=cluster_id, name=normalized, description=description.strip()[:255])
    db.add(bundle)
    db.flush()
    for path, content in template_files.items():
        bundle.files.append(ManifestFile(path=path, content=content))
    db.flush()
    create_revision(db, bundle, "Anwendung erstellt")
    db.add(AuditEvent(action="create_application", object_type="application", object_id=bundle.id, details={"cluster_id": cluster_id, "name": normalized, "template": template_id}))
    db.commit()
    return RedirectResponse(f"/clusters/{cluster_id}/applications/{bundle.id}", status_code=303)


@app.get("/clusters/{cluster_id}/applications/{bundle_id}", response_class=HTMLResponse)
def application_editor(cluster_id: str, bundle_id: str, request: Request, _: User = Depends(current_user), db: Session = Depends(get_db)):
    cluster = db.get(Cluster, cluster_id)
    bundle = db.get(ApplicationBundle, bundle_id)
    if not cluster or not bundle or bundle.cluster_id != cluster_id:
        raise HTTPException(404)
    files = db.scalars(select(ManifestFile).where(ManifestFile.bundle_id == bundle.id).order_by(ManifestFile.path)).all()
    selected_id = request.query_params.get("file")
    selected = next((item for item in files if item.id == selected_id), files[0] if files else None)
    revisions = db.scalars(select(ManifestRevision).where(ManifestRevision.bundle_id == bundle.id).order_by(ManifestRevision.version.desc()).limit(15)).all()
    cluster_jobs = db.scalars(select(Job).where(Job.cluster_id == cluster_id).order_by(Job.created_at.desc()).limit(50)).all()
    jobs = [job for job in cluster_jobs if job.payload.get("bundle_id") == bundle.id][:10]
    kubeconfig_available = cluster_runtime_is_current(cluster) and (settings.data_root / "clusters" / cluster.id / "kubeconfig").is_file()
    return templates.TemplateResponse(request, "application_editor.html", {"cluster": cluster, "bundle": bundle, "files": files, "selected": selected, "revisions": revisions, "jobs": jobs, "kubeconfig_available": kubeconfig_available})


@app.post("/clusters/{cluster_id}/applications/{bundle_id}/delete")
def delete_application(
    cluster_id: str,
    bundle_id: str,
    delete_confirmation: str = Form(),
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    bundle = db.get(ApplicationBundle, bundle_id)
    if not bundle or bundle.cluster_id != cluster_id:
        raise HTTPException(404)
    if delete_confirmation != bundle.name:
        raise HTTPException(400, "Zur Bestaetigung muss der Anwendungsname eingegeben werden")
    active_jobs = db.scalars(select(Job).where(Job.cluster_id == cluster_id, Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))).all()
    if any(job.payload.get("bundle_id") == bundle.id for job in active_jobs):
        raise HTTPException(409, "Anwendung kann waehrend eines laufenden Jobs nicht geloescht werden")
    db.add(AuditEvent(action="delete_application", object_type="application", object_id=bundle.id, details={"cluster_id": cluster_id, "name": bundle.name}))
    db.delete(bundle)
    db.commit()
    return RedirectResponse(f"/clusters/{cluster_id}/applications", status_code=303)


@app.post("/clusters/{cluster_id}/applications/{bundle_id}/files")
def create_manifest_file(
    cluster_id: str, bundle_id: str, path: str = Form(),
    _: User = Depends(current_user), db: Session = Depends(get_db),
):
    bundle = db.get(ApplicationBundle, bundle_id)
    if not bundle or bundle.cluster_id != cluster_id:
        raise HTTPException(404)
    try:
        normalized = validate_manifest_path(path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if db.scalar(select(ManifestFile).where(ManifestFile.bundle_id == bundle.id, ManifestFile.path == normalized)):
        raise HTTPException(409, "Eine Datei mit diesem Pfad existiert bereits")
    manifest = ManifestFile(bundle_id=bundle.id, path=normalized, content="apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: example\n  namespace: default\ndata: {}\n")
    db.add(manifest)
    db.flush()
    create_revision(db, bundle, f"Datei {normalized} erstellt")
    db.commit()
    return RedirectResponse(f"/clusters/{cluster_id}/applications/{bundle.id}?file={manifest.id}", status_code=303)


@app.post("/clusters/{cluster_id}/applications/{bundle_id}/files/{file_id}")
def save_manifest_file(
    cluster_id: str, bundle_id: str, file_id: str, content: str = Form(), message: str = Form("Manifest bearbeitet"),
    _: User = Depends(current_user), db: Session = Depends(get_db),
):
    bundle = db.get(ApplicationBundle, bundle_id)
    manifest = db.get(ManifestFile, file_id)
    if not bundle or bundle.cluster_id != cluster_id or not manifest or manifest.bundle_id != bundle.id:
        raise HTTPException(404)
    try:
        validate_manifest_content(content)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    manifest.content = content
    bundle.updated_at = utcnow()
    db.flush()
    revision = create_revision(db, bundle, message or f"{manifest.path} bearbeitet")
    db.add(AuditEvent(action="save_manifest", object_type="application", object_id=bundle.id, details={"file": manifest.path, "revision": revision.version}))
    db.commit()
    return RedirectResponse(f"/clusters/{cluster_id}/applications/{bundle.id}?file={manifest.id}", status_code=303)


@app.post("/clusters/{cluster_id}/applications/{bundle_id}/files/{file_id}/delete")
def delete_manifest_file(cluster_id: str, bundle_id: str, file_id: str, _: User = Depends(current_user), db: Session = Depends(get_db)):
    bundle = db.get(ApplicationBundle, bundle_id)
    manifest = db.get(ManifestFile, file_id)
    if not bundle or bundle.cluster_id != cluster_id or not manifest or manifest.bundle_id != bundle.id:
        raise HTTPException(404)
    if len(bundle.files) <= 1:
        raise HTTPException(409, "Eine Anwendung muss mindestens eine Manifestdatei enthalten")
    path = manifest.path
    bundle.files.remove(manifest)
    bundle.updated_at = utcnow()
    db.flush()
    create_revision(db, bundle, f"Datei {path} entfernt")
    db.commit()
    return RedirectResponse(f"/clusters/{cluster_id}/applications/{bundle.id}", status_code=303)


@app.post("/clusters/{cluster_id}/applications/{bundle_id}/revisions/{revision_id}/restore")
def restore_manifest_revision(cluster_id: str, bundle_id: str, revision_id: str, _: User = Depends(current_user), db: Session = Depends(get_db)):
    bundle = db.get(ApplicationBundle, bundle_id)
    revision = db.get(ManifestRevision, revision_id)
    if not bundle or bundle.cluster_id != cluster_id or not revision or revision.bundle_id != bundle.id:
        raise HTTPException(404)
    bundle.files.clear()
    db.flush()
    for path, content in revision.snapshot.items():
        bundle.files.append(ManifestFile(path=path, content=content))
    bundle.updated_at = utcnow()
    db.flush()
    restored = create_revision(db, bundle, f"Revision {revision.version} wiederhergestellt")
    db.add(AuditEvent(action="restore_manifest_revision", object_type="application", object_id=bundle.id, details={"from": revision.version, "revision": restored.version}))
    db.commit()
    return RedirectResponse(f"/clusters/{cluster_id}/applications/{bundle.id}", status_code=303)


@app.post("/clusters/{cluster_id}/applications/{bundle_id}/jobs/{action}")
def start_manifest_job(
    cluster_id: str,
    bundle_id: str,
    action: str,
    apply_confirmation: str = Form(""),
    delete_confirmation: str = Form(""),
    _: User = Depends(current_user), db: Session = Depends(get_db),
):
    cluster = db.get(Cluster, cluster_id)
    bundle = db.get(ApplicationBundle, bundle_id)
    if not cluster or not bundle or bundle.cluster_id != cluster_id:
        raise HTTPException(404)
    if not cluster_runtime_is_current(cluster) or not (settings.data_root / "clusters" / cluster.id / "kubeconfig").is_file():
        raise HTTPException(409, "Anwendungsjobs sind erst nach erfolgreichem Cluster-Apply möglich")
    kinds = {"validate": JobKind.MANIFEST_VALIDATE, "diff": JobKind.MANIFEST_DIFF, "apply": JobKind.MANIFEST_APPLY, "delete": JobKind.MANIFEST_DELETE}
    if action not in kinds:
        raise HTTPException(404)
    if action == "apply" and apply_confirmation != bundle.name:
        raise HTTPException(400, "Für Apply muss der Anwendungsname bestätigt werden")
    if action == "delete" and delete_confirmation != bundle.name:
        raise HTTPException(400, "Fuer Delete muss der Anwendungsname bestaetigt werden")
    revision = create_revision(db, bundle, f"Snapshot für {action}")
    db.flush()
    try:
        queue_job(db, cluster, kinds[action], {"bundle_id": bundle.id, "revision_id": revision.id})
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(f"/clusters/{cluster_id}/applications/{bundle.id}", status_code=303)


@app.post("/clusters/{cluster_id}/applications/{bundle_id}/revisions/prune")
def prune_manifest_revisions(cluster_id: str, bundle_id: str, _: User = Depends(current_user), db: Session = Depends(get_db)):
    bundle = db.get(ApplicationBundle, bundle_id)
    if not bundle or bundle.cluster_id != cluster_id:
        raise HTTPException(404)
    revisions = db.scalars(select(ManifestRevision).where(ManifestRevision.bundle_id == bundle.id).order_by(ManifestRevision.version.desc())).all()
    keep_ids = {revision.id for revision in revisions[: settings.manifest_revision_retention_keep]}
    referenced_ids = {
        str(job.payload.get("revision_id"))
        for job in db.scalars(select(Job).where(Job.cluster_id == cluster_id)).all()
        if job.payload.get("revision_id")
    }
    removed = 0
    for revision in revisions:
        if revision.id in keep_ids or revision.id in referenced_ids:
            continue
        db.delete(revision)
        removed += 1
    db.add(AuditEvent(action="prune_manifest_revisions", object_type="application", object_id=bundle.id, details={"removed": removed, "keep": settings.manifest_revision_retention_keep}))
    db.commit()
    return RedirectResponse(f"/clusters/{cluster_id}/applications/{bundle.id}", status_code=303)


@app.websocket("/ws/clusters/{cluster_id}/kubectl")
async def kubectl_websocket(websocket: WebSocket, cluster_id: str):
    user_id = websocket.session.get("user_id")
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if not user_id or (origin and urlsplit(origin).netloc != host):
        await websocket.close(code=4401)
        return
    with SessionLocal() as db:
        user = db.get(User, user_id)
        cluster = db.get(Cluster, cluster_id)
    kubeconfig = settings.data_root / "clusters" / cluster_id / "kubeconfig"
    if not user or not user.enabled or not cluster or not cluster_runtime_is_current(cluster) or not kubeconfig.is_file():
        await websocket.close(code=4404)
        return
    await websocket.accept()
    messages: asyncio.Queue[dict] = asyncio.Queue()

    async def receive_messages() -> None:
        try:
            while True:
                await messages.put(await websocket.receive_json())
        except WebSocketDisconnect:
            await messages.put({"type": "disconnect"})

    receiver = asyncio.create_task(receive_messages())
    process: asyncio.subprocess.Process | None = None
    try:
        await websocket.send_json({"type": "ready", "cluster": cluster.name})
        disconnected = False
        while True:
            message = await messages.get()
            if message.get("type") == "disconnect":
                break
            if message.get("type") != "command":
                continue
            raw = str(message.get("command", ""))
            try:
                parsed = parse_kubectl_command(raw, bool(message.get("confirm_mutation")))
            except (ValueError, PermissionError) as exc:
                await websocket.send_json({"type": "error", "message": str(exc)})
                continue
            await websocket.send_json({"type": "start", "mutating": parsed.mutating})
            process = await asyncio.create_subprocess_exec(
                "kubectl", "--kubeconfig", str(kubeconfig), *parsed.args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            assert process.stdout is not None
            interrupted = False
            while process.returncode is None:
                output_task = asyncio.create_task(process.stdout.readline())
                control_task = asyncio.create_task(messages.get())
                done, pending = await asyncio.wait({output_task, control_task}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if control_task in done:
                    control = control_task.result()
                    if control.get("type") in ("interrupt", "disconnect"):
                        process.terminate()
                        interrupted = control.get("type") == "interrupt"
                        disconnected = control.get("type") == "disconnect"
                        await process.wait()
                elif output_task in done:
                    line = output_task.result()
                    if line:
                        await websocket.send_json({"type": "output", "data": line.decode(errors="replace")})
                    else:
                        await process.wait()
            exit_code = process.returncode
            with SessionLocal() as db:
                db.add(AuditEvent(
                    action="kubectl_command", object_type="cluster", object_id=cluster_id,
                    details={"command": audit_safe_command(parsed), "verb": parsed.verb, "mutating": parsed.mutating, "exit_code": exit_code},
                ))
                db.commit()
            if disconnected:
                break
            await websocket.send_json({"type": "exit", "code": exit_code, "interrupted": interrupted})
            process = None
    finally:
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        if process and process.returncode is None:
            process.terminate()
            await process.wait()


@app.post("/clusters/{cluster_id}/jobs/{kind}")
def start_job(cluster_id: str, kind: JobKind, destroy_confirmation: str = Form(""), _: User = Depends(current_user), db: Session = Depends(get_db)):
    cluster = db.get(Cluster, cluster_id)
    if not cluster:
        raise HTTPException(404)
    if kind not in (JobKind.PLAN, JobKind.APPLY, JobKind.ANSIBLE, JobKind.VERIFY, JobKind.DESTROY_PLAN, JobKind.DESTROY):
        raise HTTPException(404)
    if kind in (JobKind.DESTROY_PLAN, JobKind.DESTROY) and destroy_confirmation != cluster.name:
        raise HTTPException(400, "Zur Bestätigung muss der Clustername eingegeben werden")
    if kind == JobKind.VERIFY and (
        not cluster_runtime_is_current(cluster)
        or not (settings.data_root / "clusters" / cluster.id / "kubeconfig").is_file()
    ):
        raise HTTPException(409, "Clusterprüfung ist erst nach einem erfolgreichen Apply mit erzeugter Kubeconfig möglich")
    if kind == JobKind.ANSIBLE and (
        not cluster_runtime_is_current(cluster)
        or not (settings.data_root / "clusters" / cluster.id / "terraform" / "terraform.tfstate").is_file()
    ):
        raise HTTPException(409, "Ansible kann erst erneut ausgefuehrt werden, wenn Terraform bereits VMs angelegt hat")
    try:
        queue_job(db, cluster, kind)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(f"/clusters/{cluster.id}", status_code=303)


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, _: User = Depends(current_user), db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404)
    return JSONResponse({"id": job.id, "kind": job.kind.value, "status": job.status.value, "log": job.log, "error": job.error})


@app.post("/clusters/{cluster_id}/prune-jobs")
def prune_cluster_jobs(cluster_id: str, _: User = Depends(current_user), db: Session = Depends(get_db)):
    cluster = db.get(Cluster, cluster_id)
    if not cluster:
        raise HTTPException(404)
    jobs = db.scalars(select(Job).where(Job.cluster_id == cluster_id).order_by(Job.created_at.desc())).all()
    finished = [job for job in jobs if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING)]
    keep_ids = {job.id for job in finished[: settings.job_retention_keep]}
    removed = 0
    for job in finished:
        if job.id in keep_ids:
            continue
        db.delete(job)
        removed += 1
    db.add(AuditEvent(action="prune_cluster_jobs", object_type="cluster", object_id=cluster_id, details={"removed": removed, "keep": settings.job_retention_keep}))
    db.commit()
    return RedirectResponse(f"/clusters/{cluster_id}", status_code=303)


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, _: User = Depends(current_user), db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404)
    if job.status == JobStatus.QUEUED:
        job.status = JobStatus.CANCELLED
        job.finished_at = utcnow()
        job.heartbeat_at = job.finished_at
    elif job.status == JobStatus.RUNNING:
        job.cancel_requested = True
    db.commit()
    return RedirectResponse(f"/clusters/{job.cluster_id}", status_code=303)


def present_cluster_vm_ids(db: Session, cluster: Cluster) -> list[int]:
    proxmox_config = cluster.config.get("proxmox", {})
    payload = credential_payload(db, str(proxmox_config.get("credential_ref", "")), CredentialKind.PROXMOX)
    discovery = ProxmoxClient(
        str(proxmox_config.get("endpoint", "")),
        payload["api_token"],
        bool(proxmox_config.get("verify_tls", True)),
    ).discover()
    existing_ids = {int(vm["vmid"]) for vm in discovery.get("vms", []) if vm.get("vmid") is not None}
    configured_ids = {int(node["vm_id"]) for node in cluster.config.get("nodes", [])}
    applied_ids = {int(vm_id) for vm_id in (cluster.applied_vm_ids or [])}
    state_path = settings.data_root / "clusters" / cluster.id / "terraform" / "terraform.tfstate"
    state_ids = managed_vm_ids(state_path)
    owned_ids = configured_ids | applied_ids | state_ids
    return sorted(owned_ids & existing_ids)


@app.post("/clusters/{cluster_id}/delete")
def delete_cluster_record(
    cluster_id: str,
    delete_confirmation: str = Form(),
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    cluster = db.get(Cluster, cluster_id)
    if not cluster:
        raise HTTPException(404)
    if delete_confirmation != cluster.name:
        raise HTTPException(400, "Zur Bestätigung muss der Clustername eingegeben werden")
    active = db.scalar(select(Job).where(Job.cluster_id == cluster_id, Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])))
    if active:
        raise HTTPException(409, "Cluster kann während eines laufenden Jobs nicht gelöscht werden")
    if cluster.status != ClusterStatus.DESTROYED:
        try:
            present_ids = present_cluster_vm_ids(db, cluster)
        except (ValueError, ProxmoxError) as exc:
            raise HTTPException(409, "VM-Existenz konnte nicht gegen Proxmox geprüft werden: " + str(exc)) from exc
        if present_ids:
            raise HTTPException(409, "Builder-Eintrag ist geschuetzt, weil diese VM-IDs noch in Proxmox existieren: " + ", ".join(map(str, present_ids)))
    clusters_root = (settings.data_root / "clusters").resolve()
    workspace = (clusters_root / cluster.id).resolve()
    if workspace.parent != clusters_root:
        raise HTTPException(400, "Ungültiger Cluster-Arbeitsbereich")
    if workspace.exists():
        shutil.rmtree(workspace)
    db.add(AuditEvent(action="delete_cluster_record", object_type="cluster", object_id=cluster.id, details={"name": cluster.name}))
    db.delete(cluster)
    db.commit()
    return RedirectResponse("/", status_code=303)
