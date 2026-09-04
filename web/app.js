'use strict';

// ─────────────────────────────────────────────────────────────────────────────
// DOM Helper
// ─────────────────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);

// ─────────────────────────────────────────────────────────────────────────────
// Comprehensive Logging & Diagnostics
// ─────────────────────────────────────────────────────────────────────────────
const LOG_TAG = '[SignalPlayer]';

function log(topic, msg, ...extra) {
  const ts = new Date().toISOString().substring(11, 23);
  console.log(`%c${LOG_TAG}[${ts}][${topic}] %c${msg}`, 'color: #3b82f6; font-weight: bold;', 'color: inherit;', ...extra);
}

function logWarn(topic, msg, ...extra) {
  const ts = new Date().toISOString().substring(11, 23);
  console.warn(`%c${LOG_TAG}[${ts}][${topic}] %c${msg}`, 'color: #f59e0b; font-weight: bold;', 'color: inherit;', ...extra);
}

function logErr(topic, msg, ...extra) {
  const ts = new Date().toISOString().substring(11, 23);
  console.error(`%c${LOG_TAG}[${ts}][${topic}] %c${msg}`, 'color: #ef4444; font-weight: bold;', 'color: inherit;', ...extra);
}

window.addEventListener('error', (e) => {
  logErr('GlobalError', `Uncaught window error: "${e.message}" at ${e.filename}:${e.lineno}:${e.colno}`, e.error);
});

window.addEventListener('unhandledrejection', (e) => {
  logErr('UnhandledRejection', 'Unhandled Promise Rejection:', e.reason);
});

// ─────────────────────────────────────────────────────────────────────────────
// Client Application State
// ─────────────────────────────────────────────────────────────────────────────
let allMedia     = [];    // full list for current group/view
let filtered     = [];    // after search/label filter
let currentIdx   = -1;
let currentView  = null;  // { type: 'group'|'all'|'favourites'|'label', id?, label? }
let groups       = [];
const hoverState = {};
let lastPendingCount = -1;

// ─────────────────────────────────────────────────────────────────────────────
// Label & Utility Helpers
// ─────────────────────────────────────────────────────────────────────────────
function labelColour(lbl) {
  let h = 0;
  for (let i = 0; i < lbl.length; i++) {
    h = (h * 31 + lbl.charCodeAt(i)) & 0xffffffff;
  }
  return `hsl(${((h >>> 0) % 360)}, 60%, 38%)`;
}

function chipHtml(lbl, removable = false, small = false) {
  const bg  = labelColour(lbl);
  const cls = removable ? 'label-chip-rm' : `chip${small ? ' chip-sm' : ''}`;
  const rm  = removable ? `<button onclick="removeLabel('${esc(lbl)}')" title="Remove">×</button>` : '';
  return `<span class="${cls}" style="background:${bg};color:#fff">${esc(lbl)}${rm}</span>`;
}

function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function fmtDur(s) {
  if (!isFinite(s) || s < 0) return '?:??';
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, '0')}`;
}

// ─────────────────────────────────────────────────────────────────────────────
// Bootstrap & Initialization
// ─────────────────────────────────────────────────────────────────────────────
async function init() {
  log('Init', 'Client script started. Initializing event listeners & views...');

  const groupListEl = $('group-list');
  const labelListEl = $('label-list');

  if (!groupListEl) {
    logErr('Init', 'Element #group-list not found in DOM!');
  } else {
    log('Init', 'Binding click event listener to #group-list container (event delegation)');
    groupListEl.addEventListener('click', (e) => {
      log('Click', 'Group-list container click event triggered.', { target: e.target });
      const item = e.target.closest('.nav-item');
      if (!item) {
        logWarn('Click', 'Click was inside #group-list but not within a .nav-item element.');
        return;
      }
      const id = item.dataset.id;
      log('Click', `Clicked .nav-item found. data-id="${id}"`);
      const g = groups.find(x => x.id === id);
      const name = g ? g.name : (item.querySelector('.group-name')?.textContent?.trim() || id);
      log('Click', `Resolved group: "${name}" (id: ${id}, existsInGroupsArray: ${Boolean(g)}). Invoking loadGroup()...`);
      loadGroup(id, name);
    });
  }

  if (!labelListEl) {
    logErr('Init', 'Element #label-list not found in DOM!');
  } else {
    log('Init', 'Binding click event listener to #label-list container');
    labelListEl.addEventListener('click', (e) => {
      log('Click', 'Label-list container click event triggered.', { target: e.target });
      const item = e.target.closest('.nav-item');
      if (!item) return;
      const lbl = item.dataset.label;
      log('Click', `Clicked label: "${lbl}". Invoking loadLabel()...`);
      if (lbl) loadLabel(lbl);
    });
  }

  log('Init', 'Fetching groups from server: GET /api/groups');
  const t0 = performance.now();
  try {
    const res = await fetch('/api/groups');
    const elapsed = (performance.now() - t0).toFixed(1);
    log('Init', `/api/groups response received in ${elapsed}ms: HTTP ${res.status} ${res.statusText}`);
    if (!res.ok) {
      const errTxt = await res.text();
      logErr('Init', `/api/groups HTTP error ${res.status}: ${errTxt}`);
      throw new Error(`Server error ${res.status}: ${errTxt}`);
    }
    groups = await res.json();
    log('Init', `Successfully parsed /api/groups. Total groups: ${groups.length}`, groups);
    renderSidebar();
    await refreshLabels();
    await updateNewCount();
    startSyncPolling();
    log('Init', 'Initialization completed successfully.');
  } catch (err) {
    const elapsed = (performance.now() - t0).toFixed(1);
    logErr('Init', `Failed to initialize groups after ${elapsed}ms:`, err);
    $('empty-msg').innerHTML = `Failed to load groups from server:<br><span style="color:#ef4444;font-size:12px;">${esc(err.message)}</span><br><small style="color:var(--muted)">Check Console (F12) for details</small>`;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Polling & Background Sync Monitoring
// ─────────────────────────────────────────────────────────────────────────────
function startSyncPolling() {
  log('Sync', 'Starting background sync status polling interval (every 4000ms)...');
  setInterval(async () => {
    try {
      const res = await fetch('/api/sync/status');
      if (!res.ok) {
        logWarn('Sync', `/api/sync/status returned HTTP ${res.status}`);
        return;
      }
      const data = await res.json();
      const banner = $('sync-banner');
      if (!banner) return;

      if (data.is_running) {
        banner.style.display = 'flex';
        const rem = data.pending_count ?? 0;
        $('sync-stats').textContent = `${rem} pending video${rem !== 1 ? 's' : ''}`;
        log('Sync', `Background download active. ${rem} video(s) pending.`);

        // If newly downloaded videos were detected, refresh UI
        if (lastPendingCount !== -1 && rem < lastPendingCount) {
          log('Sync', `Pending count decreased from ${lastPendingCount} to ${rem}. Refreshing UI...`);
          const gRes = await fetch('/api/groups');
          groups = await gRes.json();
          renderSidebar();
          await updateNewCount();
          if (currentView && currentView.type === 'new') {
            await setView('new');
          } else if (currentView && currentView.type === 'group') {
            await loadGroup(currentView.id, $('group-title').textContent);
          }
        }
        lastPendingCount = rem;
      } else {
        if (banner.style.display !== 'none') {
          log('Sync', 'Background sync finished. Hiding banner and refreshing group list.');
          banner.style.display = 'none';
          const gRes = await fetch('/api/groups');
          groups = await gRes.json();
          renderSidebar();
          await updateNewCount();
        }
      }
    } catch (e) {
      logWarn('Sync', 'Polling /api/sync/status encountered error:', e);
    }
  }, 4000);
}

async function updateNewCount() {
  try {
    log('NewCount', 'Fetching new video count: GET /api/media/new_count');
    const res = await fetch('/api/media/new_count');
    if (!res.ok) {
      logWarn('NewCount', `/api/media/new_count returned HTTP ${res.status}`);
      return;
    }
    const data = await res.json();
    const count = data.count ?? 0;
    log('NewCount', `Received count: ${count}`);
    const badge = $('new-count');
    if (badge) {
      badge.textContent = count;
      badge.style.display = count > 0 ? 'inline-block' : 'none';
    }
  } catch (e) {
    logWarn('NewCount', 'Failed to fetch /api/media/new_count:', e);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Sidebar & Labels
// ─────────────────────────────────────────────────────────────────────────────
function loadGroupById(el) {
  const id = el.dataset.id;
  const g = groups.find(x => String(x.id) === String(id));
  const name = g ? g.name : (el.querySelector('.group-name')?.textContent?.trim() || id);
  log('Click', `loadGroupById invoked: "${name}" (${id})`);
  loadGroup(id, name);
}

function loadLabelByEl(el) {
  const lbl = el.dataset.label;
  if (lbl) {
    log('Click', `loadLabelByEl invoked: "${lbl}"`);
    loadLabel(lbl);
  }
}

function renderSidebar() {
  log('Sidebar', `renderSidebar called. Rendering ${groups.length} groups.`);
  const list = $('group-list');
  if (!list) {
    logErr('Sidebar', '#group-list element missing in DOM!');
    return;
  }
  if (!groups.length) {
    logWarn('Sidebar', 'groups list is empty.');
    list.innerHTML = '<div style="padding:16px;color:var(--muted);font-size:12px;text-align:center">No downloaded videos</div>';
    return;
  }
  list.innerHTML = groups.map(g => `
    <div class="nav-item" data-id="${esc(g.id)}" onclick="loadGroupById(this)">
      <div>
        <div class="group-name">${esc(g.name)}</div>
        <div class="group-count">${g.video_count} video${g.video_count!==1?'s':''} • ${esc(g.type)}</div>
      </div>
    </div>`).join('');
  log('Sidebar', `Injected ${groups.length} group items into #group-list.`);
}

async function refreshLabels() {
  log('Labels', 'Fetching labels: GET /api/labels');
  try {
    const res    = await fetch('/api/labels');
    const labels = await res.json();
    log('Labels', `Received ${labels.length} labels:`, labels);
    const sec    = $('label-section');
    const list   = $('label-list');
    if (!labels.length) {
      if (sec) sec.style.display = 'none';
      if (list) list.innerHTML = '';
      return;
    }
    if (sec) sec.style.display = '';
    if (list) {
      list.innerHTML = labels.map(lbl => `
        <div class="nav-item" data-label="${esc(lbl)}" onclick="loadLabelByEl(this)">
          <span>${chipHtml(lbl, false, true)}</span>
        </div>`).join('');
    }
  } catch (e) {
    logWarn('Labels', 'Failed to refresh labels:', e);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// View Switching & Group Navigation
// ─────────────────────────────────────────────────────────────────────────────
function setActiveNav(type, id = null, label = null) {
  log('Nav', `setActiveNav: type="${type}", id="${id}", label="${label}"`);
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  const btnMark = $('btn-mark-all');
  if (btnMark) btnMark.style.display = type === 'new' ? 'inline-block' : 'none';

  if (type === 'new')         $('nav-new')?.classList.add('active');
  else if (type === 'all')    $('nav-all')?.classList.add('active');
  else if (type === 'favourites') $('nav-fav')?.classList.add('active');
  else if (type === 'group' && id) {
    const el = document.querySelector(`.nav-item[data-id="${CSS.escape(id)}"]`);
    if (el) el.classList.add('active');
    else logWarn('Nav', `Could not find sidebar .nav-item with data-id="${id}" to set active.`);
  } else if (type === 'label' && label) {
    const el = document.querySelector(`.nav-item[data-label="${CSS.escape(label)}"]`);
    if (el) el.classList.add('active');
  }
}

async function setView(type) {
  log('View', `===> setView called with type: "${type}"`);
  currentView = { type };
  setActiveNav(type);
  const titles = {
    new: '✨ New Videos',
    all: 'All Videos',
    favourites: 'Favourites'
  };
  const title = titles[type] || 'Videos';
  $('group-title').textContent = title;
  showLoading();
  const url = '/api/media?group=all';
  log('View', `Sending request: GET ${url}`);
  const t0 = performance.now();
  try {
    const res = await fetch(url);
    const elapsed = (performance.now() - t0).toFixed(1);
    log('View', `Response received in ${elapsed}ms: HTTP ${res.status} ${res.statusText}`);
    if (!res.ok) {
      const err = await res.text();
      logErr('View', `Server error (${res.status}): ${err}`);
      throw new Error(`Server error ${res.status}: ${err}`);
    }
    log('View', 'Parsing JSON media items...');
    const items = await res.json();
    log('View', `Total media items returned: ${items.length}`);
    let toRender = items;
    if (type === 'new') {
      toRender = items.filter(m => m.is_new);
      log('View', `Filtered for view="new": ${toRender.length} items.`);
    } else if (type === 'all') {
      log('View', `Showing all ${toRender.length} items.`);
    } else if (type === 'favourites') {
      toRender = items.filter(m => m.favourite);
      log('View', `Filtered for view="favourites": ${toRender.length} items.`);
    }
    renderGrid(toRender, title);
    log('View', `<=== setView finished rendering for view: "${type}".`);
  } catch (err) {
    const elapsed = (performance.now() - t0).toFixed(1);
    logErr('View', `FAILED setView("${type}") after ${elapsed}ms:`, err);
    $('loading').style.display = 'none';
    $('empty-msg').innerHTML = `Error loading videos:<br><span style="color:#ef4444;font-size:12px;">${esc(err.message)}</span><br><small style="color:var(--muted)">Check DevTools Console (F12) for detailed logs</small>`;
    $('empty').style.display = 'flex';
  }
}

async function loadGroup(id, name) {
  log('Group', `===> loadGroup called: id="${id}", name="${name}"`);
  currentView = { type: 'group', id };
  setActiveNav('group', id);
  $('group-title').textContent = name;
  showLoading();
  const url = '/api/media?group=' + encodeURIComponent(id);
  log('Group', `Fetching media items from URL: ${url}`);
  const t0 = performance.now();
  try {
    const res = await fetch(url);
    const elapsed = (performance.now() - t0).toFixed(1);
    log('Group', `Fetch responded in ${elapsed}ms: HTTP ${res.status} ${res.statusText}`, {
      contentType: res.headers.get('content-type'),
      status: res.status
    });
    if (!res.ok) {
      const errText = await res.text();
      logErr('Group', `Server responded with error status ${res.status}: ${errText}`);
      throw new Error(`Server error (${res.status}): ${errText}`);
    }
    log('Group', 'Parsing JSON payload...');
    const parseT0 = performance.now();
    const items = await res.json();
    const parseElapsed = (performance.now() - parseT0).toFixed(1);
    log('Group', `JSON parsed successfully in ${parseElapsed}ms. Received ${items.length} media items for "${name}".`, {
      totalCount: items.length,
      sampleFirstItem: items[0] ?? null
    });
    renderGrid(items, name);
    log('Group', `<=== loadGroup complete for "${name}". Rendered ${items.length} videos.`);
  } catch (err) {
    const elapsed = (performance.now() - t0).toFixed(1);
    logErr('Group', `FAILED to load media for group "${name}" (${id}) after ${elapsed}ms:`, err);
    $('loading').style.display = 'none';
    $('empty-msg').innerHTML = `Failed to load videos for <b>${esc(name)}</b>:<br><span style="color:#ef4444;font-size:12px;">${esc(err.message)}</span><br><small style="color:var(--muted)">Check DevTools Console (F12) for full trace</small>`;
    $('empty').style.display = 'flex';
  }
}

async function loadLabel(label) {
  log('Label', `===> loadLabel called: label="${label}"`);
  currentView = { type: 'label', label };
  setActiveNav('label', null, label);
  const title = `Label: ${label}`;
  $('group-title').textContent = title;
  showLoading();
  const url = '/api/media?group=all';
  log('Label', `Fetching media items from URL: ${url}`);
  const t0 = performance.now();
  try {
    const res = await fetch(url);
    const elapsed = (performance.now() - t0).toFixed(1);
    log('Label', `Response received in ${elapsed}ms: HTTP ${res.status} ${res.statusText}`);
    if (!res.ok) {
      const err = await res.text();
      logErr('Label', `Server error (${res.status}): ${err}`);
      throw new Error(`Server error (${res.status}): ${err}`);
    }
    const items = await res.json();
    const matching = items.filter(m => m.labels && m.labels.includes(label));
    log('Label', `Filtered ${items.length} total items down to ${matching.length} matching label "${label}".`);
    renderGrid(matching, title);
    log('Label', `<=== loadLabel complete for "${label}".`);
  } catch (err) {
    const elapsed = (performance.now() - t0).toFixed(1);
    logErr('Label', `FAILED to load media for label "${label}" after ${elapsed}ms:`, err);
    $('loading').style.display = 'none';
    $('empty-msg').innerHTML = `Error loading videos for label "${esc(label)}":<br><span style="color:#ef4444;font-size:12px;">${esc(err.message)}</span>`;
    $('empty').style.display = 'flex';
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Grid Rendering & Filtering
// ─────────────────────────────────────────────────────────────────────────────
function showLoading() {
  log('Grid', 'showLoading: Clearing grid, hiding empty state, showing spinner.');
  $('grid').innerHTML = '';
  $('empty').style.display = 'none';
  $('loading').style.display = 'flex';
}

function renderGrid(media, title) {
  log('Grid', `renderGrid called with ${media.length} items, title="${title}"`);
  allMedia = media;
  $('loading').style.display = 'none';
  $('toolbar-info').textContent = `${media.length} video${media.length!==1?'s':''}`;
  $('search').value = '';
  applyFilter();
}

function applyFilter() {
  const q = $('search').value.toLowerCase().trim();
  log('Filter', `applyFilter: Query="${q}", Current allMedia.length=${allMedia.length}`);
  filtered = q
    ? allMedia.filter(m =>
        (m.filename || '').toLowerCase().includes(q) ||
        (m.sender   || '').toLowerCase().includes(q) ||
        (m.labels   || []).some(l => l.toLowerCase().includes(q))
      )
    : [...allMedia];
  log('Filter', `applyFilter: Filtered result count=${filtered.length}`);

  const grid = $('grid');
  if (!grid) {
    logErr('Grid', 'Element #grid not found in DOM!');
    return;
  }

  if (!filtered.length) {
    log('Grid', 'No items in filtered array. Displaying #empty state.');
    grid.innerHTML = '';
    $('empty-msg').textContent = q ? `No videos matching "${q}"` : 'No videos here yet';
    $('empty').style.display = 'flex';
    return;
  }
  $('empty').style.display = 'none';
  log('Grid', `Building and injecting ${filtered.length} card HTML elements into #grid...`);

  grid.innerHTML = filtered.map((m, i) => `
    <div class="card${m.favourite?' is-fav':''}" data-index="${i}"
         onclick="openModal(${i})"
         onmouseenter="hoverStart(${i})"
         onmouseleave="hoverStop(${i})">
      <div class="thumb">
        <video id="v${i}" src="/stream/${encodeURIComponent(m.id)}"
               preload="none" muted playsinline></video>
        <div class="play-icon">
          <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
        </div>
        <div class="dur" id="d${i}">—:——</div>
        ${m.favourite ? '<div class="fav-badge">⭐</div>' : ''}
        ${m.is_new ? '<div class="new-badge">NEW</div>' : ''}
      </div>
      <div class="card-meta">
        <div class="card-fn">${esc(m.filename||'Video')}</div>
        <div class="card-ts">${esc(m.sent_time)}</div>
        ${m.labels.length ? `<div class="card-labels">${m.labels.map(l=>chipHtml(l,false,true)).join('')}</div>` : ''}
      </div>
    </div>`).join('');

  log('Grid', 'Setting up IntersectionObserver for cards...');
  const obs = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      const vid = entry.target.querySelector('video');
      if (vid && vid.preload === 'none') {
        vid.preload = 'metadata';
        vid.load();
        obs.unobserve(entry.target);
        vid.addEventListener('loadedmetadata', () => {
          const badge = $(vid.id.replace('v','d'));
          if (badge) badge.textContent = fmtDur(vid.duration);
        }, { once: true });
      }
    });
  }, { rootMargin: '120px' });

  grid.querySelectorAll('.card').forEach(c => obs.observe(c));
  log('Grid', `IntersectionObserver observing ${grid.querySelectorAll('.card').length} cards.`);
}

// ─────────────────────────────────────────────────────────────────────────────
// YouTube-style Hover Preview
// ─────────────────────────────────────────────────────────────────────────────
function hoverStart(i) {
  const s = hoverState[i] = hoverState[i] || {};
  clearTimeout(s.stopTimer);
  s.startTimer = setTimeout(() => {
    const vid = $('v' + i);
    if (!vid) return;
    const begin = () => {
      vid.currentTime = 0;
      let t0 = null;
      const SWEEP = 4000;
      function frame(ts) {
        if (!t0) t0 = ts;
        const prog = Math.min((ts - t0) / SWEEP, 1);
        vid.currentTime = prog * (vid.duration || 30) * 0.9;
        if (prog < 1 && $('v'+i)?.closest('.card:hover')) {
          s.rafId = requestAnimationFrame(frame);
        }
      }
      s.rafId = requestAnimationFrame(frame);
    };
    if (vid.readyState >= 1) begin();
    else {
      vid.preload = 'metadata';
      vid.load();
      vid.addEventListener('loadedmetadata', begin, { once: true });
    }
  }, 220);
}

function hoverStop(i) {
  const s = hoverState[i] || {};
  clearTimeout(s.startTimer);
  cancelAnimationFrame(s.rafId);
  s.stopTimer = setTimeout(() => {
    const v = $('v' + i);
    if (v) v.currentTime = 0;
  }, 80);
}

// ─────────────────────────────────────────────────────────────────────────────
// Modal Video Player
// ─────────────────────────────────────────────────────────────────────────────
function openModal(i) {
  log('Modal', `openModal called for index: ${i}`);
  currentIdx = i;
  renderModal();
  $('modal').classList.add('open');

  const m = filtered[i];
  if (m && m.is_new) {
    log('Modal', `Clearing is_new flag for item: ${m.id}`);
    m.is_new = false;
    const am = allMedia.find(x => x.id === m.id);
    if (am) am.is_new = false;
    fetch('/api/meta/seen', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: m.id })
    }).catch(err => logWarn('Modal', 'Error marking item seen:', err));
    refreshCard(i, m);
    updateNewCount();
  }
}

function closeModal() {
  log('Modal', 'closeModal called');
  $('modal').classList.remove('open');
  const v = $('modal-video');
  v.pause();
  v.src = '';
}

function renderModal() {
  const m = filtered[currentIdx];
  if (!m) {
    logWarn('Modal', `No item found for currentIdx: ${currentIdx}`);
    return;
  }
  log('Modal', `renderModal for item: id="${m.id}", filename="${m.filename}", sender="${m.sender}"`);

  const v = $('modal-video');
  v.src = '/stream/' + encodeURIComponent(m.id);
  v.load();
  v.play().catch(err => logWarn('Modal', 'Autoplay prevented or failed:', err));

  $('modal-info').innerHTML =
    `<strong>${esc(m.filename||'Video')}</strong> &nbsp;*&nbsp; `+
    `${esc(m.sender)} &nbsp;*&nbsp; ${esc(m.sent_time)}`;

  renderFavBtn(m.favourite);
  renderChips(m.labels);

  $('btn-prev').disabled = currentIdx === 0;
  $('btn-next').disabled = currentIdx === filtered.length - 1;
}

function navigate(dir) {
  const n = currentIdx + dir;
  log('Modal', `navigate: dir=${dir}, from=${currentIdx} to=${n}`);
  if (n < 0 || n >= filtered.length) return;
  currentIdx = n;
  renderModal();

  const m = filtered[n];
  if (m && m.is_new) {
    m.is_new = false;
    const am = allMedia.find(x => x.id === m.id);
    if (am) am.is_new = false;
    fetch('/api/meta/seen', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: m.id })
    }).catch(() => {});
    refreshCard(n, m);
    updateNewCount();
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Favourites & Annotations
// ─────────────────────────────────────────────────────────────────────────────
function renderFavBtn(isFav) {
  const btn = $('fav-btn');
  $('fav-icon').textContent  = isFav ? '★' : '☆';
  $('fav-label').textContent = isFav ? 'Favourited' : 'Favourite';
  btn.classList.toggle('active', isFav);
}

async function toggleFav() {
  const m = filtered[currentIdx];
  if (!m) return;
  const newFav = !m.favourite;
  log('Fav', `toggleFav: item=${m.id}, setting favourite=${newFav}`);
  m.favourite  = newFav;
  const am = allMedia.find(x => x.id === m.id);
  if (am) am.favourite = newFav;

  try {
    await fetch('/api/meta/' + encodeURIComponent(m.id), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ favourite: newFav }),
    });
    log('Fav', `Successfully saved favourite status for item: ${m.id}`);
  } catch (err) {
    logErr('Fav', `Failed to save favourite status for item: ${m.id}:`, err);
  }

  renderFavBtn(newFav);
  refreshCard(currentIdx, m);
}

function renderChips(labels) {
  log('Labels', `renderChips called with ${labels.length} labels:`, labels);
  $('modal-chips').innerHTML = labels.map(l => chipHtml(l, true)).join('');
  $('label-input').value = '';
}

function handleLabelKey(e) {
  if (e.key !== 'Enter' && e.key !== ',') return;
  e.preventDefault();
  const val = $('label-input').value.trim();
  if (!val) return;
  log('Labels', `handleLabelKey submitted label: "${val}"`);
  addLabel(val);
}

async function addLabel(lbl) {
  const m = filtered[currentIdx];
  if (!m) return;
  if (m.labels.includes(lbl)) {
    log('Labels', `Label "${lbl}" already present on item ${m.id}`);
    $('label-input').value = '';
    return;
  }
  log('Labels', `Adding label "${lbl}" to item ${m.id}`);
  m.labels.push(lbl);
  const am = allMedia.find(x => x.id === m.id);
  if (am) am.labels = [...m.labels];

  await saveMeta(m);
  renderChips(m.labels);
  refreshCard(currentIdx, m);
  await refreshLabels();
}

async function removeLabel(lbl) {
  const m = filtered[currentIdx];
  if (!m) return;
  log('Labels', `Removing label "${lbl}" from item ${m.id}`);
  m.labels = m.labels.filter(l => l !== lbl);
  const am = allMedia.find(x => x.id === m.id);
  if (am) am.labels = [...m.labels];

  await saveMeta(m);
  renderChips(m.labels);
  refreshCard(currentIdx, m);
  await refreshLabels();
}

async function saveMeta(m) {
  log('Meta', `Saving metadata for item ${m.id}: fav=${m.favourite}, labels=`, m.labels);
  try {
    const res = await fetch('/api/meta/' + encodeURIComponent(m.id), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ favourite: m.favourite, labels: m.labels }),
    });
    log('Meta', `saveMeta response: HTTP ${res.status}`);
  } catch (err) {
    logErr('Meta', `saveMeta failed for item ${m.id}:`, err);
  }
}

function refreshCard(idx, m) {
  const card = $('grid').querySelectorAll('.card')[idx];
  if (!card) return;
  card.classList.toggle('is-fav', m.favourite);
  
  // Fav badge
  const existingFav = card.querySelector('.fav-badge');
  if (m.favourite && !existingFav) {
    card.querySelector('.thumb').insertAdjacentHTML('beforeend', '<div class="fav-badge">⭐</div>');
  } else if (!m.favourite && existingFav) {
    existingFav.remove();
  }
  
  // New badge
  const existingNew = card.querySelector('.new-badge');
  if (m.is_new && !existingNew) {
    card.querySelector('.thumb').insertAdjacentHTML('beforeend', '<div class="new-badge">NEW</div>');
  } else if (!m.is_new && existingNew) {
    existingNew.remove();
  }
  
  // Labels
  const labelEl = card.querySelector('.card-labels');
  const meta    = card.querySelector('.card-meta');
  if (m.labels.length) {
    const html = `<div class="card-labels">${m.labels.map(l => chipHtml(l, false, true)).join('')}</div>`;
    if (labelEl) labelEl.outerHTML = html;
    else meta.insertAdjacentHTML('beforeend', html);
  } else if (labelEl) {
    labelEl.remove();
  }
}

async function markAllSeen() {
  const newItems = allMedia.filter(m => m.is_new);
  log('Seen', `markAllSeen called. Found ${newItems.length} new items to mark.`);
  if (!newItems.length) return;
  const ids = newItems.map(m => m.id);

  newItems.forEach(m => { m.is_new = false; });
  filtered.forEach(m => { if (ids.includes(m.id)) m.is_new = false; });

  try {
    const res = await fetch('/api/meta/seen', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids })
    });
    log('Seen', `markAllSeen POST response: HTTP ${res.status}`);
  } catch (err) {
    logErr('Seen', 'markAllSeen POST failed:', err);
  }

  await updateNewCount();

  if (currentView && currentView.type === 'new') {
    renderGrid([], '✨ New Videos');
  } else {
    applyFilter();
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Global Keyboard Shortcuts
// ─────────────────────────────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  if (document.activeElement === $('label-input') || document.activeElement === $('search')) return;

  if (!$('modal').classList.contains('open')) return;
  log('Keyboard', `Keydown detected: "${e.key}"`);
  if (e.key === 'Escape')     closeModal();
  if (e.key === 'ArrowRight') navigate(1);
  if (e.key === 'ArrowLeft')  navigate(-1);
  if (e.key === 'f' || e.key === 'F') {
    e.preventDefault();
    toggleFav();
  }
});

// Run bootstrap when DOM is ready
log('Init', 'Invoking init() bootstrap function...');
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
