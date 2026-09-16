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
let allMedia      = [];    // full progressive list for current view
let filtered      = [];    // after local search/label filter
let currentIdx    = -1;
let currentView   = null;  // { type: 'group'|'all'|'favourites'|'label'|'new', id?, label? }
let groups        = [];
let sortOrder     = 'desc'; // 'desc' = newest to oldest, 'asc' = oldest to newest
let nextCursor    = null;  // Keyset cursor token for progressive pagination
let hasMore       = false;
let isLoadingMore = false;
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
  if (!isFinite(s) || s <= 0) return '—:——';
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

  if (groupListEl) {
    groupListEl.addEventListener('click', (e) => {
      const item = e.target.closest('.nav-item');
      if (!item) return;
      const id = item.dataset.id;
      const g = groups.find(x => x.id === id);
      const name = g ? g.name : (item.querySelector('.group-name')?.textContent?.trim() || id);
      loadGroup(id, name);
    });
  }

  if (labelListEl) {
    labelListEl.addEventListener('click', (e) => {
      const item = e.target.closest('.nav-item');
      if (!item) return;
      const lbl = item.dataset.label;
      if (lbl) loadLabel(lbl);
    });
  }

  try {
    const res = await fetch('/api/groups');
    if (!res.ok) {
      const errTxt = await res.text();
      throw new Error(`Server error ${res.status}: ${errTxt}`);
    }
    groups = await res.json();
    renderSidebar();
    await refreshLabels();
    await updateNewCount();
    startSyncPolling();
    await setView('all');
  } catch (err) {
    logErr('Init', 'Failed to initialize groups:', err);
    $('empty-msg').innerHTML = `Failed to load groups from server:<br><span style="color:#ef4444;font-size:12px;">${esc(err.message)}</span>`;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Polling & Background Sync Monitoring
// ─────────────────────────────────────────────────────────────────────────────
function startSyncPolling() {
  setInterval(async () => {
    try {
      const res = await fetch('/api/sync/status');
      if (!res.ok) return;
      const data = await res.json();
      const banner = $('sync-banner');
      if (!banner) return;

      if (data.is_running) {
        banner.style.display = 'flex';
        const rem = data.pending_count ?? 0;
        $('sync-stats').textContent = `${rem} pending video${rem !== 1 ? 's' : ''}`;

        if (lastPendingCount !== -1 && rem < lastPendingCount) {
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
    const res = await fetch('/api/media/new_count');
    if (!res.ok) return;
    const data = await res.json();
    const count = data.count ?? 0;
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
  loadGroup(id, name);
}

function loadLabelByEl(el) {
  const lbl = el.dataset.label;
  if (lbl) loadLabel(lbl);
}

function renderSidebar() {
  const list = $('group-list');
  if (!list) return;
  if (!groups.length) {
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
}

async function refreshLabels() {
  try {
    const res    = await fetch('/api/labels');
    const labels = await res.json();
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
// View Switching & Progressive Pagination
// ─────────────────────────────────────────────────────────────────────────────
function setActiveNav(type, id = null, label = null) {
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  const btnMark = $('btn-mark-all');
  if (btnMark) btnMark.style.display = type === 'new' ? 'inline-block' : 'none';

  if (type === 'new')         $('nav-new')?.classList.add('active');
  else if (type === 'all')    $('nav-all')?.classList.add('active');
  else if (type === 'favourites') $('nav-fav')?.classList.add('active');
  else if (type === 'group' && id) {
    const el = document.querySelector(`.nav-item[data-id="${CSS.escape(id)}"]`);
    if (el) el.classList.add('active');
  } else if (type === 'label' && label) {
    const el = document.querySelector(`.nav-item[data-label="${CSS.escape(label)}"]`);
    if (el) el.classList.add('active');
  }
}

async function loadMediaPage(reset = false) {
  if (isLoadingMore) return;
  if (!reset && (!hasMore || !nextCursor)) return;

  isLoadingMore = true;
  if (reset) {
    allMedia = [];
    nextCursor = null;
    hasMore = false;
    showLoading();
  } else {
    showLoadMoreSpinner(true);
  }

  let url = '/api/media?limit=50';
  if (currentView) {
    if (currentView.type === 'group' && currentView.id) {
      url += '&group=' + encodeURIComponent(currentView.id);
    } else {
      url += '&group=all';
    }

    if (['new', 'favourites', 'label'].includes(currentView.type)) {
      url += '&view=' + encodeURIComponent(currentView.type);
    }

    if (currentView.type === 'label' && currentView.label) {
      url += '&label=' + encodeURIComponent(currentView.label);
    }
  }

  const searchQ = $('search')?.value.trim();
  if (searchQ) {
    url += '&search=' + encodeURIComponent(searchQ);
  }

  if (!reset && nextCursor) {
    url += '&cursor=' + encodeURIComponent(nextCursor);
  }

  log('Media', `Fetching media page: GET ${url}`);
  const t0 = performance.now();
  try {
    const res = await fetch(url);
    const elapsed = (performance.now() - t0).toFixed(1);
    if (!res.ok) {
      const errTxt = await res.text();
      logErr('Media', `HTTP error ${res.status}: ${errTxt}`);
      throw new Error(`Server error (${res.status}): ${errTxt}`);
    }

    const data = await res.json();
    const items = data.items || [];
    hasMore = Boolean(data.has_more);
    nextCursor = data.next_cursor || null;

    if (reset) {
      allMedia = items;
    } else {
      allMedia = allMedia.concat(items);
    }

    log('Media', `Received ${items.length} items in ${elapsed}ms. Total items: ${allMedia.length}. HasMore: ${hasMore}`);
    renderGrid(reset);
  } catch (err) {
    logErr('Media', 'Failed to fetch media page:', err);
    if (reset) {
      $('loading').style.display = 'none';
      $('empty-msg').innerHTML = `Error loading videos:<br><span style="color:#ef4444;font-size:12px;">${esc(err.message)}</span>`;
      $('empty').style.display = 'flex';
    }
  } finally {
    isLoadingMore = false;
    showLoadMoreSpinner(false);
    updateLoadMoreButton();
  }
}

function loadNextPage() {
  if (hasMore && nextCursor && !isLoadingMore) {
    loadMediaPage(false);
  }
}

function showLoadMoreSpinner(loading) {
  const btn = $('btn-load-more');
  const spin = $('load-more-spinner');
  if (btn && spin) {
    btn.style.display = loading ? 'none' : 'inline-block';
    spin.style.display = loading ? 'block' : 'none';
  }
}

function updateLoadMoreButton() {
  const wrap = $('load-more-wrap');
  if (wrap) {
    wrap.style.display = (hasMore && filtered.length > 0) ? 'block' : 'none';
  }
}

async function setView(type) {
  currentView = { type };
  setActiveNav(type);
  const titles = {
    new: '✨ New Videos',
    all: 'All Videos',
    favourites: 'Favourites'
  };
  $('group-title').textContent = titles[type] || 'Videos';
  await loadMediaPage(true);
}

async function loadGroup(id, name) {
  currentView = { type: 'group', id };
  setActiveNav('group', id);
  $('group-title').textContent = name;
  await loadMediaPage(true);
}

async function loadLabel(label) {
  currentView = { type: 'label', label };
  setActiveNav('label', null, label);
  $('group-title').textContent = `Label: ${label}`;
  await loadMediaPage(true);
}

// ─────────────────────────────────────────────────────────────────────────────
// Grid Rendering & Filtering
// ─────────────────────────────────────────────────────────────────────────────
function showLoading() {
  $('grid').innerHTML = '';
  $('empty').style.display = 'none';
  $('loading').style.display = 'flex';
}

function sortMedia() {
  allMedia.sort((a, b) => {
    const tA = Number(a.sent_at) || 0;
    const tB = Number(b.sent_at) || 0;
    return sortOrder === 'asc' ? tA - tB : tB - tA;
  });
}

function changeSort(order) {
  sortOrder = order;
  const sel = $('sort-select');
  if (sel && sel.value !== order) sel.value = order;
  sortMedia();
  applyFilter();
}

function renderGrid(reset = false) {
  const sel = $('sort-select');
  if (sel) sel.value = sortOrder;
  sortMedia();
  $('loading').style.display = 'none';
  $('toolbar-info').textContent = `${allMedia.length} video${allMedia.length!==1?'s':''}`;
  applyFilter();
}

function applyFilter() {
  const q = $('search').value.toLowerCase().trim();
  filtered = q
    ? allMedia.filter(m =>
        (m.filename || '').toLowerCase().includes(q) ||
        (m.sender   || '').toLowerCase().includes(q) ||
        (m.labels   || []).some(l => l.toLowerCase().includes(q))
      )
    : [...allMedia];

  const grid = $('grid');
  if (!grid) return;

  if (!filtered.length) {
    grid.innerHTML = '';
    $('empty-msg').textContent = q ? `No videos matching "${q}"` : 'No videos here yet';
    $('empty').style.display = 'flex';
    updateLoadMoreButton();
    return;
  }

  $('empty').style.display = 'none';

  grid.innerHTML = filtered.map((m, i) => `
    <div class="card${m.favourite?' is-fav':''}" data-index="${i}"
         onclick="openModal(${i})"
         onmousemove="hoverMove(event, ${i})"
         onmouseleave="hoverStop(${i})">
      <div class="thumb">
        <img class="poster" src="${esc(m.poster_url)}" alt="${esc(m.filename||'Video')}" loading="lazy">
        <div class="sprite-preview" id="sp${i}" style="display:none;background-image:url('${esc(m.preview_url)}')"></div>
        <div class="play-icon">
          <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
        </div>
        <div class="dur">${m.duration ? fmtDur(m.duration) : '—:——'}</div>
        ${m.favourite ? '<div class="fav-badge">⭐</div>' : ''}
        ${m.is_new ? '<div class="new-badge">NEW</div>' : ''}
      </div>
      <div class="card-meta">
        <div class="card-fn">${esc(m.filename||'Video')}</div>
        <div class="card-ts">${esc(m.sent_time)}</div>
        ${m.labels.length ? `<div class="card-labels">${m.labels.map(l=>chipHtml(l,false,true)).join('')}</div>` : ''}
      </div>
    </div>`).join('');

  updateLoadMoreButton();
}

// ─────────────────────────────────────────────────────────────────────────────
// Lightweight Sprite Preview Scrubbing
// ─────────────────────────────────────────────────────────────────────────────
function hoverMove(e, i) {
  const sp = $('sp' + i);
  if (!sp) return;
  const card = e.currentTarget;
  const rect = card.getBoundingClientRect();
  const x = e.clientX - rect.left;
  const ratio = Math.max(0, Math.min(0.99, x / rect.width));
  const frameIdx = Math.floor(ratio * 5); // 5 keyframe columns in sprite sheet
  sp.style.display = 'block';
  sp.style.backgroundPosition = `${(frameIdx / 4) * 100}% 0%`;
}

function hoverStop(i) {
  const sp = $('sp' + i);
  if (sp) sp.style.display = 'none';
}

// ─────────────────────────────────────────────────────────────────────────────
// Modal Video Player
// ─────────────────────────────────────────────────────────────────────────────
function openModal(i) {
  currentIdx = i;
  renderModal();
  $('modal').classList.add('open');

  const m = filtered[i];
  if (m && m.is_new) {
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
  $('modal').classList.remove('open');
  const v = $('modal-video');
  v.pause();
  v.src = '';
}

function renderModal() {
  const m = filtered[currentIdx];
  if (!m) return;

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
  m.favourite  = newFav;
  const am = allMedia.find(x => x.id === m.id);
  if (am) am.favourite = newFav;

  try {
    await fetch('/api/meta/' + encodeURIComponent(m.id), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ favourite: newFav }),
    });
  } catch (err) {
    logErr('Fav', `Failed to save favourite status for item ${m.id}:`, err);
  }

  renderFavBtn(newFav);
  refreshCard(currentIdx, m);
}

function renderChips(labels) {
  $('modal-chips').innerHTML = labels.map(l => chipHtml(l, true)).join('');
  $('label-input').value = '';
}

function handleLabelKey(e) {
  if (e.key !== 'Enter' && e.key !== ',') return;
  e.preventDefault();
  const val = $('label-input').value.trim();
  if (!val) return;
  addLabel(val);
}

async function addLabel(lbl) {
  const m = filtered[currentIdx];
  if (!m) return;
  if (m.labels.includes(lbl)) {
    $('label-input').value = '';
    return;
  }
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
  m.labels = m.labels.filter(l => l !== lbl);
  const am = allMedia.find(x => x.id === m.id);
  if (am) am.labels = [...m.labels];

  await saveMeta(m);
  renderChips(m.labels);
  refreshCard(currentIdx, m);
  await refreshLabels();
}

async function saveMeta(m) {
  try {
    await fetch('/api/meta/' + encodeURIComponent(m.id), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ favourite: m.favourite, labels: m.labels }),
    });
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
  if (!newItems.length) return;
  const ids = newItems.map(m => m.id);

  newItems.forEach(m => { m.is_new = false; });
  filtered.forEach(m => { if (ids.includes(m.id)) m.is_new = false; });

  try {
    await fetch('/api/meta/seen', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids })
    });
  } catch (err) {
    logErr('Seen', 'markAllSeen POST failed:', err);
  }

  await updateNewCount();

  if (currentView && currentView.type === 'new') {
    renderGrid(true);
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
  if (e.key === 'Escape')     closeModal();
  if (e.key === 'ArrowRight') navigate(1);
  if (e.key === 'ArrowLeft')  navigate(-1);
  if (e.key === 'f' || e.key === 'F') {
    e.preventDefault();
    toggleFav();
  }
});

// Run bootstrap when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
