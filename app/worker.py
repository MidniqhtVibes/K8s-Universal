import copy
import os
import socket
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml
from sqlalchemy import select

from .config import get_settings
from .db import SessionLocal
from .generator import render_cluster
from .manifests import render_snapshot
from .models import ApplicationBundle, Cluster, ClusterStatus, Job, JobKind, JobStatus, ManifestRevision
from .schemas import ClusterConfig
from .security import redact
from .services import credential_payload
from .models import CredentialKind
from .proxmox import ProxmoxClient


settings = get_settings()


class JobCancelled(RuntimeError):
    pass


def ensure_not_cancelled(job_id: str) -> None:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job and job.cancel_requested:
            raise JobCancelled("Job wurde durch den Administrator abgebrochen")


def append_log(job_id: str, text: str, secrets: list[str] | None = None) -> None:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job:
            job.log = (job.log or "") + redact(text, secrets)
            db.commit()


def run_command(
    job: Job,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    secrets: list[str],
    allowed_codes: frozenset[int] = frozenset({0}),
) -> int:
    ensure_not_cancelled(job.id)
    append_log(job.id, f"\n$ {' '.join(command)}\n")
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        append_log(job.id, line, secrets)
        try:
            ensure_not_cancelled(job.id)
        except JobCancelled:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            raise
    code = process.wait()
    if code not in allowed_codes:
        raise RuntimeError(f"Befehl fehlgeschlagen ({code}): {' '.join(command)}")
    return code


def terraform_parallelism_arg() -> str:
    return f"-parallelism={settings.terraform_parallelism}"


def create_terraform_plan(
    job: Job,
    terraform_dir: Path,
    env: dict[str, str],
    secrets: list[str],
    *,
    destroy: bool = False,
    announce_apply: bool = True,
) -> None:
    plan_name = "destroy.tfplan" if destroy else "tfplan"
    if announce_apply and not destroy:
        append_log(job.id, "Terraform-Plan wird fuer den aktuellen Apply neu erzeugt.\n")
    run_command(job, ["terraform", "init", "-input=false"], terraform_dir, env, secrets)
    run_command(job, ["terraform", "validate"], terraform_dir, env, secrets)
    command = ["terraform", "plan", "-input=false", terraform_parallelism_arg(), "-out", plan_name]
    if destroy:
        command.extend(["-destroy", "-refresh=false"])
    run_command(job, command, terraform_dir, env, secrets)


def wait_for_ssh(job: Job, config: ClusterConfig) -> None:
    deadline = time.monotonic() + settings.ssh_wait_timeout
    pending = {str(node.ip) for node in config.nodes}
    while pending and time.monotonic() < deadline:
        ensure_not_cancelled(job.id)
        for ip in list(pending):
            try:
                with socket.create_connection((ip, config.ssh.port), timeout=3):
                    pending.remove(ip)
                    append_log(job.id, f"SSH erreichbar: {ip}\n")
            except OSError:
                pass
        if pending:
            time.sleep(5)
    if pending:
        raise RuntimeError(f"SSH-Timeout für: {', '.join(sorted(pending))}")


def validate_proxmox(job: Job, config: ClusterConfig, api_token: str, workspace: Path) -> None:
    append_log(job.id, "Proxmox-Ressourcen und Konflikte werden geprüft …\n")
    discovery = ProxmoxClient(config.proxmox.endpoint, api_token, config.proxmox.verify_tls).discover()
    node_names = {item.get("node") for item in discovery.get("nodes", [])}
    if config.proxmox.node not in node_names:
        raise RuntimeError(f"Proxmox-Node {config.proxmox.node} ist nicht verfügbar")
    details = discovery.get("details", {}).get(config.proxmox.node, {})
    storage_ids = {item.get("storage") for item in details.get("storages", [])}
    bridges = {item.get("iface") for item in details.get("bridges", [])}
    if config.proxmox.datastore not in storage_ids:
        raise RuntimeError(f"Storage {config.proxmox.datastore} ist auf {config.proxmox.node} nicht verfügbar")
    if config.proxmox.bridge not in bridges:
        raise RuntimeError(f"Bridge {config.proxmox.bridge} ist auf {config.proxmox.node} nicht verfügbar")
    resources = discovery.get("vms", [])
    templates = {int(item["vmid"]) for item in resources if item.get("template") == 1 and item.get("vmid") is not None}
    if config.proxmox.template_vm_id not in templates:
        raise RuntimeError(f"Template-VM {config.proxmox.template_vm_id} wurde nicht gefunden")
    if not (workspace / "terraform" / "terraform.tfstate").exists():
        used_ids = {int(item["vmid"]) for item in resources if item.get("vmid") is not None}
        collisions = sorted({node.vm_id for node in config.nodes} & used_ids)
        if collisions:
            raise RuntimeError(f"VM-IDs sind bereits belegt: {', '.join(map(str, collisions))}")
    append_log(job.id, "Proxmox-Prüfung erfolgreich.\n")


def execute(job_id: str) -> None:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if not job:
            return
        cluster = db.get(Cluster, job.cluster_id)
        if not cluster or cluster.config_hash != job.requested_config_hash:
            raise RuntimeError("Job-Konfiguration ist veraltet")
        config = ClusterConfig.model_validate(cluster.config)
        workspace = settings.data_root / "clusters" / config.id
        if job.kind in (JobKind.MANIFEST_VALIDATE, JobKind.MANIFEST_DIFF, JobKind.MANIFEST_APPLY, JobKind.MANIFEST_DELETE):
            execute_manifest_job(job, cluster, workspace)
            return
        # Refresh generated inputs and IaC sources so existing clusters also pick
        # up compatible runner changes after an application update.
        render_cluster(config, workspace, settings.source_root)
        proxmox = credential_payload(db, config.proxmox.credential_ref, CredentialKind.PROXMOX)
        ssh = credential_payload(db, config.ssh.credential_ref, CredentialKind.SSH)

    workspace = settings.data_root / "clusters" / config.id
    terraform_dir = workspace / "terraform"
    ansible_dir = workspace / "ansible"
    kubeconfig = workspace / "kubeconfig"
    token = proxmox["api_token"]
    secrets = [token, ssh.get("private_key", "")]
    env = os.environ.copy()
    env.update({"TF_IN_AUTOMATION": "1", "PROXMOX_VE_API_TOKEN": token})
    env["ANSIBLE_FORKS"] = str(settings.ansible_forks)
    env["KEEPALIVED_AUTH_PASS"] = job.requested_config_hash[:8]

    with tempfile.TemporaryDirectory(prefix="cluster-builder-") as temporary:
        key_path = Path(temporary) / "id_cluster"
        key_path.write_text(ssh["private_key"], encoding="utf-8")
        os.chmod(key_path, 0o600)
        env["CLUSTER_SSH_KEY_PATH"] = str(key_path)
        env["CLUSTER_KUBECONFIG_DEST"] = str(kubeconfig)

        if job.kind in (JobKind.PLAN, JobKind.DESTROY_PLAN):
            if job.kind == JobKind.PLAN:
                validate_proxmox(job, config, token, workspace)
            create_terraform_plan(job, terraform_dir, env, secrets, destroy=job.kind == JobKind.DESTROY_PLAN, announce_apply=False)
            with SessionLocal() as db:
                cluster = db.get(Cluster, config.id)
                if cluster:
                    if job.kind == JobKind.PLAN:
                        cluster.planned_hash = job.requested_config_hash
                        cluster.status = ClusterStatus.PLANNED
                    else:
                        cluster.destroy_planned_hash = job.requested_config_hash
                    db.commit()
            return

        if job.kind == JobKind.APPLY:
            create_terraform_plan(job, terraform_dir, env, secrets)
            with SessionLocal() as db:
                cluster = db.get(Cluster, config.id)
                if cluster:
                    cluster.status = ClusterStatus.APPLYING
                    db.commit()
            run_command(job, ["terraform", "apply", "-input=false", terraform_parallelism_arg(), "tfplan"], terraform_dir, env, secrets)
            wait_for_ssh(job, config)
            run_command(job, ["ansible-playbook", "-i", "inventory.generated.yml", "site.yml"], ansible_dir, env, secrets)
            if config.addons.ingress.enabled:
                run_command(job, ["helm", "repo", "add", "traefik", "https://traefik.github.io/charts", "--force-update"], workspace, env, secrets)
                run_command(job, ["helm", "repo", "update"], workspace, env, secrets)
                run_command(job, ["helm", "upgrade", "--install", "traefik", "traefik/traefik", "--namespace", "traefik", "--create-namespace", "-f", str(workspace / "generated" / "traefik-values.yaml"), "--kubeconfig", str(kubeconfig)], workspace, env, secrets)
                run_command(job, ["ansible-playbook", "-i", "inventory.generated.yml", "playbooks/02-loadbalancer.yml"], ansible_dir, env, secrets)
            verify_cluster(job, workspace, env, secrets)
            with SessionLocal() as db:
                cluster = db.get(Cluster, config.id)
                if cluster:
                    cluster.status = ClusterStatus.READY
                    db.commit()
            return

        if job.kind == JobKind.VERIFY:
            verify_cluster(job, workspace, env, secrets)
            return

        if job.kind == JobKind.DESTROY:
            run_command(job, ["terraform", "apply", "-input=false", terraform_parallelism_arg(), "destroy.tfplan"], terraform_dir, env, secrets)
            with SessionLocal() as db:
                cluster = db.get(Cluster, config.id)
                if cluster:
                    cluster.status = ClusterStatus.DESTROYED
                    cluster.planned_hash = None
                    cluster.destroy_planned_hash = None
                    db.commit()


def execute_manifest_job(job: Job, cluster: Cluster, workspace: Path) -> None:
    kubeconfig = workspace / "kubeconfig"
    if not kubeconfig.is_file():
        raise RuntimeError("Manifest-Jobs benötigen eine vorhandene Cluster-Kubeconfig")
    revision_id = str(job.payload.get("revision_id", ""))
    with SessionLocal() as db:
        revision = db.get(ManifestRevision, revision_id)
        bundle = db.get(ApplicationBundle, revision.bundle_id) if revision else None
        if not revision or not bundle or bundle.cluster_id != cluster.id:
            raise RuntimeError("Manifest-Revision gehört nicht zu diesem Cluster")
        rendered, documents = render_snapshot(revision.snapshot)
        revision_version = revision.version
        bundle_name = bundle.name
    append_log(job.id, f"Anwendung {bundle_name}, Revision {revision_version}\n")
    env = os.environ.copy()
    with tempfile.TemporaryDirectory(prefix="cluster-manifests-") as temporary:
        manifest = Path(temporary) / "bundle.yaml"
        manifest.write_text(rendered, encoding="utf-8")
        declared_namespaces = {
            str(document.get("metadata", {}).get("name"))
            for document in documents
            if document.get("kind") == "Namespace"
        }
        validation_documents = copy.deepcopy(documents)
        remapped = 0
        for document in validation_documents:
            metadata = document.get("metadata", {})
            if document.get("kind") != "Namespace" and metadata.get("namespace") in declared_namespaces:
                metadata["namespace"] = "default"
                remapped += 1
        validation_manifest = Path(temporary) / "validation.yaml"
        validation_manifest.write_text(yaml.safe_dump_all(validation_documents, sort_keys=False), encoding="utf-8")
        if remapped:
            append_log(job.id, f"Servervalidierung: {remapped} Ressourcen aus neu deklarierten Namespaces werden temporär gegen 'default' geprüft.\n")
        base = ["kubectl", "--kubeconfig", str(kubeconfig)]
        if job.kind == JobKind.MANIFEST_DELETE:
            delete_manifest = Path(temporary) / "delete.yaml"
            delete_manifest.write_text(yaml.safe_dump_all(reversed(documents), sort_keys=False), encoding="utf-8")
            run_command(job, [*base, "delete", "--ignore-not-found", "--wait=true", "--timeout=300s", "-f", str(delete_manifest)], workspace, env, [])
            append_log(job.id, "Anwendungs-Bundle erfolgreich aus dem Cluster entfernt.\n")
            return
        run_command(job, [*base, "apply", "--server-side", "--force-conflicts", "--field-manager=cluster-builder", "--dry-run=server", "-f", str(validation_manifest)], workspace, env, [])
        if job.kind == JobKind.MANIFEST_VALIDATE:
            append_log(job.id, "Serverseitige Validierung erfolgreich.\n")
            return
        if job.kind == JobKind.MANIFEST_DIFF:
            code = run_command(job, [*base, "diff", "--server-side", "--force-conflicts", "--field-manager=cluster-builder", "-f", str(manifest)], workspace, env, [], frozenset({0, 1}))
            append_log(job.id, "Keine Änderungen.\n" if code == 0 else "Diff enthält Änderungen (Exit 1 ist bei kubectl diff normal).\n")
            return
        run_command(job, [*base, "apply", "--server-side", "--force-conflicts", "--field-manager=cluster-builder", "-f", str(manifest)], workspace, env, [])
        for document in documents:
            kind = str(document.get("kind", "")).lower()
            if kind not in {"deployment", "statefulset", "daemonset"}:
                continue
            metadata = document.get("metadata", {})
            namespace = str(metadata.get("namespace", "default"))
            name = str(metadata["name"])
            run_command(job, [*base, "-n", namespace, "rollout", "status", f"{kind}/{name}", "--timeout=300s"], workspace, env, [])
    with SessionLocal() as db:
        revision = db.get(ManifestRevision, revision_id)
        if revision:
            revision.applied_at = datetime.now(UTC)
            db.commit()
    append_log(job.id, "Anwendungs-Bundle erfolgreich angewendet.\n")


def verify_cluster(job: Job, workspace: Path, env: dict[str, str], secrets: list[str]) -> None:
    kubeconfig = workspace / "kubeconfig"
    run_command(job, ["kubectl", "--kubeconfig", str(kubeconfig), "get", "nodes", "-o", "wide"], workspace, env, secrets)
    run_command(job, ["kubectl", "--kubeconfig", str(kubeconfig), "get", "pods", "-A"], workspace, env, secrets)
    run_command(job, ["kubectl", "--kubeconfig", str(kubeconfig), "get", "--raw=/readyz?verbose"], workspace, env, secrets)


def claim_job() -> str | None:
    with SessionLocal() as db:
        job = db.scalar(
            select(Job)
            .where(Job.status == JobStatus.QUEUED)
            .order_by(Job.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not job:
            return None
        job.status = JobStatus.RUNNING
        job.started_at = datetime.now(UTC)
        db.commit()
        return job.id


def finish_job(job_id: str, error: Exception | None = None) -> None:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if not job:
            return
        job.finished_at = datetime.now(UTC)
        if isinstance(error, JobCancelled):
            job.status = JobStatus.CANCELLED
            job.error = str(error)
            if job.kind in (JobKind.APPLY, JobKind.DESTROY):
                cluster = db.get(Cluster, job.cluster_id)
                if cluster:
                    cluster.status = ClusterStatus.FAILED
        elif error:
            job.status = JobStatus.FAILED
            job.error = redact(str(error))
            if job.kind not in (JobKind.MANIFEST_VALIDATE, JobKind.MANIFEST_DIFF, JobKind.MANIFEST_APPLY, JobKind.MANIFEST_DELETE):
                cluster = db.get(Cluster, job.cluster_id)
                if cluster:
                    cluster.status = ClusterStatus.FAILED
        else:
            job.status = JobStatus.SUCCEEDED
        db.commit()


def main() -> None:
    while True:
        job_id = claim_job()
        if not job_id:
            time.sleep(settings.worker_poll_seconds)
            continue
        try:
            execute(job_id)
        except Exception as exc:  # worker boundary: persist every failure
            label = "ABGEBROCHEN" if isinstance(exc, JobCancelled) else "FEHLER"
            append_log(job_id, f"\n{label}: {exc}\n")
            finish_job(job_id, exc)
        else:
            finish_job(job_id)


if __name__ == "__main__":
    main()
