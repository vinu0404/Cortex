/* Shared utilities: auth, API client, toasts */

const API = '/api/v1/';

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
  if (data.success === true || data.status === 'ok') return data.data;
  // FastAPI RequestValidationError or our VALIDATION_ERROR code
  if (Array.isArray(data.detail)) {
    const msg = data.detail.map(e => e.msg || String(e)).join('; ');
    throw new Error(msg || 'Validation error');
  }
  const msg = data.error?.message || data.message || data.detail || `Request failed (${resp.status})`;
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
      if (err.error?.message) msg = err.error.message;
      else if (err.message) msg = err.message;
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

// ---- Ambient canvas graphic ----
function initCortexAmbientCanvas() {
  if (document.getElementById('cortex-bg-canvas')) return;
  if (document.body.classList.contains('chat-page') || document.getElementById('chat-orchestration-canvas')) return;
  if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;

  const canvas = document.createElement('canvas');
  canvas.id = 'cortex-bg-canvas';
  canvas.className = 'cortex-bg-canvas';
  canvas.setAttribute('aria-hidden', 'true');
  document.body.prepend(canvas);

  const ctx = canvas.getContext('2d');
  if (!ctx) return;

  let width = 0;
  let height = 0;
  let dpr = 1;
  const dust = Array.from({ length: 26 }, (_, index) => ({
    seed: index + 1,
    drift: 0.14 + (index % 7) * 0.02,
    phase: index * 0.37,
  }));
  const stageBlueprint = [
    [{ label: 'Intent', group: 'system' }],
    [{ label: 'Route', group: 'agent' }, { label: 'Memory', group: 'agent' }],
    [{ label: 'Tools', group: 'agent' }, { label: 'Search', group: 'agent' }, { label: 'Context', group: 'agent' }],
    [{ label: 'Compose', group: 'system' }],
  ];

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function layout(time) {
    const insetX = 78;
    const insetY = 68;
    const centerY = height * 0.46;
    const usableW = Math.max(width - insetX * 2, 240);
    const colGap = stageBlueprint.length > 1 ? usableW / (stageBlueprint.length - 1) : 0;
    const waveAmp = Math.min(96, height * 0.12);

    return stageBlueprint.map((column, colIndex) => {
      const anchorX = insetX + colGap * colIndex;
      const curveT = stageBlueprint.length > 1 ? colIndex / (stageBlueprint.length - 1) : 0.5;
      const anchorY = centerY + Math.sin(curveT * Math.PI * 1.18 - 0.5) * waveAmp;
      return column.map((node, rowIndex) => {
        const spread = column.length > 1 ? (rowIndex - (column.length - 1) / 2) : 0;
        const orbital = time * 0.00045 + colIndex * 0.7 + rowIndex * 0.9;
        return {
          ...node,
          stageIndex: colIndex,
          x: anchorX + Math.sin(orbital) * (14 + Math.abs(spread) * 9),
          y: Math.min(height - insetY, Math.max(insetY, anchorY + spread * 74 + Math.cos(orbital * 1.35) * (20 + Math.abs(spread) * 7))),
        };
      });
    });
  }

  function draw(time) {
    ctx.clearRect(0, 0, width, height);
    const columns = layout(time);
    const flat = columns.flat();
    const edges = [];

    dust.forEach((point, index) => {
      const x = ((time * point.drift * 18) + point.seed * 97) % (width + 120) - 60;
      const y = height * (0.16 + (index % 9) * 0.078) + Math.sin(time * 0.0004 + point.phase) * 18;
      ctx.fillStyle = 'rgba(17,17,17,0.05)';
      ctx.beginPath();
      ctx.arc(x, y, 1.3 + (index % 3) * 0.5, 0, Math.PI * 2);
      ctx.fill();
    });

    for (let c = 0; c < columns.length - 1; c += 1) {
      columns[c].forEach((from, fromIdx) => {
        columns[c + 1].forEach((to, toIdx) => {
          edges.push({ from, to, index: edges.length + fromIdx + toIdx, nearby: false });
        });
      });
    }

    flat.forEach((from, fromIndex) => {
      for (let i = fromIndex + 1; i < flat.length; i += 1) {
        const to = flat[i];
        const dist = Math.hypot(from.x - to.x, from.y - to.y);
        const stageGap = Math.abs((from.stageIndex || 0) - (to.stageIndex || 0));
        if (stageGap > 2 || dist > 210) continue;
        edges.push({ from, to, index: edges.length + fromIndex + i, nearby: true });
      }
    });

    edges.forEach(edge => {
      const energetic = edge.from.group === 'system' || edge.to.group === 'system';
      const cx1 = edge.from.x + (edge.nearby ? 0 : 78) + Math.sin(edge.index * 0.6) * (edge.nearby ? 18 : 11);
      const cy1 = edge.from.y + (edge.nearby ? -22 : 0);
      const cx2 = edge.to.x - (edge.nearby ? 0 : 78) + Math.cos(edge.index * 0.5) * (edge.nearby ? 18 : 11);
      const cy2 = edge.to.y + (edge.nearby ? 22 : 0);
      ctx.strokeStyle = energetic ? 'rgba(17,17,17,0.14)' : edge.nearby ? 'rgba(17,17,17,0.06)' : 'rgba(17,17,17,0.08)';
      ctx.lineWidth = energetic ? 1.15 : edge.nearby ? 0.9 : 1;
      ctx.beginPath();
      ctx.moveTo(edge.from.x, edge.from.y);
      ctx.bezierCurveTo(cx1, cy1, cx2, cy2, edge.to.x, edge.to.y);
      ctx.stroke();

      const pulse = ((time * 0.00016) + edge.index * 0.09) % 1;
      const x = cubicPoint(edge.from.x, cx1, cx2, edge.to.x, pulse);
      const y = cubicPoint(edge.from.y, cy1, cy2, edge.to.y, pulse);
      ctx.fillStyle = energetic ? 'rgba(17,17,17,0.2)' : 'rgba(17,17,17,0.11)';
      ctx.beginPath();
      ctx.arc(x, y, energetic ? 2.6 : 1.9, 0, Math.PI * 2);
      ctx.fill();
    });

    flat.forEach((node, index) => {
      const pulse = 0.82 + Math.sin(time * 0.001 + index) * 0.07;
      const radius = node.group === 'system' ? 8.8 : 6.8;
      ctx.fillStyle = node.group === 'system' ? 'rgba(17,17,17,0.34)' : 'rgba(17,17,17,0.2)';
      ctx.beginPath();
      ctx.arc(node.x, node.y, radius * pulse, 0, Math.PI * 2);
      ctx.fill();

      ctx.strokeStyle = 'rgba(255,255,255,0.75)';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(node.x, node.y, Math.max(radius - 3.2, 2.4), 0, Math.PI * 2);
      ctx.stroke();
    });

    requestAnimationFrame(draw);
  }

  function cubicPoint(p0, p1, p2, p3, t) {
    const inv = 1 - t;
    return (inv ** 3) * p0 + 3 * (inv ** 2) * t * p1 + 3 * inv * (t ** 2) * p2 + (t ** 3) * p3;
  }

  resize();
  window.addEventListener('resize', resize, { passive: true });
  requestAnimationFrame(draw);
}

document.addEventListener('DOMContentLoaded', initCortexAmbientCanvas);
