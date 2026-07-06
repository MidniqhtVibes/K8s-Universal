import os
import socket
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from .config import get_settings
from .db import SessionLocal
from .models import Cluster, ClusterStatus, Job, JobKind, JobStatus
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


def run_command(job: Job, command: list[str], cwd: Path, env: dict[str, str], secrets: list[str]) -> None:
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
    if code != 0:
        raise RuntimeError(f"Befehl fehlgeschlagen ({code}): {' '.join(command)}")


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
    env["KEEPALIVED_AUTH_PASS"] = job.requested_config_hash[:8]

    with tempfile.TemporaryDirectory(prefix="cluster-builder-") as temporary:
        key_path = Path(temporary) / "id_cluster"
        key_path.write_text(ssh["private_key"], encoding="utf-8")
        os.chmod(key_path, 0o600)
        env["CLUSTER_SSH_KEY_PATH"] = str(key_path)
        env["CLUSTER_KUBECONFIG_DEST"] = str(kubeconfig)

        if job.kind in (JobKind.PLAN, JobKind.DESTROY_PLAN):
            validate_proxmox(job, config, token, workspace)
            run_command(job, ["terraform", "init", "-input=false"], terraform_dir, env, secrets)
            run_command(job, ["terraform", "validate"], terraform_dir, env, secrets)
            plan_name = "destroy.tfplan" if job.kind == JobKind.DESTROY_PLAN else "tfplan"
            command = ["terraform", "plan", "-input=false", "-out", plan_name]
            if job.kind == JobKind.DESTROY_PLAN:
                command.append("-destroy")
            run_command(job, command, terraform_dir, env, secrets)
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
            with SessionLocal() as db:
                cluster = db.get(Cluster, config.id)
                if cluster:
                    cluster.status = ClusterStatus.APPLYING
                    db.commit()
            run_command(job, ["terraform", "apply", "-input=false", "tfplan"], terraform_dir, env, secrets)
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
            run_command(job, ["terraform", "apply", "-input=false", "destroy.tfplan"], terraform_dir, env, secrets)
            with SessionLocal() as db:
                cluster = db.get(Cluster, config.id)
                if cluster:
                    cluster.status = ClusterStatus.DESTROYED
                    cluster.planned_hash = None
                    cluster.destroy_planned_hash = None
                    db.commit()


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
