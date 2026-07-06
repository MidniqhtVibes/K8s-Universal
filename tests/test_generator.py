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
    assert tfvars["nodes"]["worker-01"]["ip"] == "10.10.10.31"
    assert tfvars["nodes"]["worker-01"]["vm_name"] == "test-cluster-worker-01"
    assert inventory["all"]["children"]["control_plane"]["hosts"]["control-01"]["ansible_host"] == "10.10.10.21"
    contents = "".join(path.read_text() for path in (tmp_path / "cluster").rglob("*") if path.is_file())
    assert "PRIVATE KEY" not in contents
    assert "api_token" not in contents


def test_hash_is_deterministic():
    config = valid_config().public_dict()
    assert config_hash(config) == config_hash(dict(reversed(list(config.items()))))
