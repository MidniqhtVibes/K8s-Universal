from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.manifests import cleanup_manifest_revisions
from app.models import ApplicationBundle, Cluster, Job, JobKind, JobStatus, ManifestRevision
from app.services import cleanup_cluster_job_history


def make_cluster(db: Session, suffix: str = "1") -> Cluster:
    cluster = Cluster(
        id=str(uuid.uuid5(uuid.NAMESPACE_DNS, f"cluster-{suffix}")),
        name=f"cluster-{suffix}",
        config={},
        config_hash="a" * 64,
    )
    db.add(cluster)
    db.flush()
    return cluster


def add_jobs(db: Session, cluster: Cluster, count: int, status: JobStatus) -> list[Job]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    jobs = []
    for index in range(count):
        job = Job(
            id=str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{cluster.id}-{status.value}-{index}")),
            cluster_id=cluster.id,
            kind=JobKind.PLAN,
            status=status,
            requested_config_hash=cluster.config_hash,
            payload={},
            created_at=start + timedelta(minutes=index),
        )
        db.add(job)
        jobs.append(job)
    db.flush()
    return jobs


def test_job_cleanup_below_retention_deletes_nothing():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cluster = make_cluster(db)
        add_jobs(db, cluster, 40, JobStatus.SUCCEEDED)

        result = cleanup_cluster_job_history(db, cluster.id, 100)
        db.flush()

        assert result == {
            "deleted": 0,
            "kept": 40,
            "active_ignored": 0,
            "retention_limit": 100,
        }
        assert len(db.scalars(select(Job)).all()) == 40


def test_job_cleanup_keeps_newest_completed_and_all_active_jobs():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cluster = make_cluster(db)
        completed = add_jobs(db, cluster, 150, JobStatus.SUCCEEDED)
        queued = add_jobs(db, cluster, 1, JobStatus.QUEUED)[0]
        running = add_jobs(db, cluster, 1, JobStatus.RUNNING)[0]

        result = cleanup_cluster_job_history(db, cluster.id, 100)
        db.flush()

        remaining_ids = set(db.scalars(select(Job.id)).all())
        assert result == {
            "deleted": 50,
            "kept": 100,
            "active_ignored": 2,
            "retention_limit": 100,
        }
        assert remaining_ids == {job.id for job in completed[50:]} | {queued.id, running.id}


def add_revisions(db: Session, bundle: ApplicationBundle, count: int) -> list[ManifestRevision]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    revisions = []
    for index in range(count):
        revision = ManifestRevision(
            id=str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{bundle.id}-revision-{index}")),
            bundle_id=bundle.id,
            version=index + 1,
            snapshot={"manifest.yaml": f"revision {index + 1}"},
            message=f"revision {index + 1}",
            created_at=start + timedelta(minutes=index),
        )
        db.add(revision)
        revisions.append(revision)
    db.flush()
    return revisions


def test_revision_cleanup_below_retention_deletes_nothing():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cluster = make_cluster(db)
        bundle = ApplicationBundle(cluster_id=cluster.id, name="demo", description="")
        db.add(bundle)
        db.flush()
        add_revisions(db, bundle, 20)

        result = cleanup_manifest_revisions(db, bundle, 30)
        db.flush()

        assert result == {
            "deleted": 0,
            "kept_by_retention": 20,
            "kept_by_reference": 0,
            "retention_limit": 30,
        }
        assert len(db.scalars(select(ManifestRevision)).all()) == 20


def test_revision_cleanup_preserves_newest_and_old_job_references():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cluster = make_cluster(db)
        bundle = ApplicationBundle(cluster_id=cluster.id, name="demo", description="")
        db.add(bundle)
        db.flush()
        revisions = add_revisions(db, bundle, 50)
        jobs = []
        for index, revision in enumerate(revisions[:15]):
            job = Job(
                cluster_id=cluster.id,
                kind=JobKind.MANIFEST_APPLY,
                status=JobStatus.SUCCEEDED,
                requested_config_hash=cluster.config_hash,
                payload={"bundle_id": bundle.id, "revision_id": revision.id},
                created_at=datetime(2026, 2, 1, tzinfo=timezone.utc) + timedelta(minutes=index),
            )
            db.add(job)
            jobs.append(job)
        # This newest revision is protected by both rules and must not be double-counted.
        db.add(Job(
            cluster_id=cluster.id,
            kind=JobKind.MANIFEST_DIFF,
            status=JobStatus.SUCCEEDED,
            requested_config_hash=cluster.config_hash,
            payload={"bundle_id": bundle.id, "revision_id": revisions[-1].id},
        ))
        db.flush()

        result = cleanup_manifest_revisions(db, bundle, 30)
        db.flush()

        remaining_ids = set(db.scalars(select(ManifestRevision.id)).all())
        assert result == {
            "deleted": 5,
            "kept_by_retention": 30,
            "kept_by_reference": 15,
            "retention_limit": 30,
        }
        assert remaining_ids == {revision.id for revision in revisions[:15] + revisions[20:]}
        loaded_jobs = db.scalars(select(Job).where(Job.id.in_([job.id for job in jobs]))).all()
        assert len(loaded_jobs) == 15
        assert all(job.payload["revision_id"] in remaining_ids for job in loaded_jobs)


def test_cleanup_ui_uses_configured_limits_and_reports_structured_results():
    project = Path(__file__).parents[1]
    cluster_template = (project / "app/templates/cluster.html").read_text(encoding="utf-8")
    revision_template = (project / "app/templates/application_editor.html").read_text(encoding="utf-8")

    assert "Alte Historie aufräumen" in cluster_template
    assert "{{ job_retention_keep }}" in cluster_template
    assert "job_cleanup_result.deleted" in cluster_template
    assert "job_cleanup_result.active_ignored" in cluster_template

    assert "Alte Revisionen aufräumen" in revision_template
    assert "{{ manifest_revision_retention_keep }}" in revision_template
    assert "revision_cleanup_result.deleted" in revision_template
    assert "revision_cleanup_result.kept_by_reference" in revision_template
