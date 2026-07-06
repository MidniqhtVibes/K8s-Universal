async function refreshJobs() {
  const running = [...document.querySelectorAll('details[data-job-id]')].filter(item => ['queued','running'].includes(item.dataset.status));
  for (const item of running) {
    const response = await fetch(`/api/jobs/${item.dataset.jobId}`);
    if (!response.ok) continue;
    const job = await response.json();
    item.dataset.status = job.status;
    item.querySelector('.job-status').textContent = job.status;
    item.querySelector('.job-status').className = `badge ${job.status} job-status`;
    item.querySelector('.job-log').textContent = job.log || 'Wartet auf Worker …';
    if (!['queued','running'].includes(job.status)) setTimeout(() => location.reload(), 700);
  }
  if (running.length) setTimeout(refreshJobs, 1500);
}
refreshJobs();

