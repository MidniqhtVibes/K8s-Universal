from pathlib import Path


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
