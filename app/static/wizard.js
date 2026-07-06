const proxmoxCredential = document.querySelector('#proxmox-credential');
const sshCredential = document.querySelector('#ssh-credential');
proxmoxCredential?.addEventListener('change', () => {
  const option = proxmoxCredential.selectedOptions[0];
  if (option?.dataset.endpoint) document.querySelector('#proxmox-endpoint').value = option.dataset.endpoint;
});
sshCredential?.addEventListener('change', () => {
  const option = sshCredential.selectedOptions[0];
  document.querySelector('#ssh-public-key').value = option?.dataset.publicKey || '';
});
document.querySelector('#discover')?.addEventListener('click', async () => {
  const option = proxmoxCredential.selectedOptions[0];
  const output = document.querySelector('#discovery-result');
  if (!option?.dataset.id) { output.textContent = 'Bitte zuerst ein Proxmox-Credential auswählen.'; output.classList.remove('hidden'); return; }
  output.textContent = 'Proxmox wird abgefragt …'; output.classList.remove('hidden');
  const response = await fetch(`/api/proxmox/${option.dataset.id}/discover`);
  const data = await response.json();
  if (!response.ok) { output.textContent = data.detail || 'Discovery fehlgeschlagen'; return; }
  output.textContent = JSON.stringify(data, null, 2);
  const fill = (id, values, key, labelKey) => {
    const target = document.querySelector(id); target.innerHTML = '';
    for (const item of values || []) { const option = document.createElement('option'); option.value = item[key]; if (labelKey && item[labelKey]) option.label = item[labelKey]; target.appendChild(option); }
  };
  fill('#proxmox-nodes', data.nodes, 'node', 'status');
  fill('#proxmox-templates', (data.vms || []).filter(item => item.template === 1), 'vmid', 'name');
  if (data.nodes?.length) {
    const selectedNode = data.nodes[0].node;
    document.querySelector('#proxmox-node').value = selectedNode;
    fill('#proxmox-storages', data.details?.[selectedNode]?.storages, 'storage', 'type');
    fill('#proxmox-bridges', data.details?.[selectedNode]?.bridges, 'iface', 'comments');
  }
});
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
