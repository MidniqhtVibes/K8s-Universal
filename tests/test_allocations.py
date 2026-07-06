import ipaddress

import pytest

from app.allocations import _first_id_block, _first_ip_block, parse_reserved_ips, validate_preference_config


def test_ip_and_id_suggestions_skip_used_values():
    used_ips = {ipaddress.IPv4Address("10.0.0.10"), ipaddress.IPv4Address("10.0.0.11")}
    assert _first_ip_block("10.0.0.10", "10.0.0.20", 2, used_ips) == "10.0.0.12"
    assert _first_id_block(300, 310, 2, {300, 301, 303}) == 304


def test_reserved_ranges_are_parsed_and_bounded():
    values = parse_reserved_ips("10.0.0.2, 10.0.0.8/30")
    assert ipaddress.IPv4Address("10.0.0.2") in values
    assert ipaddress.IPv4Address("10.0.0.9") in values
    with pytest.raises(ValueError, match="4096"):
        parse_reserved_ips("10.0.0.0/8")


def test_default_preferences_are_valid():
    validated = validate_preference_config({})
    assert validated["cp_count"] == 3
