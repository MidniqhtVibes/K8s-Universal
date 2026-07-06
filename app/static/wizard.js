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
