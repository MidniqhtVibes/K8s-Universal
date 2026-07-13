let reloadScheduled = false;

async function refreshJobs() {
  const running = [...document.querySelectorAll('details[data-job-id]')].filter(item => ['queued','running'].includes(item.dataset.status));
  const statusLabels = {queued: 'Wartet', running: 'Läuft', succeeded: 'Erfolgreich', failed: 'Fehlgeschlagen', cancelled: 'Abgebrochen'};
  for (const item of running) {
    const response = await fetch(`/api/jobs/${item.dataset.jobId}`);
    if (!response.ok) continue;
    const job = await response.json();
    item.dataset.status = job.status;
    const status = item.querySelector('.job-status');
    status.className = `badge ${job.status} job-status`;
    status.dataset.statusLabel = statusLabels[job.status] || job.status;
    const statusText = status.querySelector('span');
    if (statusText) statusText.textContent = status.dataset.statusLabel;
    else status.textContent = status.dataset.statusLabel;
    const log = item.querySelector('.job-log');
    log.textContent = job.log || 'Wartet auf Worker …';
    log.scrollTop = log.scrollHeight;
    if (!['queued','running'].includes(job.status) && !reloadScheduled) {
      reloadScheduled = true;
      setTimeout(() => location.reload(), 700);
    }
  }
  if (running.length && !reloadScheduled) setTimeout(refreshJobs, 1500);
}
for (const log of document.querySelectorAll('details[data-status="running"] .job-log, details[data-status="queued"] .job-log')) {
  log.scrollTop = log.scrollHeight;
}
refreshJobs();
