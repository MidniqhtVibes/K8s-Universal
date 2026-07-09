import pytest

from app.manifests import DEFAULT_NGINX_FILES, render_snapshot, validate_manifest_content, validate_manifest_path


def test_default_nginx_bundle_contains_expected_structure():
    assert set(DEFAULT_NGINX_FILES) == {"namespace.yaml", "deployment.yaml", "service.yaml", "ingress.yaml"}
    rendered, documents = render_snapshot(DEFAULT_NGINX_FILES)
    assert documents[0]["kind"] == "Namespace"
    assert {item["kind"] for item in documents} == {"Namespace", "Deployment", "Service", "Ingress"}
    assert "nginx.lab.local" in rendered


def test_plain_kubernetes_secret_is_blocked():
    with pytest.raises(ValueError, match="Secrets"):
        validate_manifest_content("apiVersion: v1\nkind: Secret\nmetadata:\n  name: unsafe\nstringData:\n  password: test\n")


@pytest.mark.parametrize("path", ["../secret.yaml", "/tmp/file.yaml", "manifest.txt"])
def test_unsafe_manifest_paths_are_blocked(path):
    with pytest.raises(ValueError):
        validate_manifest_path(path)


def test_multi_document_yaml_is_supported():
    documents = validate_manifest_content("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: one\n---\napiVersion: v1\nkind: Service\nmetadata:\n  name: two\nspec:\n  ports: []\n")
    assert len(documents) == 2
