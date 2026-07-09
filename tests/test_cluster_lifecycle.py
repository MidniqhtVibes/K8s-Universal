from pathlib import Path


def test_cluster_lifecycle_has_ansible_rerun_safe_delete_and_retention_controls():
    project = Path(__file__).parents[1]
    main = (project / "app/main.py").read_text(encoding="utf-8")
    cluster_template = (project / "app/templates/cluster.html").read_text(encoding="utf-8")
    application_template = (project / "app/templates/application_editor.html").read_text(encoding="utf-8")
    config = (project / "app/config.py").read_text(encoding="utf-8")

    assert "JobKind.ANSIBLE" in main
    assert "terraform.tfstate" in main
    assert "def present_cluster_vm_ids" in main
    assert "Builder-Eintrag ist geschuetzt" in main
    assert '@app.post("/clusters/{cluster_id}/prune-jobs")' in main
    assert '@app.post("/clusters/{cluster_id}/applications/{bundle_id}/revisions/prune")' in main

    assert "/jobs/ansible" in cluster_template
    assert "Ansible erneut ausfuehren" in cluster_template
    assert "/prune-jobs" in cluster_template
    assert "VMs bereits ausserhalb des Builders geloescht" in cluster_template
    assert "/revisions/prune" in application_template

    assert "job_retention_keep" in config
    assert "manifest_revision_retention_keep" in config
    assert "stale_job_timeout_minutes" in config
