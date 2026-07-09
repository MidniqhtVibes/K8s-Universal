from pathlib import Path

from app.worker import ingress_test_commands


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


def test_apply_recreates_current_tfplan_before_applying():
    worker = (Path(__file__).parents[1] / "app/worker.py").read_text(encoding="utf-8")
    assert "def create_terraform_plan(" in worker
    assert "Terraform-Plan wird fuer den aktuellen Apply neu erzeugt" in worker
    assert "create_terraform_plan(job, terraform_dir, env, secrets)\n            with SessionLocal() as db:" in worker


def test_ingress_test_commands_use_vip_and_host_header():
    commands = ingress_test_commands(
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
    assert commands == [
        'curl -v -H "Host: web.lab.local" http://10.200.50.150/',
        'curl -v -H "Host: web.lab.local" http://10.200.50.150/api',
    ]


def test_manifest_apply_logs_ingress_test_commands():
    worker = (Path(__file__).parents[1] / "app/worker.py").read_text(encoding="utf-8")
    assert "Funktionstest ueber die Cluster-VIP:" in worker
    assert "commands = ingress_test_commands(documents, api_vip)" in worker
    assert "Kein Ingress-Host im Bundle gefunden" in worker


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
    assert "JobKind.ANSIBLE" in worker
    assert "Ansible/Helm/Verify wird ohne Terraform erneut ausgefuehrt." in worker
    assert "Kurzdiagnose:" in worker
