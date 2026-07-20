import copy
import json
import os
import queue
import re
import shlex
import socket
import subprocess
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address, IPv4Interface
from pathlib import Path

import httpx
import yaml
from sqlalchemy import select

from .config import get_settings
from .db import SessionLocal
from .generator import proxmox_vm_name, render_cluster
from .manifests import render_snapshot
from .models import ApplicationBundle, Cluster, ClusterStatus, CredentialKind, Job, JobKind, JobStatus, ManifestRevision
from .proxmox import ProxmoxClient
from .schemas import ClusterConfig, ClusterType
from .security import redact
from .services import credential_payload, validate_template_disk_sizes
from .talos import (
    TALOS_API_PORT,
    calico_custom_resources,
    config_generation_command,
    global_machine_patch,
    node_machine_patch,
    secrets_command,
    secure_talos_workspace,
    write_secure_yaml,
    write_secure_yaml_documents,
)
from .terraform_state import managed_vm_ids


settings = get_settings()
CLUSTER_FAILURE_JOB_KINDS = frozenset({JobKind.APPLY, JobKind.ANSIBLE, JobKind.VERIFY, JobKind.DESTROY})
MUTATING_JOB_KINDS = frozenset({JobKind.APPLY, JobKind.ANSIBLE, JobKind.DESTROY})


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
            job.heartbeat_at = datetime.now(UTC)
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
    append_log(job.id, f"\n$ {' '.join(command)}\n", secrets)
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
    output: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        try:
            for line in process.stdout:
                output.put(line)
        finally:
            output.put(None)

    reader = threading.Thread(target=read_output, name=f"job-output-{job.id}", daemon=True)
    reader.start()
    output_finished = False
    last_heartbeat = time.monotonic()
    try:
        while process.poll() is None or not output_finished:
            try:
                item = output.get(timeout=0.5)
                if item is None:
                    output_finished = True
                else:
                    append_log(job.id, item, secrets)
            except queue.Empty:
                pass
            ensure_not_cancelled(job.id)
            if time.monotonic() - last_heartbeat >= 10:
                append_log(job.id, "")
                last_heartbeat = time.monotonic()
    except JobCancelled:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        raise
    finally:
        reader.join(timeout=1)
    code = process.wait()
    if code not in allowed_codes:
        raise RuntimeError(f"Befehl fehlgeschlagen ({code}): {' '.join(command)}")
    return code


def command_succeeds(
    job: Job,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    *,
    timeout: int = 20,
) -> bool:
    """Run a quiet, read-only probe without exposing its output in job logs."""
    ensure_not_cancelled(job.id)
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    ensure_not_cancelled(job.id)
    return result.returncode == 0


def quiet_command_output(
    job: Job,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    *,
    timeout: int = 20,
) -> tuple[bool, str]:
    """Run a read-only probe and return output without writing it to job logs."""
    ensure_not_cancelled(job.id)
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    ensure_not_cancelled(job.id)
    return result.returncode == 0, result.stdout or ""


def terraform_parallelism_arg() -> str:
    return f"-parallelism={settings.terraform_parallelism}"


def create_terraform_plan(
    job: Job,
    terraform_dir: Path,
    env: dict[str, str],
    secrets: list[str],
    *,
    destroy: bool = False,
) -> None:
    plan_name = "destroy.tfplan" if destroy else "tfplan"
    run_command(job, ["terraform", "init", "-input=false"], terraform_dir, env, secrets)
    run_command(job, ["terraform", "validate"], terraform_dir, env, secrets)
    command = ["terraform", "plan", "-input=false", terraform_parallelism_arg(), "-out", plan_name]
    if destroy:
        command.extend(["-destroy", "-refresh=false"])
    run_command(job, command, terraform_dir, env, secrets)


def is_manifest_job(kind: JobKind) -> bool:
    return kind in (JobKind.MANIFEST_VALIDATE, JobKind.MANIFEST_DIFF, JobKind.MANIFEST_APPLY, JobKind.MANIFEST_DELETE)


def recommended_next_step(text: str) -> str:
    if "Too Many Requests" in text or "status code=429" in text:
        return "Apply spaeter erneut starten; der externe Download wurde rate-limitiert."
    if "Could not resolve host" in text or "Temporary failure resolving" in text:
        return "DNS/Gateway der Ziel-VMs pruefen oder Apply erneut starten, falls es ein kurzer Aussetzer war."
    if "NO_PUBKEY" in text:
        return "Mit der aktuellen Version Apply erneut starten; der Kubernetes-APT-Keyring wird neu aufgebaut."
    if "SSH-Timeout" in text:
        return "IPs, VLAN, Gateway, Cloud-Init und SSH-Key pruefen."
    if "CoreDNS" in text:
        return "Direkt oberhalb im Log Pod-Status, Deployment-Details und Events pruefen."
    if "Befehl fehlgeschlagen" in text:
        return "Die letzte FAILED-, fatal- oder error-Zeile oberhalb enthaelt die konkrete Ursache."
    return "Die letzten Logzeilen oberhalb pruefen und denselben Job nach Behebung erneut starten."


def extract_ansible_failure(log: str) -> tuple[str | None, str | None, str | None]:
    task = None
    for match in re.finditer(r"^TASK \[(.+?)\]", log, re.MULTILINE):
        task = match.group(1)
    fatal = None
    for match in re.finditer(r"^fatal: \[([^\]]+)\]: FAILED! => (.+)$", log, re.MULTILINE):
        fatal = match
    if not fatal:
        # A later Helm/kubectl failure must not be attributed to the final
        # successful Ansible task that merely remains in the shared job log.
        return None, None, None
    host = fatal.group(1)
    raw_payload = fatal.group(2).strip()
    cause = raw_payload
    if raw_payload.startswith("{"):
        try:
            payload = json.loads(raw_payload)
            cause = str(payload.get("stderr") or payload.get("msg") or payload.get("stdout") or raw_payload)
        except json.JSONDecodeError:
            pass
    cause = " ".join(cause.split())
    if len(cause) > 600:
        cause = cause[:597] + "..."
    return host, task, cause


def job_log_tail(job_id: str, limit: int = 12000) -> str:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if not job or not job.log:
            return ""
        return job.log[-limit:]


def failure_summary(error: Exception, log: str = "") -> str | None:
    text = f"{error}\n{log}"
    host, task, cause = extract_ansible_failure(log)
    if host or task or cause:
        lines = ["Kurzdiagnose:"]
        if host:
            lines.append(f"- Host: {host}")
        if task:
            lines.append(f"- Task: {task}")
        if cause:
            lines.append(f"- Ursache: {cause}")
        lines.append(f"- Naechster Schritt: {recommended_next_step(text)}")
        return "\n".join(lines)
    if "Too Many Requests" in text or "status code=429" in text:
        return "Kurzdiagnose: Externer Download wurde rate-limitiert.\nNaechster Schritt: " + recommended_next_step(text)
    if "Could not resolve host" in text or "Temporary failure resolving" in text:
        return "Kurzdiagnose: DNS-Aufloesung auf mindestens einem Zielhost fehlgeschlagen.\nNaechster Schritt: " + recommended_next_step(text)
    if "NO_PUBKEY" in text:
        return "Kurzdiagnose: APT-Keyring ist unvollstaendig oder fehlt.\nNaechster Schritt: " + recommended_next_step(text)
    if "SSH-Timeout" in text:
        return "Kurzdiagnose: Nicht alle VMs waren per SSH erreichbar.\nNaechster Schritt: " + recommended_next_step(text)
    if "CoreDNS" in text:
        return "Kurzdiagnose: CoreDNS wurde nicht rechtzeitig bereit.\nNaechster Schritt: " + recommended_next_step(text)
    if "helm upgrade" in text or "helm install" in text:
        return (
            "Kurzdiagnose: Der Helm-Release wurde nicht erfolgreich bereit.\n"
            "Naechster Schritt: Pod-Status, Service-Typ und Events im Ziel-Namespace pruefen."
        )
    if "Befehl fehlgeschlagen" in text:
        return "Kurzdiagnose: Ein externer Terraform-, Ansible-, Helm- oder kubectl-Befehl ist fehlgeschlagen.\nNaechster Schritt: " + recommended_next_step(text)
    return None


def wait_for_ssh(job: Job, config: ClusterConfig) -> None:
    if config.ssh is None:
        raise RuntimeError("SSH-Konfiguration für kubeadm fehlt")
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
            append_log(job.id, "")
            time.sleep(5)
    if pending:
        raise RuntimeError(f"SSH-Timeout für: {', '.join(sorted(pending))}")


def wait_for_load_balancer_ssh(job: Job, config: ClusterConfig) -> None:
    ssh = config.load_balancer_ssh
    if ssh is None:
        raise RuntimeError("SSH-Konfiguration für Talos-Load-Balancer fehlt")
    deadline = time.monotonic() + settings.ssh_wait_timeout
    pending = {str(node.ip) for node in config.nodes if node.role == "loadbalancer"}
    while pending and time.monotonic() < deadline:
        ensure_not_cancelled(job.id)
        for ip in list(pending):
            try:
                with socket.create_connection((ip, ssh.port), timeout=3):
                    pending.remove(ip)
                    append_log(job.id, f"[Ansible] Load-Balancer per SSH erreichbar: {ip}\n")
            except OSError:
                pass
        if pending:
            append_log(job.id, "")
            time.sleep(5)
    if pending:
        raise RuntimeError(f"SSH-Timeout für Load-Balancer: {', '.join(sorted(pending))}")


def wait_for_talos_api(job: Job, config: ClusterConfig) -> None:
    deadline = time.monotonic() + settings.ssh_wait_timeout
    pending = {
        str(node.ip)
        for node in config.nodes
        if node.role in {"control_plane", "worker"}
    }
    while pending and time.monotonic() < deadline:
        ensure_not_cancelled(job.id)
        for ip in list(pending):
            try:
                with socket.create_connection((ip, TALOS_API_PORT), timeout=3):
                    pending.remove(ip)
                    append_log(job.id, f"[Talos] API erreichbar: {ip}:{TALOS_API_PORT}\n")
            except OSError:
                pass
        if pending:
            append_log(job.id, "")
            time.sleep(5)
    if pending:
        raise RuntimeError(
            "Talos-API-Timeout für: "
            + ", ".join(sorted(pending))
            + ". Das Talos-Template muss NoCloud-Netzwerkdaten im Maintenance Mode verarbeiten."
        )


def configured_guest_ipv4_addresses(guest_config: dict) -> set[IPv4Address]:
    """Extract static IPv4 addresses from Proxmox QEMU/LXC network fields."""
    addresses: set[IPv4Address] = set()
    for key, value in guest_config.items():
        if not re.fullmatch(r"(?:ipconfig|net)\d+", str(key)):
            continue
        for option in str(value).split(","):
            name, separator, raw_value = option.partition("=")
            if not separator or name.strip() != "ip":
                continue
            candidate = raw_value.strip()
            if candidate.lower() in {"dhcp", "manual"}:
                continue
            try:
                addresses.add(IPv4Interface(candidate).ip)
            except ValueError:
                # IPv6 values and malformed third-party metadata are not IPv4
                # allocations and must not create a false collision.
                continue
    return addresses


def validate_proxmox(job: Job, config: ClusterConfig, api_token: str, workspace: Path) -> None:
    append_log(job.id, "Proxmox-Ressourcen und Konflikte werden geprüft …\n")
    client = ProxmoxClient(config.proxmox.endpoint, api_token, config.proxmox.verify_tls)
    discovery = client.discover()
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
    try:
        template_disks = validate_template_disk_sizes(config, discovery)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    # Preserve the existing one-line kubeadm log while validating and logging
    # both live templates for Talos below.
    template_disk_gb = template_disks[config.proxmox.template_vm_id]
    if config.cluster_type == ClusterType.TALOS:
        append_log(
            job.id,
            "Talos-Template-Zuordnung: "
            + ", ".join(
                f"VM-ID {vm_id} >= {disk_gb} GB"
                for vm_id, disk_gb in sorted(template_disks.items())
            )
            + ".\n",
        )
    else:
        append_log(
            job.id,
            f"Template-Disk geprüft: mindestens {template_disk_gb} GB für alle Cluster-Nodes.\n",
        )
    resource_entries = []
    for item in resources if isinstance(resources, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            vm_id = int(item.get("vmid"))
        except (TypeError, ValueError):
            continue
        resource_entries.append((item, vm_id))
    used_ids = {vm_id for _, vm_id in resource_entries}
    owned_ids = managed_vm_ids(workspace / "terraform" / "terraform.tfstate")
    collisions = sorted({node.vm_id for node in config.nodes} & (used_ids - owned_ids))
    if collisions:
        raise RuntimeError(f"VM-IDs sind bereits durch fremde Ressourcen belegt: {', '.join(map(str, collisions))}")

    foreign_resources = [entry for entry in resource_entries if entry[1] not in owned_ids]
    planned_names = {
        (
            proxmox_vm_name(config.name, node.name)
            if config.proxmox.vm_name_include_cluster
            else node.name
        )
        for node in config.nodes
    }
    name_conflicts = sorted(
        {
            (str(item["name"]), vm_id)
            for item, vm_id in foreign_resources
            if item.get("name") in planned_names
        }
    )
    if name_conflicts:
        conflict_details = ", ".join(
            f"{name} (VM-ID {vm_id})" for name, vm_id in name_conflicts
        )
        raise RuntimeError(
            f"Proxmox-Namen sind bereits durch fremde Ressourcen belegt: {conflict_details}"
        )

    requested_addresses = {config.network.api_vip, *(node.ip for node in config.nodes)}
    ip_conflicts: set[tuple[str, int, str]] = set()
    for resource, vm_id in foreign_resources:
        if resource.get("template") in (1, True, "1"):
            continue
        if resource.get("type") not in {"qemu", "lxc"}:
            continue
        guest_addresses = configured_guest_ipv4_addresses(client.guest_config(resource))
        for address in requested_addresses & guest_addresses:
            ip_conflicts.add(
                (str(address), vm_id, str(resource.get("name") or "ohne Name"))
            )
    if ip_conflicts:
        conflict_details = ", ".join(
            f"{address} (VM-ID {vm_id}, {name})"
            for address, vm_id, name in sorted(ip_conflicts)
        )
        raise RuntimeError(
            "IP-Adressen sind bereits in fremden Proxmox-Gaesten konfiguriert: "
            + conflict_details
        )
    append_log(job.id, "Proxmox-Prüfung erfolgreich.\n")


def ingress_test_targets(documents: list[dict], api_vip: str) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for document in documents:
        if not isinstance(document, dict):
            continue
        if document.get("kind") != "Ingress":
            continue
        spec = document.get("spec", {})
        if not isinstance(spec, dict):
            continue
        for rule in spec.get("rules", []) or []:
            if not isinstance(rule, dict) or not rule.get("host"):
                continue
            host = str(rule["host"])
            http = rule.get("http", {})
            paths = http.get("paths", []) if isinstance(http, dict) else []
            if not paths:
                paths = [{"path": "/"}]
            for path_item in paths:
                path = "/"
                if isinstance(path_item, dict) and path_item.get("path"):
                    path = str(path_item["path"])
                if not path.startswith("/"):
                    path = "/" + path
                key = (host, path)
                if key in seen:
                    continue
                seen.add(key)
                targets.append((f"http://{api_vip}{path}", host))
    return targets


def ingress_test_commands(documents: list[dict], api_vip: str) -> list[str]:
    """Build copyable, shell-safe curl commands for all declared Ingress paths."""
    return [
        f'curl -v -H "Host: {host}" "{url}"'
        for url, host in ingress_test_targets(documents, api_vip)
    ]


def run_ingress_tests(job: Job, documents: list[dict], api_vip: str) -> None:
    targets = ingress_test_targets(documents, api_vip)
    if not targets:
        append_log(job.id, "\nKein Ingress-Host im Bundle gefunden; es wurde weder ein HTTP-Test noch ein Curl-Testbefehl erzeugt.\n")
        return
    append_log(job.id, "\nHTTP-Funktionstest über die Cluster-VIP:\n")
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        for url, host in targets:
            ensure_not_cancelled(job.id)
            try:
                response = client.get(url, headers={"Host": host})
                append_log(job.id, f"{host} {url} -> HTTP {response.status_code}\n")
            except httpx.HTTPError as exc:
                append_log(job.id, f"{host} {url} -> nicht erreichbar: {exc}\n")
    commands = ingress_test_commands(documents, api_vip)
    append_log(job.id, "\nManueller Curl-Test (auf einem Host mit Zugriff auf die Cluster-VIP):\n")
    append_log(job.id, "".join(f"{command}\n" for command in commands))


def run_traefik_stack(
    job: Job,
    config: ClusterConfig,
    workspace: Path,
    ansible_dir: Path,
    kubeconfig: Path,
    env: dict[str, str],
    secrets: list[str],
) -> None:
    if not config.addons.ingress.enabled:
        return
    run_command(job, ["helm", "repo", "add", "traefik", "https://traefik.github.io/charts", "--force-update"], workspace, env, secrets)
    run_command(job, ["helm", "repo", "update"], workspace, env, secrets)
    run_command(
        job,
        [
            "helm", "upgrade", "--install", "traefik", "traefik/traefik",
            "--version", config.addons.ingress.chart_version,
            "--namespace", "traefik", "--create-namespace",
            "--wait", "--wait-for-jobs", "--timeout", "10m",
            "-f", str(workspace / "generated" / "traefik-values.yaml"),
            "--kubeconfig", str(kubeconfig),
        ],
        workspace,
        env,
        secrets,
    )
    run_command(job, ["ansible-playbook", "-i", "inventory.generated.yml", "playbooks/02-loadbalancer.yml"], ansible_dir, env, secrets)


def run_load_balancer_stack(
    job: Job,
    ansible_dir: Path,
    env: dict[str, str],
    secrets: list[str],
) -> None:
    """Configure only the Ubuntu load balancers of a Talos cluster."""
    for playbook in (
        "playbooks/00-wait-for-hosts.yml",
        "playbooks/00-check.yml",
        "playbooks/01-bootstrap-os.yml",
        "playbooks/02-loadbalancer.yml",
    ):
        run_command(
            job,
            ["ansible-playbook", "-i", "inventory.generated.yml", playbook, "--limit", "loadbalancer"],
            ansible_dir,
            env,
            secrets,
        )


def prepare_talos_assets(
    job: Job,
    config: ClusterConfig,
    workspace: Path,
    env: dict[str, str],
    secrets: list[str],
) -> tuple[Path, dict[str, Path]]:
    if config.talos is None:
        raise RuntimeError("Talos-Konfiguration fehlt")
    talos_dir = workspace / "talos"
    secure_talos_workspace(talos_dir)
    secrets_path = talos_dir / "secrets.yaml"
    controlplane_path = talos_dir / "controlplane.yaml"
    worker_path = talos_dir / "worker.yaml"
    talosconfig_path = talos_dir / "talosconfig"
    sensitive_outputs = (controlplane_path, worker_path, talosconfig_path)

    append_log(job.id, "[Talos] Bereite stabile Machine-Config und Cluster-PKI vor.\n")
    if not secrets_path.is_file():
        with tempfile.TemporaryDirectory(prefix=".secrets-", dir=talos_dir) as temporary_name:
            temporary_secret = Path(temporary_name) / "secrets.yaml"
            if controlplane_path.is_file():
                command = [
                    "talosctl", "gen", "secrets",
                    "--from-controlplane-config", str(controlplane_path),
                    "--talos-version", config.talos.version.value,
                    "--output-file", str(temporary_secret),
                ]
            elif any(path.exists() for path in sensitive_outputs):
                raise RuntimeError(
                    "Talos-PKI ist unvollständig. Vorhandene Machine-Configs nicht mit neuen Secrets überschreiben; "
                    "bitte den Cluster-Workspace aus einem Backup wiederherstellen."
                )
            else:
                command = secrets_command(config, temporary_secret)
            run_command(job, command, talos_dir, env, secrets)
            os.chmod(temporary_secret, 0o600)
            os.replace(temporary_secret, secrets_path)

    global_patch_path = talos_dir / "common.patch.yaml"
    write_secure_yaml_documents(global_patch_path, global_machine_patch(config))
    node_configs: dict[str, Path] = {}
    with tempfile.TemporaryDirectory(prefix=".render-", dir=talos_dir) as temporary_name:
        staging = Path(temporary_name)
        os.chmod(staging, 0o700)
        run_command(
            job,
            config_generation_command(config, staging, secrets_path, global_patch_path),
            talos_dir,
            env,
            secrets,
        )
        staged_nodes = staging / "nodes"
        staged_nodes.mkdir(mode=0o700)
        for node in config.nodes:
            if node.role == "loadbalancer":
                continue
            patch_path = staging / f"{node.name}.patch.yaml"
            write_secure_yaml_documents(patch_path, node_machine_patch(config, node))
            base_path = staging / ("controlplane.yaml" if node.role == "control_plane" else "worker.yaml")
            node_path = staged_nodes / f"{node.name}.yaml"
            run_command(
                job,
                [
                    "talosctl", "machineconfig", "patch", str(base_path),
                    "--patch", f"@{patch_path}", "--output", str(node_path),
                ],
                talos_dir,
                env,
                secrets,
            )
            os.chmod(node_path, 0o600)
            run_command(
                job,
                ["talosctl", "validate", "--config", str(node_path), "--mode", "cloud", "--strict"],
                talos_dir,
                env,
                secrets,
            )

        for name in ("controlplane.yaml", "worker.yaml", "talosconfig"):
            staged_path = staging / name
            if not staged_path.is_file():
                raise RuntimeError(f"talosctl hat {name} nicht erzeugt")
            os.chmod(staged_path, 0o600)
            os.replace(staged_path, talos_dir / name)
        live_nodes = talos_dir / "nodes"
        live_nodes.mkdir(exist_ok=True, mode=0o700)
        os.chmod(live_nodes, 0o700)
        for staged_path in staged_nodes.glob("*.yaml"):
            destination = live_nodes / staged_path.name
            os.replace(staged_path, destination)
            node_configs[staged_path.stem] = destination

    calico_path = talos_dir / "calico-custom-resources.yaml"
    write_secure_yaml_documents(calico_path, calico_custom_resources(config))
    secure_talos_workspace(talos_dir)
    return talos_dir, node_configs


def _talos_connection_args(ip: str, talosconfig: Path) -> list[str]:
    return ["--nodes", ip, "--endpoints", ip, "--talosconfig", str(talosconfig)]


def _parse_talos_server_version(output: str) -> str | None:
    _, separator, server_output = output.partition("Server:")
    if not separator:
        return None
    match = re.search(r"\bv\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?\b", server_output)
    return match.group(0) if match else None


def _talos_authenticated_version(
    job: Job,
    ip: str,
    talosconfig: Path,
    cwd: Path,
    env: dict[str, str],
) -> str | None:
    succeeded, output = quiet_command_output(
        job,
        ["talosctl", "version", "--short", *_talos_connection_args(ip, talosconfig)],
        cwd,
        env,
    )
    return _parse_talos_server_version(output) if succeeded else None


def _talos_maintenance_version(job: Job, ip: str, cwd: Path, env: dict[str, str]) -> str | None:
    succeeded, output = quiet_command_output(
        job,
        ["talosctl", "version", "--short", "--nodes", ip, "--endpoints", ip, "--insecure"],
        cwd,
        env,
    )
    return _parse_talos_server_version(output) if succeeded else None


def classify_talos_nodes(
    job: Job,
    config: ClusterConfig,
    talosconfig: Path,
    cwd: Path,
    env: dict[str, str],
) -> dict[str, str]:
    """Classify every node before mutating any of them.

    This prevents a foreign/stale PKI on a later node from leaving an earlier
    maintenance node partially joined to the new cluster.
    """
    if config.talos is None:
        raise RuntimeError("Talos-Konfiguration fehlt")
    expected_version = config.talos.version.value
    pending = {
        str(node.ip): node.name
        for node in config.nodes
        if node.role != "loadbalancer"
    }
    states: dict[str, str] = {}
    deadline = time.monotonic() + settings.ssh_wait_timeout
    while pending and time.monotonic() < deadline:
        for ip, name in list(pending.items()):
            authenticated_version = _talos_authenticated_version(job, ip, talosconfig, cwd, env)
            if authenticated_version is not None:
                if authenticated_version != expected_version:
                    raise RuntimeError(
                        f"Talos-Node {name} ({ip}) läuft mit {authenticated_version}, erwartet wird {expected_version}. "
                        "Automatische Talos-Upgrades sind nicht aktiviert."
                    )
                states[ip] = "authenticated"
                pending.pop(ip)
                continue
            maintenance_version = _talos_maintenance_version(job, ip, cwd, env)
            if maintenance_version is not None:
                if maintenance_version != expected_version:
                    raise RuntimeError(
                        f"Talos-Template auf {name} ({ip}) läuft mit {maintenance_version}, erwartet wird {expected_version}."
                    )
                states[ip] = "maintenance"
                pending.pop(ip)
        if pending:
            append_log(job.id, "")
            time.sleep(5)
    if pending:
        unresolved = ", ".join(f"{name} ({ip})" for ip, name in sorted(pending.items()))
        raise RuntimeError(
            "Talos-API-Zustand konnte nicht sicher bestimmt werden: " + unresolved + ". "
            "Die Nodes müssen entweder unkonfiguriert im Maintenance Mode oder mit dieser Cluster-PKI erreichbar sein."
        )
    return states


def wait_for_authenticated_talos(
    job: Job,
    nodes: list[str],
    talosconfig: Path,
    cwd: Path,
    env: dict[str, str],
    expected_version: str,
) -> None:
    deadline = time.monotonic() + settings.ssh_wait_timeout
    pending = set(nodes)
    while pending and time.monotonic() < deadline:
        for ip in list(pending):
            version = _talos_authenticated_version(job, ip, talosconfig, cwd, env)
            if version == expected_version:
                pending.remove(ip)
                append_log(job.id, f"[Talos] Authentifizierte API bereit: {ip}\n")
            elif version is not None:
                raise RuntimeError(f"Talos-Node {ip} läuft mit {version}, erwartet wird {expected_version}")
        if pending:
            append_log(job.id, "")
            time.sleep(5)
    if pending:
        raise RuntimeError(f"Talos-Nodes wurden nach Machine-Config nicht bereit: {', '.join(sorted(pending))}")


def apply_talos_machine_configs(
    job: Job,
    config: ClusterConfig,
    talos_dir: Path,
    node_configs: dict[str, Path],
    env: dict[str, str],
    secrets: list[str],
) -> None:
    talosconfig = talos_dir / "talosconfig"
    nodes = [node for node in config.nodes if node.role != "loadbalancer"]
    wait_for_talos_api(job, config)
    states = classify_talos_nodes(job, config, talosconfig, talos_dir, env)
    for node in nodes:
        ip = str(node.ip)
        node_config = node_configs[node.name]
        if states[ip] == "authenticated":
            append_log(job.id, f"[Talos] Gleiche bestehende Machine-Config ab: {node.name} ({ip}).\n")
            command = [
                "talosctl", "apply-config", *_talos_connection_args(ip, talosconfig),
                "--file", str(node_config), "--mode", "auto",
            ]
        else:
            append_log(job.id, f"[Talos] Initiale Machine-Config: {node.name} ({ip}).\n")
            command = [
                "talosctl", "apply-config", "--nodes", ip, "--endpoints", ip,
                "--insecure", "--file", str(node_config), "--mode", "auto",
            ]
        run_command(job, command, talos_dir, env, secrets)
        wait_for_authenticated_talos(
            job, [ip], talosconfig, talos_dir, env, config.talos.version.value
        )


def ensure_talos_bootstrap(
    job: Job,
    config: ClusterConfig,
    talos_dir: Path,
    env: dict[str, str],
    secrets: list[str],
) -> str:
    bootstrap_node = next(node for node in config.nodes if node.role == "control_plane")
    bootstrap_ip = str(bootstrap_node.ip)
    talosconfig = talos_dir / "talosconfig"
    args = _talos_connection_args(bootstrap_ip, talosconfig)
    requested_marker = talos_dir / "bootstrap.requested.yaml"
    complete_marker = talos_dir / "bootstrap.complete.yaml"
    members_command = ["talosctl", "etcd", "members", *args]
    if command_succeeds(job, members_command, talos_dir, env, timeout=30):
        write_secure_yaml(complete_marker, {"node": bootstrap_node.name, "confirmed": True})
        append_log(job.id, "[Talos] etcd ist bereits gebootstrapped; kein erneuter Bootstrap.\n")
        return bootstrap_ip
    bootstrap_error: RuntimeError | None = None
    if requested_marker.exists() or complete_marker.exists():
        append_log(
            job.id,
            "[Talos] Vorheriger Bootstrap-Auftrag gefunden; prüfe ausschließlich den Remote-Zustand.\n",
        )
    else:
        write_secure_yaml(requested_marker, {"node": bootstrap_node.name, "requested": True})
        append_log(job.id, f"[Talos] Bootstrap einmalig auf {bootstrap_node.name}.\n")
        try:
            run_command(job, ["talosctl", "bootstrap", *args], talos_dir, env, secrets)
        except JobCancelled:
            raise
        except RuntimeError as exc:
            # A transport error can happen after the server accepted bootstrap.
            # The only safe decision is based on the remote etcd postcondition.
            bootstrap_error = exc
            append_log(job.id, "[Talos] Bootstrap-Antwort war unklar; prüfe etcd ohne erneuten Bootstrap.\n")
    deadline = time.monotonic() + settings.ssh_wait_timeout
    while time.monotonic() < deadline:
        if command_succeeds(job, members_command, talos_dir, env, timeout=30):
            write_secure_yaml(complete_marker, {"node": bootstrap_node.name, "confirmed": True})
            return bootstrap_ip
        append_log(job.id, "")
        time.sleep(5)
    error = RuntimeError(
        "Talos-etcd wurde nach dem einmaligen Bootstrap-Auftrag nicht bereit; "
        "ein automatischer zweiter Bootstrap wird aus Sicherheitsgründen nicht ausgeführt"
    )
    if bootstrap_error is not None:
        raise error from bootstrap_error
    raise error


def fetch_talos_kubeconfig(
    job: Job,
    config: ClusterConfig,
    workspace: Path,
    talos_dir: Path,
    bootstrap_ip: str,
    env: dict[str, str],
    secrets: list[str],
) -> Path:
    talosconfig = talos_dir / "talosconfig"
    destination = workspace / "kubeconfig"
    with tempfile.TemporaryDirectory(prefix=".kubeconfig-", dir=talos_dir) as temporary_name:
        staging = Path(temporary_name)
        os.chmod(staging, 0o700)
        run_command(
            job,
            [
                "talosctl", "kubeconfig", str(staging), "--merge=false", "--force",
                *_talos_connection_args(bootstrap_ip, talosconfig),
            ],
            talos_dir,
            env,
            secrets,
        )
        candidate = staging / "kubeconfig"
        if not candidate.is_file():
            raise RuntimeError("talosctl hat keine kubeconfig erzeugt")
        try:
            kubeconfig_document = yaml.safe_load(candidate.read_text(encoding="utf-8"))
            servers = {
                cluster_entry["cluster"]["server"]
                for cluster_entry in kubeconfig_document["clusters"]
            }
        except (KeyError, TypeError, yaml.YAMLError) as exc:
            raise RuntimeError("Talos-kubeconfig ist nicht gültig") from exc
        expected_server = f"https://{config.network.api_vip}:{config.kubernetes.api_port}"
        if servers != {expected_server}:
            raise RuntimeError("Talos-kubeconfig verwendet nicht die konfigurierte API-VIP")
        os.chmod(candidate, 0o600)
        deadline = time.monotonic() + settings.ssh_wait_timeout
        while time.monotonic() < deadline:
            if command_succeeds(
                job,
                ["kubectl", "--kubeconfig", str(candidate), "get", "--raw=/readyz"],
                workspace,
                env,
                timeout=30,
            ):
                os.replace(candidate, destination)
                os.chmod(destination, 0o600)
                append_log(job.id, "[Talos] kubeconfig atomar im Cluster-Workspace gespeichert.\n")
                return destination
            append_log(job.id, "")
            time.sleep(5)
    raise RuntimeError("Kubernetes-API wurde nach dem Talos-Bootstrap nicht bereit")


def install_talos_calico(
    job: Job,
    config: ClusterConfig,
    workspace: Path,
    talos_dir: Path,
    kubeconfig: Path,
    env: dict[str, str],
    secrets: list[str],
) -> None:
    kubectl = ["kubectl", "--kubeconfig", str(kubeconfig)]
    base_url = f"https://raw.githubusercontent.com/projectcalico/calico/{config.addons.cni.version}/manifests"
    append_log(job.id, "[Calico] Installiere Talos-kompatibles NFTables/VXLAN-Profil.\n")
    run_command(
        job,
        [*kubectl, "apply", "--server-side", "--force-conflicts", "-f", f"{base_url}/v1_crd_projectcalico_org.yaml"],
        workspace,
        env,
        secrets,
    )
    run_command(
        job,
        [*kubectl, "apply", "--server-side", "--force-conflicts", "-f", f"{base_url}/tigera-operator.yaml"],
        workspace,
        env,
        secrets,
    )
    run_command(
        job,
        [
            *kubectl, "wait", "--for=condition=Established",
            "crd/installations.operator.tigera.io", "crd/apiservers.operator.tigera.io",
            "--timeout=180s",
        ],
        workspace,
        env,
        secrets,
    )
    run_command(
        job,
        [*kubectl, "-n", "tigera-operator", "rollout", "status", "deployment/tigera-operator", "--timeout=300s"],
        workspace,
        env,
        secrets,
    )
    run_command(
        job,
        [
            *kubectl, "apply", "--server-side", "--force-conflicts",
            "-f", str(talos_dir / "calico-custom-resources.yaml"),
        ],
        workspace,
        env,
        secrets,
    )
    run_command(job, [*kubectl, "wait", "--for=condition=Ready", "nodes", "--all", "--timeout=600s"], workspace, env, secrets)
    run_command(job, [*kubectl, "-n", "kube-system", "rollout", "restart", "deployment/coredns"], workspace, env, secrets)
    run_command(job, [*kubectl, "-n", "kube-system", "rollout", "status", "deployment/coredns", "--timeout=300s"], workspace, env, secrets)


def run_talos_stack(
    job: Job,
    config: ClusterConfig,
    workspace: Path,
    ansible_dir: Path,
    kubeconfig: Path,
    env: dict[str, str],
    secrets: list[str],
    prepared_assets: tuple[Path, dict[str, Path]] | None = None,
) -> None:
    append_log(job.id, "Provisionierungsmodus: Talos\n")
    wait_for_load_balancer_ssh(job, config)
    append_log(job.id, "[Ansible] Konfiguriere ausschließlich die Ubuntu-Load-Balancer.\n")
    run_load_balancer_stack(job, ansible_dir, env, secrets)
    talos_dir, node_configs = prepared_assets or prepare_talos_assets(job, config, workspace, env, secrets)
    apply_talos_machine_configs(job, config, talos_dir, node_configs, env, secrets)
    bootstrap_ip = ensure_talos_bootstrap(job, config, talos_dir, env, secrets)
    fetch_talos_kubeconfig(job, config, workspace, talos_dir, bootstrap_ip, env, secrets)
    install_talos_calico(job, config, workspace, talos_dir, kubeconfig, env, secrets)
    run_traefik_stack(job, config, workspace, ansible_dir, kubeconfig, env, secrets)
    if config.registry_enabled:
        append_log(job.id, "[Registry] Talos RegistryMirrorConfig ist auf den Kubernetes-Nodes aktiv.\n")
    control_planes = ",".join(str(node.ip) for node in config.nodes if node.role == "control_plane")
    workers = ",".join(str(node.ip) for node in config.nodes if node.role == "worker")
    run_command(
        job,
        [
            "talosctl", "health", "--nodes", bootstrap_ip, "--endpoints", bootstrap_ip,
            "--control-plane-nodes", control_planes, "--worker-nodes", workers,
            "--talosconfig", str(talos_dir / "talosconfig"), "--wait-timeout", "10m",
        ],
        talos_dir,
        env,
        secrets,
    )
    verify_cluster(job, config, workspace, env, secrets)


def run_ansible_stack(
    job: Job,
    config: ClusterConfig,
    workspace: Path,
    ansible_dir: Path,
    kubeconfig: Path,
    env: dict[str, str],
    secrets: list[str],
) -> None:
    if config.registry_enabled:
        append_log(job.id, f"[Registry] Private Registry aktiviert: {config.registry_endpoint}\n")
        append_log(job.id, f"[Registry] Protokoll: {'HTTP' if config.registry_use_http else 'HTTPS'}\n")
        append_log(job.id, "[Registry] Konfiguriere containerd auf Control Plane Nodes.\n")
        append_log(job.id, "[Registry] Konfiguriere containerd auf Worker Nodes.\n")
        append_log(job.id, "[Registry] Prüfe Erreichbarkeit auf allen Kubernetes-Nodes.\n")
    wait_for_ssh(job, config)
    run_command(job, ["ansible-playbook", "-i", "inventory.generated.yml", "site.yml"], ansible_dir, env, secrets)
    if config.registry_enabled:
        append_log(job.id, "[Registry] Registry erreichbar.\n")
    if config.addons.ingress.enabled:
        run_command(job, ["helm", "repo", "add", "traefik", "https://traefik.github.io/charts", "--force-update"], workspace, env, secrets)
        run_command(job, ["helm", "repo", "update"], workspace, env, secrets)
        run_command(
            job,
            [
                "helm", "upgrade", "--install", "traefik", "traefik/traefik",
                "--version", config.addons.ingress.chart_version,
                "--namespace", "traefik", "--create-namespace",
                "--wait", "--wait-for-jobs", "--timeout", "10m",
                "-f", str(workspace / "generated" / "traefik-values.yaml"),
                "--kubeconfig", str(kubeconfig),
            ],
            workspace,
            env,
            secrets,
        )
        run_command(job, ["ansible-playbook", "-i", "inventory.generated.yml", "playbooks/02-loadbalancer.yml"], ansible_dir, env, secrets)
    verify_cluster(job, config, workspace, env, secrets)


def invalidate_plan_authorization(
    cluster_id: str,
    *,
    destroy: bool = False,
    status: ClusterStatus | None = None,
) -> None:
    """Atomically consume or invalidate a reviewed Terraform plan."""
    with SessionLocal() as db:
        cluster = db.get(Cluster, cluster_id)
        if not cluster:
            raise RuntimeError("Cluster nicht gefunden")
        if destroy:
            cluster.destroy_planned_hash = None
        else:
            cluster.planned_hash = None
            if status is None and cluster.applied_hash != cluster.config_hash:
                cluster.status = ClusterStatus.DRAFT
        if status is not None:
            cluster.status = status
        db.commit()


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
        if is_manifest_job(job.kind):
            execute_manifest_job(job, cluster, workspace)
            return
        # Refresh generated inputs and IaC sources so existing clusters also pick
        # up compatible runner changes after an application update.
        render_cluster(config, workspace, settings.source_root)
        proxmox = credential_payload(db, config.proxmox.credential_ref, CredentialKind.PROXMOX)
        ssh = credential_payload(db, config.provisioning_ssh.credential_ref, CredentialKind.SSH)

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
            invalidate_plan_authorization(config.id, destroy=job.kind == JobKind.DESTROY_PLAN)
            if job.kind == JobKind.PLAN:
                validate_proxmox(job, config, token, workspace)
            create_terraform_plan(job, terraform_dir, env, secrets, destroy=job.kind == JobKind.DESTROY_PLAN)
            with SessionLocal() as db:
                cluster = db.get(Cluster, config.id)
                if cluster:
                    if job.kind == JobKind.PLAN:
                        cluster.planned_hash = job.requested_config_hash
                        if cluster.applied_hash != cluster.config_hash:
                            cluster.status = ClusterStatus.PLANNED
                    else:
                        cluster.destroy_planned_hash = job.requested_config_hash
                    db.commit()
            return

        if job.kind == JobKind.APPLY:
            plan_path = terraform_dir / "tfplan"
            if not plan_path.is_file():
                raise RuntimeError("Der geprüfte Terraform-Plan fehlt; bitte einen neuen Plan erstellen")
            # A reviewed plan may remain queued while the Proxmox environment
            # changes. Re-run the read-only collision checks directly before
            # consuming and applying it to close that race window.
            validate_proxmox(job, config, token, workspace)
            prepared_talos_assets = None
            if config.cluster_type == ClusterType.TALOS:
                # Generate persistent PKI before creating VMs. A worker crash
                # after Terraform must never cause replacement cluster secrets.
                prepared_talos_assets = prepare_talos_assets(job, config, workspace, env, secrets)
            invalidate_plan_authorization(config.id, status=ClusterStatus.APPLYING)
            try:
                run_command(job, ["terraform", "apply", "-input=false", terraform_parallelism_arg(), "tfplan"], terraform_dir, env, secrets)
            finally:
                plan_path.unlink(missing_ok=True)
            state_ids = managed_vm_ids(terraform_dir / "terraform.tfstate")
            expected_ids = {node.vm_id for node in config.nodes}
            if not expected_ids <= state_ids:
                missing = ", ".join(map(str, sorted(expected_ids - state_ids)))
                raise RuntimeError(f"Terraform-State enthält nicht alle erwarteten VM-IDs: {missing}")
            with SessionLocal() as db:
                cluster = db.get(Cluster, config.id)
                if cluster:
                    cluster.applied_hash = job.requested_config_hash
                    cluster.applied_vm_ids = sorted(state_ids)
                    db.commit()
            if config.cluster_type == ClusterType.KUBEADM:
                append_log(job.id, "Provisionierungsmodus: Kubernetes / kubeadm\n")
                run_ansible_stack(job, config, workspace, ansible_dir, kubeconfig, env, secrets)
            else:
                run_talos_stack(
                    job, config, workspace, ansible_dir, kubeconfig, env, secrets,
                    prepared_assets=prepared_talos_assets,
                )
            with SessionLocal() as db:
                cluster = db.get(Cluster, config.id)
                if cluster:
                    cluster.status = ClusterStatus.READY
                    db.commit()
            return

        if job.kind == JobKind.ANSIBLE:
            with SessionLocal() as db:
                cluster = db.get(Cluster, config.id)
                if cluster:
                    cluster.status = ClusterStatus.APPLYING
                    db.commit()
            if config.cluster_type == ClusterType.KUBEADM:
                append_log(job.id, "Ansible/Helm/Verify wird ohne Terraform erneut ausgefuehrt.\n")
                run_ansible_stack(job, config, workspace, ansible_dir, kubeconfig, env, secrets)
            else:
                append_log(job.id, "Talos/LB/Calico/Helm/Verify wird ohne Terraform fortgesetzt.\n")
                run_talos_stack(job, config, workspace, ansible_dir, kubeconfig, env, secrets)
            with SessionLocal() as db:
                cluster = db.get(Cluster, config.id)
                if cluster:
                    cluster.status = ClusterStatus.READY
                    db.commit()
            return

        if job.kind == JobKind.VERIFY:
            verify_cluster(job, config, workspace, env, secrets)
            with SessionLocal() as db:
                cluster = db.get(Cluster, config.id)
                if cluster and cluster.applied_hash == cluster.config_hash:
                    cluster.status = ClusterStatus.READY
                    db.commit()
            return

        if job.kind == JobKind.DESTROY:
            plan_path = terraform_dir / "destroy.tfplan"
            invalidate_plan_authorization(config.id, destroy=True, status=ClusterStatus.APPLYING)
            if not plan_path.is_file():
                raise RuntimeError("Der geprüfte Destroy-Plan fehlt; bitte einen neuen Destroy-Plan erstellen")
            try:
                run_command(job, ["terraform", "apply", "-input=false", terraform_parallelism_arg(), "destroy.tfplan"], terraform_dir, env, secrets)
            finally:
                plan_path.unlink(missing_ok=True)
            if config.cluster_type == ClusterType.TALOS:
                for marker in ("bootstrap.requested.yaml", "bootstrap.complete.yaml"):
                    (workspace / "talos" / marker).unlink(missing_ok=True)
            with SessionLocal() as db:
                cluster = db.get(Cluster, config.id)
                if cluster:
                    cluster.status = ClusterStatus.DESTROYED
                    cluster.applied_hash = None
                    cluster.applied_vm_ids = []
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
    api_vip = str(cluster.config.get("network", {}).get("api_vip", "")).strip()
    if api_vip:
        run_ingress_tests(job, documents, api_vip)


def verify_cluster(job: Job, config: ClusterConfig, workspace: Path, env: dict[str, str], secrets: list[str]) -> None:
    kubeconfig = workspace / "kubeconfig"
    if not kubeconfig.is_file():
        raise RuntimeError("Cluster-Verifikation benötigt eine vorhandene Kubeconfig")
    kubectl = ["kubectl", "--kubeconfig", str(kubeconfig)]
    expected_nodes = [node.name for node in config.nodes if node.role in ("control_plane", "worker")]
    run_command(
        job,
        [*kubectl, "wait", "--for=condition=Ready", "--timeout=300s", *[f"node/{name}" for name in expected_nodes]],
        workspace,
        env,
        secrets,
    )
    run_command(
        job,
        [
            *kubectl, "wait", "pods", "--all", "--all-namespaces",
            "--field-selector=status.phase!=Succeeded,status.phase!=Failed",
            "--for=condition=Ready", "--timeout=300s",
        ],
        workspace,
        env,
        secrets,
    )
    run_command(job, [*kubectl, "get", "nodes", "-o", "wide"], workspace, env, secrets)
    run_command(job, [*kubectl, "get", "pods", "-A"], workspace, env, secrets)
    run_command(job, [*kubectl, "get", "--raw=/readyz?verbose"], workspace, env, secrets)


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
        job.heartbeat_at = job.started_at
        db.commit()
        return job.id


def fail_running_job(db, job: Job, reason: str) -> None:
    now = datetime.now(UTC)
    job.status = JobStatus.FAILED
    job.error = reason
    job.finished_at = now
    job.heartbeat_at = now
    job.log = (job.log or "") + f"\nFEHLER: {reason}\n"
    if job.kind in CLUSTER_FAILURE_JOB_KINDS:
        cluster = db.get(Cluster, job.cluster_id)
        if cluster:
            cluster.status = ClusterStatus.FAILED


def recover_interrupted_jobs() -> None:
    # A second worker may start while the first one legitimately owns a job.
    # Heartbeats distinguish abandoned work without invalidating active jobs.
    recover_stale_running_jobs()


def recover_stale_running_jobs() -> None:
    cutoff = datetime.now(UTC) - timedelta(minutes=settings.stale_job_timeout_minutes)
    with SessionLocal() as db:
        jobs = db.scalars(select(Job).where(Job.status == JobStatus.RUNNING)).all()
        for job in jobs:
            last_seen = job.heartbeat_at or job.started_at or job.created_at
            if last_seen and last_seen < cutoff:
                fail_running_job(db, job, f"Job hatte seit mehr als {settings.stale_job_timeout_minutes} Minuten keinen Worker-Heartbeat.")
        db.commit()


def finish_job(job_id: str, error: Exception | None = None) -> None:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if not job:
            return
        job.finished_at = datetime.now(UTC)
        job.heartbeat_at = job.finished_at
        if isinstance(error, JobCancelled):
            job.status = JobStatus.CANCELLED
            job.error = str(error)
            if job.kind in MUTATING_JOB_KINDS:
                cluster = db.get(Cluster, job.cluster_id)
                if cluster:
                    cluster.status = ClusterStatus.FAILED
        elif error:
            job.status = JobStatus.FAILED
            job.error = redact(str(error))
            if job.kind in CLUSTER_FAILURE_JOB_KINDS:
                cluster = db.get(Cluster, job.cluster_id)
                if cluster:
                    cluster.status = ClusterStatus.FAILED
        else:
            job.status = JobStatus.SUCCEEDED
        db.commit()


def main() -> None:
    recover_interrupted_jobs()
    while True:
        recover_stale_running_jobs()
        job_id = claim_job()
        if not job_id:
            time.sleep(settings.worker_poll_seconds)
            continue
        try:
            execute(job_id)
        except Exception as exc:  # worker boundary: persist every failure
            label = "ABGEBROCHEN" if isinstance(exc, JobCancelled) else "FEHLER"
            append_log(job_id, f"\n{label}: {exc}\n")
            summary = failure_summary(exc, job_log_tail(job_id))
            if summary:
                append_log(job_id, summary + "\n")
            finish_job(job_id, exc)
        else:
            finish_job(job_id)


if __name__ == "__main__":
    main()
