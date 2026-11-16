'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const state = { data: null, view: 'opportunities', status: 'all', search: '', mode: 'all', tier: 'all', freshness: 'fresh', selectedKey: null, csrf: null, loading: false, scanPending: false, previousFocus: null };
  const stages = { new: 'New', shortlisted: 'Shortlisted', prepared: 'Prepared', applied: 'Submitted', contacted: 'Contacted', replied: 'Replied', interviewing: 'Interviewing', rejected: 'Rejected', offer: 'Offer', withdrawn: 'Withdrawn' };
  const views = {
    opportunities: ['Opportunities', 'THE NEXT STEP STARTS HERE', 'Your opportunity workspace', 'India · SRE & DevOps · Built around your experience'],
    applications: ['Applications', 'EVERY STEP, IN ONE PLACE', 'Make progress, not guesses', 'Your shortlist, prepared material and confirmed applications'],
    automation: ['Automation', 'A STEADIER WAY TO SEARCH', 'Let the search come to you', 'Scheduled discovery, transparent results and a history you can trust'],
    profile: ['My profile', 'THE EXPERIENCE BEHIND THE MATCH', 'A profile built on proof', 'Your skills and resume facts shape every opportunity']
  };

  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined && content !== null) node.textContent = String(content);
    return node;
  }

  function safeUrl(value) {
    try { const url = new URL(value); return url.protocol === 'https:' ? url.href : null; } catch { return null; }
  }

  function dateLabel(value, compact = false) {
    if (!value) return 'Not scheduled';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return 'Unavailable';
    return new Intl.DateTimeFormat('en-IN', { timeZone: 'Asia/Kolkata', month: 'short', day: 'numeric', ...(compact ? {} : { hour: 'numeric', minute: '2-digit' }) }).format(date);
  }

  function shortMode(value) { return { remote: 'Remote', hybrid: 'Hybrid', onsite: 'On-site', unknown: 'Not specified' }[value] || 'Not specified'; }
  function review(job) { return job.review || {}; }
  function jobStatus(job) { return review(job).status || 'new'; }
  function isPrepared(job) { return jobStatus(job) === 'prepared' || Boolean(job.packet && job.packet.ready); }
  function isApplied(job) { return Boolean(review(job).applied_at); }
  function isPending(job) { return !isApplied(job) && ['new', 'shortlisted', 'prepared'].includes(jobStatus(job)) && (jobStatus(job) !== 'new' || job.discovery_ready || (isRecentAlert(job) && fitKind(job) !== 'stretch')); }
  function isRecentAlert(job) { const age = Date.now() - Date.parse(job.alert_received_at || ''); return age >= 0 && age <= 72 * 3600000; }
  function fitKind(job) { const tier = String((job.assessment || {}).tier || '').toLowerCase(); return tier.includes('stretch') ? 'stretch' : tier.includes('priority') ? 'priority' : 'review'; }
  function runInProgress(run) { return ['running', 'queued'].includes(String(run.status).toLowerCase()); }

  function toast(message, isError = false) {
    const notice = element('div', `toast${isError ? ' error' : ''}`, message);
    $('toast-region').append(notice);
    setTimeout(() => notice.remove(), 6500);
  }

  async function ensureCsrf() {
    if (state.csrf) return;
    const response = await fetch('/api/csrf', { credentials: 'same-origin', headers: { Accept: 'application/json' } });
    if (response.status === 401) { window.location.assign('/login'); throw new Error('Please sign in again.'); }
    if (!response.ok) throw new Error('Could not establish a secure session. Refresh the page.');
    const data = await response.json();
    state.csrf = data.csrf_token || data.token;
    if (!state.csrf) throw new Error('Your session token is unavailable. Refresh the page.');
  }

  async function api(path, options = {}) {
    const request = { credentials: 'same-origin', ...options, headers: { Accept: 'application/json', ...(options.headers || {}) } };
    if (request.method && request.method !== 'GET') {
      await ensureCsrf();
      request.headers['X-CSRF-Token'] = state.csrf;
      if (!(options.body instanceof FormData)) {
        request.headers['Content-Type'] = 'application/json';
        request.body = JSON.stringify(options.body || {});
      }
    }
    const response = await fetch(path, request);
    if (response.status === 401) { window.location.assign('/login'); throw new Error('Please sign in again.'); }
    let data;
    try { data = await response.json(); } catch { throw new Error('The server returned an unexpected response. Please try again.'); }
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
    return data;
  }

  async function loadWorkspace({ quiet = false } = {}) {
    if (state.loading) return;
    state.loading = true;
    try {
      const data = await api('/api/bootstrap');
      if (!data || !Array.isArray(data.jobs)) throw new Error('The workspace response is incomplete.');
      state.data = data; state.csrf = data.csrf_token || state.csrf;
      $('connection-error').hidden = true; $('jobs-loading').hidden = true;
      $('last-updated').textContent = `Updated ${new Intl.DateTimeFormat('en-IN', { timeZone: 'Asia/Kolkata', hour: 'numeric', minute: '2-digit' }).format(new Date())} IST`;
      render();
    } catch (error) {
      if (!quiet || !state.data) { $('connection-error').hidden = false; $('connection-error-text').textContent = error.message; }
      if (!state.data) { $('jobs-loading').hidden = true; $('jobs-empty').hidden = false; $('empty-title').textContent = 'Your workspace is unavailable.'; $('empty-text').textContent = 'Reconnect to the server to see your saved opportunities.'; }
    } finally { state.loading = false; }
  }

  function render() { if (state.data) { renderMetrics(); renderProfile(); renderAutomation(); renderJobs(); } }

  function renderMetrics() {
    const jobs = state.data.jobs;
    const active = jobs.filter((job) => job.discovery_ready);
    const applied = jobs.filter(isApplied).length;
    $('stat-opportunities').textContent = active.length;
    $('stat-prepared').textContent = jobs.filter(isPending).length;
    $('pending-tab-count').textContent = jobs.filter(isPending).length;
    $('submitted-tab-count').textContent = applied;
    $('stat-applied').textContent = applied;
    $('stat-interviewing').textContent = jobs.filter((job) => ['replied', 'interviewing'].includes(jobStatus(job))).length;
    $('nav-job-count').textContent = active.length; $('nav-applied-count').textContent = applied;
    $('all-tab-count').textContent = active.length;
  }

  function renderProfile() {
    const profile = state.data.profile || {};
    const name = profile.display_name || 'Your workspace';
    const initials = name.split(/\s+/).slice(0, 2).map((part) => part[0] || '').join('').toUpperCase();
    $('sidebar-name').textContent = name; $('avatar').textContent = initials; $('large-avatar').textContent = initials;
    $('profile-full-name').textContent = name;
    $('profile-location').textContent = [profile.city || profile.location, profile.country].filter(Boolean).join(' · ') || 'Location not configured';
    $('profile-evidence').textContent = profile.evidence_for_outreach || 'Resume evidence hasn’t been configured yet. Add verified achievements before using application drafts.';
    const years = Number(profile.experience_years);
    const experience = Number.isFinite(years) ? `${Math.floor(years)}+ years` : 'Your experience';
    $('profile-card-description').textContent = `${experience} in DevOps & SRE. Matching considers your cloud, infrastructure and operations skills.`;
    const skills = Array.isArray(profile.skills) ? profile.skills : Object.keys(profile.skills || {});
    for (const [id, limit] of [['profile-skill-chips', 8], ['all-profile-skills', 100]]) {
      $(id).replaceChildren(...skills.slice(0, limit).map((skill) => element('span', 'skill-chip', skill)));
      if (!skills.length) $(id).append(element('span', 'helper-text', 'No skills configured yet.'));
    }
    $('resume-link').hidden = !profile.resume_available;
    const strengths = $('profile-strengths');
    strengths.replaceChildren();
    const facts = Array.isArray(profile.resume_facts) ? profile.resume_facts : [];
    for (const [index, fact] of facts.entries()) {
      if (!fact || !['string', 'object'].includes(typeof fact)) continue;
      const text = typeof fact === 'string' ? fact : fact.text || fact.fact || fact.achievement || fact.description || '';
      if (!text) continue;
      const row = element('div', 'strength');
      const content = element('div');
      content.append(element('h4', '', typeof fact === 'object' && fact.title ? fact.title : `Resume evidence ${String(index + 1).padStart(2, '0')}`), element('p', '', text));
      row.append(element('strong', '', '↗'), content);
      strengths.append(row);
    }
    if (profile.current_employer || profile.current_title) {
      const experience = element('div', 'experience-block');
      experience.append(element('span', 'small-label', 'CURRENT EXPERIENCE'));
      if (profile.current_employer) experience.append(element('h4', '', profile.current_employer));
      if (profile.current_title) experience.append(element('p', '', profile.current_title));
      if (profile.city) experience.append(element('span', '', profile.city));
      strengths.append(experience);
    }
    if (!strengths.children.length) strengths.append(element('div', 'compact-empty', 'Add verified resume facts to build your application evidence.'));
    if (!$('profile-form').contains(document.activeElement)) {
      $('profile-display-name').value = profile.display_name || '';
      $('profile-years').value = Number.isFinite(years) ? years : 2;
      $('profile-proof').value = profile.evidence_for_outreach || '';
    }
  }

  function dayKey(value) {
    const date = new Date(value);
    if (!value || Number.isNaN(date.getTime())) return '';
    return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Kolkata', year: 'numeric', month: '2-digit', day: '2-digit' }).format(date);
  }

  function dayGroup(job) {
    const submitted = state.view === 'applications' && isApplied(job);
    const value = submitted ? review(job).applied_at : job.freshness?.day ? job.published_at : job.alert_received_at;
    const day = dayKey(value);
    if (!day) return { key: '0000', label: 'Posting date unverified' };
    const today = dayKey(new Date().toISOString());
    const yesterday = dayKey(new Date(Date.now() - 86400000).toISOString());
    const label = day === today ? 'Today' : day === yesterday ? 'Yesterday' : dateLabel(value, true);
    return { key: day, label: `${submitted ? 'Submitted' : job.freshness?.day ? 'Posted' : 'Alert received'} · ${label}${!submitted && !job.freshness?.day ? ' · posting date unverified' : ''}` };
  }

  function filteredJobs() {
    let jobs = state.data.jobs.slice();
    if (state.view === 'applications') {
      jobs = jobs.filter((job) => state.status === 'pending' ? isPending(job) : jobStatus(job) !== 'new' || isApplied(job) || isPrepared(job));
    } else if (state.freshness === 'fresh') jobs = jobs.filter((job) => job.discovery_ready);
    else if (state.freshness === 'review') jobs = jobs.filter((job) => job.availability === 'active' && fitKind(job) !== 'stretch' && (job.freshness?.state === 'fresh' || isRecentAlert(job) || (job.freshness?.state === 'undated' && !job.alert_received_at)));
    if (state.status !== 'all') jobs = jobs.filter((job) => state.status === 'pending' ? isPending(job) : state.status === 'applied' ? isApplied(job) : state.status === 'prepared' ? isPrepared(job) : jobStatus(job) === state.status);
    if (state.mode !== 'all') jobs = jobs.filter((job) => job.workplace === state.mode);
    if (state.tier !== 'all') jobs = jobs.filter((job) => fitKind(job) === state.tier);
    if (state.search) {
      const query = state.search.toLowerCase();
      jobs = jobs.filter((job) => [job.title, job.company, job.location, job.description, ...((job.assessment || {}).skills || [])].join(' ').toLowerCase().includes(query));
    }
    return jobs.sort((a, b) => dayGroup(b).key.localeCompare(dayGroup(a).key) || dayGroup(a).label.localeCompare(dayGroup(b).label) || Number((b.assessment || {}).score || 0) - Number((a.assessment || {}).score || 0));
  }

  function renderJobs() {
    const jobs = filteredJobs(); const body = $('jobs-body'); body.replaceChildren();
    $('jobs-count-label').textContent = state.view === 'applications' ? `${jobs.length} tracked ${jobs.length === 1 ? 'opportunity' : 'opportunities'} in this view` : `${jobs.length} ${jobs.length === 1 ? 'opportunity' : 'opportunities'} for your next chapter`;
    $('all-tab-count').textContent = state.view === 'applications' ? state.data.jobs.filter((job) => jobStatus(job) !== 'new' || isApplied(job) || isPrepared(job)).length : state.data.jobs.filter((job) => job.discovery_ready).length;
    $('freshness-filter').hidden = state.view === 'applications';
    $('freshness-note').textContent = state.view === 'applications' ? 'Saved pipeline retains older applications · Submitted requires your confirmation.' : '72-hour rolling window · IST day groups · unknown posting dates stay in review.';
    $('jobs-heading').textContent = state.view === 'applications' ? 'Your application pipeline' : 'Discover your next role';
    $('table-footer-label').textContent = `Showing ${jobs.length} of ${state.data.jobs.length} saved leads`;
    $('job-table-wrap').hidden = !jobs.length; $('jobs-empty').hidden = Boolean(jobs.length);
    $('reset-filters-button').hidden = !state.search && state.mode === 'all' && state.tier === 'all' && state.status === 'all' && state.freshness === 'fresh';
    $('empty-title').textContent = state.data.jobs.length ? 'Nothing in this view yet.' : 'Your next chapter starts here.';
    $('empty-text').textContent = state.data.jobs.length ? 'Try another stage or clear your filters to explore more opportunities.' : 'Run a scan to collect employer openings and start building your shortlist.';
    let previousDay = null;
    if (!jobs.length && state.view === 'opportunities' && state.freshness === 'fresh') { $('empty-title').textContent = 'No verified fresh entry / 2-year roles yet.'; $('empty-text').textContent = 'The hourly scan checks the last 72 hours. Choose Recent / date needs review for email alerts or openings with unclear experience. Older postings are not relabelled as fresh.'; }
    for (const job of jobs) {
      const group = dayGroup(job);
      if (group.label !== previousDay) {
        previousDay = group.label; const heading = element('tr', 'day-group'); const cell = element('th', '', `${group.label} · ${jobs.filter((j) => dayGroup(j).label === group.label).length}`); cell.colSpan = 5; cell.scope = 'rowgroup'; heading.append(cell); body.append(heading);
      }
      const tr = element('tr', 'job-row'); const roleTd = element('td'); const role = element('div', 'role-cell');
      const monogram = element('div', 'company-monogram', String(job.company || '?').split(/\s+/).slice(0, 2).map((word) => word[0]).join('').toUpperCase());
      monogram.setAttribute('aria-hidden', 'true');
      const info = element('div', 'role-information'); const title = element('button', 'role-title', job.title);
      title.addEventListener('click', () => openJob(job.key));
      info.append(title, element('div', 'role-company', `${job.company} · ${job.location || 'Location not specified'}`));
      info.append(element('div', 'role-date', job.availability === 'not_in_latest_feed' ? 'Absent from latest feed · recheck opening' : `${job.freshness?.day ? 'Posted ' + dateLabel(job.published_at) : job.alert_received_at ? 'Alert ' + dateLabel(job.alert_received_at) + ' · posting date unknown' : 'Posting date unknown'} · ${job.source}`));
      role.append(monogram, info); roleTd.append(role);
      const fitTd = element('td'); const fit = element('div', `fit-cell ${fitKind(job)}`);
      fit.append(element('span', 'fit-score', `${Number((job.assessment || {}).score || 0)}`), element('span', 'fit-name', { priority: 'Priority review', review: 'Needs review', stretch: 'Stretch role' }[fitKind(job)])); fitTd.append(fit);
      const modeTd = element('td'); modeTd.append(element('span', 'mode-label', shortMode(job.workplace)));
      const statusTd = element('td'); statusTd.append(element('span', `status-badge ${isApplied(job) ? 'submitted' : isPending(job) ? 'pending' : jobStatus(job)}`, isApplied(job) ? 'Submitted' : isPending(job) ? 'Pending' : stages[jobStatus(job)] || 'New'));
      if (!['new', 'applied'].includes(jobStatus(job))) statusTd.append(element('small', 'stage-detail', stages[jobStatus(job)]));
      const actionTd = element('td'); const action = element('button', 'row-arrow', '↗'); action.setAttribute('aria-label', `View ${job.title} at ${job.company}`); action.addEventListener('click', () => openJob(job.key)); actionTd.append(action);
      tr.append(roleTd, fitTd, modeTd, statusTd, actionTd);
      tr.addEventListener('click', (event) => { if (!event.target.closest('button,a')) openJob(job.key); });
      body.append(tr);
    }
  }

  function runSummary(run) {
    if (run.error) return String(run.error);
    if (!run.summary) return 'No summary recorded';
    if (typeof run.summary !== 'object') return String(run.summary);
    const summary = run.summary;
    const parts = [];
    if (summary.openings_collected !== undefined) parts.push(`${summary.openings_collected} openings`);
    if (summary.fresh_entry_leads !== undefined) parts.push(`${summary.fresh_entry_leads} fresh entry / 2-year leads`);
    if (summary.relevant_leads !== undefined) parts.push(`${summary.relevant_leads} leads`);
    if (summary.packets_prepared !== undefined) parts.push(`${summary.packets_prepared} packs prepared`);
    if (summary.sources_failed) parts.push(`${summary.sources_failed} sources failed`);
    return parts.length ? parts.join(' · ') : JSON.stringify(summary);
  }

  function renderAutomation() {
    const automation = state.data.automation || {}; const runs = state.data.runs || [];
    const running = runs.some(runInProgress);
    $('scan-button').classList.toggle('is-running', running || state.scanPending);
    $('scan-button-text').textContent = running || state.scanPending ? 'Scanning…' : 'Scan for jobs';
    $('scan-button').disabled = running || state.scanPending;
    const cloudReady = Boolean(automation.cloud_ready); const enabled = automation.enabled === true;
    $('automation-summary').textContent = running ? 'Your scan is in progress.' : enabled && cloudReady ? Number(automation.interval_hours) === 1 ? 'A fresh look every hour.' : `A fresh look every ${automation.interval_hours || 1} hours.` : enabled ? 'Schedule saved. Cloud setup pending.' : 'Ready when you are.';
    $('automation-explanation').textContent = enabled && cloudReady ? 'Scans run while the service is active. Free hosting can sleep; check the run history for gaps.' : cloudReady ? 'Your workspace is connected. Enable scheduled scans or start one when you’re ready.' : 'Unattended cloud execution is not confirmed. Check deployment and storage in Automation.';
    $('last-scan-label').textContent = automation.last_run ? dateLabel(automation.last_run) : runs.length ? dateLabel(runs[0].started_at) : 'No scan recorded';
    $('next-scan-label').textContent = enabled && automation.next_run ? dateLabel(automation.next_run) : 'Not scheduled';
    $('automation-status-badge').textContent = running ? 'Scan running' : cloudReady && enabled ? 'Runtime scheduled' : cloudReady ? 'Manual' : 'Deployment pending';
    if (!$('automation-form').contains(document.activeElement)) { $('schedule-enabled').checked = enabled; $('scan-interval').value = String(automation.interval_hours || 1); }
    $('hosting-notice').textContent = cloudReady ? `Storage: ${automation.storage || 'configured'}. Scheduler: ${String(automation.mode || 'manual').replaceAll('_', ' ')}. A free Render instance can pause while idle; an uptime monitor is not an availability guarantee. Times are IST.` : `Cloud scheduling is not yet verified. Storage: ${automation.storage || 'unconfirmed'}. Use durable Postgres before relying on hosted application history; a free Render service loses local files on restart.`;
    $('health-url').value = safeUrl(automation.health_url) || '';
    $('copy-health-button').disabled = !$('health-url').value;
    const bridge = state.data.coverage?.alert_bridge;
    const connected = bridge?.bridge_connected_at && Date.now() - Date.parse(bridge.bridge_connected_at) < 2 * 3600000;
    $('alert-bridge-status').textContent = connected ? 'Sync connected' : 'Setup needed';
    $('alert-bridge-detail').textContent = connected ? `Last automatic sync ${dateLabel(bridge.bridge_connected_at)}. Email delivery frequency depends on each portal.` : `Employer feeds scan automatically. ${bridge?.last_sync ? 'A one-time alert import was completed. ' : ''}Install the private Google script below for hourly portal alert imports.`;
    $('portal-coverage').replaceChildren(...[...new Set(Object.values(state.data.coverage?.portals || {}))].map((name) => element('span', 'skill-chip', name)));
    const sourceList = $('source-list'); sourceList.replaceChildren();
    for (const source of state.data.sources || []) {
      const row = element('div', 'source-row'); const text = element('div');
      text.append(element('div', 'source-name', source.source || 'Employer feed'), element('div', 'source-meta', source.success ? `${source.job_count || 0} openings · ${dateLabel(source.at)}` : `${source.error || 'Feed request failed'} · ${dateLabel(source.at)}`));
      row.append(text, element('span', `source-status${source.success ? '' : ' failed'}`, source.success ? 'Healthy' : 'Needs attention')); sourceList.append(row);
    }
    if (!sourceList.children.length) sourceList.append(element('div', 'compact-empty', 'Source health appears after the first scan.'));
    $('runs-empty').hidden = Boolean(runs.length); $('runs-body').replaceChildren();
    for (const run of runs.slice(0, 25)) {
      const row = element('tr'); const statusCell = element('td');
      statusCell.append(element('span', `status-badge ${['failed', 'error'].includes(run.status) ? 'rejected' : 'prepared'}`, run.status || 'Unknown'));
      row.append(element('td', '', dateLabel(run.started_at)), statusCell, element('td', '', runSummary(run)), element('td', '', run.finished_at ? dateLabel(run.finished_at) : 'In progress'));
      $('runs-body').append(row);
    }
  }

  function setView(view, { reset = false } = {}) {
    if (!views[view]) return;
    state.view = view;
    if (reset) resetFilters(false);
    document.querySelectorAll('.nav-item').forEach((button) => { button.classList.toggle('active', button.dataset.view === view); if (button.dataset.view === view) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current'); });
    const [breadcrumb, eyebrow, title, subtitle] = views[view];
    $('breadcrumb-current').textContent = breadcrumb; $('page-eyebrow').textContent = eyebrow;
    $('page-title').replaceChildren(document.createTextNode(title), element('span', 'heading-dot', '.')); $('page-subtitle').textContent = subtitle;
    $('opportunities-view').hidden = !['opportunities', 'applications'].includes(view);
    $('automation-view').hidden = view !== 'automation'; $('profile-view').hidden = view !== 'profile';
    if (state.data) renderJobs();
  }

  function setStatus(status) {
    state.status = status;
    document.querySelectorAll('.status-tab').forEach((tab) => tab.classList.toggle('active', tab.dataset.status === status));
    $('other-status-filter').value = ['all', 'pending', 'shortlisted', 'prepared', 'applied'].includes(status) ? '' : status;
    if (state.data) renderJobs();
  }

  function resetFilters(renderNow = true) {
    state.search = ''; state.mode = 'all'; state.tier = 'all'; state.status = 'all'; state.freshness = 'fresh'; $('freshness-filter').value = 'fresh';
    $('search-input').value = ''; $('mode-filter').value = 'all'; $('tier-filter').value = 'all';
    if (renderNow) setStatus('all');
    else { document.querySelectorAll('.status-tab').forEach((tab) => tab.classList.toggle('active', tab.dataset.status === 'all')); $('other-status-filter').value = ''; }
  }

  function selectedJob() { return state.data && state.data.jobs.find((job) => job.key === state.selectedKey); }

  function openJob(key) {
    const job = state.data && state.data.jobs.find((item) => item.key === key);
    if (!job) return;
    state.selectedKey = key;
    if ($('job-drawer').hidden) state.previousFocus = document.activeElement;
    $('drawer-company').textContent = job.company; $('drawer-title').textContent = job.title;
    $('drawer-location').textContent = `${job.location || 'Location not specified'} · ${shortMode(job.workplace)}`;
    $('drawer-badges').replaceChildren(element('span', `status-badge ${isApplied(job) ? 'submitted' : isPending(job) ? 'pending' : jobStatus(job)}`, isApplied(job) ? 'Submitted' : isPending(job) ? 'Pending' : stages[jobStatus(job)]), element('span', `status-badge ${jobStatus(job)}`, stages[jobStatus(job)] || 'New'), element('span', 'status-badge', (job.assessment || {}).tier || 'Needs review'));
    const employerUrl = safeUrl(job.apply_url) || safeUrl(job.url);
    $('employer-link').hidden = !employerUrl; if (employerUrl) $('employer-link').href = employerUrl;
    const assessment = job.assessment || {}; const assessmentNode = $('drawer-assessment'); assessmentNode.replaceChildren();
    assessmentNode.append(element('p', 'assessment-text', assessment.geography || 'Verify the location and eligibility requirements.'));
    assessmentNode.append(element('p', 'assessment-text', job.freshness?.day ? `Source publication: ${dateLabel(job.published_at)} IST ? ${job.freshness.state === 'fresh' ? 'within' : 'outside'} the last 72 hours.` : `Original publication date unverified.${job.alert_received_at ? ' Alert received ' + dateLabel(job.alert_received_at) + ' IST.' : ''}`));
    if (job.description_complete === false) assessmentNode.append(element('p', 'assessment-text', 'This is an email-alert excerpt. Read the full employer description to verify experience and posting date before preparing or applying.'));
    const sources = element('p', 'assessment-text', 'Sources: ');
    for (const source of job.source_links || [{source: job.source, url: job.url}]) { const url = safeUrl(source.url); if (!url) continue; const link = element('a', '', source.source); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; sources.append(link, document.createTextNode(' ')); }
    assessmentNode.append(sources);
    const evidence = Array.isArray(assessment.experience_evidence) ? assessment.experience_evidence.join('; ') : '';
    assessmentNode.append(element('p', 'assessment-text', evidence ? `Experience evidence: ${evidence}` : 'No reliable numeric experience requirement was found. Read the job description.'));
    if (job.availability === 'not_in_latest_feed') assessmentNode.append(element('p', 'assessment-text', 'This opening is absent from the latest feed. Verify availability before applying.'));
    $('drawer-skills').replaceChildren(...(assessment.skills || []).map((skill) => element('span', 'skill-chip', skill)));
    const saved = review(job);
    $('review-status').value = jobStatus(job); $('review-notes').value = saved.notes || '';
    $('contact-name').value = saved.contact_name || ''; $('contact-url').value = saved.contact_url || '';
    $('confirmation-url').value = saved.confirmation_url || ''; $('confirm-applied').checked = false; updateAppliedConfirmation();
    $('drawer-description').textContent = job.description || 'No description was supplied. Open the employer page for full details.';
    const timestamps = [];
    if (saved.applied_at) timestamps.push(`Application recorded ${dateLabel(saved.applied_at)}`);
    if (saved.contacted_at) timestamps.push(`Contacted ${dateLabel(saved.contacted_at)}`);
    if (saved.followup_at && !saved.replied_at && jobStatus(job) === 'contacted') timestamps.push(`Follow-up due ${dateLabel(saved.followup_at)}`);
    $('drawer-tracking').textContent = timestamps.join(' · ');
    $('packet-content').hidden = true; $('prepare-packet-button').disabled = false; $('prepare-packet-button').textContent = 'Prepare application pack';
    if (job.packet && (job.packet.cover_letter || job.packet.outreach)) showPacket(job.packet);
    $('drawer-backdrop').hidden = false; $('job-drawer').hidden = false; document.body.classList.add('drawer-open'); $('close-drawer').focus();
  }

  function closeDrawer() {
    $('job-drawer').hidden = true; $('drawer-backdrop').hidden = true; document.body.classList.remove('drawer-open'); state.selectedKey = null;
    if (state.previousFocus && state.previousFocus.isConnected) state.previousFocus.focus();
  }

  function updateAppliedConfirmation() { $('applied-confirmation').hidden = $('review-status').value !== 'applied'; }

  function showPacket(packet) {
    $('cover-letter-text').value = packet.cover_letter || ''; $('outreach-text').value = packet.outreach || '';
    const items = Array.isArray(packet.checklist) ? packet.checklist : ['Verify role availability, location and experience requirements.', 'Check resume claims and remove placeholders.', 'Review the employer application before submitting.'];
    $('packet-checklist').replaceChildren(...items.map((item) => element('li', '', typeof item === 'object' ? item.text || item.label || JSON.stringify(item) : item)));
    $('packet-content').hidden = false; $('prepare-packet-button').textContent = 'Regenerate application pack';
  }

  async function runScan() {
    state.scanPending = true; $('scan-button').disabled = true; $('scan-button').classList.add('is-running'); $('scan-button-text').textContent = 'Starting scan…';
    try {
      await api('/api/run', { method: 'POST', body: {} }); toast('Scan started. New results appear as the run completes.'); await loadWorkspace({ quiet: true });
      let polls = 0;
      const timer = setInterval(async () => {
        if (state.loading) return;
        await loadWorkspace({ quiet: true }); polls += 1;
        const running = ((state.data && state.data.runs) || []).some(runInProgress);
        if (!running || polls >= 40) { clearInterval(timer); state.scanPending = false; if (state.data) renderAutomation(); }
      }, 3000);
    } catch (error) { toast(error.message, true); state.scanPending = false; if (state.data) renderAutomation(); else { $('scan-button').disabled = false; $('scan-button').classList.remove('is-running'); $('scan-button-text').textContent = 'Scan for jobs'; } }
  }

  async function saveReview(event) {
    event.preventDefault(); const job = selectedJob(); if (!job) return;
    const status = $('review-status').value;
    if (status === 'applied' && !$('confirm-applied').checked) { toast('Confirm that you actually submitted the application before marking it applied.', true); $('confirm-applied').focus(); return; }
    const contactUrl = $('contact-url').value.trim(); const confirmationUrl = $('confirmation-url').value.trim();
    if ((contactUrl && !safeUrl(contactUrl)) || (confirmationUrl && !safeUrl(confirmationUrl))) { toast('Use valid HTTPS contact and confirmation URLs.', true); return; }
    $('save-review-button').disabled = true;
    try {
      await api('/api/jobs/review', { method: 'POST', body: { key: job.key, status, notes: $('review-notes').value, contact_name: $('contact-name').value, contact_url: contactUrl, confirmation_url: confirmationUrl, confirmed: status === 'applied' && $('confirm-applied').checked } });
      await loadWorkspace({ quiet: true }); toast(status === 'applied' ? 'Confirmed application recorded.' : 'Progress saved.');
      if (state.selectedKey) openJob(state.selectedKey);
    } catch (error) { toast(error.message, true); }
    finally { $('save-review-button').disabled = false; }
  }

  async function copyField(id) {
    const field = $(id);
    try { await navigator.clipboard.writeText(field.value); toast('Copied.'); }
    catch { field.focus(); field.select(); toast('Select and copy the text from this field.'); }
  }

  document.querySelectorAll('[data-view]').forEach((button) => button.addEventListener('click', () => setView(button.dataset.view, { reset: true })));
  document.querySelectorAll('[data-open-profile]').forEach((button) => button.addEventListener('click', () => setView('profile')));
  document.querySelectorAll('[data-open-automation]').forEach((button) => button.addEventListener('click', () => setView('automation')));
  document.querySelectorAll('[data-status]').forEach((button) => button.addEventListener('click', () => { if (['pending', 'applied'].includes(button.dataset.status)) setView('applications'); setStatus(button.dataset.status); }));
  document.querySelectorAll('[data-stat]').forEach((button) => button.addEventListener('click', () => { const status = button.dataset.stat; setView(status === 'all' ? 'opportunities' : 'applications', { reset: true }); setStatus(status === 'all' ? 'all' : status); }));
  $('other-status-filter').addEventListener('change', (event) => { if (event.target.value) setStatus(event.target.value); });
  $('search-input').addEventListener('input', (event) => { state.search = event.target.value.trim(); if (state.data) renderJobs(); });
  $('freshness-filter').addEventListener('change', (event) => { state.freshness = event.target.value; if (state.data) renderJobs(); });
  $('mode-filter').addEventListener('change', (event) => { state.mode = event.target.value; if (state.data) renderJobs(); });
  $('tier-filter').addEventListener('change', (event) => { state.tier = event.target.value; if (state.data) renderJobs(); });
  $('reset-filters-button').addEventListener('click', () => resetFilters()); $('retry-button').addEventListener('click', () => loadWorkspace()); $('scan-button').addEventListener('click', runScan);
  $('close-drawer').addEventListener('click', closeDrawer); $('drawer-backdrop').addEventListener('click', closeDrawer); $('review-status').addEventListener('change', updateAppliedConfirmation); $('review-form').addEventListener('submit', saveReview);
  $('prepare-packet-button').addEventListener('click', async () => {
    const job = selectedJob(); if (!job) return;
    $('prepare-packet-button').disabled = true; $('prepare-packet-button').textContent = 'Preparing…';
    try {
      const packet = await api('/api/jobs/packet', { method: 'POST', body: { key: job.key } });
      job.packet = { ...packet, ready: true }; showPacket(packet); renderMetrics(); toast('Application pack prepared. Review the drafts before using them.');
    } catch (error) { toast(error.message, true); $('prepare-packet-button').textContent = 'Prepare application pack'; }
    finally { $('prepare-packet-button').disabled = false; }
  });
  document.querySelectorAll('[data-copy]').forEach((button) => button.addEventListener('click', () => copyField(button.dataset.copy)));
  $('copy-health-button').addEventListener('click', () => copyField('health-url'));
  $('automation-form').addEventListener('submit', async (event) => {
    event.preventDefault(); $('save-settings-button').disabled = true;
    try { await api('/api/settings', { method: 'POST', body: { interval_hours: Number($('scan-interval').value), enabled: $('schedule-enabled').checked, submission_mode: 'prepare' } }); await loadWorkspace({ quiet: true }); toast('Schedule settings saved. Check run history to verify unattended execution.'); }
    catch (error) { toast(error.message, true); }
    finally { $('save-settings-button').disabled = false; }
  });
  $('profile-form').addEventListener('submit', async (event) => {
    event.preventDefault(); $('save-profile-button').disabled = true;
    try {
      await api('/api/profile', { method: 'POST', body: { display_name: $('profile-display-name').value.trim(), experience_years: Number($('profile-years').value), evidence_for_outreach: $('profile-proof').value.trim() } });
      await loadWorkspace({ quiet: true }); toast('Profile saved. Future packs use these verified details.');
    } catch (error) { toast(error.message, true); }
    finally { $('save-profile-button').disabled = false; }
  });
  $('resume-upload-form').addEventListener('submit', async (event) => {
    event.preventDefault(); const file = $('resume-upload').files[0];
    if (!file || !/\.pdf$/i.test(file.name)) { toast('Choose a PDF resume to upload.', true); return; }
    if (file.size > 10 * 1024 * 1024) { toast('Please use a PDF smaller than 10 MB.', true); return; }
    $('upload-resume-button').disabled = true;
    try { const form = new FormData(); form.append('file', file); await api('/api/resume', { method: 'POST', body: form }); $('resume-upload').value = ''; await loadWorkspace({ quiet: true }); toast('Resume uploaded privately. Check your profile facts before preparing applications.'); }
    catch (error) { toast(error.message, true); }
    finally { $('upload-resume-button').disabled = false; }
  });
  $('logout-button').addEventListener('click', async () => { try { await api('/api/logout', { method: 'POST', body: {} }); window.location.assign('/login'); } catch (error) { toast(error.message, true); } });
  document.addEventListener('keydown', (event) => {
    if ($('job-drawer').hidden) return;
    if (event.key === 'Escape') { closeDrawer(); return; }
    if (event.key === 'Tab') {
      const focusable = [...$('job-drawer').querySelectorAll('button,a[href],input,select,textarea,summary,[tabindex="0"]')].filter((node) => !node.disabled && node.getClientRects().length);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  $('today-date').textContent = new Intl.DateTimeFormat('en-IN', { timeZone: 'Asia/Kolkata', weekday: 'short', day: 'numeric', month: 'short' }).format(new Date());
  setInterval(() => { if (!document.hidden && $('job-drawer').hidden) loadWorkspace({ quiet: true }); }, 60000);
  loadWorkspace();
})();
