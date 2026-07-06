import pytest
from pydantic import ValidationError

from .helpers import valid_config


def test_valid_cluster_schema():
    config = valid_config()
    assert config.network.cidr.prefixlen == 24
    assert len(config.nodes) == 6


@pytest.mark.parametrize("field,value", [("api_vip", "10.20.1.1"), ("api_vip", "10.10.10.11")])
def test_rejects_invalid_vip(field, value):
    payload = valid_config().model_dump(mode="json")
    payload["network"][field] = value
    with pytest.raises(ValidationError):
        type(valid_config()).model_validate(payload)


def test_rejects_overlapping_networks():
    payload = valid_config().model_dump(mode="json")
    payload["kubernetes"]["pod_cidr"] = "10.10.10.0/25"
    with pytest.raises(ValidationError, match="überschneiden"):
        type(valid_config()).model_validate(payload)


def test_rejects_duplicate_vm_id():
    payload = valid_config().model_dump(mode="json")
    payload["nodes"][1]["vm_id"] = payload["nodes"][0]["vm_id"]
    with pytest.raises(ValidationError, match="VM-IDs"):
        type(valid_config()).model_validate(payload)

