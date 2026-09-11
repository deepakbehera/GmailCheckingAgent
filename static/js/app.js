// AI Gmail Job Hunter Frontend Application Logic

let allJobs = [];
let currentFilter = 'ALL';
let currentPlatform = 'ALL';
let searchQuery = '';
let nextCheckTime = null;
let countdownInterval = null;
let currentSettings = {};
let currentPublicUrl = '';

// Tracks job ids seen before the latest refresh so newly-arrived jobs can be
// temporarily highlighted with a pulsing "NEW" badge after each refresh.
let seenJobIds = new Set();
let newlyAddedIds = new Set();

// Backend API base: on GitHub Pages the Python backend runs on Vercel,
// everywhere else (local dev / Vercel itself) we use same-origin paths.
const API_BASE = window.location.hostname.endsWith('github.io')
  ? 'https://gmail-checking-agent.vercel.app'
  : '';

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
  initApp();
  setupSSE();
});

async function initApp() {
  await loadSettings();
  await loadStats();
  await loadJobs();
  startLocalCountdown();
  checkAndPromptForAppPassword();
}

// --- REST API Calls ---

// Cold-start resilience: Vercel serverless functions can take ~30s to wake on
// the first request after inactivity, and the very first fetch may fail while
// the instance boots. Retry transient failures instead of leaving the
// dashboard stuck on an empty state.
let coldStartStatusTouched = false;

async function fetchWithRetry(url, opts = {}, retries = 4, delayMs = 6000) {
  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      const res = await fetch(url, opts);
      if (res.status < 500) return res; // 4xx = real error, surface it
      console.warn(`Server responded ${res.status} (attempt ${attempt}/${retries}) - retrying`);
    } catch (err) {
      console.warn(`Request failed (attempt ${attempt}/${retries}):`, err.message || err);
    }
    if (attempt < retries) {
      const pill = document.getElementById('live-connection-status');
      if (pill) {
        pill.innerText = `Waking up server... (${attempt}/${retries})`;
        coldStartStatusTouched = true;
      }
      await new Promise(r => setTimeout(r, delayMs));
    }
  }
  throw new Error(`Server did not respond after ${retries} attempts`);
}

function clearColdStartStatus() {
  if (!coldStartStatusTouched) return;
  const pill = document.getElementById('live-connection-status');
  if (pill && pill.innerText.startsWith('Waking up')) {
    pill.innerText = 'Live Feed Connected';
  }
  coldStartStatusTouched = false;
}

async function loadJobs() {
  try {
    // Always load ALL recent jobs and filter client-side (applyFiltersAndRender).
    // Fetching with server-side status/platform filters can race a slow server
    // (AI quota retry storms, cold starts) and blank the list mid-browse right
    // after an action like 'Apply Online'. Client-side filtering keeps every
    // card visible irrespective of job status and keeps the filter tabs
    // consistent even if a background refetch fails or returns slowly.
    const url = `${API_BASE}/api/jobs?status=ALL`;
    const res = await fetchWithRetry(url);
    const data = await res.json();
    if (data.status === 'success') {
      clearColdStartStatus();
      // Detect newly-arrived jobs (present now, not seen in the previous load)
      const freshIds = new Set();
      data.jobs.forEach(j => { if (!seenJobIds.has(j.id)) freshIds.add(j.id); });
      if (seenJobIds.size > 0 && freshIds.size > 0) {
        freshIds.forEach(id => newlyAddedIds.add(id));
        // Keep the highlight for 2 minutes, then the cards look normal
        setTimeout(() => { freshIds.forEach(id => newlyAddedIds.delete(id)); applyFiltersAndRender(); }, 120000);
        const sample = data.jobs.find(j => freshIds.has(j.id));
        showToast(`🆕 ${freshIds.size} new job(s) added to the dashboard!`, 'success');
      }
      seenJobIds = new Set(data.jobs.map(j => j.id));
      allJobs = data.jobs;
      applyFiltersAndRender();
    }
  } catch (err) {
    console.error('Failed to load jobs:', err);
    const pill = document.getElementById('live-connection-status');
    if (pill && coldStartStatusTouched) pill.innerText = 'Connection failed - try refresh';
    showToast('❌ Error loading jobs from server', 'error');
  }
}

async function loadStats() {
  try {
    const res = await fetchWithRetry(`${API_BASE}/api/stats`);
    const data = await res.json();
    if (data.status === 'success') {
      clearColdStartStatus();
      const stats = data.stats;
      document.getElementById('stat-total-jobs').innerText = stats.total_jobs || 0;
      document.getElementById('stat-new-jobs').innerText = stats.new_jobs || 0;
      document.getElementById('stat-applied-jobs').innerText = stats.applied_jobs || 0;
      document.getElementById('stat-repeat-companies').innerText = stats.repeat_companies || 0;
      document.getElementById('stat-in-review-jobs').innerText = stats.in_review_jobs || 0;
      document.getElementById('stat-see-later-jobs').innerText = stats.see_later_jobs || 0;
      document.getElementById('stat-checked-jobs').innerText = stats.checked_jobs || 0;
      
      if (stats.target_email) {
        document.getElementById('header-target-email').innerText = stats.target_email;
      }

      // Public URL banner: on any deployed host the dashboard's own origin IS
      // the public URL (e.g. https://gmail-checking-agent.vercel.app). Never
      // show a cloudflared tunnel URL stored in the DB by an old local run —
      // it is stale and unreachable outside that machine. Tunnel URLs are only
      // meaningful while viewing the dashboard on localhost.
      const host = window.location.hostname;
      const isLocalHost = host === 'localhost' || host === '127.0.0.1';
      const publicUrl = (!isLocalHost || !stats.public_url)
        ? window.location.origin
        : stats.public_url; // local run: fresh cloudflared tunnel for phone access
      currentPublicUrl = publicUrl;
      document.getElementById('display-public-url').innerText = publicUrl;
      document.getElementById('display-public-url').style.color = '#34d399';

      if (stats.last_checked_at) {
        document.getElementById('last-check-text').innerText = formatIST(stats.last_checked_at);
      } else {
        document.getElementById('last-check-text').innerText = 'Pending initial cycle';
      }

      if (stats.next_check_at) {
        // Server now returns UTC-aware ISO strings; Date parses the offset
        // correctly regardless of the viewer's local timezone.
        nextCheckTime = new Date(stats.next_check_at).getTime();
        // Ignore stale server timestamps: if next_check_at is already in the
        // past (server down, cold start, old data), restart from the full
        // interval instead of showing a stuck 00:00 that flickers.
        if (nextCheckTime < Date.now() - 5000) {
          const intervalMins = parseInt(stats.check_interval_mins || '15', 10);
          nextCheckTime = Date.now() + intervalMins * 60 * 1000;
        }
      } else {
        const intervalMins = parseInt(stats.check_interval_mins || '15', 10);
        nextCheckTime = Date.now() + intervalMins * 60 * 1000;
      }

      // Update platform counts on tabs
      updatePlatformCounts(stats.platform_counts || {});
    }
  } catch (err) {
    console.error('Failed to load stats:', err);
  }
}

function updatePlatformCounts(counts) {
  // counts is {source_platform: count} from get_dashboard_stats (server-side
  // GROUP BY over the whole jobs table), so the numbers persist across
  // refreshes and are unaffected by the current client-side filter selection.
  const allTab = document.querySelector('.platform-tab[data-platform="ALL"]');

  document.querySelectorAll('.platform-tab').forEach(tab => {
    const p = tab.getAttribute('data-platform');
    const countSpan = tab.querySelector('.tab-count') || document.createElement('span');
    countSpan.className = 'tab-count';
    countSpan.style.fontSize = '0.72rem';
    countSpan.style.opacity = '0.75';
    countSpan.style.marginLeft = '4px';

    if (p === 'ALL') {
      // Total = sum over every platform bucket (Direct included)
      const known = ['LinkedIn', 'Naukri', 'Indeed', 'Glassdoor', 'Monster', 'Direct'];
      let total = 0;
      known.forEach(k => { total += counts[k] || 0; });
      // Include any other platform values that might exist in the DB
      Object.keys(counts).forEach(k => {
        if (!known.includes(k)) total += counts[k] || 0;
      });
      countSpan.innerText = `(${total})`;
    } else {
      let cnt = counts[p] || 0;
      if (p === 'Direct') {
        // Direct Recruiter bucket: everything not in the 5 named platforms
        const named = ['LinkedIn', 'Naukri', 'Indeed', 'Glassdoor', 'Monster'];
        Object.keys(counts).forEach(k => {
          if (!named.includes(k)) cnt += counts[k] || 0;
        });
      }
      countSpan.innerText = `(${cnt})`;
    }
    if (!tab.querySelector('.tab-count')) {
      tab.appendChild(countSpan);
    }
  });

  if (allTab) {
    allTab.setAttribute('data-total', Object.values(counts).reduce((a, b) => a + b, 0));
  }
}

async function loadSettings() {
  try {
    const res = await fetchWithRetry(`${API_BASE}/api/settings`);
    const data = await res.json();
    if (data.status === 'success') {
      currentSettings = data.settings;
      document.getElementById('set-target-email').value = currentSettings.target_email || 'deepak.gvit@gmail.com';
      document.getElementById('set-check-interval').value = currentSettings.check_interval_mins || '15';
      document.getElementById('set-auth-mode').value = currentSettings.auth_mode || 'simulator';
      document.getElementById('set-gemini-key').value = currentSettings.gemini_api_key || '';
      document.getElementById('set-ntfy-topic').value = currentSettings.ntfy_topic || 'deepak-job-hunter-alerts';
      document.getElementById('set-desktop-notify').value = currentSettings.desktop_notify || 'true';
      document.getElementById('set-mobile-notify').value = currentSettings.mobile_notify || 'true';

      // Show whether an App Password is already stored (the real value is
      // never sent to the browser).
      const pwStatus = document.getElementById('imap-pw-status');
      if (pwStatus) {
        if (currentSettings.imap_password_set) {
          pwStatus.innerHTML = '✅ A password is currently saved ' +
            (currentSettings.imap_password_masked || '') +
            ' — type a new one above only if you want to replace it.';
          pwStatus.style.color = '#34d399';
        } else {
          pwStatus.innerHTML = '⚠️ No App Password saved yet — the inbox cannot be scanned until one is entered.';
          pwStatus.style.color = '#fbbf24';
        }
      }

      toggleAuthFields(currentSettings.auth_mode || 'simulator');
    }
  } catch (err) {
    console.error('Failed to load settings:', err);
  }
}

// --- Real-Time Server-Sent Events (SSE) ---

function setupSSE() {
  try {
    const eventSource = new EventSource(`${API_BASE}/api/events`);

    eventSource.onopen = () => {
      document.getElementById('live-connection-status').innerText = 'Live Feed Connected';
    };

    eventSource.onmessage = (e) => {
      try {
        const payload = JSON.parse(e.data);
        if (payload.type === 'CHECK_COMPLETED') {
          showToast(`📬 Checked Gmail: ${payload.data.new_jobs_found} new job(s) found!`, 'info');
          loadStats();
          loadJobs();
          if (payload.data.next_check_at) {
            nextCheckTime = new Date(payload.data.next_check_at).getTime();
          }
        } else if (payload.type === 'NEW_JOB_RECEIVED') {
          showToast(`🎯 New Job Opportunity: ${payload.data.job.job_title}`, 'success');
          loadStats();
          loadJobs();
        } else if (payload.type === 'JOB_STATUS_UPDATED') {
          loadStats();
          loadJobs();
        } else if (payload.type === 'JOBS_BULK_UPDATED') {
          loadStats();
          loadJobs();
        }
      } catch (parseErr) {
        // keepalive
      }
    };

    eventSource.onerror = () => {
      document.getElementById('live-connection-status').innerText = 'Polling (15m Interval)';
    };
  } catch (err) {
    console.log('SSE failed to connect:', err);
  }
}

// --- 15-Minute Countdown Timer ---

function startLocalCountdown() {
  if (countdownInterval) clearInterval(countdownInterval);

  countdownInterval = setInterval(() => {
    if (!nextCheckTime) {
      nextCheckTime = Date.now() + 15 * 60 * 1000;
    }

    const now = Date.now();
    const diff = nextCheckTime - now;

    if (diff <= 0) {
      // Held at zero instead of resetting: loadStats() updates nextCheckTime
      // from the server, and the interval guard prevents flicker from stale
      // timestamps racing a fresh one.
      document.getElementById('countdown-timer').innerText = '00:00';
      loadStats();
      return;
    }

    const mins = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));
    const secs = Math.floor((diff % (1000 * 60)) / 1000);

    const formatted = `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
    document.getElementById('countdown-timer').innerText = formatted;
  }, 1000);
}

// --- Client-Side Dual Filtering Engine ---

function matchesPlatform(job, filter) {
  if (!filter || filter === 'ALL') return true;
  const p = (job.source_platform || 'Direct').toLowerCase();
  const f = filter.toLowerCase();
  
  if (f === 'direct') {
    const named = ['linkedin', 'naukri', 'indeed', 'glassdoor', 'monster'];
    return p === 'direct' || !named.some(n => p.includes(n));
  }
  return p.includes(f) || 
         (job.email_sender && job.email_sender.toLowerCase().includes(f)) || 
         (job.email_subject && job.email_subject.toLowerCase().includes(f));
}

function matchesStatus(job, statusFilter) {
  if (!statusFilter || statusFilter === 'ALL') return true;
  return (job.status || 'NEW').toUpperCase() === statusFilter.toUpperCase();
}

function matchesSearch(job, query) {
  if (!query || !query.trim()) return true;
  const q = query.trim().toLowerCase();
  const searchable = `${job.job_title} ${job.company_name} ${job.location} ${job.skills} ${job.summary} ${job.source_platform}`.toLowerCase();
  return searchable.includes(q);
}

function applyFiltersAndRender() {
  const filtered = allJobs.filter(j => 
    matchesStatus(j, currentFilter) && 
    matchesPlatform(j, currentPlatform) && 
    matchesSearch(j, searchQuery)
  );
  renderJobs(filtered);
}

// --- Render Job Cards & Applied State ---

function renderJobs(jobs) {
  const container = document.getElementById('job-feed-container');
  if (!jobs || jobs.length === 0) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">📭</div>
        <h3>No Job Postings Found</h3>
        <p>No jobs matching the current filter (${currentPlatform} / ${currentFilter}). Use "Check Gmail Now" or "Simulate Job Alerts" to scan for openings.</p>
        <button class="btn btn-primary" onclick="openSimulateMenu()">🧪 Simulate Job Alerts</button>
      </div>
    `;
    return;
  }

  let html = '';
  jobs.forEach(job => {
    const status = (job.status || 'NEW').toUpperCase();
    const isApplied = status === 'APPLIED';
    // 'Checked' = user already looked at this job (clicked Apply/Details or
    // flagged manually). The card grays out but the status stays independent.
    const isChecked = Boolean(job.checked_at) && !isApplied;
    const appliedClass = isApplied ? 'is-applied' : '';
    const checkedClass = isChecked ? 'is-checked' : '';
    const statusClass = status === 'IN_REVIEW' ? 'is-in-review' : (status === 'SEE_LATER' ? 'is-see-later' : '');
    const newCardClass = newlyAddedIds.has(job.id) ? 'is-new-arrival' : '';
    const platform = job.source_platform || 'Direct';
    
    // Skills tags
    let skillsList = [];
    if (job.skills) {
      try {
        skillsList = typeof job.skills === 'string' && job.skills.startsWith('[') ? JSON.parse(job.skills) : job.skills.split(',');
      } catch (e) {
        skillsList = [job.skills];
      }
    }

    const skillsHtml = skillsList.slice(0, 5).map(s => `<span class="skill-tag">${escapeHtml(s.trim())}</span>`).join('');

    // Previous Application Warning Pill
    let repeatCompanyBanner = '';
    if (job.applied_earlier) {
      repeatCompanyBanner = `
        <div class="repeat-company-banner">
          <span>⚠️ <strong>Previous Application Detected:</strong> You previously applied to ${escapeHtml(job.company_name)} ${job.previous_applied_date ? 'on ' + escapeHtml(job.previous_applied_date) : ''} for '${escapeHtml(job.previous_job_title || 'Role')}'.</span>
        </div>
      `;
    }

    // Applied Red Badge
    let appliedBadge = '';
    if (isApplied) {
      const appliedDate = job.applied_at ? new Date(job.applied_at).toLocaleDateString() : 'Done';
      appliedBadge = `<span class="badge-applied-red">● ALREADY APPLIED (${appliedDate})</span>`;
    }

    // Status / Checked badges for the non-applied workflow states
    let statusBadge = '';
    if (status === 'IN_REVIEW') {
      statusBadge = `<span class="badge-status-in-review">🔎 IN REVIEW</span>`;
    } else if (status === 'SEE_LATER') {
      statusBadge = `<span class="badge-status-see-later">⏰ SEE LATER</span>`;
    } else if (isChecked) {
      const checkedDate = job.checked_at ? new Date(job.checked_at).toLocaleDateString() : '';
      statusBadge = `<span class="badge-checked" onclick="uncheckJob(${job.id})" title="Click to clear the grayed-out checked state">✓ CHECKED${checkedDate ? ' (' + checkedDate + ')' : ''}</span>`;
    }

    // Temporary "NEW" badge for jobs that arrived in the latest refresh
    const isNewArrival = newlyAddedIds.has(job.id);
    const newBadge = isNewArrival ? '<span class="badge-new-arrival">🆕 NEW</span>' : '';

    // Platform Badge
    const platformBadge = `<span class="badge-platform ${platform}">${platform}</span>`;

    // Direct Apply URL - use the REAL link extracted from the email body.
    // (Silent search-URL fallback only prevents dead buttons in edge cases;
    // jobs without real email links are never stored in the first place.)
    let effectiveApplyUrl = job.apply_url;
    if (!effectiveApplyUrl || !effectiveApplyUrl.startsWith('http')) {
      const query = encodeURIComponent(`${job.job_title} ${job.company_name}`);
      effectiveApplyUrl = `https://www.google.com/search?q=Apply+${query}`;
    }

    // Apply button: clicking it flags the job as Checked (grayed out) so the
    // user knows this posting was already looked at.
    const applyButton = `
      <a href="${escapeHtml(effectiveApplyUrl)}" target="_blank" rel="noopener noreferrer" class="btn btn-apply" onclick="markJobChecked(${job.id})" ${job.link_source === 'email' ? 'title="Open job link from email"' : ''}>
        <span>Apply Online ↗</span>
      </a>
    `;

    // Compact workflow-status dropdown: Open / In Review / See Later / Applied
    const statusDropdown = `
      <select class="status-dropdown" onchange="changeJobStatus(${job.id}, this.value)" title="Change job status" aria-label="Job status">
        <option value="NEW" ${status === 'NEW' ? 'selected' : ''}>🔵 Open Role</option>
        <option value="IN_REVIEW" ${status === 'IN_REVIEW' ? 'selected' : ''}>🔎 In Review</option>
        <option value="SEE_LATER" ${status === 'SEE_LATER' ? 'selected' : ''}>⏰ See Later</option>
        <option value="APPLIED" ${isApplied ? 'selected' : ''}>✅ Applied</option>
      </select>
    `;

    // Per-job delete (used after the job has been checked/reviewed)
    const deleteButton = `
      <button class="btn btn-delete-job" onclick="confirmDeleteJob(${job.id})" title="Delete this job" aria-label="Delete job">
        <span>🗑️</span>
      </button>
    `;

    // Applied Toggle Button
    const appliedToggleBtn = isApplied ? `
      <button class="btn btn-applied-toggle btn-applied-active" onclick="toggleAppliedStatus(${job.id}, 'APPLIED')" title="Click to unmark as applied">
        <span>✓ Already Applied (Done)</span>
      </button>
    ` : `
      <button class="btn btn-applied-toggle" onclick="toggleAppliedStatus(${job.id}, 'NEW')" title="Mark as applied (strike through & red marker)">
        <span>Mark as Applied</span>
      </button>
    `;

    html += `
      <div class="job-card ${appliedClass} ${checkedClass} ${statusClass} ${newCardClass}" id="job-card-${job.id}">
        <div class="job-card-header">
          <div class="job-title-group">
            <h2 class="job-title-text">${escapeHtml(job.job_title)}</h2>
            <div class="job-company-meta">
              ${newBadge}
              ${platformBadge}
              <span class="company-pill">${escapeHtml(job.company_name)}</span>
              <span>•</span>
              <span class="badge badge-remote">${escapeHtml(job.location || 'Remote')}</span>
              ${job.salary && job.salary !== 'Not specified' ? `<span class="badge badge-salary">${escapeHtml(job.salary)}</span>` : ''}
              ${job.match_score ? `<span class="badge badge-match">${job.match_score}% Match</span>` : ''}
              ${appliedBadge}
              ${statusBadge}
            </div>
          </div>
        </div>

        ${repeatCompanyBanner}

        <p class="job-summary-text" style="color: var(--text-muted); font-size: 0.92rem; line-height: 1.5;">
          ${escapeHtml(job.summary || 'Job opportunity extracted from email.')}
        </p>

        ${skillsHtml ? `<div class="skills-container">${skillsHtml}</div>` : ''}

        <div class="job-card-footer">
          <div class="date-received">
            <span>Received: ${escapeHtml(job.date_received || 'Recently')}</span>
          </div>
          <div class="action-buttons">
            ${statusDropdown}
            <button class="btn btn-secondary" style="font-size: 0.82rem; padding: 6px 12px;" onclick="viewJobDetails(${job.id})">
              <span>🔍 Details</span>
            </button>
            ${appliedToggleBtn}
            ${applyButton}
            ${deleteButton}
          </div>
        </div>
      </div>
    `;
  });

  container.innerHTML = html;
}

// --- Job Status / Checked / Delete Actions ---

// Workflow status change from the per-card dropdown.
// Selecting 'Open Role' also clears the grayed-out Checked flag (server-side).
async function changeJobStatus(jobId, newStatus) {
  try {
    const res = await fetch(`${API_BASE}/api/jobs/${jobId}/status`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: newStatus })
    });
    const data = await res.json();
    if (data.status === 'success') {
      const messages = {
        'APPLIED': '🎯 Marked as Applied! Struck through and highlighted in red.',
        'IN_REVIEW': '🔎 Marked as In Review. Find it under the In Review tab.',
        'SEE_LATER': '⏰ Saved as See Later. Find it under the See Later tab.',
        'NEW': '🔄 Status reset to Open Role (grayed-out cleared).'
      };
      showToast(messages[newStatus] || 'Status updated.', 'info');
      await loadStats();
      await loadJobs();
    } else {
      showToast(data.detail || 'Failed to update status', 'error');
      await loadJobs(); // re-render to restore the actual saved value
    }
  } catch (err) {
    console.error('Error updating job status:', err);
    showToast('Failed to update status', 'error');
  }
}

// Kept for the existing "Mark as Applied" toggle button.
async function toggleAppliedStatus(jobId, currentStatus) {
  const newStatus = currentStatus === 'APPLIED' ? 'NEW' : 'APPLIED';
  await changeJobStatus(jobId, newStatus);
}

// Flags the job as visually reviewed; the card grays out immediately.
// Called when the user clicks 'Apply Online' or opens 'Details'.
async function markJobChecked(jobId, silent = true) {
  try {
    const res = await fetch(`${API_BASE}/api/jobs/${jobId}/checked`, { method: 'POST' });
    const data = await res.json();
    if (data.status === 'success') {
      const job = allJobs.find(j => j.id === jobId);
      if (job) {
        job.checked_at = new Date().toISOString();
        applyFiltersAndRender(); // instant gray-out without a full refetch
      }
      if (!silent) showToast('✓ Marked as checked (grayed out).', 'info');
    }
  } catch (err) {
    console.error('Error marking job as checked:', err);
  }
}

// Per-job delete with confirmation (used after checking/reviewing a job).
function confirmDeleteJob(jobId) {
  const job = allJobs.find(j => j.id === jobId);
  const label = job ? `"${job.job_title}" at "${job.company_name}"` : `Job #${jobId}`;
  if (confirm(`🗑️ Delete ${label}?\n\nThis permanently removes the job from the dashboard. This cannot be undone.`)) {
    deleteSingleJob(jobId);
  }
}

async function deleteSingleJob(jobId) {
  const card = document.getElementById(`job-card-${jobId}`);
  if (card) card.style.opacity = '0.4';
  try {
    const res = await fetch(`${API_BASE}/api/jobs/${jobId}`, { method: 'DELETE' });
    const data = await res.json();
    if (data.status === 'success') {
      showToast(`🗑️ ${data.message}`, 'success');
      seenJobIds.delete(jobId);
      await loadStats();
      await loadJobs();
    } else {
      if (card) card.style.opacity = '1';
      showToast(data.detail || 'Failed to delete job', 'error');
    }
  } catch (err) {
    if (card) card.style.opacity = '1';
    console.error('Error deleting job:', err);
    showToast('Failed to delete job', 'error');
  }
}

// --- Trigger Manual Check & Simulation ---

async function triggerCheckNow() {
  const btn = document.getElementById('btn-check-now');
  const originalText = btn.innerHTML;
  btn.innerHTML = '<span>⏳ Checking Gmail...</span>';
  btn.disabled = true;

  try {
    const res = await fetch(`${API_BASE}/api/check-now`, { method: 'POST' });
    const data = await res.json();
    showToast(`✅ ${data.message || 'Email check cycle complete!'}`, 'success');
    await loadStats();
    await loadJobs();
  } catch (err) {
    console.error('Error checking emails:', err);
    showToast('Check failed. Verify network or credentials.', 'error');
  } finally {
    btn.innerHTML = originalText;
    btn.disabled = false;
  }
}

// --- Bulk Actions: Mark All Applied / Delete All ---

function confirmMarkAllApplied() {
  const newCount = allJobs.filter(j => (j.status || 'NEW').toUpperCase() !== 'APPLIED').length;
  const label = newCount > 0 ? `${newCount} job(s)` : 'all jobs';
  if (confirm(`✅ Mark ${label} as APPLIED?\n\nEvery job will be crossed out and flagged as applied.\nTip: use the per-card toggle if you only applied to some of them.`)) {
    markAllApplied();
  }
}

async function markAllApplied() {
  const btn = document.getElementById('btn-mark-all-applied');
  const originalText = btn.innerHTML;
  btn.innerHTML = '<span>⏳ Marking...</span>';
  btn.disabled = true;
  try {
    const res = await fetch(`${API_BASE}/api/jobs/mark-all-applied`, { method: 'POST' });
    const data = await res.json();
    if (data.status === 'success') {
      showToast(`✅ ${data.message}`, 'success');
      await loadStats();
      await loadJobs();
    } else {
      showToast('Failed to mark all as applied', 'error');
    }
  } catch (err) {
    console.error('Error marking all applied:', err);
    showToast('Failed to mark all as applied', 'error');
  } finally {
    btn.innerHTML = originalText;
    btn.disabled = false;
  }
}

function confirmDeleteAll() {
  const total = allJobs.length;
  const label = total > 0 ? `${total} job(s)` : 'all jobs';
  if (confirm(`⚠️ Delete ALL ${label} from the dashboard?\n\nThis permanently removes every job record. This cannot be undone.`)) {
    deleteAllJobs();
  }
}

async function deleteAllJobs() {
  const btn = document.getElementById('btn-delete-all');
  const originalText = btn.innerHTML;
  btn.innerHTML = '<span>⏳ Deleting...</span>';
  btn.disabled = true;
  try {
    const res = await fetch(`${API_BASE}/api/jobs/all`, { method: 'DELETE' });
    const data = await res.json();
    if (data.status === 'success') {
      showToast(`🗑️ ${data.message}`, 'success');
      await loadStats();
      await loadJobs();
    } else {
      showToast('Failed to delete jobs', 'error');
    }
  } catch (err) {
    console.error('Error deleting all jobs:', err);
    showToast('Failed to delete jobs', 'error');
  } finally {
    btn.innerHTML = originalText;
    btn.disabled = false;
  }
}

function openSimulateMenu() {
  document.getElementById('sim-modal').classList.add('active');
}

function closeSimModal() {
  document.getElementById('sim-modal').classList.remove('active');
}

async function simulateSpecificJob(source) {
  closeSimModal();
  showToast(`🧪 Injecting simulated ${source} multi-job digest...`, 'info');

  try {
    const res = await fetch(`${API_BASE}/api/simulate-job?source=${encodeURIComponent(source)}`, { method: 'POST' });
    const data = await res.json();
    if (data.status === 'success') {
      showToast(`✨ ${data.message}`, 'success');
      await loadStats();
      await loadJobs();
    }
  } catch (err) {
    console.error('Error simulating job:', err);
    showToast('Simulation error', 'error');
  }
}

// --- Filters and Search ---

function setFilter(status, el) {
  currentFilter = status;
  document.querySelectorAll('.filter-tab').forEach(t => t.classList.remove('active', 'active-applied', 'active-in-review', 'active-see-later'));
  if (status === 'APPLIED') {
    el.classList.add('active', 'active-applied');
  } else if (status === 'IN_REVIEW') {
    el.classList.add('active', 'active-in-review');
  } else if (status === 'SEE_LATER') {
    el.classList.add('active', 'active-see-later');
  } else {
    el.classList.add('active');
  }
  applyFiltersAndRender();
}

// Manually clear the grayed-out Checked flag from a card.
async function uncheckJob(jobId) {
  try {
    const res = await fetch(`${API_BASE}/api/jobs/${jobId}/checked`, { method: 'DELETE' });
    const data = await res.json();
    if (data.status === 'success') {
      const job = allJobs.find(j => j.id === jobId);
      if (job) {
        delete job.checked_at;
        applyFiltersAndRender();
      }
      showToast('🔄 Checked flag cleared.', 'info');
    }
  } catch (err) {
    console.error('Error clearing checked flag:', err);
    showToast('Failed to clear checked flag', 'error');
  }
}

function setPlatformFilter(platform, el) {
  currentPlatform = platform;
  document.querySelectorAll('.platform-tab').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  applyFiltersAndRender();
}

let searchDebounce = null;
function handleSearch(val) {
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(() => {
    searchQuery = val;
    applyFiltersAndRender();
  }, 200);
}

// --- Modals ---

async function viewJobDetails(jobId) {
  try {
    const res = await fetch(`${API_BASE}/api/jobs/${jobId}`);
    const data = await res.json();
    if (data.status === 'success') {
      const job = data.job;
      const history = data.company_history || [];

      document.getElementById('modal-job-title').innerText = job.job_title;

      // Opening the details counts as having reviewed the job: gray it out.
      await markJobChecked(jobId);

      let historyHtml = '';
      if (history.length > 0) {
        historyHtml = `
          <div style="background: rgba(245, 158, 11, 0.1); border: 1px solid rgba(245, 158, 11, 0.3); padding: 14px; border-radius: 8px; margin-top: 14px;">
            <strong style="color: #fde68a;">🏢 Past History with ${escapeHtml(job.company_name)}:</strong>
            <ul style="margin-top: 8px; padding-left: 20px; font-size: 0.85rem; color: var(--text-muted);">
              ${history.map(h => `<li>Applied on ${h.applied_at || h.created_at} for '${escapeHtml(h.job_title)}' (${h.status})</li>`).join('')}
            </ul>
          </div>
        `;
      }

      let effectiveApplyUrl = job.apply_url;
      if (!effectiveApplyUrl || !effectiveApplyUrl.startsWith('http')) {
        effectiveApplyUrl = `https://www.google.com/search?q=Apply+${encodeURIComponent(job.job_title + ' ' + job.company_name)}`;
      }

      document.getElementById('modal-job-body').innerHTML = `
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <h3 style="color: #c7d2fe;">${escapeHtml(job.company_name)}</h3>
          <span class="badge badge-remote">${escapeHtml(job.location || 'Remote')}</span>
        </div>
        <div style="font-size: 0.85rem; color: var(--text-dim); margin-bottom: 12px;">
          Source: <strong style="color:#a5b4fc;">${escapeHtml(job.source_platform || 'Direct')}</strong> | 
          Sender: <code>${escapeHtml(job.email_sender || 'N/A')}</code><br>
          Subject: <em>${escapeHtml(job.email_subject || 'N/A')}</em>
          ${job.link_source === 'email' ? ' | <span style="color:#34d399;">🔗 Direct link from email</span>' : ''}
        </div>

        <div class="form-group">
          <label>AI Analysis & Summary</label>
          <p style="background: rgba(0,0,0,0.3); padding: 12px; border-radius: 8px; font-size: 0.9rem; color: #e0e7ff; line-height: 1.5;">
            ${escapeHtml(job.summary || 'No summary available.')}
          </p>
        </div>

        <div class="form-group">
          <label>Email Snippet</label>
          <pre style="background: rgba(0,0,0,0.4); padding: 12px; border-radius: 8px; font-size: 0.82rem; font-family: var(--font-mono); white-space: pre-wrap; color: var(--text-muted); max-height: 200px; overflow-y: auto;">
${escapeHtml(job.raw_email_snippet || 'No email snippet available.')}
          </pre>
        </div>

        ${historyHtml}

        <div class="form-group">
          <label>Job Status</label>
          <select class="status-dropdown" id="modal-status-select" onchange="changeJobStatus(${job.id}, this.value)">
            <option value="NEW" ${((job.status || 'NEW').toUpperCase() === 'NEW') ? 'selected' : ''}>🔵 Open Role</option>
            <option value="IN_REVIEW" ${((job.status || '').toUpperCase() === 'IN_REVIEW') ? 'selected' : ''}>🔎 In Review</option>
            <option value="SEE_LATER" ${((job.status || '').toUpperCase() === 'SEE_LATER') ? 'selected' : ''}>⏰ See Later</option>
            <option value="APPLIED" ${((job.status || '').toUpperCase() === 'APPLIED') ? 'selected' : ''}>✅ Applied</option>
          </select>
        </div>

        <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 14px;">
          <a href="${escapeHtml(effectiveApplyUrl)}" target="_blank" class="btn btn-apply">Apply Online ↗</a>
          <button class="btn btn-secondary" onclick="closeJobModal()">Close</button>
        </div>
      `;

      document.getElementById('job-modal').classList.add('active');
    }
  } catch (err) {
    console.error('Error opening job details:', err);
  }
}

function closeJobModal() {
  document.getElementById('job-modal').classList.remove('active');
}

// --- IST time formatting (Asia/Kolkata) ---

function formatIST(isoString) {
  try {
    const d = new Date(isoString);
    if (isNaN(d.getTime())) return isoString;
    return d.toLocaleTimeString('en-IN', {
      timeZone: 'Asia/Kolkata',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: true
    }) + ' IST';
  } catch (e) {
    return isoString;
  }
}

// --- App Password help modal ---

function openAppPasswordHelp() {
  document.getElementById('apppw-help-modal').classList.add('active');
}

function closeAppPasswordHelp() {
  document.getElementById('apppw-help-modal').classList.remove('active');
}

function focusAppPasswordField() {
  const field = document.getElementById('set-imap-password');
  if (field) field.focus();
}

function openSettingsModal() {
  document.getElementById('settings-modal').classList.add('active');
}

function closeSettingsModal() {
  document.getElementById('settings-modal').classList.remove('active');
}

// If Gmail IMAP mode is active but no App Password is stored, open Settings
// automatically so the user can enter one.
function checkAndPromptForAppPassword() {
  fetch(`${API_BASE}/api/settings`)
    .then(r => r.json())
    .then(data => {
      if (data.status !== 'success') return;
      const s = data.settings;
      const mode = s.auth_mode || 'simulator';
      const hasPw = Boolean(s.imap_password_set);
      if (mode === 'imap' && !hasPw) {
        openSettingsModal();
        toggleAuthFields('imap');
        showToast('🔑 Enter your 16-character Gmail App Password to start scanning your inbox.', 'info');
      }
    })
    .catch(() => { /* non-fatal */ });
}

function toggleAuthFields(mode) {
  const imapBox = document.getElementById('imap-credentials-box');
  if (mode === 'imap') {
    imapBox.style.display = 'block';
  } else {
    imapBox.style.display = 'none';
  }
}

async function saveSettings() {
  const updates = {
    target_email: document.getElementById('set-target-email').value.trim(),
    check_interval_mins: document.getElementById('set-check-interval').value.trim(),
    auth_mode: document.getElementById('set-auth-mode').value,
    gemini_api_key: document.getElementById('set-gemini-key').value.trim(),
    ntfy_topic: document.getElementById('set-ntfy-topic').value.trim(),
    desktop_notify: document.getElementById('set-desktop-notify').value,
    mobile_notify: document.getElementById('set-mobile-notify').value,
  };

  const imapPw = document.getElementById('set-imap-password').value.trim();
  if (imapPw) {
    updates.imap_password = imapPw;
  }

  try {
    const res = await fetch(`${API_BASE}/api/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updates)
    });
    const data = await res.json();
    if (data.status === 'success') {
      showToast('⚙️ Settings saved successfully!', 'success');
      closeSettingsModal();
      await loadStats();
    }
  } catch (err) {
    showToast('Failed to save settings', 'error');
  }
}

function openMobileModal() {
  const topic = (currentSettings.ntfy_topic || 'deepak-job-hunter-alerts').trim();
  const effectiveUrl = currentPublicUrl || window.location.origin;
  
  const qrUrl = `https://quickchart.io/qr?text=${encodeURIComponent(effectiveUrl)}&size=200&dark=000000`;
  document.getElementById('qr-code-img').src = qrUrl;
  
  const linkEl = document.getElementById('public-dashboard-direct-link');
  linkEl.innerText = effectiveUrl;
  linkEl.href = effectiveUrl;

  document.getElementById('display-ntfy-topic').innerText = topic;
  document.getElementById('mobile-modal').classList.add('active');
}

function closeMobileModal() {
  document.getElementById('mobile-modal').classList.remove('active');
}

function copyPublicUrl() {
  const url = currentPublicUrl || window.location.origin;
  navigator.clipboard.writeText(url).then(() => {
    showToast('📋 Public URL copied to clipboard!', 'success');
  }).catch(() => {
    prompt('Copy public URL:', url);
  });
}

async function testNotifications() {
  try {
    showToast('🚀 Triggering test desktop and mobile push...', 'info');
    const res = await fetch(`${API_BASE}/api/test-notification`, { method: 'POST' });
    const data = await res.json();
    showToast(`🔔 Test fired! Public URL: ${data.public_url}`, 'success');
  } catch (err) {
    showToast('Notification test failed', 'error');
  }
}

async function openLogsModal() {
  document.getElementById('logs-modal').classList.add('active');
  const body = document.getElementById('logs-body');
  body.innerHTML = '<p>Loading logs...</p>';

  try {
    const [historyRes, logsRes] = await Promise.all([
      fetch(`${API_BASE}/api/history`),
      fetch(`${API_BASE}/api/logs`)
    ]);
    const histData = await historyRes.json();
    const logData = await logsRes.json();

    let html = `
      <h3 style="font-size: 1rem; margin-bottom: 8px; color: #a5b4fc;">Recent 15-Minute Check Cycles</h3>
      <div style="max-height: 200px; overflow-y: auto; background: rgba(0,0,0,0.3); border-radius: 8px; padding: 10px; margin-bottom: 20px;">
        <table style="width: 100%; font-size: 0.82rem; text-align: left; border-collapse: collapse;">
          <thead>
            <tr style="border-bottom: 1px solid var(--border-color); color: var(--text-muted);">
              <th style="padding: 6px;">Time</th>
              <th style="padding: 6px;">Scanned</th>
              <th style="padding: 6px;">Jobs Found</th>
              <th style="padding: 6px;">Trigger</th>
              <th style="padding: 6px;">Message</th>
            </tr>
          </thead>
          <tbody>
            ${histData.history.map(h => `
              <tr style="border-bottom: 1px solid rgba(255,255,255,0.05);">
                <td style="padding: 6px;">${new Date(h.checked_at).toLocaleTimeString()}</td>
                <td style="padding: 6px;">${h.emails_scanned}</td>
                <td style="padding: 6px;">${h.new_jobs_found}</td>
                <td style="padding: 6px;"><code>${h.triggered_by}</code></td>
                <td style="padding: 6px;">${escapeHtml(h.status_message)}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>

      <h3 style="font-size: 1rem; margin-bottom: 8px; color: #a5b4fc;">Scanned Email Audit Trail</h3>
      <div style="max-height: 220px; overflow-y: auto; background: rgba(0,0,0,0.3); border-radius: 8px; padding: 10px;">
        <table style="width: 100%; font-size: 0.82rem; text-align: left; border-collapse: collapse;">
          <thead>
            <tr style="border-bottom: 1px solid var(--border-color); color: var(--text-muted);">
              <th style="padding: 6px;">Subject</th>
              <th style="padding: 6px;">Sender</th>
              <th style="padding: 6px;">Jobs Extracted</th>
              <th style="padding: 6px;">Classification</th>
            </tr>
          </thead>
          <tbody>
            ${logData.logs.map(l => `
              <tr style="border-bottom: 1px solid rgba(255,255,255,0.05);">
                <td style="padding: 6px; font-weight: 500;">${escapeHtml(l.subject || 'No Subject')}</td>
                <td style="padding: 6px; color: var(--text-dim);">${escapeHtml(l.sender || '')}</td>
                <td style="padding: 6px;">${l.jobs_extracted_count || (l.is_job ? 1 : 0)}</td>
                <td style="padding: 6px; color: var(--text-muted);">${escapeHtml(l.ai_classification_summary || '')}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    `;

    body.innerHTML = html;
  } catch (err) {
    body.innerHTML = '<p style="color:red;">Failed to load logs.</p>';
  }
}

function closeLogsModal() {
  document.getElementById('logs-modal').classList.remove('active');
}

function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = 'app-toast';
  
  let icon = 'ℹ️';
  if (type === 'success') icon = '✅';
  if (type === 'error') icon = '❌';

  toast.innerHTML = `<span>${icon}</span><span>${escapeHtml(message)}</span>`;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transition = 'opacity 0.3s ease';
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}
