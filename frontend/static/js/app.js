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

// ---- Shared motion system ----
function initSharedMotionSystem() {
  document.body.dataset.pageReady = 'false';
  requestAnimationFrame(() => {
    document.body.dataset.pageReady = 'true';
  });

  document.addEventListener('click', event => {
    const link = event.target.closest('a[href]');
    if (!link) return;
    const href = link.getAttribute('href');
    if (!href || href.startsWith('#') || href.startsWith('http') || link.target === '_blank' || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (!href.endsWith('.html') && !href.startsWith('/')) return;
    event.preventDefault();
    transitionTo(href);
  });

  const revealTargets = document.querySelectorAll([
    'main > *',
    '.dashboard-shell > *',
    '.panel',
    '.card',
    '.stat-card',
    '.agent-card',
    '.kb-card',
    '.wc-card',
    '.mcp-card',
    '.cron-card',
    '.url-row',
    '.vinu-conv-row',
    '.sidebar-shell .conv-item',
    '.empty-state'
  ].join(','));

  if (!('IntersectionObserver' in window)) {
    revealTargets.forEach(node => node.classList.add('reveal-visible'));
    return;
  }

  const observer = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      entry.target.classList.add('reveal-visible');
      observer.unobserve(entry.target);
    });
  }, { threshold: 0.14, rootMargin: '0px 0px -32px 0px' });

  revealTargets.forEach((node, index) => {
    if (node.closest('.modal-overlay')) return;
    node.classList.add('reveal-on-scroll');
    node.style.transitionDelay = `${Math.min(index * 22, 220)}ms`;
    observer.observe(node);
  });
}

function transitionTo(href) {
  if (!href) return;
  document.body.classList.add('page-transitioning');
  window.setTimeout(() => {
    window.location.href = href;
  }, 170);
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
  const mode = resolvePageGraphicMode();
  const dust = Array.from({ length: mode.dustCount }, (_, index) => ({
    seed: index + 1,
    drift: mode.driftBase + (index % 7) * mode.driftStep,
    phase: index * 0.37,
  }));

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
    if (mode.type === 'scheduler') return schedulerLayout(time);
    if (mode.type === 'radial') return radialLayout(time);
    if (mode.type === 'dashboard') return dashboardLayout(time);
    const insetX = 78;
    const insetY = 68;
    const centerY = height * mode.centerY;
    const usableW = Math.max(width - insetX * 2, 240);
    const colGap = mode.stageBlueprint.length > 1 ? usableW / (mode.stageBlueprint.length - 1) : 0;
    const waveAmp = Math.min(mode.waveCap, height * mode.waveScale);

    return mode.stageBlueprint.map((column, colIndex) => {
      const anchorX = insetX + colGap * colIndex;
      const curveT = mode.stageBlueprint.length > 1 ? colIndex / (mode.stageBlueprint.length - 1) : 0.5;
      const anchorY = centerY + Math.sin(curveT * Math.PI * mode.curveFrequency + mode.curvePhase) * waveAmp;
      return column.map((node, rowIndex) => {
        const spread = column.length > 1 ? (rowIndex - (column.length - 1) / 2) : 0;
        const orbital = time * mode.orbitalSpeed + colIndex * 0.7 + rowIndex * 0.9;
        return {
          ...node,
          stageIndex: colIndex,
          x: anchorX + Math.sin(orbital) * (mode.nodeDriftX + Math.abs(spread) * 9),
          y: Math.min(height - insetY, Math.max(insetY, anchorY + spread * mode.rowGap + Math.cos(orbital * 1.35) * (mode.nodeDriftY + Math.abs(spread) * 7))),
        };
      });
    });
  }

  function draw(time) {
    ctx.clearRect(0, 0, width, height);
    const columns = layout(time);
    const flat = columns.flat();
    const edges = [];

    if (mode.type === 'dashboard') {
      drawDashboardGrid(time);
    }

    dust.forEach((point, index) => {
      const x = ((time * point.drift * 18) + point.seed * 97) % (width + 120) - 60;
      const y = height * (mode.dustStartY + (index % mode.dustBands) * mode.dustBandStep) + Math.sin(time * 0.0004 + point.phase) * mode.dustFloat;
      ctx.fillStyle = mode.dustColor;
      ctx.beginPath();
      ctx.arc(x, y, mode.dustRadius + (index % 3) * 0.5, 0, Math.PI * 2);
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
        if (stageGap > mode.maxStageGap || dist > mode.maxNearbyDistance) continue;
        edges.push({ from, to, index: edges.length + fromIndex + i, nearby: true });
      }
    });

    edges.forEach(edge => {
      const energetic = edge.from.group === 'system' || edge.to.group === 'system';
      const cx1 = edge.from.x + (edge.nearby ? 0 : mode.edgePull) + Math.sin(edge.index * 0.6) * (edge.nearby ? mode.nearbySwing : mode.farSwing);
      const cy1 = edge.from.y + (edge.nearby ? -mode.edgeArc : 0);
      const cx2 = edge.to.x - (edge.nearby ? 0 : mode.edgePull) + Math.cos(edge.index * 0.5) * (edge.nearby ? mode.nearbySwing : mode.farSwing);
      const cy2 = edge.to.y + (edge.nearby ? mode.edgeArc : 0);
      ctx.strokeStyle = energetic ? mode.energeticStroke : edge.nearby ? mode.nearbyStroke : mode.edgeStroke;
      ctx.lineWidth = energetic ? mode.energeticWidth : edge.nearby ? mode.nearbyWidth : mode.edgeWidth;
      ctx.beginPath();
      ctx.moveTo(edge.from.x, edge.from.y);
      ctx.bezierCurveTo(cx1, cy1, cx2, cy2, edge.to.x, edge.to.y);
      ctx.stroke();

      const pulse = ((time * mode.pulseSpeed) + edge.index * 0.09) % 1;
      const x = cubicPoint(edge.from.x, cx1, cx2, edge.to.x, pulse);
      const y = cubicPoint(edge.from.y, cy1, cy2, edge.to.y, pulse);
      ctx.fillStyle = energetic ? mode.energeticPulse : mode.pulseColor;
      ctx.beginPath();
      ctx.arc(x, y, energetic ? mode.energeticPulseRadius : mode.pulseRadius, 0, Math.PI * 2);
      ctx.fill();
    });

    flat.forEach((node, index) => {
      const pulse = 0.82 + Math.sin(time * 0.001 + index) * 0.07;
      const radius = node.group === 'system' ? mode.systemRadius : mode.agentRadius;
      ctx.fillStyle = node.group === 'system' ? mode.systemFill : mode.agentFill;
      ctx.beginPath();
      ctx.arc(node.x, node.y, radius * pulse, 0, Math.PI * 2);
      ctx.fill();

      ctx.strokeStyle = mode.nodeOutline;
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

  function drawDashboardGrid(time) {
    const columns = 11;
    const rows = 7;
    const gapX = width / (columns + 1);
    const gapY = height / (rows + 2);
    ctx.save();
    ctx.strokeStyle = 'rgba(17,17,17,0.045)';
    ctx.lineWidth = 1;
    for (let c = 1; c <= columns; c += 1) {
      const x = gapX * c;
      ctx.beginPath();
      ctx.moveTo(x, gapY * 0.9);
      ctx.lineTo(x, height - gapY * 0.8);
      ctx.stroke();
    }
    for (let r = 1; r <= rows; r += 1) {
      const y = gapY * r + gapY * 0.5;
      ctx.beginPath();
      ctx.moveTo(gapX * 0.7, y);
      ctx.lineTo(width - gapX * 0.7, y);
      ctx.stroke();
    }
    for (let c = 0; c < columns; c += 1) {
      const x = gapX * (c + 1);
      const barHeight = 22 + Math.sin(time * 0.001 + c * 0.8) * 14 + (c % 4) * 8;
      const top = height - gapY * 1.25 - barHeight * 2.2;
      ctx.fillStyle = 'rgba(17,17,17,0.05)';
      ctx.fillRect(x - 7, top, 14, barHeight * 2.2);
    }
    ctx.restore();
  }

  function dashboardLayout(time) {
    const bands = [
      { count: 4, y: height * 0.26, drift: 22 },
      { count: 5, y: height * 0.48, drift: 18 },
      { count: 4, y: height * 0.7, drift: 22 },
    ];
    return bands.map((band, bandIndex) => Array.from({ length: band.count }, (_, idx) => {
      const x = width * (0.14 + (idx / Math.max(band.count - 1, 1)) * 0.72);
      const orbit = time * 0.00042 + idx + bandIndex * 0.8;
      return {
        group: bandIndex === 1 ? 'system' : 'agent',
        stageIndex: bandIndex,
        x: x + Math.sin(orbit) * band.drift,
        y: band.y + Math.cos(orbit * 1.2) * 16,
      };
    }));
  }

  function radialLayout(time) {
    const centerX = width * 0.52;
    const centerY = height * 0.48;
    const rings = mode.stageBlueprint.map((column, ringIndex) => {
      const radius = 82 + ringIndex * mode.ringGap;
      return column.map((node, nodeIndex) => {
        const slice = (Math.PI * 2) / Math.max(column.length, 1);
        const theta = slice * nodeIndex + time * mode.orbitalSpeed + ringIndex * 0.55;
        return {
          ...node,
          stageIndex: ringIndex,
          x: centerX + Math.cos(theta) * radius,
          y: centerY + Math.sin(theta) * (radius * mode.ringEllipse),
        };
      });
    });
    if (rings[0]?.length) {
      rings[0][0].x = centerX;
      rings[0][0].y = centerY;
    }
    return rings;
  }

  function schedulerLayout(time) {
    const centerX = width * 0.54;
    const centerY = height * 0.5;
    return mode.stageBlueprint.map((column, ringIndex) => {
      const radius = 70 + ringIndex * 56;
      return column.map((node, nodeIndex) => {
        const slice = (Math.PI * 2) / Math.max(column.length, 1);
        const theta = time * (mode.orbitalSpeed + ringIndex * 0.00005) + nodeIndex * slice;
        return {
          ...node,
          stageIndex: ringIndex,
          x: centerX + Math.cos(theta) * radius,
          y: centerY + Math.sin(theta) * radius * 0.64,
        };
      });
    });
  }

  function resolvePageGraphicMode() {
    const page = document.body.dataset.pageGraphic || 'default';
    const base = {
      type: 'pipeline',
      stageBlueprint: [
        [{ group: 'system' }],
        [{ group: 'agent' }, { group: 'agent' }],
        [{ group: 'agent' }, { group: 'agent' }, { group: 'agent' }],
        [{ group: 'system' }],
      ],
      dustCount: 26,
      driftBase: 0.14,
      driftStep: 0.02,
      centerY: 0.46,
      waveCap: 96,
      waveScale: 0.12,
      curveFrequency: 1.18,
      curvePhase: -0.5,
      orbitalSpeed: 0.00045,
      nodeDriftX: 14,
      nodeDriftY: 20,
      rowGap: 74,
      maxStageGap: 2,
      maxNearbyDistance: 210,
      edgePull: 78,
      nearbySwing: 18,
      farSwing: 11,
      edgeArc: 22,
      energeticStroke: 'rgba(17,17,17,0.14)',
      nearbyStroke: 'rgba(17,17,17,0.06)',
      edgeStroke: 'rgba(17,17,17,0.08)',
      energeticWidth: 1.15,
      nearbyWidth: 0.9,
      edgeWidth: 1,
      pulseSpeed: 0.00016,
      energeticPulse: 'rgba(17,17,17,0.2)',
      pulseColor: 'rgba(17,17,17,0.11)',
      energeticPulseRadius: 2.6,
      pulseRadius: 1.9,
      systemRadius: 8.8,
      agentRadius: 6.8,
      systemFill: 'rgba(17,17,17,0.34)',
      agentFill: 'rgba(17,17,17,0.2)',
      nodeOutline: 'rgba(255,255,255,0.75)',
      dustStartY: 0.16,
      dustBands: 9,
      dustBandStep: 0.078,
      dustFloat: 18,
      dustRadius: 1.3,
      dustColor: 'rgba(17,17,17,0.05)',
      ringGap: 54,
      ringEllipse: 0.86,
    };

    const overrides = {
      home: {
        stageBlueprint: [
          [{ group: 'system' }, { group: 'agent' }],
          [{ group: 'agent' }, { group: 'agent' }, { group: 'agent' }],
          [{ group: 'agent' }, { group: 'agent' }],
          [{ group: 'system' }, { group: 'agent' }],
        ],
        centerY: 0.48,
        waveScale: 0.1,
      },
      dashboard: {
        type: 'dashboard',
        stageBlueprint: [
          [{ group: 'agent' }, { group: 'agent' }, { group: 'agent' }, { group: 'agent' }],
          [{ group: 'system' }, { group: 'system' }, { group: 'system' }, { group: 'system' }, { group: 'system' }],
          [{ group: 'agent' }, { group: 'agent' }, { group: 'agent' }, { group: 'agent' }],
        ],
        dustCount: 34,
        dustColor: 'rgba(17,17,17,0.045)',
        dustStartY: 0.14,
        dustBandStep: 0.068,
        maxNearbyDistance: 180,
      },
      workspace: {
        stageBlueprint: [
          [{ group: 'system' }],
          [{ group: 'agent' }, { group: 'agent' }, { group: 'agent' }],
          [{ group: 'agent' }, { group: 'agent' }, { group: 'agent' }, { group: 'agent' }],
          [{ group: 'system' }, { group: 'agent' }],
        ],
        curveFrequency: 1.28,
        waveCap: 112,
        rowGap: 70,
      },
      knowledge: {
        type: 'pipeline',
        stageBlueprint: [
          [{ group: 'system' }, { group: 'system' }],
          [{ group: 'agent' }, { group: 'agent' }, { group: 'agent' }, { group: 'agent' }],
          [{ group: 'agent' }, { group: 'agent' }, { group: 'agent' }, { group: 'agent' }, { group: 'agent' }],
          [{ group: 'system' }],
        ],
        rowGap: 58,
        nodeDriftY: 16,
        edgeStroke: 'rgba(17,17,17,0.07)',
      },
      web: {
        type: 'radial',
        stageBlueprint: [
          [{ group: 'system' }],
          [{ group: 'agent' }, { group: 'agent' }, { group: 'agent' }, { group: 'agent' }],
          [{ group: 'agent' }, { group: 'agent' }, { group: 'agent' }, { group: 'agent' }, { group: 'agent' }, { group: 'agent' }],
          [{ group: 'agent' }, { group: 'agent' }, { group: 'agent' }, { group: 'agent' }, { group: 'agent' }],
        ],
        ringGap: 62,
        ringEllipse: 0.76,
        maxNearbyDistance: 190,
      },
      mcp: {
        type: 'radial',
        stageBlueprint: [
          [{ group: 'system' }],
          [{ group: 'agent' }, { group: 'agent' }, { group: 'agent' }],
          [{ group: 'agent' }, { group: 'agent' }, { group: 'agent' }, { group: 'agent' }, { group: 'agent' }],
          [{ group: 'agent' }, { group: 'agent' }, { group: 'agent' }],
        ],
        ringGap: 56,
        ringEllipse: 0.92,
        energeticStroke: 'rgba(17,17,17,0.16)',
      },
      cron: {
        type: 'scheduler',
        stageBlueprint: [
          [{ group: 'system' }],
          [{ group: 'agent' }, { group: 'agent' }, { group: 'agent' }, { group: 'agent' }],
          [{ group: 'agent' }, { group: 'agent' }, { group: 'agent' }, { group: 'agent' }, { group: 'agent' }, { group: 'agent' }],
        ],
        dustCount: 22,
        orbitalSpeed: 0.00032,
        maxNearbyDistance: 170,
      },
    };

    return { ...base, ...(overrides[page] || {}) };
  }

  resize();
  window.addEventListener('resize', resize, { passive: true });
  requestAnimationFrame(draw);
}

document.addEventListener('DOMContentLoaded', () => {
  initSharedMotionSystem();
  initCortexAmbientCanvas();
});
