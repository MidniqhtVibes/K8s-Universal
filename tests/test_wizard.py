from pathlib import Path


def test_load_balancer_defaults_are_sized_for_keepalived_and_haproxy():
    wizard = (Path(__file__).parents[1] / "app/templates/wizard.html").read_text(encoding="utf-8")
    assert 'name="lb_memory" value="{{ values.get(\'lb_memory\',\'2048\') }}"' in wizard
    assert 'name="lb_disk" min="8" value="{{ values.get(\'lb_disk\',\'30\') }}"' in wizard


def test_template_vm_id_comes_from_input_or_proxmox_discovery():
    project = Path(__file__).parents[1]
    wizard = (project / "app/templates/wizard.html").read_text(encoding="utf-8")
    javascript = (project / "app/static/wizard.js").read_text(encoding="utf-8")

    assert "values.get('template_vm_id','')" in wizard
    assert "values.get('template_vm_id','9000')" not in wizard
    assert "proxmoxTemplates.length === 1" in javascript
    assert "templateVmId.value = proxmoxTemplates[0].vmid" in javascript


def test_template_disk_minimum_is_visible_and_drives_all_role_inputs():
    project = Path(__file__).parents[1]
    wizard = (project / "app/templates/wizard.html").read_text(encoding="utf-8")
    javascript = (project / "app/static/wizard.js").read_text(encoding="utf-8")

    assert 'id="template-disk-value"' in wizard
    assert 'id="template-disk-message"' in wizard
    for field in ("lb_disk", "cp_disk", "worker_disk"):
        assert f'name="{field}" min="8"' in wizard
        assert f"document.querySelector(`[name=\"${{name}}\"]`)" in javascript
    assert "selected.template_disk_gb" in javascript
    assert "input.min = String(effectiveMinimum)" in javascript
    assert "templateVmId.setCustomValidity" in javascript
    assert "Die Template-Disk ist über die Proxmox-API nicht verfügbar." in javascript
    assert "requestSequence !== discoverySequence" in javascript


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


def test_registry_fields_are_optional_and_provisioner_independent():
    project = Path(__file__).parents[1]
    wizard = (project / "app/templates/wizard.html").read_text(encoding="utf-8")
    javascript = (project / "app/static/wizard.js").read_text(encoding="utf-8")

    assert 'name="registry_enabled"' in wizard
    assert 'name="registry_endpoint"' in wizard
    assert 'name="registry_use_http"' in wizard
    assert "values.get('registry_enabled', '') == 'on'" in wizard
    assert "values.get('registry_endpoint', '')" in wizard
    assert "values.get('registry_use_http', '') == 'on'" in wizard
    assert "registryEndpoint.disabled = !enabled" in javascript
    assert "registryEndpoint.required = enabled" in javascript
    assert "registryUseHttp.disabled = !enabled" in javascript


def test_registry_client_validation_and_http_warning_are_explicit():
    project = Path(__file__).parents[1]
    wizard = (project / "app/templates/wizard.html").read_text(encoding="utf-8")
    javascript = (project / "app/static/wizard.js").read_text(encoding="utf-8")

    assert "Bitte eine Registry-Adresse im Format host:port angeben" in javascript
    assert "value.includes('://')" in javascript
    assert "port < 1 || port > 65535" in javascript
    assert "part => Number(part) <= 255" in javascript
    assert "Unsichere HTTP-Verbindung" in wizard
    assert "nur für vertrauenswürdige interne Test- oder Lab-Netze" in wizard
    assert "Für produktive Umgebungen" in wizard


def test_cluster_detail_shows_registry_status_with_legacy_defaults():
    cluster = (Path(__file__).parents[1] / "app/templates/cluster.html").read_text(encoding="utf-8")

    assert "cluster.config.get('registry_enabled', false)" in cluster
    assert "cluster.config.get('registry_use_http', false)" in cluster
    assert "cluster.config.get('registry_endpoint', '')" in cluster
    assert "Nicht konfiguriert" in cluster
    assert "Unverschlüsseltes HTTP" in cluster
