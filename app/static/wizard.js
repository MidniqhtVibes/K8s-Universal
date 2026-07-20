const proxmoxCredential = document.querySelector('#proxmox-credential');
const sshCredential = document.querySelector('#ssh-credential');
const proxmoxNode = document.querySelector('#proxmox-node');
const templateVmId = document.querySelector('#template-vm-id');
const templateDiskValue = document.querySelector('#template-disk-value');
const templateDiskMessage = document.querySelector('#template-disk-message');
const discoveryOutput = document.querySelector('#discovery-result');
const diskInputs = ['lb_disk', 'cp_disk', 'worker_disk']
  .map(name => document.querySelector(`[name="${name}"]`))
  .filter(Boolean);
let discoveryData = null;
let discoverySequence = 0;

const resetTemplateDiskMinimum = message => {
  if (templateDiskValue) templateDiskValue.textContent = 'Noch nicht ermittelt';
  if (templateDiskMessage) templateDiskMessage.textContent = message;
  for (const input of diskInputs) {
    input.min = '8';
    const hint = input.parentElement?.querySelector('.template-disk-minimum');
    if (hint) hint.textContent = 'Statisches Minimum: 8 GB';
  }
  if (templateVmId) {
    templateVmId.setCustomValidity(templateVmId.value ? 'Bitte die Template-Disk per Proxmox-Discovery ermitteln.' : '');
  }
};

const updateTemplateDiskMinimum = () => {
  if (!templateVmId) return;
  if (!discoveryData) {
    resetTemplateDiskMinimum('Bitte Proxmox-Ressourcen erkennen.');
    return;
  }
  const selected = (discoveryData.vms || []).find(item =>
    Number(item.template) === 1
    && item.type === 'qemu'
    && item.node === proxmoxNode?.value
    && Number(item.vmid) === Number(templateVmId.value)
  );
  if (!selected) {
    resetTemplateDiskMinimum(templateVmId.value ? 'Das ausgewählte Template wurde auf diesem Node nicht gefunden.' : 'Bitte ein Template auswählen.');
    return;
  }
  const minimum = Number(selected.template_disk_gb);
  if (!Number.isInteger(minimum) || minimum < 1) {
    resetTemplateDiskMinimum('Die Template-Disk ist über die Proxmox-API nicht verfügbar.');
    templateVmId.setCustomValidity('Die Disk-Größe des ausgewählten Proxmox-Templates konnte nicht ermittelt werden.');
    return;
  }
  const effectiveMinimum = Math.max(8, minimum);
  templateVmId.setCustomValidity('');
  if (templateDiskValue) templateDiskValue.textContent = `${minimum} GB`;
  if (templateDiskMessage) templateDiskMessage.textContent = `Alle VM-Disks müssen mindestens ${effectiveMinimum} GB groß sein.`;
  for (const input of diskInputs) {
    input.min = String(effectiveMinimum);
    const hint = input.parentElement?.querySelector('.template-disk-minimum');
    if (hint) hint.textContent = `Minimum aufgrund des ausgewählten Templates: ${effectiveMinimum} GB`;
  }
};

proxmoxCredential?.addEventListener('change', () => {
  discoverySequence += 1;
  const option = proxmoxCredential.selectedOptions[0];
  document.querySelector('#proxmox-endpoint').value = option?.dataset.endpoint || '';
  discoveryData = null;
  for (const selector of ['#proxmox-nodes', '#proxmox-storages', '#proxmox-templates', '#proxmox-bridges']) {
    const list = document.querySelector(selector);
    if (list) list.innerHTML = '';
  }
  if (discoveryOutput) {
    discoveryOutput.textContent = '';
    discoveryOutput.classList.add('hidden');
  }
  resetTemplateDiskMinimum('Bitte Proxmox-Ressourcen für das ausgewählte Credential erkennen.');
});
sshCredential?.addEventListener('change', () => {
  const option = sshCredential.selectedOptions[0];
  document.querySelector('#ssh-public-key').value = option?.dataset.publicKey || '';
});
const fill = (id, values, key, labelKey) => {
  const target = document.querySelector(id);
  target.innerHTML = '';
  for (const item of values || []) {
    const option = document.createElement('option');
    option.value = item[key];
    if (labelKey && item[labelKey]) option.label = item[labelKey];
    target.appendChild(option);
  }
};

const updateNodeDiscovery = () => {
  if (!discoveryData) {
    updateTemplateDiskMinimum();
    return;
  }
  const selectedNode = proxmoxNode.value;
  const details = discoveryData.details?.[selectedNode] || {};
  fill('#proxmox-storages', details.storages, 'storage', 'type');
  fill('#proxmox-bridges', details.bridges, 'iface', 'comments');
  const proxmoxTemplates = (discoveryData.vms || []).filter(item =>
    Number(item.template) === 1 && item.type === 'qemu' && item.node === selectedNode
  );
  fill('#proxmox-templates', proxmoxTemplates, 'vmid', 'name');
  if (templateVmId && !templateVmId.value && proxmoxTemplates.length === 1) {
    templateVmId.value = proxmoxTemplates[0].vmid;
  }
  updateTemplateDiskMinimum();
};

proxmoxNode?.addEventListener('change', updateNodeDiscovery);
templateVmId?.addEventListener('input', updateTemplateDiskMinimum);
templateVmId?.addEventListener('change', updateTemplateDiskMinimum);

document.querySelector('#discover')?.addEventListener('click', async () => {
  const option = proxmoxCredential.selectedOptions[0];
  const requestSequence = ++discoverySequence;
  if (!option?.dataset.id) { discoveryOutput.textContent = 'Bitte zuerst ein Proxmox-Credential auswählen.'; discoveryOutput.classList.remove('hidden'); return; }
  discoveryOutput.textContent = 'Proxmox wird abgefragt …'; discoveryOutput.classList.remove('hidden');
  try {
    const response = await fetch(`/api/proxmox/${option.dataset.id}/discover`);
    const data = await response.json();
    if (requestSequence !== discoverySequence) return;
    if (!response.ok) { discoveryOutput.textContent = data.detail || 'Discovery fehlgeschlagen'; discoveryData = null; updateTemplateDiskMinimum(); return; }
    discoveryData = data;
    discoveryOutput.textContent = JSON.stringify(data, null, 2);
    fill('#proxmox-nodes', data.nodes, 'node', 'status');
    const availableNodes = new Set((data.nodes || []).map(item => item.node));
    if (!availableNodes.has(proxmoxNode.value) && data.nodes?.length) {
      proxmoxNode.value = data.nodes[0].node;
    }
    updateNodeDiscovery();
  } catch (error) {
    if (requestSequence !== discoverySequence) return;
    discoveryOutput.textContent = `Discovery fehlgeschlagen: ${error.message}`;
    discoveryData = null;
    updateTemplateDiskMinimum();
  }
});
proxmoxCredential?.dispatchEvent(new Event('change'));
sshCredential?.dispatchEvent(new Event('change'));

const topologyCard = document.querySelector('[name="lb_count"]')?.closest('.card');
if (topologyCard && !document.querySelector('#suggest-allocations')) {
  const button = document.createElement('button');
  button.type = 'button'; button.id = 'suggest-allocations'; button.className = 'secondary';
  button.textContent = 'Freie IPs und VM-IDs vorschlagen';
  topologyCard.insertBefore(button, topologyCard.querySelector('h3'));
}
document.querySelector('#suggest-allocations')?.addEventListener('click', async () => {
  const value = name => document.querySelector(`[name="${name}"]`)?.value;
  const params = new URLSearchParams({lb_count: value('lb_count'), cp_count: value('cp_count'), worker_count: value('worker_count')});
  const match = location.pathname.match(/^\/clusters\/([^/]+)\/edit$/);
  if (match) params.set('exclude_cluster_id', match[1]);
  const credentialId = proxmoxCredential?.selectedOptions[0]?.dataset.id;
  if (credentialId) params.set('credential_id', credentialId);
  const response = await fetch(`/api/allocations/suggest?${params}`);
  const data = await response.json();
  if (!response.ok) { window.alert(data.detail || 'Keine freie Vergabe gefunden.'); return; }
  for (const field of ['api_vip', 'lb_ip_start', 'cp_ip_start', 'worker_ip_start', 'lb_vm_id_start', 'cp_vm_id_start', 'worker_vm_id_start']) {
    const input = document.querySelector(`[name="${field}"]`);
    if (input) input.value = data[field];
  }
});

const registryEnabled = document.querySelector('#registry-enabled');
const registryOptions = document.querySelector('#registry-options');
const registryEndpoint = document.querySelector('#registry-endpoint');
const registryEndpointError = document.querySelector('#registry-endpoint-error');
const registryUseHttp = document.querySelector('#registry-use-http');
const registryHttpWarning = document.querySelector('#registry-http-warning');
const registryValidationMessage = 'Bitte eine Registry-Adresse im Format host:port angeben, zum Beispiel 10.200.50.240:5000.';
let registryEndpointTouched = false;

const isValidRegistryEndpoint = rawValue => {
  const value = rawValue.trim();
  if (!value || value.includes('://') || value.includes('/') || /\s/.test(value)) return false;

  const separator = value.lastIndexOf(':');
  if (separator <= 0 || value.indexOf(':') !== separator) return false;
  const host = value.slice(0, separator);
  const portText = value.slice(separator + 1);
  if (!/^\d{1,5}$/.test(portText)) return false;
  const port = Number(portText);
  if (port < 1 || port > 65535) return false;

  if (/^\d+(?:\.\d+){3}$/.test(host)) {
    return host.split('.').every(part => Number(part) <= 255);
  }
  if (host.length > 253) return false;
  return host.split('.').every(label =>
    label.length > 0 && label.length <= 63 && /^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$/.test(label)
  );
};

const validateRegistryEndpoint = () => {
  if (!registryEndpoint) return true;
  const valid = !registryEnabled?.checked || isValidRegistryEndpoint(registryEndpoint.value);
  registryEndpoint.setCustomValidity(valid ? '' : registryValidationMessage);
  if (valid) {
    registryEndpoint.removeAttribute('aria-invalid');
  } else {
    registryEndpoint.setAttribute('aria-invalid', 'true');
  }
  if (registryEndpointError) {
    registryEndpointError.textContent = valid ? '' : registryValidationMessage;
    registryEndpointError.classList.toggle('hidden', valid || !registryEndpointTouched);
  }
  return valid;
};

const updateRegistryFields = () => {
  if (!registryEnabled || !registryOptions || !registryEndpoint || !registryUseHttp) return;
  const enabled = registryEnabled.checked;
  registryOptions.hidden = !enabled;
  registryOptions.setAttribute('aria-hidden', String(!enabled));
  registryEnabled.setAttribute('aria-expanded', String(enabled));
  registryEndpoint.disabled = !enabled;
  registryEndpoint.required = enabled;
  registryUseHttp.disabled = !enabled;
  if (!enabled) registryEndpointTouched = false;
  validateRegistryEndpoint();
  if (registryHttpWarning) registryHttpWarning.hidden = !enabled || !registryUseHttp.checked;
};

registryEnabled?.addEventListener('change', updateRegistryFields);
registryUseHttp?.addEventListener('change', updateRegistryFields);
registryEndpoint?.addEventListener('input', validateRegistryEndpoint);
registryEndpoint?.addEventListener('blur', () => {
  registryEndpoint.value = registryEndpoint.value.trim();
  registryEndpointTouched = true;
  validateRegistryEndpoint();
});
registryEndpoint?.addEventListener('invalid', () => {
  registryEndpointTouched = true;
  validateRegistryEndpoint();
});
document.querySelector('#wizard')?.addEventListener('submit', () => {
  if (!registryEnabled?.checked || !registryEndpoint) return;
  registryEndpoint.value = registryEndpoint.value.trim();
  registryEndpointTouched = true;
  validateRegistryEndpoint();
});
updateRegistryFields();
