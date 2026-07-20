from types import SimpleNamespace

from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.allocations import (
    DEFAULT_PREFERENCES,
    effective_preferences,
    get_preferences,
    validate_preference_config,
    wizard_default_values,
)
from app.db import Base, get_db
from app.main import app, current_user
from app.models import Cluster, Preference

from .helpers import valid_config


def test_preference_reads_are_non_persistent_and_merge_legacy_rows():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        assert get_preferences(db) is None
        assert effective_preferences(db)["worker_disk"] == DEFAULT_PREFERENCES["worker_disk"]
        db.flush()
        assert db.scalar(select(Preference)) is None

        db.add(Preference(id=1, config={"worker_disk": 75, "legacy_unknown": "ignored"}))
        db.commit()

        effective = effective_preferences(db)
        assert effective["worker_disk"] == 75
        assert effective["gateway"] == DEFAULT_PREFERENCES["gateway"]
        assert "legacy_unknown" not in effective


def test_preference_validation_and_wizard_defaults_never_copy_secrets():
    validated = validate_preference_config({
        "worker_disk": "80",
        "api_token": "plain-text-token",
        "private_key": "plain-text-private-key",
    })
    values = wizard_default_values(validated)

    assert validated["worker_disk"] == 80
    assert "api_token" not in validated
    assert "private_key" not in validated
    assert values["worker_disk"] == "80"
    assert values["api_vip"] == str(validated["vip_pool_start"])
    for role in ("lb", "cp", "worker"):
        assert values[f"{role}_ip_start"] == str(validated[f"{role}_ip_start"])
        assert values[f"{role}_vm_id_start"] == str(validated[f"{role}_vm_id_start"])
    assert all("token" not in key and "key" not in key for key in values)


def test_first_run_create_update_and_reset_defaults_through_authenticated_ui():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        original_cluster_config = valid_config().model_dump(mode="json")
        cluster = Cluster(
            id="00000000-0000-0000-0000-000000000777",
            name="existing-cluster",
            config=original_cluster_config,
            config_hash="b" * 64,
        )
        db.add(cluster)
        db.commit()

        def override_db():
            yield db

        def override_user(request: Request):
            request.session["user_id"] = "test-user"
            return SimpleNamespace(id="test-user", username="tester", enabled=True)

        original_overrides = dict(app.dependency_overrides)
        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[current_user] = override_user
        try:
            with TestClient(app) as client:
                dashboard = client.get("/")
                wizard = client.get("/clusters/new")

                assert dashboard.status_code == 200
                assert "Es wurde noch keine Standard-Cluster-Konfiguration eingerichtet" in dashboard.text
                assert wizard.status_code == 200
                assert "Keine Standard-Konfiguration vorhanden" in wizard.text
                assert 'name="api_vip" value="10.200.50.140"' in wizard.text
                assert get_preferences(db) is None

                created = client.post(
                    "/settings",
                    data={
                        "worker_disk": "75",
                        "api_token": "must-not-be-stored",
                        "private_key": "must-not-be-stored",
                    },
                    follow_redirects=False,
                )
                assert created.status_code == 303
                stored = get_preferences(db)
                assert stored is not None
                assert stored.config["worker_disk"] == 75
                assert "api_token" not in stored.config
                assert "private_key" not in stored.config
                assert db.get(Cluster, cluster.id).config == original_cluster_config

                configured_wizard = client.get("/clusters/new")
                assert configured_wizard.status_code == 200
                assert "Keine Standard-Konfiguration vorhanden" not in configured_wizard.text
                assert 'name="worker_disk" min="8" value="75"' in configured_wizard.text

                updated = client.post(
                    "/settings",
                    data={"worker_disk": "80"},
                    follow_redirects=False,
                )
                assert updated.status_code == 303
                assert get_preferences(db).config["worker_disk"] == 80
                assert 'name="worker_disk" min="8" value="80"' in client.get("/clusters/new").text

                reset = client.post("/settings/reset", follow_redirects=False)
                assert reset.status_code == 303
                assert get_preferences(db) is None
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(original_overrides)
