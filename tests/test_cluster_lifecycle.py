from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.generator import config_hash
from app.models import Cluster, ClusterStatus
from app.schemas import ClusterConfig
from app.services import save_cluster
from .helpers import valid_config


def test_cluster_lifecycle_has_ansible_rerun_safe_delete_and_retention_controls():
    project = Path(__file__).parents[1]
    main = (project / "app/main.py").read_text(encoding="utf-8")
    cluster_template = (project / "app/templates/cluster.html").read_text(encoding="utf-8")
    application_template = (project / "app/templates/application_editor.html").read_text(encoding="utf-8")
    config = (project / "app/config.py").read_text(encoding="utf-8")
    models = (project / "app/models.py").read_text(encoding="utf-8")
    services = (project / "app/services.py").read_text(encoding="utf-8")

    assert "JobKind.ANSIBLE" in main
    assert "terraform.tfstate" in main
    assert "def present_cluster_vm_ids" in main
    assert "managed_vm_ids(state_path)" in main
    assert "cluster_runtime_is_current" in main
    assert "Builder-Eintrag ist geschuetzt" in main
    assert '@app.post("/clusters/{cluster_id}/prune-jobs")' in main
    assert '@app.post("/clusters/{cluster_id}/applications/{bundle_id}/revisions/prune")' in main

    assert "/jobs/ansible" in cluster_template
    assert "Ansible erneut ausführen" in cluster_template
    assert "cluster.planned_hash != cluster.config_hash" in cluster_template
    assert "not terraform_state_available" in cluster_template
    assert "not kubeconfig_available" in cluster_template
    assert "/prune-jobs" in cluster_template
    assert "VMs bereits außerhalb des Builders gelöscht" in cluster_template
    assert "/revisions/prune" in application_template

    assert "job_retention_keep" in config
    assert "manifest_revision_retention_keep" in config
    assert "stale_job_timeout_minutes" in config
    assert "applied_hash" in models and "applied_vm_ids" in models
    assert "cluster.status = ClusterStatus.DRAFT" in services


def test_edit_invalidates_runtime_and_remembers_previously_applied_vm_ids(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    source = Path(__file__).parents[1]
    config = valid_config()

    with Session(engine) as db:
        cluster = save_cluster(db, config, tmp_path, source)
        cluster.status = ClusterStatus.READY
        cluster.applied_hash = cluster.config_hash
        cluster.applied_vm_ids = None
        db.commit()
        kubeconfig = tmp_path / "clusters" / config.id / "kubeconfig"
        kubeconfig.write_text("old cluster access", encoding="utf-8")

        payload = config.model_dump(mode="json")
        payload["nodes"][0]["cores"] = 2
        edited = type(config).model_validate(payload)
        cluster = save_cluster(db, edited, tmp_path, source)

        assert cluster.status == ClusterStatus.DRAFT
        assert cluster.applied_hash != cluster.config_hash
        assert set(cluster.applied_vm_ids or []) == {node.vm_id for node in config.nodes}
        assert not kubeconfig.exists()


def test_legacy_defaults_do_not_invalidate_an_unchanged_ready_cluster(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    source = Path(__file__).parents[1]
    config = valid_config()
    legacy = config.model_dump(mode="json")
    for field in ("cluster_type", "load_balancer_ssh", "talos"):
        legacy.pop(field, None)
    legacy["proxmox"].pop("load_balancer_template_vm_id", None)
    legacy_digest = config_hash(legacy)

    with Session(engine) as db:
        cluster = Cluster(
            id=config.id,
            name=config.name,
            config=legacy,
            config_hash=legacy_digest,
            applied_hash=legacy_digest,
            applied_vm_ids=[node.vm_id for node in config.nodes],
            status=ClusterStatus.READY,
        )
        db.add(cluster)
        db.commit()
        kubeconfig = tmp_path / "clusters" / config.id / "kubeconfig"
        kubeconfig.parent.mkdir(parents=True)
        kubeconfig.write_text("existing access", encoding="utf-8")

        saved = save_cluster(db, ClusterConfig.model_validate(legacy), tmp_path, source)

        assert saved.config == legacy
        assert saved.config_hash == legacy_digest
        assert saved.applied_hash == legacy_digest
        assert saved.status == ClusterStatus.READY
        assert kubeconfig.read_text(encoding="utf-8") == "existing access"


def test_editing_an_unapplied_draft_does_not_mark_vm_ids_as_deployed(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    source = Path(__file__).parents[1]
    config = valid_config()

    with Session(engine) as db:
        cluster = save_cluster(db, config, tmp_path, source)
        payload = config.model_dump(mode="json")
        payload["nodes"][0]["cores"] = 2
        saved = save_cluster(db, ClusterConfig.model_validate(payload), tmp_path, source)

        assert saved.status == ClusterStatus.DRAFT
        assert saved.applied_hash is None
        assert not saved.applied_vm_ids
