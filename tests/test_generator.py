import json

import yaml

from app.generator import config_hash, render_cluster
from .helpers import valid_config


def test_generator_produces_secret_free_outputs(tmp_path):
    config = valid_config()
    source = tmp_path / "source"
    (source / "terraform").mkdir(parents=True)
    (source / "terraform" / "main.tf").write_text("# test")
    (source / "ansible" / "group_vars").mkdir(parents=True)
    (source / "ansible" / "site.yml").write_text("---")
    render_cluster(config, tmp_path / "cluster", source)
    tfvars = json.loads((tmp_path / "cluster/generated/terraform.auto.tfvars.json").read_text())
    inventory = yaml.safe_load((tmp_path / "cluster/generated/ansible-inventory.yml").read_text())
    ansible_vars = yaml.safe_load((tmp_path / "cluster/generated/ansible-vars.yml").read_text())
    traefik_values = yaml.safe_load((tmp_path / "cluster/generated/traefik-values.yaml").read_text())
    assert tfvars["template_vm_id"] == 9876
    assert tfvars["nodes"]["worker-01"]["ip"] == "10.10.10.31"
    assert tfvars["nodes"]["worker-01"]["vm_name"] == "test-cluster-worker-01"
    assert inventory["all"]["children"]["control_plane"]["hosts"]["control-01"]["ansible_host"] == "10.10.10.21"
    assert 1 <= ansible_vars["keepalived_virtual_router_id"] <= 255
    assert ansible_vars["calico_block_size"] == 26
    assert traefik_values["service"] == {
        "spec": {"type": "NodePort", "externalTrafficPolicy": "Cluster"}
    }
    assert traefik_values["ports"]["web"]["nodePort"] == 30080
    assert traefik_values["ports"]["websecure"]["nodePort"] == 30443
    contents = "".join(path.read_text() for path in (tmp_path / "cluster").rglob("*") if path.is_file())
    assert "PRIVATE KEY" not in contents
    assert "api_token" not in contents


def test_hash_is_deterministic():
    config = valid_config().public_dict()
    assert config_hash(config) == config_hash(dict(reversed(list(config.items()))))


def test_generator_removes_stale_sources_but_preserves_terraform_runtime(tmp_path):
    config = valid_config()
    source = tmp_path / "source"
    terraform_source = source / "terraform"
    ansible_source = source / "ansible"
    (ansible_source / "group_vars").mkdir(parents=True)
    terraform_source.mkdir(parents=True)
    (terraform_source / "obsolete.tf").write_text("# obsolete", encoding="utf-8")
    (ansible_source / "obsolete.yml").write_text("---", encoding="utf-8")
    (ansible_source / "site.yml").write_text("---", encoding="utf-8")
    destination = tmp_path / "cluster"

    render_cluster(config, destination, source)
    (terraform_source / "obsolete.tf").unlink()
    (ansible_source / "obsolete.yml").unlink()
    (terraform_source / "main.tf").write_text("# current", encoding="utf-8")
    (destination / "terraform/terraform.tfstate").write_text("{}", encoding="utf-8")
    (destination / "terraform/tfplan").write_bytes(b"reviewed plan")
    (destination / "terraform/.terraform").mkdir()
    (destination / "terraform/.terraform/provider-marker").write_text("keep", encoding="utf-8")

    render_cluster(config, destination, source)

    assert not (destination / "terraform/obsolete.tf").exists()
    assert not (destination / "ansible/obsolete.yml").exists()
    assert (destination / "terraform/main.tf").is_file()
    assert (destination / "terraform/terraform.tfstate").read_text(encoding="utf-8") == "{}"
    assert (destination / "terraform/tfplan").read_bytes() == b"reviewed plan"
    assert (destination / "terraform/.terraform/provider-marker").is_file()
