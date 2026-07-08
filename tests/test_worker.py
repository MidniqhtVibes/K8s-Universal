from pathlib import Path


def test_destroy_plan_avoids_terraform_refresh_and_proxmox_preflight():
    worker = (Path(__file__).parents[1] / "app/worker.py").read_text(encoding="utf-8")
    assert 'command.extend(["-destroy", "-refresh=false"])' in worker
    assert "if job.kind == JobKind.PLAN:\n                validate_proxmox" in worker
