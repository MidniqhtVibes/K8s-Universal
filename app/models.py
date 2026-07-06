import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class CredentialKind(str, enum.Enum):
    PROXMOX = "proxmox"
    SSH = "ssh"


class ClusterStatus(str, enum.Enum):
    DRAFT = "draft"
    PLANNED = "planned"
    APPLYING = "applying"
    READY = "ready"
    FAILED = "failed"
    DESTROYED = "destroyed"


class JobKind(str, enum.Enum):
    PLAN = "plan"
    APPLY = "apply"
    VERIFY = "verify"
    DESTROY_PLAN = "destroy_plan"
    DESTROY = "destroy"


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[str] = mapped_column(String(100), unique=True, default="admin")
    password_hash: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Credential(Base):
    __tablename__ = "credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[CredentialKind] = mapped_column(Enum(CredentialKind))
    encrypted_payload: Mapped[str] = mapped_column(Text)
    public_data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("name", "kind"),)


class Cluster(Base):
    __tablename__ = "clusters"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(63), unique=True)
    status: Mapped[ClusterStatus] = mapped_column(Enum(ClusterStatus), default=ClusterStatus.DRAFT)
    config: Mapped[dict] = mapped_column(JSON)
    config_hash: Mapped[str] = mapped_column(String(64))
    planned_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    destroy_planned_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    jobs: Mapped[list["Job"]] = relationship(back_populates="cluster", cascade="all, delete-orphan")


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_id: Mapped[str] = mapped_column(ForeignKey("clusters.id", ondelete="CASCADE"), index=True)
    kind: Mapped[JobKind] = mapped_column(Enum(JobKind))
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.QUEUED, index=True)
    requested_config_hash: Mapped[str] = mapped_column(String(64))
    log: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cluster: Mapped[Cluster] = relationship(back_populates="jobs")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    action: Mapped[str] = mapped_column(String(120))
    object_type: Mapped[str] = mapped_column(String(80))
    object_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
