import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.proxmox import ProxmoxClient
from app.worker import configured_guest_ipv4_addresses, failure_summary, ingress_test_commands, ingress_test_targets, run_ingress_tests, validate_proxmox

from .helpers import valid_config


def test_proxmox_preflight_uses_configured_template_vm_id(monkeypatch, tmp_path):
    config = valid_config()

    class FakeProxmoxClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def discover(self):
            return {
                "nodes": [{"node": "pve"}],
                "vms": [{"vmid": config.proxmox.template_vm_id, "template": 1, "type": "qemu", "node": "pve"}],
                "details": {
                    "pve": {
                        "storages": [{"storage": "local-lvm"}],
                        "bridges": [{"iface": "vmbr0"}],
                    }
                },
            }

    monkeypatch.setattr("app.worker.ProxmoxClient", FakeProxmoxClient)
    monkeypatch.setattr("app.worker.append_log", lambda *_args, **_kwargs: None)

    validate_proxmox(SimpleNamespace(id="test-job"), config, "test-token", tmp_path)


def test_destroy_plan_avoids_terraform_refresh_and_proxmox_preflight():
    worker = (Path(__file__).parents[1] / "app/worker.py").read_text(encoding="utf-8")
    assert 'command.extend(["-destroy", "-refresh=false"])' in worker
    assert "if job.kind == JobKind.PLAN:\n                validate_proxmox" in worker


def test_terraform_commands_limit_parallelism():
    project = Path(__file__).parents[1]
    worker = (project / "app/worker.py").read_text(encoding="utf-8")
    config = (project / "app/config.py").read_text(encoding="utf-8")
    assert "terraform_parallelism: int = Field(default=2" in config
    assert "terraform_parallelism_arg()" in worker
    assert '["terraform", "apply", "-input=false", terraform_parallelism_arg(), "tfplan"]' in worker
    assert '["terraform", "apply", "-input=false", terraform_parallelism_arg(), "destroy.tfplan"]' in worker


def test_apply_consumes_the_reviewed_tfplan_without_replacing_it():
    worker = (Path(__file__).parents[1] / "app/worker.py").read_text(encoding="utf-8")
    assert "def create_terraform_plan(" in worker
    apply_branch = worker.split("if job.kind == JobKind.APPLY:", 1)[1].split("if job.kind == JobKind.ANSIBLE:", 1)[0]
    assert "create_terraform_plan(" not in apply_branch
    assert 'plan_path = terraform_dir / "tfplan"' in apply_branch
    assert "Der geprüfte Terraform-Plan fehlt" in apply_branch
    assert apply_branch.index("validate_proxmox(") < apply_branch.index('run_command(job, ["terraform", "apply"')
    assert "plan_path.unlink(missing_ok=True)" in apply_branch


def test_ingress_test_targets_use_vip_and_host_header():
    targets = ingress_test_targets(
        [
            {
                "kind": "Ingress",
                "spec": {
                    "rules": [
                        {"host": "web.lab.local", "http": {"paths": [{"path": "/"}, {"path": "/api"}]}},
                        {"host": "web.lab.local", "http": {"paths": [{"path": "/"}]}},
                    ]
                },
            }
        ],
        "10.200.50.150",
    )
    assert targets == [
        ("http://10.200.50.150/", "web.lab.local"),
        ("http://10.200.50.150/api", "web.lab.local"),
    ]


def test_ingress_test_commands_are_copyable_and_shell_safe():
    documents = [{
        "kind": "Ingress",
        "spec": {"rules": [{"host": "web.lab.local", "http": {"paths": [{"path": "/"}, {"path": "/api?q=test value"}]}}]},
    }]

    assert ingress_test_commands(documents, "10.200.50.150") == [
        'curl -v -H "Host: web.lab.local" "http://10.200.50.150/"',
        'curl -v -H "Host: web.lab.local" "http://10.200.50.150/api?q=test value"',
    ]


def test_run_ingress_tests_logs_manual_curl_command_after_http_result(monkeypatch):
    log: list[str] = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, headers):
            assert url == "http://10.200.50.150/"
            assert headers == {"Host": "web.lab.local"}
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr("app.worker.httpx.Client", lambda **_kwargs: FakeClient())
    monkeypatch.setattr("app.worker.ensure_not_cancelled", lambda _job_id: None)
    monkeypatch.setattr("app.worker.append_log", lambda _job_id, text: log.append(text))

    documents = [{
        "kind": "Ingress",
        "spec": {"rules": [{"host": "web.lab.local", "http": {"paths": [{"path": "/"}]}}]},
    }]
    run_ingress_tests(SimpleNamespace(id="job-1"), documents, "10.200.50.150")

    output = "".join(log)
    command = 'curl -v -H "Host: web.lab.local" "http://10.200.50.150/"'
    assert "web.lab.local http://10.200.50.150/ -> HTTP 200" in output
    assert "Manueller Curl-Test" in output
    assert command in output
    assert output.index("-> HTTP 200") < output.index(command)
    assert output.rstrip().endswith(command)


def test_manifest_apply_runs_ingress_tests_inside_the_worker():
    worker = (Path(__file__).parents[1] / "app/worker.py").read_text(encoding="utf-8")
    assert "HTTP-Funktionstest über die Cluster-VIP:" in worker
    assert "Manueller Curl-Test" in worker
    assert "commands = ingress_test_commands(documents, api_vip)" in worker
    assert "run_ingress_tests(job, documents, api_vip)" in worker
    assert "client.get(url, headers={\"Host\": host})" in worker
    assert "Kein Ingress-Host im Bundle gefunden" in worker


def test_helm_and_cluster_verification_wait_for_ready_resources():
    worker = (Path(__file__).parents[1] / "app/worker.py").read_text(encoding="utf-8")
    assert '"--version", config.addons.ingress.chart_version' in worker
    assert '"--wait", "--wait-for-jobs", "--timeout", "10m"' in worker
    assert '"wait", "--for=condition=Ready", "--timeout=300s"' in worker
    assert '"--field-selector=status.phase!=Succeeded,status.phase!=Failed"' in worker


def test_helm_failure_does_not_report_last_successful_ansible_task():
    log = """TASK [Replace server endpoint with VIP]
ok: [localhost]
$ helm upgrade --install traefik traefik/traefik
Error: UPGRADE FAILED: timed out waiting for the condition
"""
    summary = failure_summary(
        RuntimeError("Befehl fehlgeschlagen (1): helm upgrade --install traefik"),
        log,
    )

    assert summary is not None
    assert "Helm-Release" in summary
    assert "Replace server endpoint with VIP" not in summary


def test_proxmox_preflight_only_exempts_vm_ids_owned_by_terraform_state(monkeypatch, tmp_path):
    config = valid_config()
    state_dir = tmp_path / "terraform"
    state_dir.mkdir()
    (state_dir / "terraform.tfstate").write_text(json.dumps({
        "resources": [{
            "type": "proxmox_virtual_environment_vm",
            "instances": [{"attributes": {"vm_id": config.nodes[0].vm_id}}],
        }]
    }), encoding="utf-8")

    class FakeProxmoxClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def discover(self):
            return {
                "nodes": [{"node": "pve"}],
                "vms": [
                    {"vmid": config.proxmox.template_vm_id, "template": 1, "type": "qemu", "node": "pve"},
                    {"vmid": config.nodes[0].vm_id, "template": 0, "type": "qemu", "node": "pve"},
                    {"vmid": config.nodes[1].vm_id, "template": 0, "type": "qemu", "node": "pve"},
                ],
                "details": {"pve": {"storages": [{"storage": "local-lvm"}], "bridges": [{"iface": "vmbr0"}]}},
            }

    monkeypatch.setattr("app.worker.ProxmoxClient", FakeProxmoxClient)
    monkeypatch.setattr("app.worker.append_log", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match=str(config.nodes[1].vm_id)):
        validate_proxmox(SimpleNamespace(id="test-job"), config, "test-token", tmp_path)


@pytest.mark.parametrize(
    ("guest_config", "expected"),
    [
        ({"ipconfig0": "ip=10.20.30.40/24,gw=10.20.30.1", "ipconfig1": "ip=dhcp"}, {"10.20.30.40"}),
        ({"net0": "name=eth0,bridge=vmbr0,ip=10.20.30.41/24,type=veth"}, {"10.20.30.41"}),
        ({"ipconfig0": "ip6=2001:db8::10/64", "unrelated": "ip=10.20.30.42/24"}, set()),
    ],
)
def test_extracts_static_ipv4_addresses_from_proxmox_guest_configs(guest_config, expected):
    assert {str(item) for item in configured_guest_ipv4_addresses(guest_config)} == expected


@pytest.mark.parametrize(
    ("resource", "expected_path"),
    [
        ({"type": "qemu", "node": "pve-a", "vmid": 301}, "nodes/pve-a/qemu/301/config"),
        ({"type": "lxc", "node": "pve-b", "vmid": "401"}, "nodes/pve-b/lxc/401/config"),
    ],
)
def test_proxmox_client_reads_guest_config_from_resource_location(resource, expected_path):
    client = object.__new__(ProxmoxClient)
    requested_paths = []

    def fake_get(path, **_params):
        requested_paths.append(path)
        return {"ipconfig0": "ip=10.20.30.40/24"}

    client.get = fake_get

    assert client.guest_config(resource) == {"ipconfig0": "ip=10.20.30.40/24"}
    assert requested_paths == [expected_path]


def test_proxmox_preflight_rejects_foreign_vm_names(monkeypatch, tmp_path):
    config = valid_config()

    class FakeProxmoxClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def discover(self):
            return {
                "nodes": [{"node": "pve"}],
                "vms": [
                    {"vmid": config.proxmox.template_vm_id, "template": 1, "type": "qemu", "node": "pve"},
                    {"vmid": 777, "name": "test-cluster-lb-01", "template": 0, "type": "qemu", "node": "pve"},
                ],
                "details": {"pve": {"storages": [{"storage": "local-lvm"}], "bridges": [{"iface": "vmbr0"}]}},
            }

    monkeypatch.setattr("app.worker.ProxmoxClient", FakeProxmoxClient)
    monkeypatch.setattr("app.worker.append_log", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError) as error:
        validate_proxmox(SimpleNamespace(id="test-job"), config, "test-token", tmp_path)
    assert "Proxmox-Namen" in str(error.value)
    assert "test-cluster-lb-01 (VM-ID 777)" in str(error.value)


def test_proxmox_preflight_rejects_foreign_static_ips(monkeypatch, tmp_path):
    config = valid_config()

    class FakeProxmoxClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def discover(self):
            return {
                "nodes": [{"node": "pve"}],
                "vms": [
                    {"vmid": config.proxmox.template_vm_id, "template": 1, "type": "qemu", "node": "pve"},
                    {"vmid": 778, "name": "legacy-vm", "template": 0, "type": "qemu", "node": "pve"},
                ],
                "details": {"pve": {"storages": [{"storage": "local-lvm"}], "bridges": [{"iface": "vmbr0"}]}},
            }

        def guest_config(self, resource):
            assert resource["vmid"] == 778
            return {"ipconfig0": f"ip={config.nodes[0].ip}/24,gw={config.network.gateway}"}

    monkeypatch.setattr("app.worker.ProxmoxClient", FakeProxmoxClient)
    monkeypatch.setattr("app.worker.append_log", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError) as error:
        validate_proxmox(SimpleNamespace(id="test-job"), config, "test-token", tmp_path)
    assert "IP-Adressen" in str(error.value)
    assert f"{config.nodes[0].ip} (VM-ID 778, legacy-vm)" in str(error.value)


def test_proxmox_preflight_allows_state_owned_names_and_ips(monkeypatch, tmp_path):
    config = valid_config()
    state_dir = tmp_path / "terraform"
    state_dir.mkdir()
    (state_dir / "terraform.tfstate").write_text(json.dumps({
        "resources": [{
            "type": "proxmox_virtual_environment_vm",
            "instances": [{"attributes": {"vm_id": config.nodes[0].vm_id}}],
        }]
    }), encoding="utf-8")

    class FakeProxmoxClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def discover(self):
            return {
                "nodes": [{"node": "pve"}],
                "vms": [
                    {"vmid": config.proxmox.template_vm_id, "template": 1, "type": "qemu", "node": "pve"},
                    {"vmid": config.nodes[0].vm_id, "name": "test-cluster-lb-01", "template": 0, "type": "qemu", "node": "pve"},
                ],
                "details": {"pve": {"storages": [{"storage": "local-lvm"}], "bridges": [{"iface": "vmbr0"}]}},
            }

    monkeypatch.setattr("app.worker.ProxmoxClient", FakeProxmoxClient)
    monkeypatch.setattr("app.worker.append_log", lambda *_args, **_kwargs: None)

    validate_proxmox(SimpleNamespace(id="test-job"), config, "test-token", tmp_path)


def test_worker_recovers_stale_jobs_and_supports_ansible_rerun():
    project = Path(__file__).parents[1]
    worker = (project / "app/worker.py").read_text(encoding="utf-8")
    models = (project / "app/models.py").read_text(encoding="utf-8")
    migration = (project / "migrations/versions/0005_job_recovery_ansible.py").read_text(encoding="utf-8")

    assert 'ANSIBLE = "ansible"' in models
    assert "heartbeat_at" in models
    assert "ALTER TYPE jobkind ADD VALUE IF NOT EXISTS 'ANSIBLE'" in migration
    assert "op.add_column(\"jobs\", sa.Column(\"heartbeat_at\"" in migration
    assert "def recover_interrupted_jobs()" in worker
    assert "def recover_stale_running_jobs()" in worker
    assert "recover_interrupted_jobs()" in worker
    assert "recover_stale_running_jobs()" in worker.split("def recover_interrupted_jobs()", 1)[1].split("def recover_stale_running_jobs()", 1)[0]
    assert "JobKind.ANSIBLE" in worker
    assert "Ansible/Helm/Verify wird ohne Terraform erneut ausgefuehrt." in worker
    assert "Kurzdiagnose:" in worker