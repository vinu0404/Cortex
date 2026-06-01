/* Shared utilities: auth, API client, toasts */

const API = '/';

// ---- Timezone ----
let _appTimezone = (function() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch { return 'UTC'; }
})();

async function initTimezone() {
  try {
    const user = await apiGet('auth/me');
    if (user?.timezone && user.timezone !== 'UTC') {
      _appTimezone = user.timezone;
    } else if (user && (!user.timezone || user.timezone === 'UTC')) {
      const detected = _appTimezone;
      if (detected !== 'UTC') await apiPatch('auth/me', { timezone: detected });
    }
  } catch { /* silent */ }
  const sel = document.getElementById('tz-select');
  if (sel) sel.value = _appTimezone;
}

async function setTimezone(tz) {
  _appTimezone = tz;
  try { await apiPatch('auth/me', { timezone: tz }); } catch { /* silent */ }
}

function getToken() { return localStorage.getItem('access_token'); }
function getRefreshToken() { return localStorage.getItem('refresh_token'); }
function setTokens(access, refresh) {
  localStorage.setItem('access_token', access);
  localStorage.setItem('refresh_token', refresh);
}
function clearTokens() {
  localStorage.removeItem('access_token');
  localStorage.removeItem('refresh_token');
}

function isLoggedIn() { return !!getToken(); }

function redirectToAuth() { window.location.href = '/auth.html'; }
function redirectHome() { window.location.href = '/index.html'; }

// Auto-redirect if not logged in (call on protected pages)
function requireAuth() { if (!isLoggedIn()) redirectToAuth(); }

async function apiFetch(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', 'X-Timezone': _appTimezone, ...(options.headers || {}) };
  const token = getToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;

  let resp;
  try {
    resp = await fetch(API + path, { ...options, headers });
  } catch {
    throw new Error('Connection error — check your network and try again.');
  }

  if (resp.status === 401) {
    const refreshed = await _tryRefresh();
    if (refreshed) return apiFetch(path, options);
    clearTokens();
    redirectToAuth();
    return null;
  }
  return resp;
}

async function _parseApiResponse(resp) {
  let data;
  try {
    data = await resp.json();
  } catch {
    throw new Error(`Server error (${resp.status}) — please try again.`);
  }
  if (data.status === 'ok') return data.data;
  // FastAPI RequestValidationError or our VALIDATION_ERROR code
  if (Array.isArray(data.detail)) {
    const msg = data.detail.map(e => e.msg || String(e)).join('; ');
    throw new Error(msg || 'Validation error');
  }
  const msg = data.message || data.detail || `Request failed (${resp.status})`;
  throw new Error(typeof msg === 'string' ? msg : 'Request failed');
}

async function _tryRefresh() {
  const rt = getRefreshToken();
  if (!rt) return false;
  try {
    const resp = await fetch(API + 'auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: rt }),
    });
    if (!resp.ok) return false;
    const data = await resp.json();
    if (data.data) { setTokens(data.data.access_token, data.data.refresh_token); return true; }
    return false;
  } catch { return false; }
}

async function apiGet(path) {
  const resp = await apiFetch(path);
  if (!resp) return null;
  return _parseApiResponse(resp);
}

async function apiPost(path, body) {
  const resp = await apiFetch(path, { method: 'POST', body: JSON.stringify(body) });
  if (!resp) return null;
  return _parseApiResponse(resp);
}

async function apiPut(path, body) {
  const resp = await apiFetch(path, { method: 'PUT', body: JSON.stringify(body) });
  if (!resp) return null;
  return _parseApiResponse(resp);
}

async function apiPatch(path, body) {
  const resp = await apiFetch(path, { method: 'PATCH', body: JSON.stringify(body) });
  if (!resp) return null;
  return _parseApiResponse(resp);
}

async function apiDelete(path) {
  const resp = await apiFetch(path, { method: 'DELETE' });
  if (!resp) return null;
  return _parseApiResponse(resp);
}

// ---- Toast notifications ----
function toast(msg, type = 'info') {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    document.body.appendChild(container);
  }
  // Cap length so technical stack traces never reach the user
  const display = _sanitizeToastMsg(msg);
  const el = document.createElement('div');
  el.className = `toast${type === 'error' ? ' error' : ''}`;
  el.textContent = display;
  el.title = 'Click to dismiss';
  el.style.cursor = 'pointer';
  el.addEventListener('click', () => el.remove(), { once: true });
  container.appendChild(el);
  // Errors stay 5 s so the user has time to read; info/success 3 s
  const duration = type === 'error' ? 5000 : 3000;
  setTimeout(() => el.remove(), duration);
}

function _sanitizeToastMsg(msg) {
  if (!msg) return 'Something went wrong.';
  const s = String(msg);
  // Strip anything that looks like a Python traceback or raw exception class
  if (s.includes('Traceback') || s.includes('  File "')) return 'An unexpected error occurred.';
  return s.length > 180 ? s.slice(0, 177) + '…' : s;
}

// ---- SSE fetch (POST-based for auth headers) ----
async function* sseStream(path, body) {
  const token = getToken();
  const resp = await fetch(API + path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${token}`,
      'X-Timezone': _appTimezone,
    },
    body: JSON.stringify(body),
  });

  if (!resp.ok) {
    if (resp.status === 401) { clearTokens(); redirectToAuth(); return; }
    let msg = `Request failed (${resp.status})`;
    try {
      const err = await resp.json();
      if (err.message) msg = err.message;
      else if (err.detail) msg = typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail);
    } catch {}
    throw new Error(msg);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const parts = buffer.split('\n\n');
    buffer = parts.pop();

    for (const chunk of parts) {
      if (!chunk.trim()) continue;
      const lines = chunk.split('\n');
      let event = null, data = null;
      for (const line of lines) {
        if (line.startsWith('event: ')) event = line.slice(7).trim();
        if (line.startsWith('data: ')) data = line.slice(6).trim();
      }
      if (data !== null) {
        try { yield { event, data: JSON.parse(data) }; }
        catch { yield { event, data }; }
      }
    }
  }
}

// ---- Modal helpers ----
function showModal(id) { document.getElementById(id)?.classList.remove('hidden'); }
function hideModal(id) { document.getElementById(id)?.classList.add('hidden'); }

// ---- Timezone selector population ----
function _populateTzSelect(selId) {
  const sel = document.getElementById(selId || 'tz-select');
  if (!sel) return;
  const zones = [
    'Pacific/Honolulu','America/Anchorage','America/Los_Angeles','America/Denver',
    'America/Chicago','America/New_York','America/Sao_Paulo','Atlantic/Azores',
    'Europe/London','Europe/Paris','Europe/Berlin','Europe/Moscow',
    'Asia/Dubai','Asia/Karachi','Asia/Kolkata','Asia/Dhaka',
    'Asia/Bangkok','Asia/Singapore','Asia/Tokyo','Asia/Seoul',
    'Australia/Sydney','Pacific/Auckland','UTC',
  ];
  sel.innerHTML = zones.map(z => `<option value="${z}"${z === _appTimezone ? ' selected' : ''}>${z.replace('_',' ')}</option>`).join('');
}

// ---- Confirm dialog ----
function confirmDialog({ title, message, confirmText = 'Delete', confirmClass = 'btn-danger', onConfirm }) {
  let modal = document.getElementById('confirm-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'confirm-modal';
    modal.className = 'modal-overlay hidden';
    modal.innerHTML = `
      <div class="modal" style="max-width:400px;">
        <div class="modal-header">
          <span class="modal-title" id="confirm-modal-title"></span>
          <button class="modal-close" onclick="hideModal('confirm-modal')">×</button>
        </div>
        <p id="confirm-modal-msg" style="margin:0 0 24px;font-size:14px;color:var(--text-muted);line-height:1.6;"></p>
        <div style="display:flex;gap:8px;justify-content:flex-end;">
          <button class="btn btn-secondary" onclick="hideModal('confirm-modal')">Cancel</button>
          <button id="confirm-modal-ok" class="btn btn-danger">Confirm</button>
        </div>
      </div>`;
    modal.addEventListener('click', e => { if (e.target === modal) hideModal('confirm-modal'); });
    document.body.appendChild(modal);
  }
  document.getElementById('confirm-modal-title').textContent = title;
  document.getElementById('confirm-modal-msg').textContent = message;
  const btn = document.getElementById('confirm-modal-ok');
  btn.textContent = confirmText;
  btn.className = `btn ${confirmClass}`;
  btn.onclick = () => { hideModal('confirm-modal'); onConfirm(); };
  showModal('confirm-modal');
}
