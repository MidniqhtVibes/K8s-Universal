from pathlib import Path


def test_load_balancer_defaults_are_sized_for_keepalived_and_haproxy():
    wizard = (Path(__file__).parents[1] / "app/templates/wizard.html").read_text(encoding="utf-8")
    assert 'name="lb_memory" value="{{ values.get(\'lb_memory\',\'2048\') }}"' in wizard
    assert 'name="lb_disk" value="{{ values.get(\'lb_disk\',\'30\') }}"' in wizard


def test_template_vm_id_comes_from_input_or_proxmox_discovery():
    project = Path(__file__).parents[1]
    wizard = (project / "app/templates/wizard.html").read_text(encoding="utf-8")
    javascript = (project / "app/static/wizard.js").read_text(encoding="utf-8")

    assert "values.get('template_vm_id','')" in wizard
    assert "values.get('template_vm_id','9000')" not in wizard
    assert "proxmoxTemplates.length === 1" in javascript
    assert "templateVmId.value = proxmoxTemplates[0].vmid" in javascript


def test_proxmox_connection_is_credential_owned_and_discovery_is_node_scoped():
    project = Path(__file__).parents[1]
    wizard = (project / "app/templates/wizard.html").read_text(encoding="utf-8")
    javascript = (project / "app/static/wizard.js").read_text(encoding="utf-8")

    assert 'id="proxmox-endpoint"' in wizard and "readonly required" in wizard
    assert 'data-verify-tls=' not in wizard
    assert 'id="proxmox-verify-tls"' not in wizard
    assert 'name="verify_tls"' not in wizard
    assert "verifyTls" not in javascript
    assert "proxmox-verify-tls" not in javascript
    assert "item.type === 'qemu' && item.node === selectedNode" in javascript
    assert "availableNodes.has(proxmoxNode.value)" in javascript
    assert "proxmoxNode?.addEventListener('change'" in javascript


def test_wizard_does_not_offer_unimplemented_ports_or_minors():
    wizard = (Path(__file__).parents[1] / "app/templates/wizard.html").read_text(encoding="utf-8")
    assert 'name="ssh_port"' not in wizard
    assert 'name="api_port"' not in wizard
    assert '<option value="v1.36" selected>v1.36</option>' in wizard
    assert 'name="lb_count" min="2" max="10"' in wizard
